"""Rakit gateway: satu proses, dua listener (GW§3.1), basis data, seed, pekerja outbox, dan poller Telegram.

`astria-gateway` (atau `python -m astria_gateway`) menjalankan listener internal dan, bila
`ASTRIA_GW_PUBLIC_BIND` diisi, listener publik dalam satu event loop sehingga hanya ada satu penulis basis data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn
from astria_common.app import create_app
from astria_common.hmac_auth import NonceCache
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_gateway import api
from astria_gateway.channels import Adapter, Smtp, Telegram
from astria_gateway.facade import FacadeClient
from astria_gateway.outbox import OutboxWorker
from astria_gateway.poller import TelegramPoller
from astria_gateway.public import public_app
from astria_gateway.seed import SeedLoader, load_seed
from astria_gateway.settings import Settings
from astria_gateway.store import metadata


def build_telegram(settings: Settings) -> Telegram | None:
    token = settings.telegram_token()
    if not token:
        return None
    return Telegram(token, max_chars=settings.gw_telegram_max_chars, poll_timeout=settings.gw_telegram_poll_timeout)


def build_adapters(settings: Settings, telegram: Telegram | None) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {"telegram": telegram} if telegram else {}
    if settings.gw_smtp_host:
        adapters["email"] = Smtp(
            host=settings.gw_smtp_host,
            port=settings.gw_smtp_port,
            tls=settings.gw_smtp_tls,
            username=settings.gw_smtp_user,
            password=settings.smtp_password(),
            sender=settings.gw_smtp_from,
        )
    return adapters


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = standard_runtime(settings, metadata=[metadata])
    telegram = build_telegram(settings)
    adapters = build_adapters(settings, telegram)
    poller: TelegramPoller | None = None
    if runtime.db is not None:
        runtime.add(SeedLoader(runtime.db, load_seed(settings.gw_seed_file)))
        if telegram is not None:
            runtime.add(telegram)
            key = settings.service_key_bytes()
            facade = FacadeClient(settings.gw_core_url, key) if key else None
            poller = TelegramPoller(
                runtime.db,
                telegram,
                facade,
                channel_id=settings.gw_telegram_channel_id,
                rate_per_identity=settings.gw_rate_per_identity,
                rate_global=settings.gw_rate_global,
            )
            runtime.add(poller)
        runtime.add(OutboxWorker(runtime.db, adapters))
    app = create_app(settings, components=runtime.components, routers=[api.router], title="Astria gateway")
    # Log permintaan httpx memuat URL Bot API yang berisi token; jangan pernah sampai ke log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app.state.db = runtime.db
    app.state.adapters = adapters
    app.state.poller = poller
    app.state.caller_keys = api.load_keys(settings.gw_caller_keys_file)
    app.state.nonces = NonceCache()
    return app


def parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


class _FollowerServer(uvicorn.Server):
    """Listener publik: sinyal ditangani listener internal, yang lalu menghentikan server ini."""

    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


def public_config(bind: str) -> uvicorn.Config:
    host, port = parse_bind(bind)
    return uvicorn.Config(
        public_app,
        host=host,
        port=port,
        lifespan="off",
        ws="none",
        log_config=None,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        limit_concurrency=50,
        timeout_keep_alive=5,
    )


async def serve(settings: Settings) -> None:
    internal = uvicorn.Server(
        uvicorn.Config(
            build_app(settings), host=settings.host, port=settings.port, log_config=None, proxy_headers=False
        )
    )
    if not settings.gw_public_bind:
        await internal.serve()
        return
    public = _FollowerServer(public_config(settings.gw_public_bind))

    async def run_internal() -> None:
        try:
            await internal.serve()
        finally:
            public.should_exit = True

    await asyncio.gather(run_internal(), public.serve())
