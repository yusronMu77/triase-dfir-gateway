"""Konfigurasi gateway dari env `ASTRIA_*` (GW§5.9).

Rahasia kanal (token bot, kata sandi SMTP) hanya dari env atau berkas `_FILE`, tidak pernah dicetak.
Penyimpanan rahasia terenkripsi AES-GCM di basis data (GW§4 tabel `secret`) ditunda.
"""

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

    gw_telegram_bot_token: SecretStr | None = None
    gw_telegram_bot_token_file: Path | None = None
    """Token bot Telegram. Kosong = adapter dan poller Telegram mati."""
    gw_telegram_channel_id: str = "telegram-tim"
    """Kanal yang dilayani poller (identitas, offset, dedupe)."""
    gw_telegram_poll_timeout: int = 30
    gw_telegram_max_chars: int = 3000

    gw_smtp_host: str = ""
    """Kosong = adapter email mati."""
    gw_smtp_port: int = 587
    gw_smtp_tls: Literal["starttls", "tls", "none"] = "starttls"
    gw_smtp_user: str = ""
    gw_smtp_password: SecretStr | None = None
    gw_smtp_password_file: Path | None = None
    gw_smtp_from: str = "astria@localhost"

    gw_rate_per_identity: int = 20
    """Perintah per menit per identitas (GW§5.5)."""
    gw_rate_global: int = 60
    """Pesan masuk per menit per kanal."""

    def telegram_token(self) -> str:
        return _secret(self.gw_telegram_bot_token, self.gw_telegram_bot_token_file)

    def smtp_password(self) -> str:
        return _secret(self.gw_smtp_password, self.gw_smtp_password_file)
