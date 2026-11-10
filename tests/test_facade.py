"""Klien Command Facade: permintaan bertanda tangan `gateway`, hasil `hasil`, galat ditolak vs tidak tersedia."""

from __future__ import annotations

import json

import httpx
import pytest
from astria_common.hmac_auth import NonceCache, verify
from helpers import GATEWAY_KEY

from astria_gateway.facade import COMMANDS_PATH, FacadeClient, FacadeError

pytestmark = pytest.mark.anyio

ARGS = {"user_id": "usr_0142", "channel_id": "telegram-tim", "command": "status", "args": {}, "idempotency_key": "k1"}


def client(handler) -> FacadeClient:
    http = httpx.AsyncClient(base_url="http://api:8000", transport=httpx.MockTransport(handler))
    return FacadeClient("http://api:8000", GATEWAY_KEY, http=http)


async def test_signed_request_and_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"command": "status", "ok": True, "hasil": {"status": "ok"}})

    assert await client(handler).run(**ARGS) == {"status": "ok"}
    [request] = seen
    assert (request.method, request.url.path) == ("POST", COMMANDS_PATH)
    assert request.headers["X-Astria-Service"] == "gateway"
    verify(GATEWAY_KEY, "POST", COMMANDS_PATH, request.headers, request.content, NonceCache())
    assert json.loads(request.content) == ARGS


@pytest.mark.parametrize(
    ("response", "unavailable", "message"),
    [
        (
            httpx.Response(403, json={"error": {"code": "DILARANG", "message": "pengguna tidak aktif"}}),
            False,
            "pengguna tidak aktif",
        ),
        (httpx.Response(401, text="bukan json"), False, ""),
        (httpx.Response(502, json={"error": {"code": "UPSTREAM", "message": "x"}}), True, "x"),
        (httpx.Response(200, json={"ok": False}), True, ""),
    ],
)
async def test_errors(response: httpx.Response, unavailable: bool, message: str) -> None:
    with pytest.raises(FacadeError) as info:
        await client(lambda _req: response).run(**ARGS)
    assert (info.value.unavailable, info.value.message) == (unavailable, message)


async def test_connection_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tidak terjangkau", request=request)

    with pytest.raises(FacadeError) as info:
        await client(handler).run(**ARGS)
    assert info.value.unavailable is True
