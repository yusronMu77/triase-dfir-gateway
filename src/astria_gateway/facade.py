"""Klien Command Facade dan klaim pairing api (`/svc/v1/commands`, `/svc/v1/pairing/claim`, GW§3.3), HMAC `gateway`."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx
import orjson
from astria_common.component import Check
from astria_common.hmac_auth import signed_headers
from astria_common.http import make_client

COMMANDS_PATH = "/svc/v1/commands"
PAIRING_PATH = "/svc/v1/pairing/claim"
SERVICE = "gateway"


class FacadeError(Exception):
    """`unavailable`: Core tidak terjangkau atau galat 5xx. Selain itu Core menolak (`message` aman ditampilkan)."""

    def __init__(self, *, unavailable: bool, message: str = "", status: int = 0) -> None:
        super().__init__(message or ("core_tidak_tersedia" if unavailable else "ditolak"))
        self.unavailable = unavailable
        self.message = message
        self.status = status


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
        data = await self._post(COMMANDS_PATH, body)
        if data.get("ok") is not True or not isinstance(data.get("hasil"), dict):
            raise FacadeError(unavailable=True)
        return data["hasil"]

    async def claim_pairing(self, *, code: str, channel_id: str, external_id: str) -> str:
        """Klaim kode pairing untuk chat `external_id`; mengembalikan `user_id` pemilik kode."""
        body = orjson.dumps(
            {
                "code": code,
                "channel_id": channel_id,
                "external_id_hash": self._external_id_hash(channel_id, external_id),
            }
        )
        user_id = (await self._post(PAIRING_PATH, body)).get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise FacadeError(unavailable=True)
        return user_id

    def _external_id_hash(self, channel_id: str, external_id: str) -> str:
        # HMAC berkunci, bukan sha256 polos: nomor dan ID chat mudah ditebak (GW§2, hash identitas).
        msg = f"pairing:{channel_id}:{external_id}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    async def _post(self, path: str, body: bytes) -> dict[str, Any]:
        headers = signed_headers(SERVICE, self._key, "POST", path, body)
        try:
            resp = await self._http.post(path, content=body, headers=headers | {"Content-Type": "application/json"})
        except httpx.HTTPError:
            raise FacadeError(unavailable=True) from None
        if resp.status_code != 200:
            code = resp.status_code
            raise FacadeError(unavailable=code >= 500, message=_error_message(resp), status=code)
        try:
            data = resp.json()
        except ValueError:
            raise FacadeError(unavailable=True) from None
        if not isinstance(data, dict):
            raise FacadeError(unavailable=True)
        return data

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._http.aclose()

    async def check(self) -> Check:
        return Check("ok")
