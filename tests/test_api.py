"""Penerima event internal dan status: HMAC, replay, dedupe, allowlist, kanal tak dikenal (SQLite lewat TestClient)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astria_common.hmac_auth import signed_headers
from fastapi.testclient import TestClient
from helpers import NOTIFIER_KEY, gateway_settings, ready_client

from astria_gateway.main import build_app

EVENTS = "/internal/v1/events"


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"notifier": NOTIFIER_KEY.hex(), "config": ("33" * 32)}))
    with ready_client(build_app(gateway_settings(tmp_path, gw_caller_keys_file=keys))) as client:
        yield client, tmp_path / "gateway_test.db"


def card_event(event_id: str = "01JC8Z3K7Q0M2V5T9B1N4R6W8X", **payload: Any) -> dict[str, Any]:
    payload = payload or {"card_id": "IC-20261110-003", "version": 1, "band": "KRITIS", "skor": 71}
    return {
        "event_id": event_id,
        "type": "card.verified",
        "occurred_at": "2026-11-10T02:40:11Z",
        "channels": [
            {"channel_id": "email-tim", "payload": payload},
            {"channel_id": "telegram-tim", "payload": payload},
        ],
    }


def post(client: TestClient, body: Any, *, service: str = "notifier", key: bytes = NOTIFIER_KEY, **kw: Any):
    raw = json.dumps(body).encode()
    headers = signed_headers(service, key, "POST", EVENTS, raw, **kw)
    return client.post(EVENTS, content=raw, headers=headers | {"Content-Type": "application/json"})


def rows(db_file: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db_file) as conn:
        return conn.execute(sql).fetchall()


def test_accepts_signed_event_and_queues_one_message_per_active_identity(gw) -> None:
    client, db_file = gw
    resp = post(client, card_event())
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"event_id": "01JC8Z3K7Q0M2V5T9B1N4R6W8X", "messages": 2}
    queued = rows(db_file, "SELECT channel_id, recipient_ref, subject, body FROM outbox_message ORDER BY channel_id")
    assert [(r[0], r[1]) for r in queued] == [("email-tim", "idt_em1"), ("telegram-tim", "idt_tg1")]
    assert queued[0][2] == "[Astria] Kartu IC-20261110-003 terverifikasi"
    assert "Band: KRITIS" in queued[0][3] and "Skor: 71" in queued[0][3]


def test_ringkas_card_fields_are_allowed(gw) -> None:
    client, db_file = gw
    payload = {
        "card_id": "IC-20261110-003",
        "version": 2,
        "band": "KRITIS",
        "skor": 71,
        "status": "terverifikasi",
        "tautan": "https://astria.test/cards/IC-20261110-003",
        "teknik": ["T1110.001", "T1078"],
        "klaim_total": 4,
        "klaim_lolos": 3,
    }
    assert post(client, card_event(**payload)).status_code == 202
    body = rows(db_file, "SELECT body FROM outbox_message WHERE channel_id = 'email-tim'")[0][0]
    assert "Teknik: T1110.001, T1078" in body and "Klaim: 3/4 lolos" in body


def test_duplicate_event_id_is_409_and_not_queued_twice(gw) -> None:
    client, db_file = gw
    assert post(client, card_event()).status_code == 202
    resp = post(client, card_event())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "GW_EVENT_DUPLICATE"
    assert rows(db_file, "SELECT count(*) FROM outbox_message") == [(2,)]


@pytest.mark.parametrize(
    ("service", "key"),
    [("notifier", bytes(32)), ("config", bytes.fromhex("33" * 32)), ("identity", NOTIFIER_KEY)],
    ids=["kunci_salah", "layanan_tidak_diizinkan", "layanan_tidak_dikenal"],
)
def test_rejects_bad_signature_or_caller(gw, service: str, key: bytes) -> None:
    client, db_file = gw
    resp = post(client, card_event(), service=service, key=key)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "GW_AUTH_FAILED"
    assert rows(db_file, "SELECT count(*) FROM event_inbox") == [(0,)]


def test_rejects_unsigned_request(gw) -> None:
    client, _ = gw
    assert client.post(EVENTS, json=card_event()).status_code == 401


def test_rejects_replayed_nonce_and_stale_timestamp(gw) -> None:
    client, _ = gw
    raw = json.dumps(card_event()).encode()
    headers = signed_headers("notifier", NOTIFIER_KEY, "POST", EVENTS, raw) | {"Content-Type": "application/json"}
    assert client.post(EVENTS, content=raw, headers=headers).status_code == 202
    replay = client.post(EVENTS, content=raw, headers=headers)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "GW_REPLAY"
    stale = post(client, card_event("evt-lama"), now=time.time() - 301)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "GW_REPLAY"


def test_rejects_tampered_body(gw) -> None:
    client, _ = gw
    raw = json.dumps(card_event()).encode()
    headers = signed_headers("notifier", NOTIFIER_KEY, "POST", EVENTS, raw)
    tampered = raw.replace(b"KRITIS", b"RENDAH")
    assert client.post(EVENTS, content=tampered, headers=headers).status_code == 401


def test_payload_field_outside_allowlist_is_422_and_audited(gw) -> None:
    client, db_file = gw
    resp = post(client, card_event(card_id="IC-20261110-003", host="WS-ADM-04"))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "GW_EVENT_INVALID"
    assert rows(db_file, "SELECT count(*) FROM outbox_message") == [(0,)]
    audit = rows(db_file, "SELECT action, detail_json FROM gw_audit")
    assert audit[-1][0] == "event.invalid" and '"host"' in audit[-1][1]


@pytest.mark.parametrize(
    "body",
    [
        {**card_event(), "type": "ticket.created"},
        {**card_event(), "rahasia": "x"},
        {**card_event(), "channels": [{"channel_id": "email-tim", "payload": {"card_id": {"nested": 1}}}]},
        {**card_event(), "channels": [{"channel_id": "email-tim"}, {"channel_id": "email-tim"}]},
    ],
    ids=["tipe_tidak_dikenal", "field_amplop_asing", "nilai_bersarang", "kanal_ganda"],
)
def test_invalid_envelope_is_422(gw, body: dict[str, Any]) -> None:
    client, _ = gw
    resp = post(client, body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "GW_EVENT_INVALID"


def test_unknown_channel_is_404(gw) -> None:
    client, db_file = gw
    body = card_event()
    body["channels"].append({"channel_id": "whatsapp-tim", "payload": {}})
    resp = post(client, body)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GW_CHANNEL_UNKNOWN"
    assert rows(db_file, "SELECT count(*) FROM event_inbox") == [(0,)]


def test_digest_and_ping_render_from_allowlisted_fields(gw) -> None:
    client, db_file = gw
    digest = {
        "event_id": "evt-digest-1",
        "type": "digest.published",
        "occurred_at": "2026-11-10T06:00:00Z",
        "channels": [
            {
                "channel_id": "email-tim",
                "payload": {
                    "digest_id": "DG-20261110-0600",
                    "jenis": "harian",
                    "periode_mulai": "2026-11-09T06:00:00Z",
                    "periode_selesai": "2026-11-10T06:00:00Z",
                },
            }
        ],
    }
    ping = {
        "event_id": "evt-ping-1",
        "type": "ping",
        "occurred_at": "2026-11-10T06:00:00Z",
        "channels": [{"channel_id": "email-tim", "payload": {}}],
    }
    assert post(client, digest).status_code == 202
    assert post(client, ping).status_code == 202
    subjects = [r[0] for r in rows(db_file, "SELECT subject FROM outbox_message ORDER BY created_at")]
    assert subjects == ["[Astria] Digest harian DG-20261110-0600 terbit", "[Astria] Pesan uji kanal"]


def test_status_requires_signature_and_reports_outbox(gw) -> None:
    client, _ = gw
    assert client.get("/internal/v1/status").status_code == 401
    post(client, card_event())
    path = "/internal/v1/status"
    resp = client.get(path, headers=signed_headers("config", bytes.fromhex("33" * 32), "GET", path, b""))
    assert resp.status_code == 200
    body = resp.json()
    assert sum(body["outbox"].values()) == 2
    assert body["telegram"] == {"enabled": False}
    assert body["whatsapp"] == {"enabled": False}
    assert body["email"] == {"enabled": False}
    assert body["public_listener"] is False
