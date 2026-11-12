"""Penerimaan rahasia kanal dari config (`PUT /internal/v1/secrets/{key}`) dan vault AES-GCM (data sintetis)."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from astria_common.hmac_auth import signed_headers
from fastapi.testclient import TestClient
from helpers import NOTIFIER_KEY, TOKEN, TelegramFake, gateway_settings, ready_client
from pydantic import SecretStr
from sqlalchemy import select, update

from astria_gateway.channels import TELEGRAM_API, SendError, Telegram
from astria_gateway.main import build_app
from astria_gateway.store import secret
from astria_gateway.vault import SecretVault, VaultError, VaultLoadError

BOT_TOKEN = TOKEN
CONFIG_KEY = bytes.fromhex("33" * 32)
MASTER_KEY = bytes(range(32))
MASTER_B64 = base64.b64encode(MASTER_KEY).decode()


def put(client: TestClient, key: str, value: str, *, service: str = "config", sign_key: bytes = CONFIG_KEY) -> Any:
    raw = json.dumps({"value": value}).encode()
    path = f"/internal/v1/secrets/{key}"
    headers = signed_headers(service, sign_key, "PUT", path, raw)
    return client.put(path, content=raw, headers={**headers, "Content-Type": "application/json"})


def _keys(tmp_path: Path) -> Path:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"notifier": NOTIFIER_KEY.hex(), "config": CONFIG_KEY.hex()}))
    return keys


def _db_bytes(tmp_path: Path) -> bytes:
    """Seluruh berkas SQLite termasuk WAL, supaya nilai mentah tidak tersisa di salah satunya."""
    return b"".join(p.read_bytes() for p in tmp_path.glob("gateway_test.db*") if p.is_file())


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[TestClient]:
    settings = gateway_settings(tmp_path, gw_caller_keys_file=_keys(tmp_path), gw_master_key=SecretStr(MASTER_B64))
    with ready_client(build_app(settings)) as client:
        yield client


def test_put_returns_204_without_value_and_stores_only_ciphertext(gw: TestClient, tmp_path: Path) -> None:
    resp = put(gw, "telegram.bot_token", BOT_TOKEN)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    assert BOT_TOKEN not in resp.text

    raw = _db_bytes(tmp_path)
    assert BOT_TOKEN.encode() not in raw
    with sqlite3.connect(tmp_path / "gateway_test.db") as conn:
        row = conn.execute(
            "SELECT ciphertext, nonce, key_version FROM secret WHERE key = 'telegram.bot_token'"
        ).fetchone()
        audit = conn.execute("SELECT detail_json FROM gw_audit WHERE action = 'secret.updated'").fetchall()
    assert row is not None and row[2] == 1 and len(row[1]) == 12
    assert BOT_TOKEN.encode() not in row[0]
    assert len(audit) == 1 and "telegram.bot_token" in audit[0][0] and BOT_TOKEN not in audit[0][0]


def test_value_is_used_by_telegram_adapter_after_put(gw: TestClient) -> None:
    # Master key diisi tanpa token env: Telegram tetap dibangun agar token bisa ditulis config.
    telegram = gw.app.state.adapters["telegram"]
    assert telegram._token() == ""
    assert put(gw, "telegram.bot_token", BOT_TOKEN).status_code == 204
    assert telegram._token() == BOT_TOKEN


def test_same_value_is_idempotent(gw: TestClient, tmp_path: Path) -> None:
    assert put(gw, "telegram.bot_token", BOT_TOKEN).status_code == 204
    assert put(gw, "telegram.bot_token", BOT_TOKEN).status_code == 204
    with sqlite3.connect(tmp_path / "gateway_test.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM gw_audit WHERE action = 'secret.updated'").fetchone()[0] == 1


def test_value_survives_restart(tmp_path: Path) -> None:
    settings = gateway_settings(tmp_path, gw_caller_keys_file=_keys(tmp_path), gw_master_key=SecretStr(MASTER_B64))
    with ready_client(build_app(settings)) as client:
        assert put(client, "smtp.password", "kata-sandi-uji-bukan-nyata").status_code == 204
    with ready_client(build_app(settings)) as client:
        assert client.app.state.vault.value("smtp.password") == "kata-sandi-uji-bukan-nyata"


@pytest.mark.parametrize("service,sign_key", [("notifier", NOTIFIER_KEY), ("config", NOTIFIER_KEY)])
def test_only_config_may_write(gw: TestClient, service: str, sign_key: bytes) -> None:
    resp = put(gw, "telegram.bot_token", BOT_TOKEN, service=service, sign_key=sign_key)
    assert resp.status_code == 401
    assert "GW_AUTH_FAILED" in resp.text
    assert gw.app.state.vault.value("telegram.bot_token") == ""


def test_unsigned_request_is_refused(gw: TestClient) -> None:
    resp = gw.put(
        "/internal/v1/secrets/telegram.bot_token",
        content=json.dumps({"value": BOT_TOKEN}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_unknown_key_is_404(gw: TestClient) -> None:
    resp = put(gw, "wa.verify_token", "nilai-uji")
    assert resp.status_code == 404
    assert "GW_KUNCI_TIDAK_DIKENAL" in resp.text


def test_invalid_token_format_is_422_without_echo(gw: TestClient) -> None:
    value = "bukan-token-rahasia-uji"
    resp = put(gw, "telegram.bot_token", value)
    assert resp.status_code == 422
    assert value not in resp.text


def test_body_with_extra_fields_is_422(gw: TestClient) -> None:
    raw = json.dumps({"value": BOT_TOKEN, "lain": 1}).encode()
    path = "/internal/v1/secrets/telegram.bot_token"
    headers = signed_headers("config", CONFIG_KEY, "PUT", path, raw)
    resp = gw.put(path, content=raw, headers={**headers, "Content-Type": "application/json"})
    assert resp.status_code == 422


def test_without_master_key_writes_are_refused(tmp_path: Path) -> None:
    settings = gateway_settings(tmp_path, gw_caller_keys_file=_keys(tmp_path))
    with ready_client(build_app(settings)) as client:
        assert client.app.state.adapters == {}
        resp = put(client, "telegram.bot_token", BOT_TOKEN)
    assert resp.status_code == 503
    assert "GW_MASTER_KEY_TIDAK_ADA" in resp.text


def test_env_locked_key_refuses_api_write(tmp_path: Path) -> None:
    settings = gateway_settings(
        tmp_path,
        gw_caller_keys_file=_keys(tmp_path),
        gw_master_key=SecretStr(MASTER_B64),
        gw_telegram_bot_token=SecretStr(BOT_TOKEN),
    )
    with ready_client(build_app(settings)) as client:
        resp = put(
            client, "telegram.bot_token", "123456:LAIN-token-not-real-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )  # gitleaks:allow
        assert resp.status_code == 409
        assert "GW_DIKUNCI_ENV" in resp.text
        assert client.app.state.vault.value("telegram.bot_token") == BOT_TOKEN


def test_master_key_formats(tmp_path: Path) -> None:
    b64 = gateway_settings(tmp_path, gw_master_key=SecretStr(MASTER_B64))
    hexed = gateway_settings(tmp_path, gw_master_key=SecretStr(MASTER_KEY.hex()))
    assert b64.master_key() == MASTER_KEY and hexed.master_key() == MASTER_KEY
    assert gateway_settings(tmp_path).master_key() is None
    for bad in ("pendek", base64.b64encode(b"x" * 16).decode(), "z" * 64):
        with pytest.raises(ValueError) as err:
            gateway_settings(tmp_path, gw_master_key=SecretStr(bad)).master_key()
        assert bad not in str(err.value)


def test_wrong_master_key_keeps_gateway_unready(tmp_path: Path) -> None:
    keys = _keys(tmp_path)
    with ready_client(
        build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys, gw_master_key=SecretStr(MASTER_B64)))
    ) as c:
        assert put(c, "smtp.password", "kata-sandi-uji").status_code == 204
    other = base64.b64encode(bytes(32)).decode()
    with TestClient(
        build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys, gw_master_key=SecretStr(other)))
    ) as c:
        deadline = time.monotonic() + 10
        while "kunci_induk_salah" not in c.get("/ready").text and time.monotonic() < deadline:
            time.sleep(0.05)
        assert c.get("/ready").status_code == 503
        assert "kunci_induk_salah" in c.get("/ready").text


# Lapisan vault langsung di atas basis data (SQLite dan PostgreSQL bila tersedia).


@pytest.mark.anyio
async def test_vault_round_trip_and_ciphertext_binding(db) -> None:
    vault = SecretVault(db, MASTER_KEY, {})
    await vault.load()
    await vault.put("telegram.bot_token", BOT_TOKEN)
    await vault.put("smtp.password", "kata-sandi-uji")

    fresh = SecretVault(db, MASTER_KEY, {})
    await fresh.load()
    assert fresh.value("telegram.bot_token") == BOT_TOKEN
    assert fresh.value("smtp.password") == "kata-sandi-uji"

    # Ciphertext satu kunci dipindah ke kunci lain: AAD (nama kunci) menolak dekripsi.
    async with db.engine.begin() as conn:
        src = (await conn.execute(select(secret).where(secret.c.key == "smtp.password"))).mappings().one()
        await conn.execute(
            update(secret)
            .where(secret.c.key == "telegram.bot_token")
            .values(ciphertext=src["ciphertext"], nonce=src["nonce"])
        )
    broken = SecretVault(db, MASTER_KEY, {})
    with pytest.raises(VaultLoadError, match="kunci_induk_salah"):
        await broken.load()


@pytest.mark.anyio
async def test_vault_env_lock_and_missing_master(db) -> None:
    locked = SecretVault(db, MASTER_KEY, {"telegram.bot_token": BOT_TOKEN})
    await locked.load()
    assert locked.value("telegram.bot_token") == BOT_TOKEN and locked.locked("telegram.bot_token")
    with pytest.raises(VaultError) as err:
        await locked.put("telegram.bot_token", BOT_TOKEN)
    assert err.value.code == "GW_DIKUNCI_ENV" and err.value.status == 409

    no_master = SecretVault(db, None, {})
    assert not no_master.can_store
    with pytest.raises(VaultError) as err:
        await no_master.put("smtp.password", "kata-sandi-uji")
    assert err.value.code == "GW_MASTER_KEY_TIDAK_ADA"

    writer = SecretVault(db, MASTER_KEY, {})
    await writer.load()
    await writer.put("smtp.password", "kata-sandi-uji")
    orphan = SecretVault(db, None, {})
    with pytest.raises(VaultLoadError, match="kunci_induk_tidak_ada"):
        await orphan.load()


@pytest.mark.anyio
async def test_telegram_reads_token_per_call() -> None:
    current = {"token": ""}
    fake = TelegramFake()
    fake.queue("getUpdates", httpx.Response(200, json={"ok": True, "result": []}))
    http = httpx.AsyncClient(base_url=TELEGRAM_API, transport=httpx.MockTransport(fake.handler))
    tg = Telegram(lambda: current["token"], http=http)

    with pytest.raises(SendError) as err:
        await tg.get_updates(None)
    assert err.value.reason == "telegram_token_kosong" and fake.calls == []

    current["token"] = BOT_TOKEN
    assert await tg.get_updates(None) == []
    assert fake.calls and fake.calls[0][0] == "getUpdates"
