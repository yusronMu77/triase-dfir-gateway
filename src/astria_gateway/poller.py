"""Poller Telegram `getUpdates` dan pemroses pesan masuk (GW§5.1 jalur masuk, GW§5.2).

Urutan per update: dedupe `inbound_message` -> batas laju global kanal -> identitas terikat (chat pribadi) ->
batas laju per identitas -> tata bahasa tertutup -> Command Facade (hanya T0) -> balasan lewat outbox.
Offset disimpan di `poll_state` setelah tiap update, jadi restart tidak memproses ulang pesan lama.
Ditunda: konfirmasi T1/T2 (`/ya`), pairing `/daftar`, `/berhenti`, grup chat, dan mode webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import datetime
from typing import Any

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from prometheus_client import Counter
from sqlalchemy import Update, select, update
from sqlalchemy.exc import IntegrityError

from astria_gateway import grammar, render
from astria_gateway.channels import SendError, Telegram
from astria_gateway.facade import FacadeClient, FacadeError
from astria_gateway.outbox import enqueue
from astria_gateway.ratelimit import RateLimiter
from astria_gateway.store import audit, channel_identity, inbound_message, now, poll_state, upsert

log = get_logger(__name__)

INBOUND_TOTAL = Counter("gw_inbound_total", "Pesan masuk dari chat", ["channel_type", "result"])

UNKNOWN_REPLY_PER_SECONDS = 600  # satu balasan "tidak terdaftar" per 10 menit per chat


def sender_hash(chat_id: str) -> str:
    """Pengenal pengirim untuk audit dan log tanpa ID chat mentah."""
    return hashlib.sha256(f"telegram:{chat_id}".encode()).hexdigest()[:16]


class TelegramPoller:
    name = "telegram-poller"
    required = False

    def __init__(
        self,
        db: Database,
        telegram: Telegram,
        facade: FacadeClient | None,
        *,
        channel_id: str,
        rate_per_identity: int = 20,
        rate_global: int = 60,
    ) -> None:
        self._db = db
        self._tg = telegram
        self._facade = facade
        self.channel_id = channel_id
        self._per_identity = RateLimiter(rate_per_identity, 60)
        self._global = RateLimiter(rate_global, 60)
        self._unknown = RateLimiter(1, UNKNOWN_REPLY_PER_SECONDS)
        self.last_poll_at: datetime | None = None
        self._offset: int | None = None
        self._last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gw-telegram-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._facade is not None:
            await self._facade.aclose()

    async def check(self) -> Check:
        return Check("degraded", self._last_error) if self._last_error else Check("ok")

    def status(self) -> dict[str, Any]:
        last = self.last_poll_at.isoformat() if self.last_poll_at else None
        return {"enabled": True, "channel_id": self.channel_id, "last_poll_at": last, "last_error": self._last_error}

    async def _run(self) -> None:
        await self._db.wait_ready()
        delay = 1.0
        while True:
            try:
                await self.poll_once()
                self._last_error, delay = None, 1.0
            except Exception as exc:  # noqa: BLE001 - poller tidak boleh mati; jeda naik sampai 60 detik
                self._last_error = exc.reason if isinstance(exc, SendError) else type(exc).__name__
                log.warning("telegram_poll_gagal", reason=self._last_error, retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def poll_once(self) -> int:
        updates = await self._tg.get_updates(await self._load_offset())
        self.last_poll_at = now()
        for upd in updates:
            update_id = upd.get("update_id")
            if not isinstance(update_id, int):
                continue
            try:
                await self.handle(update_id, upd.get("message"))
            except Exception as exc:  # noqa: BLE001 - satu update rusak tidak boleh menahan offset
                log.warning("update_gagal_diproses", update_id=update_id, error=type(exc).__name__)
            await self._save_offset(update_id + 1)
        return len(updates)

    async def _load_offset(self) -> int | None:
        # Poller satu-satunya penulis poll_state: cukup dibaca sekali per proses.
        if self._offset is None:
            async with self._db.engine.connect() as conn:
                col = poll_state.c.offset_value
                self._offset = await conn.scalar(select(col).where(poll_state.c.channel_id == self.channel_id))
        return self._offset

    async def _save_offset(self, offset: int) -> None:
        async with self._db.engine.begin() as conn:
            await upsert(conn, poll_state, "channel_id", self.channel_id, {"offset_value": offset, "updated_at": now()})
        self._offset = offset

    async def handle(self, update_id: int, message: Any) -> None:
        try:
            async with self._db.engine.begin() as conn:
                await conn.execute(
                    inbound_message.insert().values(
                        channel_id=self.channel_id, provider_msg_id=str(update_id), status="diterima", received_at=now()
                    )
                )
        except IntegrityError:
            INBOUND_TOTAL.labels("telegram", "duplicate").inc()
            return

        if not self._global.allow(self.channel_id):
            await self._finish(update_id, "ditolak", audit_action="inbound.rate_limited_global")
            INBOUND_TOTAL.labels("telegram", "rate_limited").inc()
            return
        chat = message.get("chat") if isinstance(message, dict) else None
        text = message.get("text") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(text, str):
            await self._finish(update_id, "ditolak")
            INBOUND_TOTAL.labels("telegram", "ignored").inc()
            return

        chat_id = str(chat.get("id"))
        identity = await self._identity(chat_id)
        if identity is None:
            await self._finish(update_id, "ditolak", audit_action="sender.unknown", sender=sender_hash(chat_id))
            INBOUND_TOTAL.labels("telegram", "unknown_sender").inc()
            if self._unknown.allow(chat_id):
                with contextlib.suppress(SendError):
                    await self._tg.send(chat_id, "", render.NOT_REGISTERED)
            return

        identity_id, user_id = identity
        if not self._per_identity.allow(identity_id):
            reply, command_name, result = render.RATE_LIMITED, None, "rate_limited"
        else:
            command = grammar.parse(text)
            command_name = command.name if command else None
            reply = await self._reply(command, user_id, update_id)
            result = "ok" if command else "grammar"
        INBOUND_TOTAL.labels("telegram", result).inc()
        async with self._db.engine.begin() as conn:
            await enqueue(
                conn,
                event_id=f"in:{update_id}",
                channel_id=self.channel_id,
                recipient_ref=identity_id,
                subject="",
                body=reply,
            )
            await conn.execute(self._mark(update_id, "selesai", identity_id=identity_id, command=command_name))

    async def _reply(self, command: grammar.Command | None, user_id: str, update_id: int) -> str:
        if command is None or command.name == "bantuan":
            return render.HELP
        if command.tier == "konfirmasi":
            return render.NO_PENDING
        if command.tier != "T0" or command.facade is None:
            return render.not_available(command.name)
        if self._facade is None:
            return render.CORE_DOWN
        try:
            hasil = await self._facade.run(
                user_id=user_id,
                channel_id=self.channel_id,
                command=command.facade,
                args=command.args,
                idempotency_key=f"{self.channel_id}:{update_id}",
            )
        except FacadeError as exc:
            log.info("perintah_gagal", command=command.facade, unavailable=exc.unavailable)
            return render.CORE_DOWN if exc.unavailable else render.core_rejected(exc.message)
        return render.render_result(command.name, hasil)

    async def _identity(self, chat_id: str) -> tuple[str, str] | None:
        i = channel_identity
        query = select(i.c.identity_id, i.c.user_id).where(
            i.c.channel_id == self.channel_id, i.c.external_id == chat_id, i.c.status == "aktif"
        )
        async with self._db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return (row.identity_id, row.user_id) if row else None

    def _mark(self, update_id: int, status: str, **values: Any) -> Update:
        m = inbound_message
        where = (m.c.channel_id == self.channel_id, m.c.provider_msg_id == str(update_id))
        return update(m).where(*where).values(status=status, **values)

    async def _finish(self, update_id: int, status: str, *, audit_action: str | None = None, **detail: Any) -> None:
        async with self._db.engine.begin() as conn:
            await conn.execute(self._mark(update_id, status))
            if audit_action:
                await audit(conn, audit_action, channel_id=self.channel_id, update_id=update_id, **detail)
