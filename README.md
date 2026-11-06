# Astria gateway

Astria Gateway (A10): aplikasi mandiri di DMZ untuk kanal dan bot; hanya /hooks/* yang menghadap internet.

> Status: kerangka awal, belum ada kode. Repo ini adalah submodule dari workspace `triase-dfir` (folder `gateway/`). Rancangan: `notes/2026-11-06-triase-dfir-app-gateway.md` di repo root. Lajur developer: 2.

## Tech stack

Python 3.14, FastAPI, SQLite atau PostgreSQL (keputusan terbuka), uv

## Perintah

```text
uv sync
uv run pytest
```

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.
