"""Endpoint internal gateway (`/internal/v1`, GW§3.2), hanya di listener internal.

Autentikasi: HMAC per permintaan `astria_common.hmac_auth` (header X-Astria-Service/Ts/Nonce/Sig, jendela 300
detik, nonce sekali pakai). Kunci pemanggil dari `ASTRIA_GW_CALLER_KEYS_FILE`; tiap endpoint membatasi layanannya.
Pengecualian: `POST /whatsapp/masuk` dari `wa-bridge` (Node, khusus uji) memakai Bearer token bersama.
Galat memakai bentuk `{"error": {"code", "message", "trace_id"}}` dengan kode GW§3.7.

`POST /events` juga menerima `identity.revoked` (kontrol, `channel_id` "*"): ikatan kanal pengguna itu dicabut.
`PUT /secrets/{key}` (hanya `config`) menulis rahasia kanal lewat `vault.SecretVault`; respons 204 tanpa nilai.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from astria_common import hmac_auth
from astria_common.db import Database
from astria_common.errors import AstriaError
from astria_common.hmac_auth import H_SERVICE, HmacError
from astria_common.log import get_logger
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from astria_gateway.channels import WHATSAPP_MAX_CHARS
from astria_gateway.outbox import enqueue
from astria_gateway.render import ALLOWED_FIELDS, render_event
from astria_gateway.store import audit, channel_config, channel_identity, event_inbox, now, outbox_message
from astria_gateway.vault import SECRET_CHANNELS, TELEGRAM_TOKEN, SecretVault, VaultError

log = get_logger(__name__)

EVENTS_TOTAL = Counter("gw_events_received_total", "Event dari notifier", ["type", "result"])
REPLAY_REASONS = frozenset({"cap_waktu_di_luar_jendela", "nonce_terpakai"})
IDENTITY_REVOKED = "identity.revoked"
REVOKE_CHANNEL = "*"
REVOKE_FIELDS = frozenset({"user_id", "reason", "revoked_at"})
REVOKE_REASONS = frozenset({"akun_nonaktif", "peran_berubah", "kata_sandi_diganti", "sesi_dicabut"})
USER_ID = re.compile(r"^usr_[A-Za-z0-9]+$")

router = APIRouter(prefix="/internal/v1", tags=["gateway"])


def load_keys(path: Path | None) -> dict[str, bytes]:
    """Berkas JSON {layanan: kunci_hex}. Tidak ada berkas = tidak ada pemanggil yang dikenal."""
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): bytes.fromhex(str(key).strip()) for name, key in raw.items()}


def require_caller(*allowed: str) -> Callable[[Request], Awaitable[str]]:
    permitted = frozenset(allowed)

    async def dependency(request: Request) -> str:
        state: Any = request.app.state
        service = request.headers.get(H_SERVICE, "")
        key = state.caller_keys.get(service) if service in permitted else None
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        try:
            if key is None:
                raise HmacError("layanan_tidak_dikenal")
            hmac_auth.verify(key, request.method, path, request.headers, await request.body(), state.nonces)
        except HmacError as exc:
            log.warning("hmac_ditolak", reason=exc.reason, path=request.url.path)
            code = "GW_REPLAY" if exc.reason in REPLAY_REASONS else "GW_AUTH_FAILED"
            raise AstriaError(code, "tanda tangan tidak valid", 401) from None
        return service

    return dependency


def _db(request: Request) -> Database:
    db: Database | None = request.app.state.db
    if db is None or not db.is_ready:
        raise AstriaError("GW_NOT_READY", "basis data gateway belum siap", 503)
    return db


Text = Annotated[str, Field(max_length=500)]
# Nilai payload: skalar, atau daftar teks pendek (mis. `teknik` ATT&CK). Objek bersarang ditolak.
Value = Text | int | float | bool | None | Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=20)]


class ChannelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str = Field(pattern=r"^(\*|[a-z][a-z0-9_-]{0,63})$")
    payload: dict[str, Value] = Field(default_factory=dict, max_length=20)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,100}$")
    type: str = Field(max_length=64)
    occurred_at: datetime
    channels: list[ChannelPayload] = Field(default_factory=list, max_length=20)


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=4096)


async def _invalid(db: Database, event_type: str, message: str, **detail: Any) -> AstriaError:
    EVENTS_TOTAL.labels(event_type if event_type in ALLOWED_FIELDS else "lainnya", "invalid").inc()
    async with db.engine.begin() as conn:
        await audit(conn, "event.invalid", reason=message, **detail)
    log.warning("event_ditolak", reason=message, **detail)
    return AstriaError("GW_EVENT_INVALID", message, 422)


@router.post("/events", status_code=202)
async def receive_event(request: Request, _caller: Annotated[str, Depends(require_caller("notifier"))]) -> JSONResponse:
    db = _db(request)
    try:
        event = EventIn.model_validate_json(await request.body())
    except ValidationError:
        raise await _invalid(db, "lainnya", "amplop event tidak valid") from None
    if event.type == IDENTITY_REVOKED:
        return await _revoke(db, event)
    allowed = ALLOWED_FIELDS.get(event.type)
    if allowed is None:
        raise await _invalid(db, event.type, "tipe event tidak diterima", event_id=event.event_id)
    channel_ids = [c.channel_id for c in event.channels]
    if len(set(channel_ids)) != len(channel_ids):
        raise await _invalid(db, event.type, "channel_id ganda", event_id=event.event_id)
    for ch in event.channels:
        extra = sorted(set(ch.payload) - allowed)
        if extra:
            raise await _invalid(db, event.type, "field di luar allowlist", event_id=event.event_id, fields=extra)

    async with db.engine.connect() as conn:
        col = channel_config.c.channel_id
        known = set((await conn.scalars(select(col).where(col.in_(channel_ids)))).all())
    unknown = sorted(set(channel_ids) - known)
    if unknown:
        EVENTS_TOTAL.labels(event.type, "unknown_channel").inc()
        raise AstriaError("GW_CHANNEL_UNKNOWN", f"kanal tidak terdaftar: {', '.join(unknown)}", 404)

    messages = 0
    try:
        async with db.engine.begin() as conn:
            await conn.execute(event_inbox.insert().values(event_id=event.event_id, type=event.type, received_at=now()))
            for ch in event.channels:
                subject, body = render_event(event.type, ch.payload)
                i = channel_identity
                recipients = await conn.scalars(
                    select(i.c.identity_id).where(i.c.channel_id == ch.channel_id, i.c.status == "aktif")
                )
                for identity_id in recipients.all():
                    await enqueue(
                        conn,
                        event_id=event.event_id,
                        channel_id=ch.channel_id,
                        recipient_ref=identity_id,
                        subject=subject,
                        body=body,
                    )
                    messages += 1
            await audit(conn, "event.accepted", event_id=event.event_id, type=event.type, messages=messages)
    except IntegrityError:
        EVENTS_TOTAL.labels(event.type, "duplicate").inc()
        raise AstriaError("GW_EVENT_DUPLICATE", "event_id sudah diterima", 409) from None
    EVENTS_TOTAL.labels(event.type, "ok").inc()
    return JSONResponse({"event_id": event.event_id, "messages": messages}, status_code=202)


async def _revoke(db: Database, event: EventIn) -> JSONResponse:
    """Cabut semua ikatan kanal aktif milik `user_id`. Event ulang ditolak 409 (`event_inbox`), jadi tidak berulang."""
    if [c.channel_id for c in event.channels] != [REVOKE_CHANNEL]:
        raise await _invalid(db, event.type, "identity.revoked harus memuat satu kanal '*'", event_id=event.event_id)
    payload = event.channels[0].payload
    extra = sorted(set(payload) - REVOKE_FIELDS)
    if extra:
        raise await _invalid(db, event.type, "field di luar allowlist", event_id=event.event_id, fields=extra)
    user_id, reason = payload.get("user_id"), payload.get("reason")
    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id) or reason not in REVOKE_REASONS:
        raise await _invalid(db, event.type, "payload identity.revoked tidak valid", event_id=event.event_id)

    try:
        async with db.engine.begin() as conn:
            await conn.execute(event_inbox.insert().values(event_id=event.event_id, type=event.type, received_at=now()))
            i = channel_identity
            result = await conn.execute(
                update(i)
                .where(i.c.user_id == user_id, i.c.status == "aktif")
                .values(status="dicabut", updated_at=now())
            )
            revoked = int(result.rowcount)
            await audit(
                conn, "identity.revoked", event_id=event.event_id, user_id=user_id, reason=reason, identities=revoked
            )
    except IntegrityError:
        EVENTS_TOTAL.labels(event.type, "duplicate").inc()
        raise AstriaError("GW_EVENT_DUPLICATE", "event_id sudah diterima", 409) from None
    EVENTS_TOTAL.labels(event.type, "ok").inc()
    log.info("ikatan_dicabut", event_id=event.event_id, reason=reason, identities=revoked)
    return JSONResponse({"event_id": event.event_id, "identities": revoked}, status_code=202)


@router.put("/secrets/{key}", status_code=204)
async def put_secret(
    request: Request, key: str, _caller: Annotated[str, Depends(require_caller("config"))]
) -> Response:
    """Tulis rahasia kanal dari proksi admin config. Nilai tidak pernah dicatat atau dikembalikan."""
    _db(request)
    vault: SecretVault | None = request.app.state.vault
    if vault is None:
        raise AstriaError("GW_NOT_READY", "basis data gateway belum siap", 503)
    if key not in SECRET_CHANNELS:
        raise AstriaError("GW_KUNCI_TIDAK_DIKENAL", "kunci rahasia tidak dikenal", 404)
    try:
        body = SecretIn.model_validate_json(await request.body())
    except ValidationError:
        raise AstriaError("GW_EVENT_INVALID", "nilai rahasia tidak valid", 422) from None
    value = body.value.strip()
    if not value or (key == "telegram.bot_token" and not TELEGRAM_TOKEN.fullmatch(value)):
        raise AstriaError("GW_EVENT_INVALID", "nilai rahasia tidak valid", 422)
    try:
        await vault.put(key, value)
    except VaultError as exc:
        raise AstriaError(exc.code, exc.message, exc.status) from None
    return Response(status_code=204)


class WhatsAppIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    dari: str = Field(pattern=r"^[0-9]{8,15}$")
    teks: str = Field(max_length=WHATSAPP_MAX_CHARS)
    waktu: datetime


@router.post("/whatsapp/masuk", status_code=202)
async def whatsapp_inbound(request: Request) -> dict:
    """Pesan teks pribadi dari wa-bridge; diproses seperti perintah Telegram, balasan lewat outbox `whatsapp`."""
    state: Any = request.app.state
    if state.wa_inbound is None:
        raise AstriaError("GW_CHANNEL_UNKNOWN", "kanal whatsapp tidak aktif", 404)
    auth = request.headers.get("authorization", "")
    if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), state.wa_token.encode())):
        log.warning("wa_token_ditolak")
        raise AstriaError("GW_AUTH_FAILED", "token tidak valid", 401)
    _db(request)
    try:
        msg = WhatsAppIn.model_validate_json(await request.body())
    except ValidationError:
        raise AstriaError("GW_EVENT_INVALID", "pesan whatsapp tidak valid", 422) from None
    await state.wa_inbound.handle(msg.id, msg.dari, msg.teks)
    return {"id": msg.id}


@router.get("/status")
async def status(
    request: Request, _caller: Annotated[str, Depends(require_caller("notifier", "config", "api"))]
) -> dict:
    db = _db(request)
    m = outbox_message
    async with db.engine.connect() as conn:
        counts = dict((await conn.execute(select(m.c.status, func.count()).group_by(m.c.status))).all())
        oldest = await conn.scalar(select(func.min(m.c.created_at)).where(m.c.status == "pending"))
    state: Any = request.app.state
    return {
        "outbox": {s: counts.get(s, 0) for s in ("pending", "sending", "sent", "dead")},
        "oldest_pending_at": oldest.isoformat() if oldest else None,
        "telegram": state.poller.status() if state.poller else {"enabled": False},
        "whatsapp": {"enabled": "whatsapp" in state.adapters},
        "email": {"enabled": "email" in state.adapters},
        "public_listener": bool(state.settings.gw_public_bind),
    }
