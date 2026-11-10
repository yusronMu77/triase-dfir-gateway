"""Teks pesan berbahasa Indonesia (GW§5.7). Hanya field allowlist yang dipakai; semua nilai diperlakukan sebagai data.

Telegram dikirim sebagai teks biasa tanpa `parse_mode`, jadi tidak ada escape Markdown/HTML yang perlu dijaga.
"""

from __future__ import annotations

from typing import Any

# Pertahanan kedua setelah kebijakan kanal di Core (GW§3.4): field di luar daftar ini membuat event ditolak 422.
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    # teknik, klaim_total, klaim_lolos hanya ada di payload kanal `ringkas` (opsional).
    "card.verified": frozenset(
        {"card_id", "version", "band", "skor", "status", "tautan", "teknik", "klaim_total", "klaim_lolos"}
    ),
    "digest.published": frozenset({"digest_id", "jenis", "periode_mulai", "periode_selesai", "tautan"}),
    "ping": frozenset(),
}
MAX_VALUE_CHARS = 300

HELP = "\n".join(
    [
        "Perintah Astria:",
        "/status - status sistem",
        "/antrean [band] - antrean kartu teratas",
        "/kartu <ID> - ringkasan kartu",
        "/bukti <ID> - metadata bukti kartu",
        "/digest - digest terakhir",
        "/handover - serah terima shift",
        "/bantuan - daftar perintah ini",
    ]
)
NOT_REGISTERED = "Akun chat ini tidak terdaftar di Astria. Minta admin menghubungkan akun Anda."
RATE_LIMITED = "Terlalu banyak perintah. Coba lagi dalam satu menit."
CORE_DOWN = "Sistem inti tidak tersedia, coba lagi nanti."
NO_PENDING = "Tidak ada perintah yang menunggu konfirmasi."


def clean(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    """Satu baris teks tanpa karakter kontrol (juga mencegah injeksi header email lewat subjek)."""
    if isinstance(value, list):
        value = ", ".join(map(str, value))
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return text[:limit]


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _lines(title: str, pairs: list[tuple[str, Any]]) -> str:
    return "\n".join([title] + [f"{label}: {clean(value)}" for label, value in pairs if value not in (None, "", [])])


def render_event(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """(subjek, isi) untuk satu kanal. `payload` sudah lolos allowlist."""
    p = payload
    if event_type == "card.verified":
        subject = f"[Astria] Kartu {clean(p.get('card_id', '?'), 40)} terverifikasi"
        klaim = f"{p['klaim_lolos']}/{p['klaim_total']} lolos" if "klaim_total" in p and "klaim_lolos" in p else None
        pairs = [
            ("Band", p.get("band")),
            ("Skor", p.get("skor")),
            ("Status", p.get("status")),
            ("Teknik", p.get("teknik")),
            ("Klaim", klaim),
            ("Versi", p.get("version")),
            ("Tautan", p.get("tautan")),
        ]
        return subject, _lines(subject, pairs)
    if event_type == "digest.published":
        names = [clean(p[k], 40) for k in ("jenis", "digest_id") if p.get(k)]
        subject = " ".join(["[Astria] Digest", *names, "terbit"])
        periode = None
        if p.get("periode_mulai") or p.get("periode_selesai"):
            periode = f"{clean(p.get('periode_mulai', '?'))} s.d. {clean(p.get('periode_selesai', '?'))}"
        return subject, _lines(subject, [("Periode", periode), ("Tautan", p.get("tautan"))])
    subject = "[Astria] Pesan uji kanal"
    return subject, f"{subject}\nKanal ini terhubung dan menerima notifikasi Astria."


def _item(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{clean(k, 40)}: {clean(v)}" for k, v in value.items())
    return clean(value)


def render_result(command: str, hasil: dict[str, Any]) -> str:
    """Balasan perintah dari hasil Command Facade yang sudah dipangkas Core; gateway tidak menambah data."""
    lines = [f"/{command}"]
    for key, value in hasil.items():
        if isinstance(value, list):
            lines.append(f"{clean(key, 40)}:")
            lines += [f"- {_item(v)}" for v in value[:10]] or ["- (kosong)"]
        else:
            lines.append(f"{clean(key, 40)}: {_item(value)}")
    return "\n".join(lines) if len(lines) > 1 else f"/{command}\n(kosong)"


def not_available(command: str) -> str:
    return f"Perintah /{command} belum tersedia lewat chat."


def core_rejected(message: str) -> str:
    return f"Perintah ditolak sistem inti: {clean(message, 200)}" if message else "Perintah ditolak sistem inti."
