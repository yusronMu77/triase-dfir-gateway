"""Adapter kanal keluar: Telegram Bot API (httpx) dan SMTP (aiosmtplib). Kanal tidak tahu soal perintah.

Galat pengiriman jadi `SendError`: `permanent` (pesan jadi `dead`) atau sementara (dicoba ulang, `retry_after`
dari penyedia dihormati). `reason` aman dicatat: tidak pernah memuat token, URL bot, atau isi pesan.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import Any, Literal, Protocol

import aiosmtplib
import httpx
from astria_common.component import Check
from astria_common.http import make_client

from astria_gateway.render import truncate

TELEGRAM_API = "https://api.telegram.org"
SMTP_TIMEOUT = 10.0


class SendError(Exception):
    def __init__(self, reason: str, *, permanent: bool = False, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent
        self.retry_after = retry_after


class Adapter(Protocol):
    async def send(self, recipient: str, subject: str, text: str) -> None: ...


class Telegram:
    """Juga komponen: menutup klien HTTP saat berhenti dan melaporkan galat terakhir di /health."""

    name = "telegram"
    required = False

    def __init__(
        self,
        token: str,
        *,
        max_chars: int = 3000,
        poll_timeout: int = 30,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._max_chars = max_chars
        self._poll_timeout = poll_timeout
        self.last_error: str | None = None
        # URL dasar memuat token: logger `httpx` dibisukan di main, dan galat httpx tidak pernah diteruskan.
        self._http = http or make_client(f"{TELEGRAM_API}/bot{token}/", timeout=poll_timeout + 10)

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            resp = await self._http.post(method, json=payload)
        except httpx.HTTPError as exc:
            raise self._fail(SendError(f"telegram_transport_{type(exc).__name__}")) from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and isinstance(data, dict) and data.get("ok") is True:
            self.last_error = None
            return data.get("result")
        reason = f"telegram_{resp.status_code}"
        if resp.status_code == 429:
            params = data.get("parameters") if isinstance(data, dict) else None
            retry = params.get("retry_after") if isinstance(params, dict) else None
            raise self._fail(SendError(reason, retry_after=float(retry) if isinstance(retry, int | float) else None))
        # 401 token salah, 403 bot diblokir, 400 chat tidak ada: diulang pun tetap gagal.
        raise self._fail(SendError(reason, permanent=resp.status_code < 500))

    def _fail(self, error: SendError) -> SendError:
        self.last_error = error.reason
        return error

    async def send(self, recipient: str, subject: str, text: str) -> None:
        payload = {"chat_id": recipient, "text": truncate(text, self._max_chars), "disable_web_page_preview": True}
        await self._call("sendMessage", payload)

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": self._poll_timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._http.aclose()

    async def check(self) -> Check:
        return Check("degraded", self.last_error) if self.last_error else Check("ok")


SendMail = Callable[..., Awaitable[Any]]


class Smtp:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        tls: Literal["starttls", "tls", "none"],
        username: str,
        password: str,
        sender: str,
        send: SendMail = aiosmtplib.send,
    ) -> None:
        self._options = {
            "hostname": host,
            "port": port,
            "username": username or None,
            "password": password or None,
            "use_tls": tls == "tls",
            "start_tls": tls == "starttls",
            "timeout": SMTP_TIMEOUT,
        }
        self._sender = sender
        self._send = send

    async def send(self, recipient: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(text)
        try:
            await self._send(message, **self._options)
        except aiosmtplib.SMTPRecipientsRefused:
            raise SendError("smtp_penerima_ditolak", permanent=True) from None
        except aiosmtplib.SMTPResponseException as exc:
            raise SendError(f"smtp_{exc.code}", permanent=500 <= exc.code < 600) from None
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise SendError(f"smtp_{type(exc).__name__}") from None
