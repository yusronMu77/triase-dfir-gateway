"""Tata bahasa perintah chat yang tertutup (note kanal §7.2).

Hanya nama di `RULES` dengan argumen yang lolos pola yang menjadi perintah; teks lain menghasilkan `None`
(dibalas bantuan). Teks bebas tidak pernah dijalankan dan tidak dikirim ke model.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

CARD_ID = re.compile(r"IC-[0-9]{8}-[0-9]{3,6}")
PAIRING_CODE = re.compile(r"[A-Za-z0-9]{6,12}")
CONFIRM_CODE = re.compile(r"[0-9]{4}")
LABELS = {"benar": "benar", "salah": "salah_positif", "dfir": "perlu_dfir"}
MAX_TEXT = 300
MAX_NOTE = 200

ArgRule = Callable[[list[str]], dict[str, str] | None]


@dataclass(frozen=True)
class Command:
    name: str  # nama di chat, mis. "kartu"
    tier: str  # T0, T1, T2, lokal, pairing, konfirmasi
    facade: str | None  # nama perintah di Command Facade; None = ditangani gateway
    args: dict[str, str]


def _none(tokens: list[str]) -> dict[str, str] | None:
    return {} if not tokens else None


def _one(pattern: re.Pattern[str], key: str) -> ArgRule:
    def rule(tokens: list[str]) -> dict[str, str] | None:
        return {key: tokens[0]} if len(tokens) == 1 and pattern.fullmatch(tokens[0]) else None

    return rule


def _queue(tokens: list[str]) -> dict[str, str] | None:
    # Command Facade api (`queue`) tidak menerima argumen; filter band belum ada di ticket.
    return {} if not tokens else None


def _label(tokens: list[str]) -> dict[str, str] | None:
    if len(tokens) < 2 or not CARD_ID.fullmatch(tokens[0]) or tokens[1].lower() not in LABELS:
        return None
    if len(" ".join(tokens[2:])) > MAX_NOTE:
        return None
    # Catatan bebas tidak diteruskan: Command Facade `feedback` hanya menerima card_id dan label.
    return {"card_id": tokens[0], "label": LABELS[tokens[1].lower()]}


RULES: dict[str, tuple[str, str | None, ArgRule]] = {
    "bantuan": ("lokal", None, _none),
    "status": ("T0", "status", _none),
    "antrean": ("T0", "queue", _queue),
    "kartu": ("T0", "card", _one(CARD_ID, "card_id")),
    "bukti": ("T0", "evidence_meta", _one(CARD_ID, "card_id")),
    "digest": ("T0", "digest_latest", _none),
    "handover": ("T0", "handover", _none),
    "label": ("T1", "feedback", _label),
    "dfir": ("T1", "deep_dive", _one(CARD_ID, "card_id")),
    "jalankan-digest": ("T2", "digest_run", _none),
    "berhenti": ("lokal", None, _none),
    "daftar": ("pairing", None, _one(PAIRING_CODE, "kode")),
    "ya": ("konfirmasi", None, _one(CONFIRM_CODE, "kode")),
    "batal": ("konfirmasi", None, _none),
}


def parse(text: str) -> Command | None:
    if len(text) > MAX_TEXT or not text.startswith("/"):
        return None
    head, *tokens = text.split()
    name = head[1:].split("@", 1)[0].lower()  # "/kartu@AstriaBot" di grup
    rule = RULES.get(name)
    if rule is None:
        return None
    tier, facade, arg_rule = rule
    args = arg_rule(tokens)
    return None if args is None else Command(name, tier, facade, args)
