"""Klien Command Facade api (`POST /svc/v1/commands`, GW§3.3), ditandatangani HMAC sebagai layanan `gateway`."""

from __future__ import annotations

from typing import Any

import httpx
import orjson
from astria_common.component import Check
from astria_common.hmac_auth import signed_headers
from astria_common.http import make_client

COMMANDS_PATH = "/svc/v1/commands"
SERVICE = "gateway"


class FacadeError(Exception):
    """`unavailable`: Core tidak terjangkau atau galat 5xx. Selain itu Core menolak (`message` aman ditampilkan)."""

    def __init__(self, *, unavailable: bool, message: str = "") -> None:
        super().__init__(message or ("core_tidak_tersedia" if unavailable else "ditolak"))
        self.unavailable = unavailable
        self.message = message


def _error_message(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error", {})
    except ValueError, AttributeError:
        return ""
    return str(error.get("message", "")) if isinstance(error, dict) else ""


class FacadeClient:
    """Juga komponen: dipakai bersama poller Telegram dan penerima WhatsApp, ditutup sekali saat berhenti."""

    name = "command-facade"
    required = False

    def __init__(self, base_url: str, key: bytes, *, http: httpx.AsyncClient | None = None) -> None:
        self._key = key
        self._http = http or make_client(base_url, timeout=10.0)

    async def run(
        self, *, user_id: str, channel_id: str, command: str, args: dict[str, str], idempotency_key: str
    ) -> dict[str, Any]:
        body = orjson.dumps(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "command": command,
                "args": args,
                "idempotency_key": idempotency_key,
            }
        )
        headers = signed_headers(SERVICE, self._key, "POST", COMMANDS_PATH, body)
        try:
            resp = await self._http.post(
                COMMANDS_PATH, content=body, headers=headers | {"Content-Type": "application/json"}
            )
        except httpx.HTTPError:
            raise FacadeError(unavailable=True) from None
        if resp.status_code != 200:
            raise FacadeError(unavailable=resp.status_code >= 500, message=_error_message(resp))
        try:
            data = resp.json()
        except ValueError:
            raise FacadeError(unavailable=True) from None
        if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("hasil"), dict):
            raise FacadeError(unavailable=True)
        return data["hasil"]

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._http.aclose()

    async def check(self) -> Check:
        return Check("ok")
