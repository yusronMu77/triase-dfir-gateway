"""Kanal WhatsApp lewat wa-bridge: adapter `POST /kirim`, penerima `/internal/v1/whatsapp/masuk`, alur perintah."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import WA_NUMBER, WA_TOKEN, CoreFake, facade, gateway_settings, outbox_rows, ready_client, seeded
from pydantic import SecretStr
from sqlalchemy import select

from astria_gateway import render
from astria_gateway.channels import WhatsApp
from astria_gateway.inbound import InboundHandler
from astria_gateway.main import build_app
from astria_gateway.outbox import OutboxWorker, enqueue
from astria_gateway.settings import Settings
from astria_gateway.store import gw_audit, inbound_message

BRIDGE = "http://wa-bridge:3000"
MASUK = "/internal/v1/whatsapp/masuk"


class BridgeFake:
    """wa-bridge tiruan: mencatat `POST /kirim` dan membalas sesuai antrean."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.sent: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/kirim"
        self.auth.append(request.headers.get("authorization", ""))
        if self.responses:
            reply = self.responses.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "3EB0UJI"})

    def adapter(self) -> WhatsApp:
        http = httpx.AsyncClient(base_url=BRIDGE, transport=httpx.MockTransport(self.handler))
        return WhatsApp(BRIDGE, WA_TOKEN, http=http)


async def _queue(db, body: str = "Halo") -> None:
    async with db.engine.begin() as conn:
        await enqueue(conn, event_id="evt-1", channel_id="wa-tim", recipient_ref="idt_wa1", subject="S", body=body)


# ---- adapter keluar ----


@pytest.mark.anyio
async def test_send_plain_text_with_bearer_and_cap(db) -> None:
    await seeded(db)
    bridge = BridgeFake()
    await _queue(db, body="x" * 5000)
    assert await OutboxWorker(db, {"whatsapp": bridge.adapter()}).run_once() == 1
    [row] = await outbox_rows(db)
    assert (row.status, row.last_error) == ("sent", None)
    [sent] = bridge.sent
    assert sent["ke"] == WA_NUMBER and len(sent["teks"]) == 4000 and set(sent) == {"ke", "teks"}
    assert bridge.auth == [f"Bearer {WA_TOKEN}"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "status", "reason"),
    [
        (httpx.Response(400, json={"error": "ke_harus_8_15_digit"}), "dead", "whatsapp_400"),
        (httpx.Response(401, json={"error": "token_tidak_valid"}), "dead", "whatsapp_401"),
        (httpx.Response(503, json={"error": "belum_terhubung"}), "pending", "whatsapp_503"),
        (httpx.Response(502, json={"error": "gagal_kirim"}), "pending", "whatsapp_502"),
        (httpx.Response(500), "pending", "whatsapp_500"),
        (httpx.ConnectError("gagal"), "pending", "whatsapp_transport_ConnectError"),
    ],
)
async def test_bridge_errors(db, reply: httpx.Response | Exception, status: str, reason: str) -> None:
    await seeded(db)
    adapter = BridgeFake(reply).adapter()
    await _queue(db)
    await OutboxWorker(db, {"whatsapp": adapter}).run_once()
    [row] = await outbox_rows(db)
    assert (row.status, row.last_error) == (status, reason)
    assert (await adapter.check()).status == "degraded"
    async with db.engine.connect() as conn:
        audit = (await conn.execute(select(gw_audit.c.detail_json))).scalars().all()
    assert all(WA_TOKEN not in a for a in audit)


# ---- alur masuk (kedua dialek) ----


def _handler(db, bridge: BridgeFake, core: CoreFake | None = None) -> InboundHandler:
    return InboundHandler(db, facade(core), bridge.adapter().send, kind="whatsapp", channel_id="wa-tim")


@pytest.mark.anyio
async def test_command_reply_flows_through_outbox_to_bridge(db) -> None:
    await seeded(db)
    bridge, core = BridgeFake(), CoreFake()
    await _handler(db, bridge, core).handle("3EB0A1", WA_NUMBER, "/kartu IC-20261110-003")
    assert core.requests[0]["channel_id"] == "wa-tim"
    assert core.requests[0]["idempotency_key"] == "wa-tim:3EB0A1"
    [reply] = await outbox_rows(db)
    assert (reply.recipient_ref, reply.event_id) == ("idt_wa1", "in:3EB0A1")

    await OutboxWorker(db, {"whatsapp": bridge.adapter()}).run_once()
    assert [(s["ke"], s["teks"].splitlines()[0]) for s in bridge.sent] == [(WA_NUMBER, "/kartu")]


