"""Pekerja outbox dengan Telegram tiruan (httpx.MockTransport) dan SMTP tiruan, di SQLite dan PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import aiosmtplib
import httpx
import pytest
from helpers import CHAT_ID, TOKEN, TelegramFake, outbox_rows, seeded, telegram
from sqlalchemy import select, update

from astria_gateway.channels import Smtp, Telegram
from astria_gateway.outbox import MAX_ATTEMPTS, OutboxWorker, enqueue
from astria_gateway.store import gw_audit, outbox_message

pytestmark = pytest.mark.anyio


async def _queue(db, channel_id: str, recipient: str, body: str = "Halo") -> None:
    async with db.engine.begin() as conn:
        await enqueue(
            conn, event_id="evt-1", channel_id=channel_id, recipient_ref=recipient, subject="Subjek", body=body
        )


def _telegram(fake: TelegramFake, **kw: Any) -> Telegram:
    return telegram(fake.handler, **kw)


async def test_telegram_200_marks_sent_and_sends_plain_text(db) -> None:
    await seeded(db)
    fake = TelegramFake()
    await _queue(db, "telegram-tim", "idt_tg1", body="x" * 5000)
    assert await OutboxWorker(db, {"telegram": _telegram(fake, max_chars=3000)}).run_once() == 1
    [row] = await outbox_rows(db)
    assert (row.status, row.attempts, row.last_error) == ("sent", 1, None)
    [sent] = fake.sent()
    assert sent["chat_id"] == CHAT_ID
    assert "parse_mode" not in sent
    assert len(sent["text"]) == 3000


async def test_telegram_429_honors_retry_after(db) -> None:
    await seeded(db)
    fake = TelegramFake()
    fake.queue("sendMessage", httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 120}}))
    await _queue(db, "telegram-tim", "idt_tg1")
    before = datetime.now(UTC)
    await OutboxWorker(db, {"telegram": _telegram(fake)}).run_once()
    [row] = await outbox_rows(db)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "telegram_429")
    retry_at = row.next_attempt_at if row.next_attempt_at.tzinfo else row.next_attempt_at.replace(tzinfo=UTC)
    assert before + timedelta(seconds=115) < retry_at < before + timedelta(seconds=130)


async def test_telegram_401_is_dead_and_audited_without_token(db) -> None:
    await seeded(db)
    fake = TelegramFake()
    fake.queue("sendMessage", httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    telegram = _telegram(fake)
    await _queue(db, "telegram-tim", "idt_tg1")
    await OutboxWorker(db, {"telegram": telegram}).run_once()
    [row] = await outbox_rows(db)
    assert (row.status, row.last_error) == ("dead", "telegram_401")
    async with db.engine.connect() as conn:
        audit = (await conn.execute(select(gw_audit.c.action, gw_audit.c.detail_json))).all()
    assert audit[-1].action == "send.dead"
    assert TOKEN not in audit[-1].detail_json and TOKEN not in (row.last_error or "")
    assert (await telegram.check()).status == "degraded"


async def test_transport_error_is_temporary_and_gives_up_after_max_attempts(db) -> None:
    await seeded(db)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gagal", request=request)

    worker = OutboxWorker(db, {"telegram": telegram(boom)})
    await _queue(db, "telegram-tim", "idt_tg1")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with db.engine.begin() as conn:  # jatuh tempo sekarang, tanpa menunggu jeda
            await conn.execute(update(outbox_message).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
        await worker.run_once()
        [row] = await outbox_rows(db)
        assert row.attempts == attempt
        assert row.last_error == "telegram_transport_ConnectError"
    assert row.status == "dead"


async def test_inactive_identity_or_missing_adapter(db) -> None:
    await seeded(db)
    await _queue(db, "email-tim", "idt_em2")  # identitas dicabut
    await _queue(db, "telegram-tim", "idt_tg1")  # adapter Telegram mati (token kosong)
    await OutboxWorker(db, {}).run_once()
    by_channel = {r.channel_id: r for r in await outbox_rows(db)}
    assert (by_channel["email-tim"].status, by_channel["email-tim"].last_error) == ("dead", "identitas_tidak_aktif")
    assert (by_channel["telegram-tim"].status, by_channel["telegram-tim"].last_error) == (
        "pending",
        "kanal_tidak_aktif",
    )


async def test_release_stale_sending_rows(db) -> None:
    await seeded(db)
    await _queue(db, "telegram-tim", "idt_tg1")
    async with db.engine.begin() as conn:
        await conn.execute(update(outbox_message).values(status="sending"))
    await OutboxWorker(db, {}).release_stale()
    assert [r.status for r in await outbox_rows(db)] == ["pending"]


class FakeSmtp:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[tuple[EmailMessage, dict[str, Any]]] = []

    async def __call__(self, message: EmailMessage, **options: Any) -> Any:
        if self.error is not None:
            raise self.error
        self.messages.append((message, options))
        return {}, "OK"


def _smtp(fake: FakeSmtp, tls: str = "starttls") -> Smtp:
    return Smtp(
        host="smtp.astria.test", port=587, tls=tls, username="", password="", sender="astria@astria.test", send=fake
    )


async def test_smtp_sends_mail_with_starttls_and_timeout(db) -> None:
    await seeded(db)
    fake = FakeSmtp()
    await _queue(db, "email-tim", "idt_em1", body="Isi pesan")
    await OutboxWorker(db, {"email": _smtp(fake)}).run_once()
    assert [r.status for r in await outbox_rows(db)] == ["sent"]
    [(message, options)] = fake.messages
    assert (message["To"], message["From"], message["Subject"]) == ("soc@astria.test", "astria@astria.test", "Subjek")
    assert message.get_content().strip() == "Isi pesan"
    assert options == {
        "hostname": "smtp.astria.test",
        "port": 587,
        "username": None,
        "password": None,
        "use_tls": False,
        "start_tls": True,
        "timeout": 10.0,
    }


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (aiosmtplib.SMTPResponseException(550, "mailbox unavailable"), "dead", "smtp_550"),
        (aiosmtplib.SMTPRecipientsRefused([]), "dead", "smtp_penerima_ditolak"),
        (aiosmtplib.SMTPResponseException(451, "try later"), "pending", "smtp_451"),
        (aiosmtplib.SMTPConnectError("tidak terjangkau"), "pending", "smtp_SMTPConnectError"),
    ],
)
async def test_smtp_errors(db, error: Exception, status: str, reason: str) -> None:
    await seeded(db)
    await _queue(db, "email-tim", "idt_em1")
    await OutboxWorker(db, {"email": _smtp(FakeSmtp(error))}).run_once()
    [row] = await outbox_rows(db)
    assert (row.status, row.last_error) == (status, reason)


@pytest.mark.parametrize(("tls", "use_tls", "start_tls"), [("tls", True, False), ("none", False, False)])
async def test_smtp_tls_modes(tls: str, use_tls: bool, start_tls: bool) -> None:
    fake = FakeSmtp()
    await _smtp(fake, tls).send("soc@astria.test", "S", "T")
    assert (fake.messages[0][1]["use_tls"], fake.messages[0][1]["start_tls"]) == (use_tls, start_tls)
