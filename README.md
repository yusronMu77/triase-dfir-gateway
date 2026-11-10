<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria gateway

Astria Gateway (A10): aplikasi mandiri di DMZ untuk kanal dan bot; hanya /hooks/* yang menghadap internet.

> Status: Tahap 1 (gateway#3, notifikasi keluar dulu). Menerima event terender dari notifier, mengirim lewat email (SMTP) dan Telegram, dan melayani perintah T0 Telegram lewat polling ke Command Facade api. Repo ini adalah submodule dari workspace `triase-dfir` (folder `gateway/`). Rancangan: `notes/2026-11-06-triase-dfir-app-gateway.md` di repo root. Lajur developer: 2.

## Tech stack

Python 3.14, FastAPI, Uvicorn (dua listener), SQLAlchemy Core (PostgreSQL di server, SQLite lokal; gateway#2), httpx, aiosmtplib, uv

## Perintah

```text
uv sync                                   # butuh akses ke repo privat contracts dan common (tag git)
uv run ruff format --check . && uv run ruff check .
uv run pytest
sh ../common/template/localtest.sh        # di kontainer py devenv: uji terhadap salinan kerja common dan contracts
```

Uji dua dialek: SQLite selalu; PostgreSQL bila `ASTRIA_TEST_DATABASE_URL` diisi (di kontainer `py` devenv:
`postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/postgres`, skema per uji).

Di devenv (`devenv/compose.yaml`, layanan `gateway` dan `mailpit`): listener internal di `http://127.0.0.1:8112`
(`gateway:8080` di jaringan compose), SQLite `gateway_dev.db` di volume `gwdata`, email ke mailpit (UI `http://127.0.0.1:8113`),
seed `devenv/gateway/seed.json`. Setelah mengubah kode: `docker compose restart gateway` (tanpa reload otomatis).
Sendiri: `uv run astria-gateway`.

## Cara kerja

| Bagian | Isi |
|---|---|
| `main.py` | Satu proses, dua `uvicorn.Server` dalam satu event loop (GW§3.1): internal (`ASTRIA_HOST`/`ASTRIA_PORT`, bawaan `127.0.0.1:8080`, image `0.0.0.0:8080`) dan publik (`ASTRIA_GW_PUBLIC_BIND`, kosong = tidak ada). Sinyal ditangani server internal, yang lalu menghentikan server publik |
| `public.py` | Listener publik: ASGI polos, hanya `/hooks/*`; belum ada hook aktif, jadi semua jalur 404 `{"error":"bad_request"}`, body > 64 KiB 413, tanpa docs/openapi/health/metrics, tanpa header `server`, `limit_concurrency` 50, keep-alive 5 detik |
| `api.py` | `POST /internal/v1/events` (hanya `notifier`) dan `GET /internal/v1/status` (`notifier`, `config`, `api`), HMAC `astria_common.hmac_auth` dengan kunci dari `ASTRIA_GW_CALLER_KEYS_FILE` |
| `render.py` | Allowlist payload per tipe event dan teks pesan berbahasa Indonesia dari field allowlist saja; karakter kontrol dibuang |
| `outbox.py` | `outbox_message` satu baris per (event, kanal, identitas aktif); pekerja: `pending` -> `sending` -> `sent`, galat sementara jeda naik `5*2^n` detik (maks 300) atau `retry_after` penyedia, permanen atau 5 percobaan -> `dead` dan `gw_audit` |
| `channels.py` | Telegram `sendMessage`/`getUpdates` lewat httpx (teks biasa tanpa `parse_mode`, maks 3000 karakter, 429 hormati `retry_after`, 401/403/400 permanen); SMTP lewat aiosmtplib (STARTTLS, TLS, atau tanpa TLS; timeout 10 detik; 5xx permanen) |
| `poller.py` | `getUpdates` long poll; offset di `poll_state`; dedupe `inbound_message`; hanya chat pribadi yang terikat di seed; batas laju 60/menit per kanal, 20/menit per identitas, 1 balasan "tidak terdaftar" per 10 menit per chat; perintah T0 ke Command Facade, balasan lewat outbox |
| `grammar.py` | Tata bahasa tertutup (note kanal §7.2); teks bebas dan argumen di luar pola dibalas bantuan |
| `facade.py` | `POST {ASTRIA_GW_CORE_URL}/svc/v1/commands` ditandatangani sebagai `gateway` dengan `ASTRIA_SERVICE_KEY`/`_FILE` |
| `seed.py` | Kanal dan identitas dari `ASTRIA_GW_SEED_FILE` (divalidasi saat start, diterapkan setelah basis data siap; /ready `down` sampai selesai) |

Kontrak penerimaan `POST /internal/v1/events` (dari notifier):

- Header dari `astria_common.hmac_auth.signed_headers("notifier", kunci, "POST", "/internal/v1/events", body)`:
  `X-Astria-Service`, `X-Astria-Ts`, `X-Astria-Nonce`, `X-Astria-Sig`; jendela 300 detik, nonce sekali pakai.
- Body `{"event_id", "type", "occurred_at", "channels": [{"channel_id", "payload"}]}`; satu permintaan per event.
- Tipe dan field payload yang diterima: `card.verified` (`card_id`, `version`, `band`, `skor`, `status`, `tautan`, opsional
  `teknik` (daftar), `klaim_total`, `klaim_lolos`), `digest.published` (`digest_id`, `jenis`, `periode_mulai`, `periode_selesai`,
  `tautan`), `ping` (tanpa field; pesan uji kanal).
- Respons: 202 `{"event_id", "messages"}`; 409 `GW_EVENT_DUPLICATE` (event_id sudah diterima); 401 `GW_AUTH_FAILED` atau
  `GW_REPLAY`; 404 `GW_CHANNEL_UNKNOWN`; 422 `GW_EVENT_INVALID` (amplop salah, tipe tidak diterima, field di luar allowlist,
  kanal ganda); 503 `GW_NOT_READY`. Bentuk galat `{"error": {"code", "message", "trace_id"}}`.

Konfigurasi (`ASTRIA_GW_*`): `PUBLIC_BIND` (`host:port`), `CORE_URL` (bawaan `http://api:8000`), `CALLER_KEYS_FILE` (JSON
`{layanan: kunci_hex}`), `SEED_FILE`, `TELEGRAM_BOT_TOKEN` atau `TELEGRAM_BOT_TOKEN_FILE` (kosong = Telegram mati),
`TELEGRAM_CHANNEL_ID` (bawaan `telegram-tim`), `TELEGRAM_POLL_TIMEOUT`, `TELEGRAM_MAX_CHARS`, `SMTP_HOST` (kosong = email mati),
`SMTP_PORT`, `SMTP_TLS` (`starttls`, `tls`, `none`), `SMTP_USER`, `SMTP_PASSWORD` atau `SMTP_PASSWORD_FILE`, `SMTP_FROM`,
`RATE_PER_IDENTITY`, `RATE_GLOBAL`; ditambah `ASTRIA_HOST`, `ASTRIA_PORT`, `ASTRIA_DATABASE_URL`, dan `ASTRIA_SERVICE_KEY`/`_FILE` dari `common`.
Token bot dan URL Bot API tidak pernah dicatat (logger `httpx` dibisukan, galat hanya berupa kode).

Format seed:

```json
{
  "channels": [{"channel_id": "telegram-tim", "type": "telegram"}, {"channel_id": "email-tim", "type": "email"}],
  "identities": [
    {"identity_id": "idt_tg1", "channel_id": "telegram-tim", "external_id": "<chat_id>", "user_id": "usr_0142"},
    {"identity_id": "idt_em1", "channel_id": "email-tim", "external_id": "soc@astria.test", "user_id": "usr_0142"}
  ]
}
```

Belum dibangun (menyusul): konfirmasi dua langkah T1/T2 (`/label`, `/dfir`, `/jalankan-digest`, `/ya`; tabel
`pending_confirmation` sudah ada), pairing `/daftar`, `/berhenti`, `/tanya`, grup chat, webhook Telegram dan WhatsApp, webhook keluar
dan penjaga SSRF, endpoint admin rahasia/identitas/outbox, penyimpanan rahasia AES-GCM (identitas masih teks biasa dari seed),
rantai hash `gw_audit`, retensi, dan terowongan demo. Alur perintah ujung ke ujung belum jalan di devenv karena endpoint identity
dan config yang dibutuhkan Command Facade belum ada.

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.