@pytest.mark.anyio
async def test_unknown_sender_gets_one_direct_reply(db) -> None:
    await seeded(db)
    bridge, core = BridgeFake(), CoreFake()
    handler = _handler(db, bridge, core)
    for n in (1, 2):
        await handler.handle(f"M{n}", "6289999999999", "/status")
    assert core.requests == [] and await outbox_rows(db) == []
    assert bridge.sent == [{"ke": "6289999999999", "teks": render.NOT_REGISTERED}]
    async with db.engine.connect() as conn:
        audit = (await conn.execute(select(gw_audit.c.detail_json))).scalars().all()
    assert len(audit) == 2 and all("6289999999999" not in a for a in audit)


@pytest.mark.anyio
async def test_unknown_sender_reply_failure_is_swallowed(db) -> None:
    await seeded(db)
    await _handler(db, BridgeFake(httpx.Response(503))).handle("M1", "6289999999999", "/status")
    async with db.engine.connect() as conn:
        assert (await conn.execute(select(inbound_message.c.status))).scalar_one() == "ditolak"


# ---- endpoint internal (SQLite lewat TestClient) ----


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[tuple[TestClient, Path, BridgeFake]]:
    token = tmp_path / "wa_bridge.token"
    token.write_text(WA_TOKEN + "\n")
    app = build_app(gateway_settings(tmp_path, gw_wa_bridge_url=BRIDGE, gw_wa_bridge_token_file=token))
    bridge = BridgeFake()
    # Tanpa jaringan: balasan langsung "tidak terdaftar" lewat wa-bridge tiruan.
    app.state.wa_inbound._send_direct = bridge.adapter().send
    with ready_client(app) as client:
        yield client, tmp_path / "gateway_test.db", bridge


def _msg(msg_id: str = "3EB0A1", dari: str = WA_NUMBER, teks: str = "/bantuan") -> dict[str, str]:
    return {"id": msg_id, "dari": dari, "teks": teks, "waktu": "2026-11-10T02:40:11Z"}


def _auth(token: str = WA_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_inbound_requires_bearer_token(gw) -> None:
    client, *_ = gw
    for headers in ({}, _auth("salah"), {"Authorization": WA_TOKEN}):
        resp = client.post(MASUK, json=_msg(), headers=headers)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "GW_AUTH_FAILED"


def test_inbound_invalid_body(gw) -> None:
    client, *_ = gw
    for body in (_msg(dari="+6281200000001"), _msg() | {"grup": True}, {"id": "x"}):
        assert client.post(MASUK, json=body, headers=_auth()).status_code == 422


def test_inbound_dedupe_and_reply_queued(gw) -> None:
    client, db_file, _ = gw
    for _ in range(2):
        resp = client.post(MASUK, json=_msg(), headers=_auth())
        assert resp.status_code == 202 and resp.json() == {"id": "3EB0A1"}
    with sqlite3.connect(db_file) as conn:
        inbound = conn.execute("SELECT channel_id, provider_msg_id, status, command FROM inbound_message").fetchall()
        queued = conn.execute("SELECT channel_id, recipient_ref, body FROM outbox_message").fetchall()
    assert inbound == [("wa-tim", "3EB0A1", "selesai", "bantuan")]
    assert queued == [("wa-tim", "idt_wa1", render.HELP)]


def test_inbound_unknown_sender(gw) -> None:
    client, _, bridge = gw
    assert client.post(MASUK, json=_msg(dari="6289999999999"), headers=_auth()).status_code == 202
    assert bridge.sent == [{"ke": "6289999999999", "teks": render.NOT_REGISTERED}]


def test_inbound_disabled_without_bridge(tmp_path: Path) -> None:
    db = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/gateway_test.db")
    with TestClient(build_app(Settings(env="test", database_url=db))) as client:
        resp = client.post(MASUK, json=_msg(), headers=_auth())
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "GW_CHANNEL_UNKNOWN"
