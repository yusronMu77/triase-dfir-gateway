"""Log pengiriman untuk halaman web: GET /internal/v1/outbox dan POST /internal/v1/outbox/{id}/retry."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import TokenVerifier, gateway_settings, ready_client

from astria_gateway.main import build_app

API = {"Authorization": "Bearer tok-api"}
ROWS = [
    # message_id, channel_id, subject, status, attempts, last_error, created_at
    ("m-surel", "email-tim", "[Astria] Kartu IC-1 terverifikasi", "sent", 1, None, "2026-11-15 01:00:00.000000"),
    ("m-wa", "wa-tim", "", "dead", 5, "whatsapp_502", "2026-11-15 01:01:00.000000"),
    ("m-antre", "wa-tim", "", "pending", 0, None, "2026-11-15 01:02:00.000000"),
]


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"notifier": "55" * 32}))
    app = build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys))
    app.state.token_verifier = TokenVerifier()
    with ready_client(app) as client:
        db_file = tmp_path / "gateway_test.db"
        with sqlite3.connect(db_file) as conn:
            for mid, channel, subject, status, attempts, err, created in ROWS:
                conn.execute(
                    "INSERT INTO outbox_message (message_id, event_id, channel_id, recipient_ref, subject, body,"
                    " status, attempts, next_attempt_at, last_error, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'idt_rahasia', ?, 'isi pesan rahasia', ?, ?, ?, ?, ?, ?)",
                    (mid, f"ev-{mid}", channel, subject, status, attempts, created, err, created, created),
                )
        # Pekerja outbox tidak boleh mengirim baris uji: tiap kanal tidak punya adapter di setelan uji.
        yield client, db_file


def test_list_newest_first_without_body_or_recipient(gw) -> None:
    client, _ = gw
    resp = client.get("/internal/v1/outbox", headers=API)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["message_id"] for i in items] == ["m-antre", "m-wa", "m-surel"]
    assert set(items[1]) == {
        "message_id", "channel_id", "subject", "status", "attempts", "last_error",
        "created_at", "updated_at", "next_attempt_at",
    }  # fmt: skip
    assert items[1]["last_error"] == "whatsapp_502" and items[1]["created_at"] == "2026-11-15T01:01:00+00:00"
    assert "rahasia" not in resp.text


def test_status_filter_and_invalid_status(gw) -> None:
    client, _ = gw
    resp = client.get("/internal/v1/outbox", params={"status": "dead"}, headers=API)
    assert [i["message_id"] for i in resp.json()["items"]] == ["m-wa"]
    assert client.get("/internal/v1/outbox", params={"status": "hilang"}, headers=API).status_code == 422


def test_retry_dead_message_only(gw) -> None:
    client, db_file = gw
    resp = client.post("/internal/v1/outbox/m-wa/retry", headers=API)
    assert resp.status_code == 200 and resp.json() == {"message_id": "m-wa", "status": "pending"}
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT status, attempts FROM outbox_message WHERE message_id='m-wa'").fetchone() == (
            "pending",
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM gw_audit WHERE action='outbox.retry'").fetchone() == (1,)
    assert client.post("/internal/v1/outbox/m-surel/retry", headers=API).status_code == 409
    assert client.post("/internal/v1/outbox/tidak-ada/retry", headers=API).status_code == 404


def test_outbox_requires_api_or_config(gw) -> None:
    client, _ = gw
    assert client.get("/internal/v1/outbox").status_code == 401
    assert client.get("/internal/v1/outbox", headers={"Authorization": "Bearer tok-notifier"}).status_code == 403
    assert client.post("/internal/v1/outbox/m-wa/retry", headers={"Authorization": "Bearer palsu"}).status_code == 401
