"""Tata bahasa tertutup (note kanal §7.2): hanya katalog, argumen berpola, teks bebas tidak pernah jadi perintah."""

from __future__ import annotations

import random
import string

import pytest

from astria_gateway.grammar import MAX_TEXT, RULES, Command, parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/kartu IC-20261110-003", Command("kartu", "T0", "card", {"card_id": "IC-20261110-003"})),
        ("/kartu@AstriaBot IC-20261110-003", Command("kartu", "T0", "card", {"card_id": "IC-20261110-003"})),
        ("/bukti IC-20261110-003", Command("bukti", "T0", "evidence_meta", {"card_id": "IC-20261110-003"})),
        ("/status", Command("status", "T0", "status", {})),
        ("/antrean", Command("antrean", "T0", "queue", {})),
        ("/antrean kritis", Command("antrean", "T0", "queue", {"band": "KRITIS"})),
        ("/digest", Command("digest", "T0", "digest_latest", {})),
        ("/handover", Command("handover", "T0", "handover", {})),
        ("/BANTUAN", Command("bantuan", "lokal", None, {})),
        (
            "/label IC-20261110-003 salah sudah dicek ke pemilik host",
            Command("label", "T1", "feedback", {"card_id": "IC-20261110-003", "label": "salah_positif"}),
        ),
        ("/dfir IC-20261110-003", Command("dfir", "T1", "deep_dive", {"card_id": "IC-20261110-003"})),
        ("/jalankan-digest", Command("jalankan-digest", "T2", "digest_run", {})),
        ("/daftar AB12CD", Command("daftar", "pairing", None, {"kode": "AB12CD"})),
        ("/ya 4831", Command("ya", "konfirmasi", None, {"kode": "4831"})),
    ],
)
def test_valid_commands(text: str, expected: Command) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "kartu IC-20261110-003",
        "/kartu",
        "/kartu IC-2026111-003",
        "/kartu IC-20261110-003 tambahan",
        "/kartu IC-٢٠٢٦١١١٠-003",
        "/kartu ic-20261110-003; DROP TABLE",
        "/antrean SEMUA",
        "/status sekarang",
        "/label IC-20261110-003 mungkin",
        "/label IC-20261110-003 benar " + "x" * 201,
        "/ya 12345",
        "/hapus IC-20261110-003",
        "/tanya IC-20261110-003 apa ini",
        "/start",
        "/" + "a" * MAX_TEXT,
        "",
    ],
)
def test_outside_grammar_is_none(text: str) -> None:
    assert parse(text) is None


def test_random_text_never_leaves_catalog() -> None:
    rng = random.Random(20261110)  # noqa: S311 - data uji deterministik
    alphabet = string.printable + "ÄÖ٢—"
    names = list(RULES)
    facades = {rule[1] for rule in RULES.values()}
    for _ in range(2000):
        prefix = rng.choice(["", "/", f"/{rng.choice(names)} "])
        text = prefix + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        command = parse(text)
        if command is not None:
            assert command.name in RULES and command.facade in facades
            assert all(len(v) <= 40 for v in command.args.values())
