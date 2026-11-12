"""Konfigurasi gateway dari env `ASTRIA_*` (GW§5.9).

Rahasia kanal (token bot, kata sandi SMTP) dari env atau berkas `_FILE` (mengunci nilai basis data), atau ditulis
config lewat `PUT /internal/v1/secrets/{key}` dan disimpan terenkripsi AES-GCM (`vault.py`). Tidak pernah dicetak.
"""

import base64
import binascii
from pathlib import Path
from typing import Literal

from astria_common.settings import AstriaSettings
from pydantic import SecretStr


def _secret(value: SecretStr | None, file: Path | None) -> str:
    if file is not None:
        return file.read_text(encoding="utf-8").strip()
    return value.get_secret_value().strip() if value is not None else ""


class Settings(AstriaSettings):
    service_name: str = "gateway"

    host: str = "127.0.0.1"
    port: int = 8080
    """Listener internal (`/internal/v1`, `/health`, `/metrics`) lewat ASTRIA_HOST/ASTRIA_PORT. Kontainer: 0.0.0.0."""
    gw_public_bind: str = ""
    """Listener publik `host:port`, hanya `/hooks/*`. Kosong = tanpa listener publik."""

    gw_core_url: str = "http://api:8000"
    """Basis URL api untuk Command Facade `POST /svc/v1/commands` (ditandatangani sebagai layanan `gateway`)."""
    gw_caller_keys_file: Path | None = None
    """Berkas JSON {layanan: kunci_hex} pemanggil `/internal/v1` (notifier, config, api). Kosong = semua ditolak."""
    gw_seed_file: Path | None = None
    """Berkas JSON kanal dan identitas awal (belum ada endpoint admin)."""

    gw_telegram_enabled: bool = True
    """`false` = kanal Telegram tidak dipakai sama sekali (adapter dan poller tidak dipasang), walau kunci induk ada."""
    gw_telegram_bot_token: SecretStr | None = None
    gw_telegram_bot_token_file: Path | None = None
    """Token bot Telegram. Kosong = adapter dan poller Telegram mati."""
    gw_telegram_channel_id: str = "telegram-tim"
    """Kanal yang dilayani poller (identitas, offset, dedupe)."""
    gw_telegram_poll_timeout: int = 30
    gw_telegram_max_chars: int = 3000

    gw_wa_bridge_url: str = ""
    """Basis URL jembatan WhatsApp Web `wa-bridge` (khusus uji). Kosong, atau token kosong = WhatsApp mati."""
    gw_wa_bridge_token_file: Path | None = None
    """Bearer token bersama gateway dan wa-bridge (`POST /kirim` dan `POST /internal/v1/whatsapp/masuk`)."""
    gw_whatsapp_channel_id: str = "wa-tim"
    """Kanal yang dilayani penerima WhatsApp (identitas, dedupe)."""

    gw_smtp_host: str = ""
    """Kosong = adapter email mati."""
    gw_smtp_port: int = 587
    gw_smtp_tls: Literal["starttls", "tls", "none"] = "starttls"
    gw_smtp_user: str = ""
    gw_smtp_password: SecretStr | None = None
    gw_smtp_password_file: Path | None = None
    gw_smtp_from: str = "astria@localhost"

    gw_master_key: SecretStr | None = None
    gw_master_key_file: Path | None = None
    """Kunci induk AES-256-GCM untuk rahasia di basis data: 32 byte, base64 atau hex 64 karakter.
    Kosong = rahasia tidak bisa ditulis lewat API. Salah format = proses menolak start."""

    gw_rate_per_identity: int = 20
    """Perintah per menit per identitas (GW§5.5)."""
    gw_rate_global: int = 60
    """Pesan masuk per menit per kanal."""

    def telegram_token(self) -> str:
        return _secret(self.gw_telegram_bot_token, self.gw_telegram_bot_token_file)

    def wa_bridge_token(self) -> str:
        return _secret(None, self.gw_wa_bridge_token_file) if self.gw_wa_bridge_url else ""

    def smtp_password(self) -> str:
        return _secret(self.gw_smtp_password, self.gw_smtp_password_file)

    def master_key(self) -> bytes | None:
        """Kunci induk 32 byte, atau None bila kosong. ValueError berpesan aman (tanpa nilai) bila salah format."""
        raw = _secret(self.gw_master_key, self.gw_master_key_file)
        if not raw:
            return None
        if len(raw) == 64:
            try:
                key = bytes.fromhex(raw)
            except ValueError:
                key = b""
        else:
            try:
                key = base64.b64decode(raw, validate=True)
            except binascii.Error, ValueError:
                key = b""
        if len(key) != 32:
            raise ValueError("ASTRIA_GW_MASTER_KEY harus 32 byte dalam base64 atau hex 64 karakter")
        return key
