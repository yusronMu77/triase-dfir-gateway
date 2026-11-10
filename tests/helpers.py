"""Data dan pembantu uji gateway (semua data sintetis)."""

from __future__ import annotations

from typing import Any

import httpx
from astria_common.db import Database
from sqlalchemy import select

from astria_gateway.seed import Seed, apply_seed
from astria_gateway.store import outbox_message

NOTIFIER_KEY = bytes.fromhex("11" * 32)
GATEWAY_KEY = bytes.fromhex("22" * 32)
TOKEN = "123456:TEST-token-not-real-aaaaaaaaaaaaaaaaaaaaaaa"  # noqa: S105  # gitleaks:allow
CHAT_ID = "900001"

SEED: dict[str, Any] = {
    "channels": [
        {"channel_id": "telegram-tim", "type": "telegram"},
        {"channel_id": "email-tim", "type": "email"},
    ],
    "identities": [
        {"identity_id": "idt_tg1", "channel_id": "telegram-tim", "external_id": CHAT_ID, "user_id": "usr_0142"},
        {"identity_id": "idt_em1", "channel_id": "email-tim", "external_id": "soc@astria.test", "user_id": "usr_0142"},
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
        import json

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
