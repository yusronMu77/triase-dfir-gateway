"""Rahasia kanal (GW§4 tabel `secret`, GW§8): AES-256-GCM dengan kunci induk, tulis-saja lewat API internal.

Nilai dibaca dari basis data sekali saat start, didekripsi, lalu disimpan di memori proses. Nilai tidak pernah
dikembalikan API, tidak dicatat, dan tidak masuk `gw_audit` (hanya nama kunci dan versi). Urutan sumber: env yang
mengunci (`ASTRIA_GW_TELEGRAM_BOT_TOKEN`, `ASTRIA_GW_SMTP_PASSWORD`) lebih dulu, lalu basis data. Kunci yang
dikunci env tidak bisa ditulis lewat API (409).

Nama kunci ikut sebagai AAD, jadi ciphertext satu kunci tidak bisa dipindah ke kunci lain.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from typing import Any

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from astria_gateway.store import audit, now, secret, upsert

log = get_logger(__name__)

KEY_VERSION = 1
# Kunci yang boleh disimpan lewat API, dan kanal yang memakainya.
SECRET_CHANNELS: dict[str, str] = {"telegram.bot_token": "telegram", "smtp.password": "email"}
TELEGRAM_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,}")


class VaultLoadError(RuntimeError):
    """Rahasia di basis data tidak bisa dibaca. Pesan sudah aman: kode pendek, tanpa nilai."""


class VaultError(Exception):
    """Galat yang aman ditampilkan: `message` tidak memuat nilai rahasia."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _aad(key: str) -> bytes:
    return f"gw-secret|{key}|v{KEY_VERSION}".encode()


class SecretVault:
    """Komponen: memuat rahasia setelah basis data siap. /ready `down` bila rahasia tidak bisa dibaca."""

    name = "secret-vault"
    required = True

    def __init__(self, db: Database, master: bytes | None, locked: dict[str, str]) -> None:
        self._db = db
        self._master = master
        self._locked = {k: v for k, v in locked.items() if v}
        self._values: dict[str, str] = {}
        self._loaded = False
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def can_store(self) -> bool:
        return self._master is not None

    def value(self, key: str) -> str:
        """Nilai aktif (env yang mengunci, atau basis data); kosong bila belum ada. Tidak pernah dicetak."""
        return self._locked.get(key) or self._values.get(key, "")

    def locked(self, key: str) -> bool:
        return key in self._locked

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gw-secret-vault")

    async def _run(self) -> None:
        await self._db.wait_ready()
        try:
            await self.load()
        except Exception as exc:  # noqa: BLE001 - terlihat di /ready; hanya kode aman, tanpa nilai
            self._error = str(exc) if isinstance(exc, VaultLoadError) else type(exc).__name__
            log.error("rahasia_gagal_dimuat", error=self._error)

    async def load(self) -> None:
        async with self._db.engine.connect() as conn:
            rows = (await conn.execute(select(secret))).mappings().all()
        if rows and self._master is None:
            raise VaultLoadError("kunci_induk_tidak_ada")
        values: dict[str, str] = {}
        for row in rows:
            if row["key"] not in SECRET_CHANNELS or row["key_version"] != KEY_VERSION:
                raise VaultLoadError("rahasia_tidak_dikenal")
            try:
                plain = AESGCM(self._master or b"").decrypt(row["nonce"], row["ciphertext"], _aad(row["key"]))
            except InvalidTag:
                raise VaultLoadError("kunci_induk_salah") from None
            values[row["key"]] = plain.decode("utf-8")
        self._values = values
        self._loaded = True
        log.info("rahasia_dimuat", jumlah=len(values))

    async def put(self, key: str, value: str) -> None:
        """Simpan rahasia terenkripsi. Nilai yang sama dengan yang aktif tidak menulis apa pun (idempoten)."""
        if self._master is None:
            raise VaultError("GW_MASTER_KEY_TIDAK_ADA", "kunci induk gateway belum diisi", 503)
        if self.locked(key):
            raise VaultError("GW_DIKUNCI_ENV", "rahasia ini dikunci oleh variabel lingkungan", 409)
        if not self._loaded:
            raise VaultError("GW_NOT_READY", "rahasia belum selesai dimuat", 503)
        if self._values.get(key) == value:
            return
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._master).encrypt(nonce, value.encode("utf-8"), _aad(key))
        async with self._db.engine.begin() as conn:
            values: dict[str, Any] = {
                "ciphertext": ciphertext,
                "nonce": nonce,
                "key_version": KEY_VERSION,
                "updated_at": now(),
            }
            await upsert(conn, secret, "key", key, values)
            await audit(conn, "secret.updated", key=key, key_version=KEY_VERSION)
        self._values[key] = value
        log.info("rahasia_diperbarui", key=key)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check(self) -> Check:
        if self._error:
            return Check("down", self._error)
        if not self._loaded:
            return Check("down", "menunggu basis data")
        if self._master is None and not self._locked:
            return Check("ok", "kunci induk tidak diisi")
        return Check("ok", f"terisi={len(self._values) + len(self._locked)}")
