"""Tabel gateway (GW§4) lewat SQLAlchemy Core: satu definisi untuk PostgreSQL (server) dan SQLite (lokal).

Kolom dipangkas ke yang dipakai sekarang. Ditunda: `subscription`, hash dan ciphertext identitas (`external_id`
masih teks biasa dari berkas seed), dan rantai hash `gw_audit`. Tabel `secret` (rahasia AES-GCM, `vault.py`) sudah ada.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

metadata = MetaData()


def now() -> datetime:
    return datetime.now(UTC)


channel_config = Table(
    "channel_config",
    metadata,
    Column("channel_id", Text, primary_key=True),
    Column("type", Text, nullable=False),  # email | telegram | whatsapp
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

channel_identity = Table(
    "channel_identity",
    metadata,
    Column("identity_id", Text, primary_key=True),
    Column("channel_id", Text, ForeignKey("channel_config.channel_id"), nullable=False),
    Column("external_id", Text, nullable=False),  # chat_id Telegram, alamat email, atau nomor WhatsApp (digit)
    Column("user_id", Text, nullable=False),
    Column("status", Text, nullable=False),  # aktif | dicabut
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("channel_id", "external_id"),
)

outbox_message = Table(
    "outbox_message",
    metadata,
    Column("message_id", Text, primary_key=True),
    Column("event_id", Text, nullable=False),
    Column("channel_id", Text, ForeignKey("channel_config.channel_id"), nullable=False),
    Column("recipient_ref", Text, nullable=False),  # identity_id, bukan alamat mentah
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", Text, nullable=False),  # pending | sending | sent | dead
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", "channel_id", "recipient_ref"),
)
Index("outbox_due", outbox_message.c.status, outbox_message.c.next_attempt_at)

inbound_message = Table(
    "inbound_message",
    metadata,
    Column("channel_id", Text, ForeignKey("channel_config.channel_id"), primary_key=True),
    Column("provider_msg_id", Text, primary_key=True),  # update_id Telegram atau id pesan WhatsApp
    Column("identity_id", Text),
    Column("command", Text),
    Column("status", Text, nullable=False),  # diterima | selesai | ditolak
    Column("received_at", DateTime(timezone=True), nullable=False),
)

event_inbox = Table(
    "event_inbox",
    metadata,
    Column("event_id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)

# Rahasia kanal: ciphertext AES-256-GCM dengan kunci induk; nama kunci ikut sebagai AAD. Tulis-saja lewat API.
secret = Table(
    "secret",
    metadata,
    Column("key", Text, primary_key=True),  # contoh: telegram.bot_token
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),  # 12 byte acak per penulisan
    Column("key_version", Integer, nullable=False),  # versi kunci induk
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

poll_state = Table(
    "poll_state",
    metadata,
    Column("channel_id", Text, primary_key=True),
    Column("offset_value", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Konfirmasi dua langkah T1/T2 (GW§5.3); dipakai saat alur /ya diaktifkan.
pending_confirmation = Table(
    "pending_confirmation",
    metadata,
    Column("confirmation_ref", Text, primary_key=True),
    Column("identity_id", Text, ForeignKey("channel_identity.identity_id"), nullable=False),
    Column("command_hash", Text, nullable=False),
    Column("code_hash", Text, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

gw_audit = Table(
    "gw_audit",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("action", Text, nullable=False),
    Column("detail_json", Text, nullable=False),  # tanpa rahasia dan tanpa isi pesan
)


async def audit(conn: AsyncConnection, action: str, **detail: Any) -> None:
    await conn.execute(gw_audit.insert().values(ts=now(), action=action, detail_json=orjson.dumps(detail).decode()))


async def upsert(conn: AsyncConnection, table: Table, key: str, key_value: Any, values: dict[str, Any]) -> None:
    """UPDATE lalu INSERT bila belum ada: portabel di PostgreSQL dan SQLite (satu proses penulis)."""
    col = table.c[key]
    if await conn.scalar(select(col).where(col == key_value)) is None:
        await conn.execute(table.insert().values({key: key_value, **values}))
    else:
        await conn.execute(update(table).where(col == key_value).values(values))
