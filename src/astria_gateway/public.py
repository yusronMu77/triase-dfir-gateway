"""Listener publik (GW§3.1): satu-satunya kode yang menerima trafik internet; hanya `/hooks/*`.

Belum ada hook aktif (Telegram memakai polling; webhook Telegram dan WhatsApp ditunda), jadi setiap
permintaan dijawab 404 dengan bentuk tetap tanpa rincian. Body di atas 64 KiB dijawab 413 sebelum diparse.
Aplikasi ASGI polos: tanpa dokumentasi API, tanpa health atau metrik, tanpa router lain.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

MAX_BODY = 64 * 1024
ERROR_BODY = b'{"error":"bad_request"}'

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


async def _body_too_large(scope: Scope, receive: Receive) -> bool:
    declared = dict(scope.get("headers", [])).get(b"content-length")
    if declared is not None:
        return not declared.isdigit() or int(declared) > MAX_BODY
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if size > MAX_BODY:
            return True
        if not message.get("more_body"):
            return False


async def public_app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    status = 413 if await _body_too_large(scope, receive) else 404
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(ERROR_BODY)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": ERROR_BODY})
