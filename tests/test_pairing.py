"""Pairing chat `/daftar <kode>` lewat api, serta daftar dan cabut ikatan kanal untuk api dan config."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from astria_common.auth import ServicePrincipal
from astria_common.errors import AstriaError
from astria_common.hmac_auth import signed_headers
from fastapi.testclient import TestClient
from helpers import WA_NUMBER, CoreFake, facade, gateway_settings, outbox_rows, ready_client, seeded
from sqlalchemy import select

from astria_gateway import render
from astria_gateway.channels import WhatsApp
from astria_gateway.facade import PAIRING_PATH
from astria_gateway.inbound import InboundHandler
from astria_gateway.main import build_app
from astria_gateway.store import channel_identity, gw_audit, inbound_message

NEW_NUMBER = "6289900001111"
API_KEY = bytes.fromhex("44" * 32)
CLAIMED = {"user_id": "usr_0500", "claimed_at": "2026-11-13T09:00:00Z"}


def _api(claim: httpx.Response | Exception | None = None) -> CoreFake:
    """api tiruan: klaim pairing di PAIRING_PATH, perintah lain dijawab `status`."""
    claim = claim or httpx.Response(200, json=CLAIMED)
    status = httpx.Response(200, json={"command": "status", "ok": True, "hasil": {"komponen": []}})
    return CoreFake(status, by_path={PAIRING_PATH: claim})


class Bridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, external_id: str, subject: str, body: str) -> None:
        self.sent.append((external_id, body))


def _handler(db: Any, api: CoreFake, bridge: Bridge) -> InboundHandler:
    return InboundHandler(db, facade(api), bridge.send, kind="whatsapp", channel_id="wa-tim")


async def _identities(db: Any) -> dict[str, tuple[str, str, str]]:
    i = channel_identity
    async with db.engine.connect() as conn:
        rows = await conn.execute(select(i.c.identity_id, i.c.external_id, i.c.user_id, i.c.status))
    return {r.identity_id: (r.external_id, r.user_id, r.status) for r in rows}


@pytest.mark.anyio
async def test_unknown_sender_pairs_then_can_command(db) -> None:
    await seeded(db)
    api, bridge = _api(), Bridge()
    handler = _handler(db, api, bridge)

    await handler.handle("P1", NEW_NUMBER, "/daftar abcd2345")
    body = api.requests[0]
    assert api.paths[0] == PAIRING_PATH
    assert body["code"] == "ABCD2345" and body["channel_id"] == "wa-tim"
    assert len(body["external_id_hash"]) == 64 and NEW_NUMBER not in json.dumps(body)

    [(identity_id, (number, user_id, status))] = [
        (k, v) for k, v in (await _identities(db)).items() if v[0] == NEW_NUMBER
    ]
    assert (number, user_id, status) == (NEW_NUMBER, "usr_0500", "aktif")
    [reply] = await outbox_rows(db)
    assert (reply.recipient_ref, reply.body) == (identity_id, render.PAIRED)
    assert bridge.sent == []

    await handler.handle("P2", NEW_NUMBER, "/status")
    assert api.paths[1] == "/svc/v1/commands" and api.requests[1]["user_id"] == "usr_0500"


@pytest.mark.parametrize(
    ("claim", "reply"),
    [
        (httpx.Response(410, json={"error": {"code": "KEDALUWARSA", "message": "x"}}), render.PAIRING_FAILED),
        (httpx.Response(409, json={"error": {"code": "SUDAH_DIPAKAI", "message": "x"}}), render.PAIRING_FAILED),
        (httpx.Response(429, json={"error": {"code": "TERLALU_SERING", "message": "x"}}), render.RATE_LIMITED),
        (httpx.Response(503), render.CORE_DOWN),
        (httpx.ConnectError("mati"), render.CORE_DOWN),
    ],
)
@pytest.mark.anyio
async def test_failed_claim_replies_directly_and_binds_nothing(db, claim: Any, reply: str) -> None:
    await seeded(db)
    bridge = Bridge()
    before = await _identities(db)
    await _handler(db, _api(claim), bridge).handle("P1", NEW_NUMBER, "/daftar ABCD2345")
    assert await _identities(db) == before
    assert bridge.sent == [(NEW_NUMBER, reply)]
    assert await outbox_rows(db) == []
    async with db.engine.connect() as conn:
        assert (await conn.execute(select(inbound_message.c.status))).scalar_one() == "ditolak"
        details = (await conn.execute(select(gw_audit.c.detail_json))).scalars().all()
    assert details and all(NEW_NUMBER not in d for d in details)


@pytest.mark.anyio
async def test_revoked_chat_is_reactivated_by_new_pairing(db) -> None:
    await seeded(db)
    async with db.engine.begin() as conn:
        await conn.execute(
            channel_identity.update().where(channel_identity.c.identity_id == "idt_wa1").values(status="dicabut")
        )
    await _handler(db, _api(), Bridge()).handle("P1", WA_NUMBER, "/daftar ABCD2345")
    assert (await _identities(db))["idt_wa1"] == (WA_NUMBER, "usr_0500", "aktif")


@pytest.mark.anyio
async def test_berhenti_unpairs_own_chat_and_daftar_reconnects(db) -> None:
    await seeded(db)
    api, bridge = _api(), Bridge()
    handler = _handler(db, api, bridge)
    await handler.handle("U1", WA_NUMBER, "/berhenti")
    assert (await _identities(db))["idt_wa1"][2] == "dicabut"
    assert bridge.sent == [(WA_NUMBER, render.UNPAIRED)]
    assert await outbox_rows(db) == []

    await handler.handle("U2", WA_NUMBER, "/status")  # sudah tidak terhubung: tidak ke Command Facade
    assert api.requests == [] and bridge.sent[-1] == (WA_NUMBER, render.NOT_REGISTERED)

    await handler.handle("U3", WA_NUMBER, "/daftar ABCD2345")
    assert (await _identities(db))["idt_wa1"] == (WA_NUMBER, "usr_0500", "aktif")


@pytest.mark.anyio
async def test_bound_chat_daftar_does_not_claim(db) -> None:
    await seeded(db)
    api = _api()
    await _handler(db, api, Bridge()).handle("P1", WA_NUMBER, "/daftar ABCD2345")
    assert api.requests == []
    [reply] = await outbox_rows(db)
    assert (reply.recipient_ref, reply.body) == ("idt_wa1", render.ALREADY_PAIRED)


# ---- GET dan DELETE /internal/v1/identities ----


class TokenVerifier:
    """Pengganti verifikasi token identity: `tok-<layanan>` sah untuk audiens `svc:gateway`."""

    async def verify(self, token: str, audience: str) -> ServicePrincipal:
        if not token.startswith("tok-") or audience != "svc:gateway":
            raise AstriaError("TOKEN_TIDAK_VALID", "token layanan tidak valid", 401)
        return ServicePrincipal(subject=f"svc:{token[4:]}", audience=audience, act=None, claims={})


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"api": API_KEY.hex(), "notifier": ("55" * 32)}))
    app = build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys))
    app.state.token_verifier = TokenVerifier()
    with ready_client(app) as client:
        yield client, tmp_path / "gateway_test.db"


def _call(client: TestClient, method: str, path: str, service: str = "api", key: bytes = API_KEY) -> Any:
    return client.request(method, path, headers=signed_headers(service, key, method, path, b""))


def test_list_identities_without_chat_handles(gw) -> None:
    client, _ = gw
    resp = _call(client, "GET", "/internal/v1/identities")
    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()["items"]}
    assert set(items) == {"idt_tg1", "idt_em1", "idt_wa1", "idt_em2"}
    assert items["idt_wa1"]["user_id"] == "usr_0142" and items["idt_em2"]["status"] == "dicabut"
    assert set(items["idt_wa1"]) == {"id", "user_id", "channel_id", "status", "updated_at"}
    assert WA_NUMBER not in resp.text


def test_revoke_identity_is_idempotent(gw) -> None:
    client, db_file = gw
    assert _call(client, "DELETE", "/internal/v1/identities/idt_wa1").status_code == 204
    assert _call(client, "DELETE", "/internal/v1/identities/idt_wa1").status_code == 204
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT status FROM channel_identity WHERE identity_id='idt_wa1'").fetchone() == (
            "dicabut",
        )
        actions = conn.execute("SELECT action FROM gw_audit WHERE action='identity.binding_revoked'").fetchall()
    assert len(actions) == 1
    assert _call(client, "DELETE", "/internal/v1/identities/idt_tidak_ada").status_code == 404


def test_identities_require_api_or_config(gw) -> None:
    client, _ = gw
    assert _call(client, "GET", "/internal/v1/identities", "notifier", bytes.fromhex("55" * 32)).status_code in (
        401,
        403,
    )
    assert client.get("/internal/v1/identities").status_code in (401, 403)


def test_api_service_token_is_accepted(gw) -> None:
    """api memanggil gateway dengan token layanan EdDSA dari identity, bukan HMAC."""
    client, _ = gw
    bearer = {"Authorization": "Bearer tok-api"}
    assert client.get("/internal/v1/identities", headers=bearer).status_code == 200
    assert client.delete("/internal/v1/identities/idt_tg1", headers=bearer).status_code == 204
    assert client.get("/internal/v1/status", headers=bearer).status_code == 200
    assert client.get("/internal/v1/identities", headers={"Authorization": "Bearer tok-notifier"}).status_code == 403
    assert client.get("/internal/v1/identities", headers={"Authorization": "Bearer palsu"}).status_code == 401


# ---- tautan bot WhatsApp untuk halaman kanal ----

API_BEARER = {"Authorization": "Bearer tok-api"}
QR = "data:image/png;base64,iVBORw0KGgo="


def _bridge(client: TestClient, handler: Any) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return handler(request)

    http = httpx.AsyncClient(base_url="http://wa-bridge:3000", transport=httpx.MockTransport(record))
    client.app.state.adapters["whatsapp"] = WhatsApp("http://wa-bridge:3000", "tok-bridge", http=http)
    return seen


def test_whatsapp_link_returns_status_and_qr(gw) -> None:
    client, _ = gw
    body = {"terhubung": False, "perlu_scan": True, "nomor_tersamar": None, "qr": QR}
    seen = _bridge(client, lambda _r: httpx.Response(200, json=body))
    resp = client.get("/internal/v1/whatsapp/link", headers=API_BEARER)
    assert resp.status_code == 200 and resp.json() == body
    assert seen == [("GET", "/tautan", "Bearer tok-bridge")]


def test_whatsapp_link_drops_unexpected_values(gw) -> None:
    client, _ = gw
    body = {"terhubung": True, "perlu_scan": "ya", "nomor_tersamar": "6281234567890", "qr": "javascript:alert(1)"}
    _bridge(client, lambda _r: httpx.Response(200, json=body))
    assert client.get("/internal/v1/whatsapp/link", headers=API_BEARER).json() == {
        "terhubung": True,
        "perlu_scan": False,
        "nomor_tersamar": None,
        "qr": None,
    }


def test_whatsapp_unlink(gw) -> None:
    client, db_file = gw
    seen = _bridge(client, lambda _r: httpx.Response(202, json={"status": "keluar"}))
    resp = client.post("/internal/v1/whatsapp/link/logout", headers=API_BEARER)
    assert resp.status_code == 202 and seen == [("POST", "/keluar", "Bearer tok-bridge")]
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gw_audit WHERE action='whatsapp.unlinked'").fetchone() == (1,)

    _bridge(client, lambda _r: httpx.Response(409, json={"error": "belum_terhubung"}))
    assert client.post("/internal/v1/whatsapp/link/logout", headers=API_BEARER).status_code == 409


def test_whatsapp_link_errors_and_auth(gw) -> None:
    client, _ = gw
    assert client.get("/internal/v1/whatsapp/link", headers=API_BEARER).status_code == 404  # tanpa wa-bridge

    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mati")

    _bridge(client, down)
    assert client.get("/internal/v1/whatsapp/link", headers=API_BEARER).status_code == 503
    assert client.post("/internal/v1/whatsapp/link/logout", headers=API_BEARER).status_code == 503
    notifier = {"Authorization": "Bearer tok-notifier"}
    assert client.get("/internal/v1/whatsapp/link", headers=notifier).status_code == 403
