# wa-bridge (khusus uji)

Jembatan kecil antara Astria Gateway dan WhatsApp Web lewat [Baileys](https://github.com/WhiskeySockets/Baileys) `7.0.0-rc14` (MIT; memakai libsignal GPL-3.0 dan whatsapp-rust-bridge berbasis WebAssembly).
Gateway mengirim notifikasi dan balasan perintah lewat `POST /kirim`; pesan teks pribadi yang masuk diteruskan ke gateway
`POST /internal/v1/whatsapp/masuk`. Keputusan PM 2026-11-10: WhatsApp dipakai untuk **uji** menggantikan Telegram.

## Risiko (baca dulu)

- **Klien tidak resmi.** Baileys meniru WhatsApp Web dan melanggar syarat layanan WhatsApp. Nomor yang ditautkan bisa dibatasi atau
  diblokir kapan saja. Pakai **nomor cadangan khusus uji**, jangan nomor pribadi atau nomor dinas.
- **Hanya untuk uji dan demo dev.** Jangan pernah dijalankan di server privat TNI AD: butuh internet ke server WhatsApp, dan sesi
  tertaut memberi akses penuh ke akun WhatsApp itu. Rancangan produksi tetap Cloud API resmi (note gateway, F3).
- **Folder sesi = kunci akun.** Siapa pun yang memegang isi `WA_BRIDGE_DATA_DIR/sesi` bisa memakai akun tersebut. Simpan di volume
  privat, jangan di-commit, hapus setelah uji.
- **`/qr` tanpa autentikasi.** Siapa pun yang bisa membuka halaman QR sebelum Anda memindai bisa menautkan HP-nya. Port hanya boleh
  terbuka di `127.0.0.1` (devenv: `127.0.0.1:8114`). `/status` hanya menampilkan nomor tersamar.

## Endpoint (satu port, bawaan 3000)

| Endpoint | Auth | Isi |
|---|---|---|
| `GET /qr` | - | Halaman QR (segar tiap 5 detik), atau "Terhubung ke 62812****5678" |
| `GET /status` | - | `{"terhubung", "perlu_scan", "nomor_tersamar"}` |
| `POST /kirim` | `Authorization: Bearer <token>` | Body `{"ke": "628...", "teks": "..."}` (`ke` 8-15 digit, `teks` maks 4000 karakter, body maks 16 KiB). 200 `{"id"}`; 400 input salah; 401 token salah; 413 body terlalu besar; 503 belum terhubung; 502 gagal kirim |

Pesan masuk: hanya teks dari chat pribadi (bukan grup, status, newsletter, atau pesan sendiri), diteruskan sebagai
`{"id", "dari", "teks", "waktu"}` dengan token yang sama; galat jaringan, 429, dan 5xx diulang 4 kali dengan jeda naik
(1, 2, 4 detik). Isi pesan dan nomor penuh tidak pernah dicatat di log; log Baileys dibatasi level `error`, dan output `console`
libsignal (yang memuat kunci sesi) dibungkam.

Baileys 7 diperlukan karena WhatsApp kini memakai sesi Signal beralamat LID; dengan 6.7.24 setiap pesan gagal didekripsi HP pada
percobaan pertama ("Waiting for this message"). Permintaan kirim ulang dari HP dijawab cache pesan terbaru bawaan Baileys 7
(per penerima, hilang saat kontainer dimulai ulang). Log `status_kirim` (2 = sampai server, 3 = sampai HP, 4 = dibaca) hanya
memuat id pesan.

## Konfigurasi

| Env | Bawaan | Isi |
|---|---|---|
| `WA_BRIDGE_TOKEN_FILE` | (wajib) | Berkas Bearer token bersama gateway (`ASTRIA_GW_WA_BRIDGE_TOKEN_FILE`) |
| `WA_BRIDGE_GATEWAY_URL` | kosong | Basis URL listener internal gateway; kosong = pesan masuk diabaikan |
| `WA_BRIDGE_DATA_DIR` | `./data` (image: `/data`) | Kredensial sesi di subfolder `sesi` |
| `WA_BRIDGE_HOST`, `WA_BRIDGE_PORT` | `127.0.0.1`, `3000` (image: `0.0.0.0`) | Alamat dengar |
| `LOG_LEVEL` | `info` | Level log pino |

## Menautkan nomor uji

1. Jalankan devenv (`sh devenv/scripts/dev.sh up`); layanan `wa-bridge` ikut.
2. Buka `http://127.0.0.1:8114/qr`.
3. Di HP nomor cadangan: WhatsApp, Perangkat tertaut, Tautkan perangkat, pindai QR.
4. `http://127.0.0.1:8114/status` menjadi `"terhubung": true`.
5. Daftarkan nomor penerima di seed lokal gateway (lihat README devenv): identitas kanal `wa-tim` dengan `external_id` nomor tanpa `+`.

Melepas tautan dari HP membuat bridge menghapus sesi dan menampilkan QR baru.

## Perintah

```text
npm ci                 # .npmrc: ignore-scripts dan legacy-peer-deps (sharp/jimp tidak dipasang; hanya teks)
npm test               # node --test: token, validasi input, penyaringan dan normalisasi pesan masuk, penerusan
WA_BRIDGE_TOKEN_FILE=... npm start
```
