"""Poller Telegram: offset, dedupe, pengirim tak dikenal, perintah T0 lewat Command Facade tiruan."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from helpers import CoreFake, TelegramFake, facade, outbox_rows, seeded, telegram, update
from sqlalchemy import select

from astria_gateway import render
from astria_gateway.inbound import InboundHandler, sender_hash
from astria_gateway.poller import TelegramPoller
from astria_gateway.store import gw_audit, inbound_message, poll_state

pytestmark = pytest.mark.anyio


def make_poller(db, tg: TelegramFake, core: CoreFake | None = None, **kw: Any) -> TelegramPoller:
    tg_adapter = telegram(tg.handler)
    handler = InboundHandler(db, facade(core), tg_adapter.send, kind="telegram", channel_id="telegram-tim", **kw)
    return TelegramPoller(db, tg_adapter, handler)


async def _offset(db) -> int | None:
    async with db.engine.connect() as conn:
        return await conn.scalar(select(poll_state.c.offset_value))


async def test_offset_persisted_and_sent_on_next_poll(db) -> None:
    await seeded(db)
    tg = TelegramFake()
    tg.queue(
        "getUpdates",
        httpx.Response(200, json={"ok": True, "result": [update(40017, "/bantuan"), update(40018, "halo")]}),
    )
    poller = make_poller(db, tg)
    assert await poller.poll_once() == 2
    assert await _offset(db) == 40019
    await poller.poll_once()
    polls = [payload for method, payload in tg.calls if method == "getUpdates"]
    assert "offset" not in polls[0] and polls[1]["offset"] == 40019
    assert polls[0]["timeout"] == 30
    # Poller baru (mis. setelah restart) melanjutkan dari offset tersimpan.
    await make_poller(db, tg).poll_once()
    assert [p for m, p in tg.calls if m == "getUpdates"][-1]["offset"] == 40019


async def test_duplicate_update_processed_once(db) -> None:
    await seeded(db)
    core = CoreFake()
    poller = make_poller(db, TelegramFake(), core)
    await poller.handle(40017, update(40017, "/kartu IC-20261110-003")["message"])
    await poller.handle(40017, update(40017, "/kartu IC-20261110-003")["message"])
    assert len(core.requests) == 1
    assert len(await outbox_rows(db)) == 1


async def test_t0_command_calls_facade_and_queues_reply(db) -> None:
    await seeded(db)
    core = CoreFake()
    poller = make_poller(db, TelegramFake(), core)
    await poller.handle(40017, update(40017, "/kartu IC-20261110-003")["message"])
    assert core.requests == [
        {
            "user_id": "usr_0142",
            "channel_id": "telegram-tim",
            "command": "card",
            "args": {"card_id": "IC-20261110-003"},
            "idempotency_key": "telegram-tim:40017",
        }
    ]
    [reply] = await outbox_rows(db)
    assert (reply.recipient_ref, reply.event_id, reply.status) == ("idt_tg1", "in:40017", "pending")
    assert reply.body.splitlines() == ["/kartu", "card_id: IC-20261110-003", "band: KRITIS", "skor: 71"]
    async with db.engine.connect() as conn:
        row = (await conn.execute(select(inbound_message))).one()
    assert (row.status, row.command, row.identity_id) == ("selesai", "kartu", "idt_tg1")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(503, json={"error": {"code": "TIDAK_TERSEDIA", "message": "x"}}), render.CORE_DOWN),
        (
            httpx.Response(403, json={"error": {"code": "DILARANG", "message": "peran Anda tidak boleh"}}),
            render.core_rejected("peran Anda tidak boleh"),
        ),
    ],
)
async def test_facade_failures_become_fixed_replies(db, response: httpx.Response, expected: str) -> None:
    await seeded(db)
    poller = make_poller(db, TelegramFake(), CoreFake(response))
    await poller.handle(1, update(1, "/status")["message"])
    assert [r.body for r in await outbox_rows(db)] == [expected]


async def test_free_text_t1_and_missing_facade(db) -> None:
    await seeded(db)
    poller = make_poller(db, TelegramFake(), core=None)
    for n, text in enumerate(["tolong hapus semua", "/label IC-20261110-003 benar", "/ya 1234", "/status"], 1):
        await poller.handle(n, update(n, text)["message"])
    bodies = [r.body for r in await outbox_rows(db)]
    assert bodies == [render.HELP, render.not_available("label"), render.NO_PENDING, render.CORE_DOWN]


async def test_unknown_sender_gets_one_fixed_reply_and_audit(db) -> None:
    await seeded(db)
    tg = TelegramFake()
    core = CoreFake()
    poller = make_poller(db, tg, core)
    for n in (1, 2):
        await poller.handle(n, update(n, "/kartu IC-20261110-003", chat_id="555")["message"])
    assert core.requests == []
    assert [s["text"] for s in tg.sent()] == [render.NOT_REGISTERED]
    assert await outbox_rows(db) == []
    async with db.engine.connect() as conn:
        audit = (await conn.execute(select(gw_audit.c.action, gw_audit.c.detail_json))).all()
    assert [a.action for a in audit] == ["sender.unknown", "sender.unknown"]
    assert sender_hash("telegram", "555") in audit[0].detail_json and '"555"' not in audit[0].detail_json


async def test_group_chat_ignored_and_rate_limits(db) -> None:
    await seeded(db)
    core = CoreFake()
    poller = make_poller(db, TelegramFake(), core, rate_per_identity=2, rate_global=60)
    await poller.handle(1, update(1, "/status", chat_id="-100", chat_type="group")["message"])
    for n in range(2, 5):
        await poller.handle(n, update(n, "/status")["message"])
    assert len(core.requests) == 2
    assert [r.body for r in await outbox_rows(db)][-1] == render.RATE_LIMITED

    limited = make_poller(db, TelegramFake(), core, rate_global=1)
    await limited.handle(10, update(10, "/status")["message"])
    await limited.handle(11, update(11, "/status")["message"])
    async with db.engine.connect() as conn:
        status = dict((await conn.execute(select(inbound_message.c.provider_msg_id, inbound_message.c.status))).all())
    assert status["1"] == "ditolak" and status["11"] == "ditolak"


async def test_poll_error_keeps_offset(db) -> None:
    await seeded(db)
    tg = TelegramFake()
    tg.queue("getUpdates", httpx.Response(409, json={"ok": False}))
    poller = make_poller(db, tg)
    with pytest.raises(Exception, match="telegram_409"):
        await poller.poll_once()
    assert await _offset(db) is None
