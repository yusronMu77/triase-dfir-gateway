"""Event kontrol `identity.revoked` di gateway: ikatan kanal dicabut, idempoten, chat tidak diproses lagi."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astria_common.hmac_auth import signed_headers
from fastapi.testclient import TestClient
from helpers import (
    CHAT_ID,
    NOTIFIER_KEY,
    CoreFake,
    TelegramFake,
    facade,
    gateway_settings,
    outbox_rows,
    ready_client,
    seeded,
    telegram,
)
from sqlalchemy import select

from astria_gateway import render
from astria_gateway.api import EventIn, _revoke
from astria_gateway.inbound import InboundHandler
from astria_gateway.main import build_app
from astria_gateway.outbox import OutboxWorker, enqueue
from astria_gateway.store import channel_identity

EVENTS = "/internal/v1/events"
EVENT_ID = "01JDREVOKE0000000000000001"
USER = "usr_0142"  # punya tiga identitas aktif di SEED (telegram, email, wa)


def revoke_body(event_id: str = EVENT_ID, user_id: str = USER, **payload: Any) -> dict[str, Any]:
    data = payload or {"user_id": user_id, "reason": "akun_nonaktif", "revoked_at": "2026-11-12T06:00:00Z"}
    return {
        "event_id": event_id,
        "type": "identity.revoked",
        "occurred_at": "2026-11-12T06:00:00Z",
        "channels": [{"channel_id": "*", "payload": data}],
    }


def card_body(event_id: str) -> dict[str, Any]:
    payload = {"card_id": "IC-20261110-003", "version": 1, "band": "KRITIS", "skor": 71}
    return {
        "event_id": event_id,
        "type": "card.verified",
        "occurred_at": "2026-11-12T06:01:00Z",
        "channels": [
            {"channel_id": "email-tim", "payload": payload},
            {"channel_id": "telegram-tim", "payload": payload},
        ],
    }


def post(client: TestClient, body: Any) -> Any:
    raw = json.dumps(body).encode()
    headers = signed_headers("notifier", NOTIFIER_KEY, "POST", EVENTS, raw)
    return client.post(EVENTS, content=raw, headers={**headers, "Content-Type": "application/json"})


def statuses(db_file: Path) -> dict[str, str]:
    with sqlite3.connect(db_file) as conn:
        return dict(conn.execute("SELECT identity_id, status FROM channel_identity").fetchall())


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"notifier": NOTIFIER_KEY.hex(), "config": ("33" * 32)}))
    with ready_client(build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys))) as client:
        yield client, tmp_path / "gateway_test.db"


def test_revokes_active_identities_of_user_only(gw) -> None:
    client, db_file = gw
    before = statuses(db_file)
    resp = post(client, revoke_body())
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"event_id": EVENT_ID, "identities": 3}
    after = statuses(db_file)
    assert {k for k in after if after[k] != before[k]} == {"idt_tg1", "idt_em1", "idt_wa1"}
    assert after["idt_em2"] == "dicabut"  # milik usr_0007, tidak disentuh


def test_same_event_id_is_duplicate_and_repeat_is_harmless(gw) -> None:
    client, db_file = gw
    assert post(client, revoke_body()).status_code == 202
    assert post(client, revoke_body()).status_code == 409
    resp = post(client, revoke_body(event_id="01JDREVOKE0000000000000002"))
    assert resp.status_code == 202 and resp.json()["identities"] == 0
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gw_audit WHERE action = 'identity.revoked'").fetchone()[0] == 2


def test_card_event_after_revoke_reaches_no_identity(gw) -> None:
    client, _ = gw
    assert post(client, revoke_body()).status_code == 202
    resp = post(client, card_body("01JDCARD000000000000000001"))
    assert resp.status_code == 202, resp.text
    assert resp.json()["messages"] == 0


def test_wrong_shape_is_refused(gw) -> None:
    client, db_file = gw
    before = statuses(db_file)
    bad_channel = revoke_body()
    bad_channel["channels"][0]["channel_id"] = "email-tim"
    assert post(client, bad_channel).status_code == 422
    extra = revoke_body(user_id=USER, reason="akun_nonaktif", revoked_at="2026-11-12T06:00:00Z", catatan="bebas")
    assert post(client, extra).status_code == 422
    bad_user = revoke_body(user_id="admin", reason="akun_nonaktif", revoked_at="2026-11-12T06:00:00Z")
    assert post(client, bad_user).status_code == 422
    bad_reason = revoke_body(user_id=USER, reason="lainnya", revoked_at="2026-11-12T06:00:00Z")
    assert post(client, bad_reason).status_code == 422
    assert statuses(db_file) == before


def test_revoke_requires_notifier_signature(gw) -> None:
    client, _ = gw
    raw = json.dumps(revoke_body()).encode()
    headers = signed_headers("config", bytes.fromhex("33" * 32), "POST", EVENTS, raw)
    resp = client.post(EVENTS, content=raw, headers={**headers, "Content-Type": "application/json"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_pending_message_is_not_delivered_after_revoke(db) -> None:
    await seeded(db)
    async with db.engine.begin() as conn:
        await enqueue(conn, event_id="ev1", channel_id="telegram-tim", recipient_ref="idt_tg1", subject="", body="x")
    resp = await _revoke(db, EventIn.model_validate(revoke_body()))
    assert resp.status_code == 202
    fake = TelegramFake()
    assert await OutboxWorker(db, {"telegram": telegram(fake.handler)}).run_once() == 1
    assert fake.sent() == []
    row = (await outbox_rows(db))[0]
    assert row.status == "dead" and row.last_error == "identitas_tidak_aktif"


@pytest.mark.anyio
async def test_inbound_chat_of_revoked_user_is_not_acted_on(db) -> None:
    await seeded(db)
    core, tg = CoreFake(), TelegramFake()
    sender = telegram(tg.handler)
    handler = InboundHandler(db, facade(core), sender.send, kind="telegram", channel_id="telegram-tim")

    await handler.handle("u1", CHAT_ID, "/status")
    assert len(core.requests) == 1  # sebelum dicabut: perintah T0 diteruskan atas nama usr_0142

    await _revoke(db, EventIn.model_validate(revoke_body()))
    async with db.engine.connect() as conn:
        status = await conn.scalar(select(channel_identity.c.status).where(channel_identity.c.identity_id == "idt_tg1"))
    assert status == "dicabut"

    await handler.handle("u2", CHAT_ID, "/status")
    assert len(core.requests) == 1  # sesudah dicabut: tidak ada act, hanya balasan tidak terdaftar
    assert render.NOT_REGISTERED in [m["text"] for m in tg.sent()]
