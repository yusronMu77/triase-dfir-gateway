// Bagian murni jembatan (tanpa jaringan dan tanpa Baileys) supaya bisa diuji dengan `node --test`.
import { createHash, timingSafeEqual } from 'node:crypto'

export const MAX_BODY_BYTES = 16 * 1024
export const MAX_TEXT_CHARS = 4000
const DIGITS = /^[0-9]{8,15}$/
const PERSONAL_JID = /^([0-9]{8,15})(?::[0-9]+)?@s\.whatsapp\.net$/

export class InputError extends Error {}

/** Bearer token dibandingkan dalam waktu konstan; hash dulu agar panjang token tidak bocor lewat waktu. */
export function tokenMatches(header, token) {
  if (!token || typeof header !== 'string' || !header.startsWith('Bearer ')) return false
  const digest = (value) => createHash('sha256').update(value, 'utf8').digest()
  return timingSafeEqual(digest(header.slice(7)), digest(token))
}

/** Body `POST /kirim` -> `{ke, teks}`; selain itu InputError (400). */
export function parseKirim(raw) {
  let body
  try {
    body = JSON.parse(raw)
  } catch {
    throw new InputError('json_tidak_valid')
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new InputError('body_harus_objek')
  const extra = Object.keys(body).filter((k) => k !== 'ke' && k !== 'teks')
  if (extra.length) throw new InputError('field_tidak_dikenal')
  const { ke, teks } = body
  if (typeof ke !== 'string' || !DIGITS.test(ke)) throw new InputError('ke_harus_8_15_digit')
  if (typeof teks !== 'string' || !teks.trim()) throw new InputError('teks_kosong')
  if (teks.length > MAX_TEXT_CHARS) throw new InputError('teks_terlalu_panjang')
  return { ke, teks }
}

/** 6281234565678 -> 62812****5678. Nomor penuh tidak pernah dicatat atau ditampilkan. */
export function maskNumber(digits) {
  if (typeof digits !== 'string' || !digits) return null
  if (digits.length <= 8) return `${digits.slice(0, 2)}****${digits.slice(-2)}`
  return `${digits.slice(0, 5)}****${digits.slice(-4)}`
}

/** JID chat pribadi (`628...@s.whatsapp.net`, boleh dengan `:device`) -> digit; selain itu null. */
export function jidToDigits(jid) {
  const match = typeof jid === 'string' ? PERSONAL_JID.exec(jid) : null
  return match ? match[1] : null
}

function textOf(message) {
  const m = message?.ephemeralMessage?.message ?? message
  const text = m?.conversation ?? m?.extendedTextMessage?.text
  return typeof text === 'string' ? text : null
}

function isoTime(ts) {
  const seconds = typeof ts?.toNumber === 'function' ? ts.toNumber() : Number(ts)
  return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toISOString() : new Date().toISOString()
}

/**
 * Pesan Baileys -> `{id, dari, teks, waktu}` untuk gateway, atau null bila diabaikan:
 * dari diri sendiri, grup, status broadcast, newsletter, dan pesan tanpa teks.
 * Akun beralamat LID memakai `senderPn` (nomor asli) bila Baileys menyertakannya.
 */
export function normalizeInbound(msg) {
  const key = msg?.key
  if (!key || key.fromMe || typeof key.id !== 'string' || !key.id) return null
  const jid = key.remoteJid
  const dari = jidToDigits(jid) ?? (typeof jid === 'string' && jid.endsWith('@lid') ? jidToDigits(key.senderPn) : null)
  const teks = textOf(msg.message)
  if (!dari || teks === null || !teks.trim()) return null
  return { id: key.id, dari, teks: teks.slice(0, MAX_TEXT_CHARS), waktu: isoTime(msg.messageTimestamp) }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Teruskan satu pesan masuk ke gateway. Galat jaringan, 429, dan 5xx diulang dengan jeda naik;
 * 2xx dan 4xx lain final (gateway sudah dedupe per id). Mengembalikan status HTTP terakhir atau 0.
 */
export async function forwardInbound(url, token, payload, { fetchFn = fetch, attempts = 4, baseDelayMs = 1000, wait = sleep } = {}) {
  let status = 0
  for (let n = 0; n < attempts; n++) {
    if (n > 0) await wait(baseDelayMs * 2 ** (n - 1))
    try {
      const resp = await fetchFn(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(30_000),
      })
      status = resp.status
      if (status !== 429 && status < 500) return status
    } catch {
      status = 0
    }
  }
  return status
}
