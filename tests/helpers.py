"""Data dan pembantu uji gateway (semua data sintetis)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from astria_common.db import Database
from astria_common.hmac_auth import NonceCache, verify
from sqlalchemy import select

from astria_gateway.seed import Seed, apply_seed
from astria_gateway.store import outbox_message

NOTIFIER_KEY = bytes.fromhex("11" * 32)
GATEWAY_KEY = bytes.fromhex("22" * 32)
TOKEN = "123456:TEST-token-not-real-aaaaaaaaaaaaaaaaaaaaaaa"  # noqa: S105  # gitleaks:allow
CHAT_ID = "900001"
WA_TOKEN = "uji-token-wa-bukan-rahasia"  # noqa: S105
WA_NUMBER = "6281200000001"  # nomor sintetis

SEED: dict[str, Any] = {
    "channels": [
        {"channel_id": "telegram-tim", "type": "telegram"},
        {"channel_id": "email-tim", "type": "email"},
        {"channel_id": "wa-tim", "type": "whatsapp"},
    ],
    "identities": [
        {"identity_id": "idt_tg1", "channel_id": "telegram-tim", "external_id": CHAT_ID, "user_id": "usr_0142"},
        {"identity_id": "idt_em1", "channel_id": "email-tim", "external_id": "soc@astria.test", "user_id": "usr_0142"},
        {"identity_id": "idt_wa1", "channel_id": "wa-tim", "external_id": WA_NUMBER, "user_id": "usr_0142"},
        {
            "identity_id": "idt_em2",
            "channel_id": "email-tim",
            "external_id": "lama@astria.test",
            "user_id": "usr_0007",
            "status": "dicabut",
        },
    ],
}


async def seeded(db: Database) -> Database:
    await apply_seed(db, Seed.model_validate(SEED))
    return db


async def outbox_rows(db: Database) -> list[Any]:
    async with db.engine.connect() as conn:
        return list((await conn.execute(select(outbox_message).order_by(outbox_message.c.created_at))).all())


def telegram(handler: Any, **kw: Any) -> Any:
    """Adapter Telegram dengan Bot API tiruan (URL dasar sama dengan produksi, termasuk token uji)."""
    from astria_gateway.channels import TELEGRAM_API, Telegram

    http = httpx.AsyncClient(base_url=f"{TELEGRAM_API}/bot{TOKEN}/", transport=httpx.MockTransport(handler))
    return Telegram(TOKEN, http=http, **kw)


class TelegramFake:
    """Bot API tiruan untuk httpx.MockTransport: mencatat panggilan, membalas sesuai `responses` per metode."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, list[httpx.Response]] = {}

    def queue(self, method: str, *responses: httpx.Response) -> None:
        self.responses.setdefault(method, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content or b"{}")))
        pending = self.responses.get(method)
        if pending:
            return pending.pop(0)
        return httpx.Response(200, json={"ok": True, "result": [] if method == "getUpdates" else {"message_id": 1}})

    def sent(self) -> list[dict[str, Any]]:
        return [payload for method, payload in self.calls if method == "sendMessage"]


def update(update_id: int, text: str, chat_id: str = CHAT_ID, chat_type: str = "private") -> dict[str, Any]:
    chat = {"id": int(chat_id), "type": chat_type}
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": chat, "text": text}}


CARD = {"card_id": "IC-20261110-003", "band": "KRITIS", "skor": 71}


class CoreFake:
    """api tiruan: memeriksa tanda tangan HMAC `gateway`, mencatat permintaan, membalas `response`."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = response or httpx.Response(200, json={"command": "card", "ok": True, "hasil": CARD})
        self.nonces = NonceCache()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Astria-Service"] == "gateway"
        verify(GATEWAY_KEY, request.method, request.url.path, request.headers, request.content, self.nonces)
        self.requests.append(json.loads(request.content))
        return self.response


def facade(core: CoreFake | None) -> Any:
    """Command Facade dengan api tiruan; None bila Core tidak dikonfigurasi."""
    from astria_gateway.facade import FacadeClient

    if core is None:
        return None
    http = httpx.AsyncClient(base_url="http://api:8000", transport=httpx.MockTransport(core.handler))
    return FacadeClient("http://api:8000", GATEWAY_KEY, http=http)


def gateway_settings(tmp_path: Path, **kw: Any) -> Any:
    """Settings uji: SQLite berkas `gateway_test.db` dan seed `SEED` di `tmp_path`."""
    from pydantic import SecretStr

    from astria_gateway.settings import Settings

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED))
    db_url = SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'gateway_test.db'}")
    return Settings(env="test", database_url=db_url, gw_seed_file=seed, **kw)


@contextmanager
def ready_client(app: Any) -> Iterator[Any]:
    """TestClient yang menunggu /ready 200 (seed diterapkan) paling lama 10 detik."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        deadline = time.monotonic() + 10
        while client.get("/ready").status_code != 200:
            assert time.monotonic() < deadline, client.get("/ready").json()
            time.sleep(0.05)
        yield client
