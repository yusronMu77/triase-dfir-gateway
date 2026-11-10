"""Kanal dan identitas awal dari berkas JSON (pengganti sementara endpoint admin GW§3.2).

Berkas divalidasi saat aplikasi dirakit (salah format = proses menolak mulai) dan diterapkan setelah basis
data siap. Hanya menambah atau memperbarui baris; identitas yang hilang dari berkas tidak dicabut otomatis.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Literal

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astria_gateway.store import channel_config, channel_identity, now, upsert

log = get_logger(__name__)

CHANNEL_ID = r"^[a-z][a-z0-9_-]{0,63}$"
EXTERNAL_ID = {"telegram": r"^-?[0-9]{1,20}$", "email": r"^[^@\s]{1,64}@[^@\s]{1,255}$"}


class SeedChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str = Field(pattern=CHANNEL_ID)
    type: Literal["telegram", "email"]


class SeedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    channel_id: str = Field(pattern=CHANNEL_ID)
    external_id: str = Field(max_length=320)
    user_id: str = Field(pattern=r"^usr_[A-Za-z0-9]+$")
    status: Literal["aktif", "dicabut"] = "aktif"


class Seed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[SeedChannel] = []
    identities: list[SeedIdentity] = []

    @model_validator(mode="after")
    def _identities_match_channels(self) -> Seed:
        types = {c.channel_id: c.type for c in self.channels}
        for ident in self.identities:
            kind = types.get(ident.channel_id)
            if kind is None:
                raise ValueError(f"identitas {ident.identity_id}: kanal {ident.channel_id} tidak ada di seed")
            if not re.fullmatch(EXTERNAL_ID[kind], ident.external_id):
                raise ValueError(f"identitas {ident.identity_id}: external_id tidak sesuai kanal {kind}")
        return self


def load_seed(path: Path | None) -> Seed:
    return Seed() if path is None else Seed.model_validate_json(path.read_bytes())


async def apply_seed(db: Database, seed: Seed) -> None:
    async with db.engine.begin() as conn:
        for ch in seed.channels:
            await upsert(conn, channel_config, "channel_id", ch.channel_id, {"type": ch.type, "updated_at": now()})
        for ident in seed.identities:
            values = ident.model_dump(exclude={"identity_id"}) | {"updated_at": now()}
            await upsert(conn, channel_identity, "identity_id", ident.identity_id, values)
    log.info("seed_diterapkan", channels=len(seed.channels), identities=len(seed.identities))


class SeedLoader:
    """Komponen: terapkan seed setelah basis data siap. /ready `down` sampai seed diterapkan."""

    name = "seed"
    required = True

    def __init__(self, db: Database, seed: Seed) -> None:
        self._db = db
        self._seed = seed
        self._done = False
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gw-seed")

    async def _run(self) -> None:
        await self._db.wait_ready()
        try:
            await apply_seed(self._db, self._seed)
            self._done = True
        except Exception as exc:  # noqa: BLE001 - terlihat di /ready; tidak menjatuhkan proses
            self._error = type(exc).__name__
            log.error("seed_gagal", error=self._error)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        if self._done:
            return Check("ok")
        return Check("down", self._error or "menunggu basis data")
