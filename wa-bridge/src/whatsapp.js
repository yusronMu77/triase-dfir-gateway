// Sesi WhatsApp Web lewat Baileys: QR, sambung ulang, kirim teks, dan pesan masuk.
import { rm } from 'node:fs/promises'
import { join } from 'node:path'
import makeWASocket, { Browsers, DisconnectReason, fetchLatestWaWebVersion, useMultiFileAuthState } from '@whiskeysockets/baileys'
import { jidToDigits, normalizeInbound } from './lib.js'

export class NotConnected extends Error {}

export class WhatsApp {
  /** `onInbound(payload)` dipanggil untuk tiap pesan teks pribadi baru (hasil `normalizeInbound`). */
  constructor({ dataDir, log, onInbound }) {
    this.sessionDir = join(dataDir, 'sesi')
    this.log = log
    this.onInbound = onInbound
    this.qr = null
    this.connected = false
    this.number = null
    this.sock = null
    this.stopping = false
    this.version = null
  }

  async start() {
    // Versi WA Web diambil sekali per proses; sambung ulang memakai nilai yang sama.
    this.version ??= fetchLatestWaWebVersion({ timeout: 10_000 }).then((r) => r.version)
    const [{ state, saveCreds }, version] = await Promise.all([useMultiFileAuthState(this.sessionDir), this.version])
    // Log Baileys hanya level error dan tanpa JID: log debug-nya memuat isi pesan dan nomor.
    const logger = this.log.child({ mod: 'baileys' }, { level: 'error', redact: { paths: ['key', 'jid', 'attrs', 'msg', 'node'], remove: true } })
    const sock = makeWASocket({ version, auth: state, logger, browser: Browsers.ubuntu('Astria'), markOnlineOnConnect: false, syncFullHistory: false })
    this.sock = sock
    sock.ev.on('creds.update', saveCreds)
    sock.ev.on('connection.update', (update) => this.#onConnection(sock, update))
    sock.ev.on('messages.upsert', ({ messages, type }) => {
      if (type !== 'notify') return
      for (const msg of messages) {
        const payload = normalizeInbound(msg)
        if (payload) this.onInbound(payload)
      }
    })
  }

  async #onConnection(sock, { connection, lastDisconnect, qr }) {
    if (qr) {
      this.qr = qr
      this.log.info('qr_baru')
    }
    if (connection === 'open') {
      this.connected = true
      this.qr = null
      this.number = jidToDigits(sock.user?.id)
      this.log.info('terhubung')
      return
    }
    if (connection !== 'close' || sock !== this.sock) return
    this.connected = false
    if (this.stopping) return
    const code = lastDisconnect?.error?.output?.statusCode
    if (code === DisconnectReason.loggedOut) {
      // Sesi dicabut dari HP: hapus kredensial lama agar QR baru muncul.
      this.log.warn('keluar_dari_perangkat')
      this.number = null
      await rm(this.sessionDir, { recursive: true, force: true })
    } else {
      this.log.info({ code }, 'koneksi_putus')
    }
    setTimeout(() => this.start().catch((err) => this.log.error({ err: err.message }, 'sambung_ulang_gagal')), code === DisconnectReason.restartRequired ? 0 : 3000)
  }

  async send(digits, text) {
    if (!this.connected) throw new NotConnected()
    const sent = await this.sock.sendMessage(`${digits}@s.whatsapp.net`, { text })
    return sent?.key?.id ?? null
  }

  stop() {
    this.stopping = true
    this.sock?.end(undefined)
  }
}
