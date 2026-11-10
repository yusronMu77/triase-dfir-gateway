# Changelog

## [Unreleased]

### Added

- gateway#3 (notifikasi keluar dulu): dua listener dalam satu proses (internal `/internal/v1`, publik hanya `/hooks/*` yang kini selalu 404, batas body 64 KiB); `POST /internal/v1/events` dari notifier dengan HMAC `astria_common.hmac_auth`, dedupe `event_id` (409), allowlist payload (422), kanal tidak dikenal (404); `GET /internal/v1/status`; tabel `channel_config`, `channel_identity`, `outbox_message`, `inbound_message`, `event_inbox`, `poll_state`, `pending_confirmation`, `gw_audit` (SQLAlchemy Core, PostgreSQL dan SQLite); seed kanal dan identitas dari berkas JSON; pekerja outbox dengan adapter Telegram (httpx) dan SMTP (aiosmtplib); poller Telegram `getUpdates` dengan tata bahasa tertutup, batas laju, dan perintah T0 ke Command Facade api. Dependensi baru: `httpx`, `aiosmtplib`.
- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).

### Changed

- Listener internal bawaan `127.0.0.1:8080` (`ASTRIA_HOST`/`ASTRIA_PORT`; image `0.0.0.0:8080`), termasuk `EXPOSE`, healthcheck image, dan smoke CI.
