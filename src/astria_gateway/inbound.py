"""Pemroses pesan chat masuk bersama Telegram dan WhatsApp (GW§5.1 jalur masuk, GW§5.2).

Urutan per pesan: dedupe `inbound_message` -> batas laju global kanal -> identitas terikat (chat pribadi) ->
batas laju per identitas -> tata bahasa tertutup -> Command Facade (hanya T0) -> balasan lewat outbox.
Pengirim tak dikenal mendapat satu balasan tetap per 10 menit, dikirim langsung (tidak ada identitas untuk outbox).
Pengirim tak dikenal boleh `/daftar <kode>`: kode diklaim lewat api (`/svc/v1/pairing/claim`), lalu chat diikat ke
pemilik kode (GW§3.3). Ditunda: konfirmasi T1/T2 (`/ya`), `/berhenti`, dan grup chat.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from astria_common.db import Database
from astria_common.log import get_logger
from prometheus_client import Counter
from sqlalchemy import Update, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_gateway import grammar, render
from astria_gateway.channels import SendError
from astria_gateway.facade import FacadeClient, FacadeError
from astria_gateway.outbox import enqueue
from astria_gateway.ratelimit import RateLimiter
from astria_gateway.store import audit, channel_identity, inbound_message, now

log = get_logger(__name__)

INBOUND_TOTAL = Counter("gw_inbound_total", "Pesan masuk dari chat", ["channel_type", "result"])

UNKNOWN_REPLY_PER_SECONDS = 600  # satu balasan "tidak terdaftar" per 10 menit per chat
PAIRING_ATTEMPTS = 5  # percobaan /daftar per chat per 10 menit (sama dengan batas identity per external_id_hash)

SendDirect = Callable[[str, str, str], Awaitable[None]]


def sender_hash(kind: str, sender: str) -> str:
    """Pengenal pengirim untuk audit dan log tanpa ID chat atau nomor mentah."""
    return hashlib.sha256(f"{kind}:{sender}".encode()).hexdigest()[:16]


class InboundHandler:
    def __init__(
        self,
        db: Database,
        facade: FacadeClient | None,
        send_direct: SendDirect,
        *,
        kind: str,
        channel_id: str,
        rate_per_identity: int = 20,
        rate_global: int = 60,
    ) -> None:
        self._db = db
        self._facade = facade
        self._send_direct = send_direct
        self.kind = kind
        self.channel_id = channel_id
        self._per_identity = RateLimiter(rate_per_identity, 60)
        self._global = RateLimiter(rate_global, 60)
        self._unknown = RateLimiter(1, UNKNOWN_REPLY_PER_SECONDS)
        self._pairing = RateLimiter(PAIRING_ATTEMPTS, UNKNOWN_REPLY_PER_SECONDS)

    async def handle(self, msg_id: str, sender: str | None, text: str | None) -> None:
        """`sender` None = bukan chat pribadi atau bukan teks; pesan dicatat lalu diabaikan."""
        try:
            async with self._db.engine.begin() as conn:
                await conn.execute(
                    inbound_message.insert().values(
                        channel_id=self.channel_id, provider_msg_id=msg_id, status="diterima", received_at=now()
                    )
                )
        except IntegrityError:
            INBOUND_TOTAL.labels(self.kind, "duplicate").inc()
            return

        if not self._global.allow(self.channel_id):
            await self._finish(msg_id, "ditolak", audit_action="inbound.rate_limited_global")
            INBOUND_TOTAL.labels(self.kind, "rate_limited").inc()
            return
        if sender is None or text is None:
            await self._finish(msg_id, "ditolak")
            INBOUND_TOTAL.labels(self.kind, "ignored").inc()
            return

        identity = await self._identity(sender)
        command = grammar.parse(text)
        if identity is None:
            if command is not None and command.tier == "pairing":
                await self._pair(msg_id, sender, command.args["kode"])
                return
            await self._finish(msg_id, "ditolak", audit_action="sender.unknown", sender=sender_hash(self.kind, sender))
            INBOUND_TOTAL.labels(self.kind, "unknown_sender").inc()
            if self._unknown.allow(sender):
                with contextlib.suppress(SendError):
                    await self._send_direct(sender, "", render.NOT_REGISTERED)
            return

        identity_id, user_id = identity
        if not self._per_identity.allow(identity_id):
            reply, command_name, result = render.RATE_LIMITED, None, "rate_limited"
        else:
            command_name = command.name if command else None
            reply = await self._reply(command, user_id, msg_id)
            result = "ok" if command else "grammar"
        INBOUND_TOTAL.labels(self.kind, result).inc()
        async with self._db.engine.begin() as conn:
            await enqueue(
                conn,
                event_id=f"in:{msg_id}",
                channel_id=self.channel_id,
                recipient_ref=identity_id,
                subject="",
                body=reply,
            )
            await conn.execute(self._mark(msg_id, "selesai", identity_id=identity_id, command=command_name))

    async def _reply(self, command: grammar.Command | None, user_id: str, msg_id: str) -> str:
        if command is None or command.name == "bantuan":
            return render.HELP
        if command.tier == "konfirmasi":
            return render.NO_PENDING
        if command.tier == "pairing":
            return render.ALREADY_PAIRED
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
                idempotency_key=f"{self.channel_id}:{msg_id}",
            )
        except FacadeError as exc:
            log.info("perintah_gagal", command=command.facade, unavailable=exc.unavailable)
            return render.CORE_DOWN if exc.unavailable else render.core_rejected(exc.message)
        return render.render_result(command.name, hasil)

    async def _pair(self, msg_id: str, sender: str, code: str) -> None:
        if not self._pairing.allow(sender):
            return await self._reject_pairing(msg_id, sender, render.RATE_LIMITED)
        if self._facade is None:
            return await self._reject_pairing(msg_id, sender, render.CORE_DOWN)
        try:
            user_id = await self._facade.claim_pairing(
                code=code.upper(), channel_id=self.channel_id, external_id=sender
            )
        except FacadeError as exc:
            log.info("pairing_gagal", status=exc.status, unavailable=exc.unavailable)
            reply = render.CORE_DOWN if exc.unavailable else render.PAIRING_FAILED
            return await self._reject_pairing(msg_id, sender, render.RATE_LIMITED if exc.status == 429 else reply)
        async with self._db.engine.begin() as conn:
            identity_id = await self._bind(conn, sender, user_id)
            who = sender_hash(self.kind, sender)
            await audit(conn, "identity.paired", channel_id=self.channel_id, identity_id=identity_id, sender=who)
            await enqueue(
                conn,
                event_id=f"in:{msg_id}",
                channel_id=self.channel_id,
                recipient_ref=identity_id,
                subject="",
                body=render.PAIRED,
            )
            await conn.execute(self._mark(msg_id, "selesai", identity_id=identity_id, command="daftar"))
        INBOUND_TOTAL.labels(self.kind, "paired").inc()

    async def _reject_pairing(self, msg_id: str, sender: str, reply: str) -> None:
        INBOUND_TOTAL.labels(self.kind, "pairing_failed").inc()
        await self._finish(msg_id, "ditolak", audit_action="pairing.failed", sender=sender_hash(self.kind, sender))
        with contextlib.suppress(SendError):
            await self._send_direct(sender, "", reply)

    async def _bind(self, conn: AsyncConnection, sender: str, user_id: str) -> str:
        """Ikat chat ke `user_id`; ikatan lama yang dicabut untuk chat yang sama diaktifkan ulang."""
        i = channel_identity
        values = {"user_id": user_id, "status": "aktif", "updated_at": now()}
        where = (i.c.channel_id == self.channel_id, i.c.external_id == sender)
        existing = await conn.scalar(select(i.c.identity_id).where(*where))
        if existing is not None:
            await conn.execute(update(i).where(i.c.identity_id == existing).values(values))
            return existing
        identity_id = f"idt_{secrets.token_hex(8)}"
        await conn.execute(
            i.insert().values(identity_id=identity_id, channel_id=self.channel_id, external_id=sender, **values)
        )
        return identity_id

    async def _identity(self, sender: str) -> tuple[str, str] | None:
        i = channel_identity
        query = select(i.c.identity_id, i.c.user_id).where(
            i.c.channel_id == self.channel_id, i.c.external_id == sender, i.c.status == "aktif"
        )
        async with self._db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return (row.identity_id, row.user_id) if row else None

    def _mark(self, msg_id: str, status: str, **values: Any) -> Update:
        m = inbound_message
        where = (m.c.channel_id == self.channel_id, m.c.provider_msg_id == msg_id)
        return update(m).where(*where).values(status=status, **values)

    async def _finish(self, msg_id: str, status: str, *, audit_action: str | None = None, **detail: Any) -> None:
        async with self._db.engine.begin() as conn:
            await conn.execute(self._mark(msg_id, status))
            if audit_action:
                await audit(conn, audit_action, channel_id=self.channel_id, msg_id=msg_id, **detail)
