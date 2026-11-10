"""Poller Telegram `getUpdates` (GW§5.1 jalur masuk); tiap update diproses `inbound.InboundHandler`.

Hanya chat pribadi bertipe teks yang diteruskan sebagai perintah; selain itu dicatat lalu diabaikan.
Offset disimpan di `poll_state` setelah tiap update, jadi restart tidak memproses ulang pesan lama.
Ditunda: mode webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from sqlalchemy import select

from astria_gateway.channels import SendError, Telegram
from astria_gateway.inbound import InboundHandler
from astria_gateway.store import now, poll_state, upsert

log = get_logger(__name__)


class TelegramPoller:
    name = "telegram-poller"
    required = False

    def __init__(self, db: Database, telegram: Telegram, inbound: InboundHandler) -> None:
        self._db = db
        self._tg = telegram
        self.inbound = inbound
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

    async def check(self) -> Check:
        return Check("degraded", self._last_error) if self._last_error else Check("ok")

    def status(self) -> dict[str, Any]:
        last = self.last_poll_at.isoformat() if self.last_poll_at else None
        return {
            "enabled": True,
            "channel_id": self.inbound.channel_id,
            "last_poll_at": last,
            "last_error": self._last_error,
        }

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
                self._offset = await conn.scalar(select(col).where(poll_state.c.channel_id == self.inbound.channel_id))
        return self._offset

    async def _save_offset(self, offset: int) -> None:
        async with self._db.engine.begin() as conn:
            await upsert(
                conn, poll_state, "channel_id", self.inbound.channel_id, {"offset_value": offset, "updated_at": now()}
            )
        self._offset = offset

    async def handle(self, update_id: int, message: Any) -> None:
        msg = message if isinstance(message, dict) else {}
        chat, text = msg.get("chat"), msg.get("text")
        if isinstance(chat, dict) and chat.get("type") == "private" and isinstance(text, str):
            await self.inbound.handle(str(update_id), str(chat.get("id")), text)
        else:
            await self.inbound.handle(str(update_id), None, None)
