"""Outbox pesan keluar dan pekerja pengirimnya (GW§4 status outbox, GW§5.1).

`pending` -> `sending` (klaim UPDATE bersyarat) -> `sent`; galat sementara kembali ke `pending` dengan jeda naik
(atau `retry_after` penyedia); galat permanen atau 5 percobaan habis -> `dead`. Satu proses penulis, jadi baris
`sending` sisa proses yang mati dikembalikan ke `pending` saat mulai.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from prometheus_client import Counter
from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_gateway.channels import Adapter, SendError
from astria_gateway.store import audit, channel_config, channel_identity, now, outbox_message

log = get_logger(__name__)

SEND_TOTAL = Counter("gw_send_total", "Pengiriman pesan keluar", ["channel_type", "result"])

MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 300


async def enqueue(
    conn: AsyncConnection, *, event_id: str, channel_id: str, recipient_ref: str, subject: str, body: str
) -> None:
    ts = now()
    await conn.execute(
        outbox_message.insert().values(
            message_id=str(uuid.uuid7()),
            event_id=event_id,
            channel_id=channel_id,
            recipient_ref=recipient_ref,
            subject=subject,
            body=body,
            status="pending",
            attempts=0,
            next_attempt_at=ts,
            created_at=ts,
            updated_at=ts,
        )
    )


def backoff(attempts: int) -> float:
    return min(5 * 2**attempts, MAX_BACKOFF_SECONDS)


class OutboxWorker:
    name = "outbox-worker"
    required = False

    def __init__(
        self, db: Database, adapters: dict[str, Adapter], *, interval: float = 1.0, batch_size: int = 20
    ) -> None:
        self._db = db
        self._adapters = adapters
        self._interval = interval
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gw-outbox-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check(self) -> Check:
        return Check("degraded", self._last_error) if self._last_error else Check("ok")

    async def _run(self) -> None:
        await self._db.wait_ready()
        await self.release_stale()
        while True:
            try:
                await self.run_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - dicoba lagi pada putaran berikutnya
                self._last_error = type(exc).__name__
                log.warning("outbox_putaran_gagal", error=self._last_error)
            await asyncio.sleep(self._interval)

    async def release_stale(self) -> None:
        async with self._db.engine.begin() as conn:
            await conn.execute(
                update(outbox_message).where(outbox_message.c.status == "sending").values(status="pending")
            )

    async def run_once(self) -> int:
        """Kirim pesan yang sudah jatuh tempo. Mengembalikan jumlah pesan yang diproses."""
        m, c, i = outbox_message, channel_config, channel_identity
        query = (
            select(
                m.c.message_id,
                m.c.subject,
                m.c.body,
                m.c.attempts,
                c.c.type,
                i.c.external_id,
                i.c.status.label("identity_status"),
            )
            .select_from(m.join(c, m.c.channel_id == c.c.channel_id).outerjoin(i, m.c.recipient_ref == i.c.identity_id))
            .where(m.c.status == "pending", m.c.next_attempt_at <= now())
            .order_by(m.c.next_attempt_at)
            .limit(self._batch_size)
        )
        async with self._db.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        done = 0
        for row in rows:
            if await self._claim(row.message_id):
                await self._deliver(row)
                done += 1
        return done

    async def _claim(self, message_id: str) -> bool:
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                update(outbox_message)
                .where(outbox_message.c.message_id == message_id, outbox_message.c.status == "pending")
                .values(status="sending", updated_at=now())
            )
        return result.rowcount == 1

    async def _deliver(self, row: Row[Any]) -> None:
        adapter = self._adapters.get(row.type)
        try:
            if row.external_id is None or row.identity_status != "aktif":
                raise SendError("identitas_tidak_aktif", permanent=True)
            if adapter is None:
                raise SendError("kanal_tidak_aktif")
            await adapter.send(row.external_id, row.subject, row.body)
        except SendError as exc:
            await self._failed(row, exc)
            return
        SEND_TOTAL.labels(row.type, "ok").inc()
        await self._finish(row.message_id, "sent", row.attempts + 1, None)

    async def _failed(self, row: Row[Any], exc: SendError) -> None:
        attempts = row.attempts + 1
        if exc.permanent or attempts >= MAX_ATTEMPTS:
            SEND_TOTAL.labels(row.type, "permanent").inc()
            log.warning("pesan_dead", message_id=row.message_id, channel_type=row.type, reason=exc.reason)
            await self._finish(row.message_id, "dead", attempts, exc.reason)
            return
        SEND_TOTAL.labels(row.type, "temporary").inc()
        delay = exc.retry_after if exc.retry_after is not None else backoff(attempts)
        retry_at = now() + timedelta(seconds=delay)
        await self._finish(row.message_id, "pending", attempts, exc.reason, next_attempt_at=retry_at)

    async def _finish(
        self,
        message_id: str,
        status: str,
        attempts: int,
        last_error: str | None,
        *,
        next_attempt_at: datetime | None = None,
    ) -> None:
        values = {"status": status, "attempts": attempts, "last_error": last_error, "updated_at": now()}
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        async with self._db.engine.begin() as conn:
            await conn.execute(update(outbox_message).where(outbox_message.c.message_id == message_id).values(values))
            if status == "dead":
                await audit(conn, "send.dead", message_id=message_id, reason=last_error)
