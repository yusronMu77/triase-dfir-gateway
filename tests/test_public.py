"""Listener publik: tanpa endpoint aktif, hanya 404 dengan bentuk tetap; body > 64 KiB ditolak 413."""

from __future__ import annotations

import httpx
import pytest

from astria_gateway.main import parse_bind, public_config
from astria_gateway.public import MAX_BODY, public_app

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://gw") as c:
        yield c


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/health"),
        ("GET", "/metrics"),
        ("GET", "/internal/v1/status"),
        ("POST", "/internal/v1/events"),
        ("POST", "/hooks/telegram"),
        ("GET", "/hooks/whatsapp"),
    ],
)
async def test_everything_is_404_without_details(client: httpx.AsyncClient, method: str, path: str) -> None:
    resp = await client.request(method, path, content=b"{}" if method == "POST" else None)
    assert resp.status_code == 404
    assert resp.content == b'{"error":"bad_request"}'
    assert "server" not in resp.headers


async def test_body_over_limit_is_413(client: httpx.AsyncClient) -> None:
    resp = await client.post("/hooks/telegram", content=b"x" * (MAX_BODY + 1))
    assert resp.status_code == 413
    assert resp.content == b'{"error":"bad_request"}'
    assert (await client.post("/hooks/telegram", content=b"x" * MAX_BODY)).status_code == 404


async def test_chunked_body_over_limit_is_413(client: httpx.AsyncClient) -> None:
    async def chunks():
        for _ in range(5):
            yield b"x" * (MAX_BODY // 4)

    assert (await client.post("/hooks/telegram", content=chunks())).status_code == 413


def test_public_server_is_hardened() -> None:
    config = public_config("127.0.0.1:8443")
    assert (config.host, config.port) == ("127.0.0.1", 8443)
    assert config.lifespan == "off" and config.server_header is False and config.proxy_headers is False
    assert config.limit_concurrency == 50 and config.timeout_keep_alive == 5
    assert parse_bind("0.0.0.0:8080") == ("0.0.0.0", 8080)
