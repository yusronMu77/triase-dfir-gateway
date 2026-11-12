"""Rakit gateway: satu proses, dua listener (GW§3.1), basis data, seed, pekerja outbox, poller Telegram, dan WhatsApp.

`astria-gateway` (atau `python -m astria_gateway`) menjalankan listener internal dan, bila
`ASTRIA_GW_PUBLIC_BIND` diisi, listener publik dalam satu event loop sehingga hanya ada satu penulis basis data.
Rahasia kanal dibaca lewat `vault.SecretVault` tiap panggilan; rahasia yang ditulis config berlaku tanpa restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import uvicorn
from astria_common.app import create_app
from astria_common.hmac_auth import NonceCache
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_gateway import api
from astria_gateway.channels import Adapter, Smtp, Telegram, WhatsApp
from astria_gateway.facade import FacadeClient
from astria_gateway.inbound import InboundHandler
from astria_gateway.outbox import OutboxWorker
from astria_gateway.poller import TelegramPoller
from astria_gateway.public import public_app
from astria_gateway.seed import SeedLoader, load_seed
from astria_gateway.settings import Settings
from astria_gateway.store import metadata
from astria_gateway.vault import SecretVault


def secret_source(vault: SecretVault | None, key: str, fallback: str) -> Callable[[], str]:
    """Pembaca rahasia per panggilan: dari vault bila ada, selain itu nilai env."""
    if vault is None:
        return lambda: fallback
    return lambda: vault.value(key)


def build_telegram(settings: Settings, vault: SecretVault | None) -> Telegram | None:
    """Telegram aktif bila ada token env, atau kunci induk diisi sehingga token bisa ditulis config."""
    token = settings.telegram_token()
    if not token and not (vault is not None and vault.can_store):
        return None
    return Telegram(
        secret_source(vault, "telegram.bot_token", token),
        max_chars=settings.gw_telegram_max_chars,
        poll_timeout=settings.gw_telegram_poll_timeout,
    )


def build_whatsapp(settings: Settings, token: str) -> WhatsApp | None:
    if not token:
        return None
    return WhatsApp(settings.gw_wa_bridge_url, token)


def build_adapters(
    settings: Settings, telegram: Telegram | None, whatsapp: WhatsApp | None, vault: SecretVault | None = None
) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {"telegram": telegram} if telegram else {}
    if whatsapp:
        adapters["whatsapp"] = whatsapp
    if settings.gw_smtp_host:
        adapters["email"] = Smtp(
            host=settings.gw_smtp_host,
            port=settings.gw_smtp_port,
            tls=settings.gw_smtp_tls,
            username=settings.gw_smtp_user,
            password=secret_source(vault, "smtp.password", settings.smtp_password()),
            sender=settings.gw_smtp_from,
        )
    return adapters


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = standard_runtime(settings, metadata=[metadata])
    db = runtime.db
    vault: SecretVault | None = None
    if db is not None:
        # Kunci induk salah format = proses menolak start (sebelum ada yang dilayani).
        vault = SecretVault(
            db,
            settings.master_key(),
            locked={"telegram.bot_token": settings.telegram_token(), "smtp.password": settings.smtp_password()},
        )
        runtime.add(vault)
    telegram = build_telegram(settings, vault)
    wa_token = settings.wa_bridge_token()
    whatsapp = build_whatsapp(settings, wa_token)
    adapters = build_adapters(settings, telegram, whatsapp, vault)
    poller: TelegramPoller | None = None
    wa_inbound: InboundHandler | None = None
    if db is not None:
        runtime.add(SeedLoader(db, load_seed(settings.gw_seed_file)))
        facade: FacadeClient | None = None
        key = settings.service_key_bytes()
        if key and (telegram or whatsapp):
            facade = FacadeClient(settings.gw_core_url, key)
            runtime.add(facade)
        limits = {"rate_per_identity": settings.gw_rate_per_identity, "rate_global": settings.gw_rate_global}
        if telegram is not None:
            runtime.add(telegram)
            channel_id = settings.gw_telegram_channel_id
            handler = InboundHandler(db, facade, telegram.send, kind="telegram", channel_id=channel_id, **limits)
            poller = TelegramPoller(db, telegram, handler)
            runtime.add(poller)
        if whatsapp is not None:
            runtime.add(whatsapp)
            channel_id = settings.gw_whatsapp_channel_id
            wa_inbound = InboundHandler(db, facade, whatsapp.send, kind="whatsapp", channel_id=channel_id, **limits)
        runtime.add(OutboxWorker(db, adapters))
    app = create_app(settings, components=runtime.components, routers=[api.router], title="Astria gateway")
    # Log permintaan httpx memuat URL Bot API yang berisi token; jangan pernah sampai ke log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app.state.db = runtime.db
    app.state.vault = vault
    app.state.adapters = adapters
    app.state.poller = poller
    app.state.wa_inbound = wa_inbound
    app.state.wa_token = wa_token
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
