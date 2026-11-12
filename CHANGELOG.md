# Changelog

## [Unreleased]

### Fixed

- `/antrean` tidak lagi menerima argumen band: Command Facade api (`queue`) menolak argumen apa pun, sehingga `/antrean <band>` selalu gagal "argumen perintah tidak sesuai". Teks bantuan disesuaikan.

### Added

- Rahasia kanal dari config dan pencabutan identitas: `PUT /internal/v1/secrets/{key}` dari config (HMAC `config` saja, 204 tanpa nilai, idempoten; kunci `telegram.bot_token` dan `smtp.password`, 404 untuk kunci lain, 422 untuk format token salah, 409 bila dikunci env, 503 bila kunci induk belum diisi). Rahasia disimpan di tabel `secret` dengan AES-256-GCM (AAD = nama kunci), dibaca ke memori saat start, dan dipakai per panggilan oleh adapter Telegram dan SMTP. Konfigurasi baru: `ASTRIA_GW_MASTER_KEY` atau `_FILE` (32 byte, base64 atau hex). Event kontrol `identity.revoked` (`channel_id` "*") mencabut semua ikatan kanal aktif milik `user_id` (status `dicabut`), idempoten lewat `event_inbox`; pesan dari chat itu tidak lagi diterjemahkan jadi perintah, dan pesan yang masih antre gagal permanen (`identitas_tidak_aktif`). Telegram aktif juga bila kunci induk diisi, supaya token bisa ditulis config.
- Kanal WhatsApp uji (keputusan PM 2026-11-10): jembatan `wa-bridge/` (Node, Baileys 6.7.24, `GET /qr`, `GET /status`, `POST /kirim` ber-Bearer token, penerusan pesan teks pribadi ke gateway, Dockerfile non-root, uji `node --test`); tipe kanal `whatsapp` di seed (`external_id` 8-15 digit); adapter outbox `whatsapp`; `POST /internal/v1/whatsapp/masuk` di listener internal dengan Bearer token dan dedupe; pemrosesan perintah chat dipindah ke `inbound.py` dan dipakai bersama poller Telegram; `GET /internal/v1/status` melaporkan `whatsapp`. Konfigurasi baru: `ASTRIA_GW_WA_BRIDGE_URL`, `ASTRIA_GW_WA_BRIDGE_TOKEN_FILE`, `ASTRIA_GW_WHATSAPP_CHANNEL_ID`.
- gateway#3 (notifikasi keluar dulu): dua listener dalam satu proses (internal `/internal/v1`, publik hanya `/hooks/*` yang kini selalu 404, batas body 64 KiB); `POST /internal/v1/events` dari notifier dengan HMAC `astria_common.hmac_auth`, dedupe `event_id` (409), allowlist payload (422), kanal tidak dikenal (404); `GET /internal/v1/status`; tabel `channel_config`, `channel_identity`, `outbox_message`, `inbound_message`, `event_inbox`, `poll_state`, `pending_confirmation`, `gw_audit` (SQLAlchemy Core, PostgreSQL dan SQLite); seed kanal dan identitas dari berkas JSON; pekerja outbox dengan adapter Telegram (httpx) dan SMTP (aiosmtplib); poller Telegram `getUpdates` dengan tata bahasa tertutup, batas laju, dan perintah T0 ke Command Facade api. Dependensi baru: `httpx`, `aiosmtplib`.
- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).

### Changed

- Listener internal bawaan `127.0.0.1:8080` (`ASTRIA_HOST`/`ASTRIA_PORT`; image `0.0.0.0:8080`), termasuk `EXPOSE`, healthcheck image, dan smoke CI.
