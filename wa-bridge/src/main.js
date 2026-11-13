// Jembatan WhatsApp Web untuk Astria Gateway (khusus uji): GET /qr, GET /status, POST /kirim, dan
// penerusan pesan masuk ke gateway. Lihat README untuk risiko klien tidak resmi.
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import pino from 'pino'
import QRCode from 'qrcode'
import { forwardInbound, InputError, MAX_BODY_BYTES, maskNumber, parseKirim, tokenMatches } from './lib.js'
import { NotConnected, WhatsApp } from './whatsapp.js'

const env = process.env
const log = pino({ level: env.LOG_LEVEL || 'info', base: { svc: 'wa-bridge' } })
// libsignal mencetak isi sesi Signal (termasuk kunci privat) lewat console. Bridge hanya memakai pino,
// jadi console dibungkam dan console.error dicatat sebagai teks pendek saja.
for (const name of ['log', 'info', 'warn', 'debug', 'trace']) console[name] = () => {}
console.error = (first) => log.warn({ pesan: typeof first === 'string' ? first.slice(0, 120) : 'objek' }, 'console')
const tokenFile = env.WA_BRIDGE_TOKEN_FILE
const token = tokenFile ? readFileSync(tokenFile, 'utf8').trim() : ''
if (!token) {
  log.fatal('WA_BRIDGE_TOKEN_FILE wajib berisi token')
  process.exit(1)
}
const gatewayUrl = (env.WA_BRIDGE_GATEWAY_URL || '').replace(/\/+$/, '')
const inboundUrl = gatewayUrl ? `${gatewayUrl}/internal/v1/whatsapp/masuk` : ''

const wa = new WhatsApp({
  dataDir: env.WA_BRIDGE_DATA_DIR || './data',
  log,
  onInbound: async (payload) => {
    const meta = { id: payload.id, dari: maskNumber(payload.dari) }
    if (!inboundUrl) return log.warn(meta, 'masuk_diabaikan_tanpa_gateway')
    const status = await forwardInbound(inboundUrl, token, payload)
    if (status >= 200 && status < 300) log.info({ ...meta, status }, 'masuk_diteruskan')
    else log.warn({ ...meta, status }, 'masuk_gagal_diteruskan')
  },
})

const SECURITY_HEADERS = { 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }

function send(res, status, body, type = 'application/json; charset=utf-8', extra = {}) {
  res.writeHead(status, { 'content-type': type, ...SECURITY_HEADERS, ...extra })
  res.end(type.startsWith('application/json') ? JSON.stringify(body) : body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    const onData = (chunk) => {
      size += chunk.length
      if (size <= MAX_BODY_BYTES) return chunks.push(chunk)
      // Sisa body dibuang tanpa disimpan agar 413 tetap sampai ke pemanggil.
      req.off('data', onData)
      req.resume()
      reject(new InputError('body_terlalu_besar'))
    }
    req.on('data', onData)
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function status() {
  return { terhubung: wa.connected, perlu_scan: !wa.connected && Boolean(wa.qr), nomor_tersamar: maskNumber(wa.number) }
}

// QR berganti sekitar 20 detik sekali, halaman dimuat ulang tiap 5 detik: PNG dibuat sekali per QR.
let qrCache = { qr: null, url: null }
async function qrImage(qr) {
  if (qrCache.qr !== qr) qrCache = { qr, url: await QRCode.toDataURL(qr, { margin: 2, width: 320 }) }
  return qrCache.url
}

async function qrPage() {
  let inner = '<p>Menunggu QR dari WhatsApp...</p>'
  if (wa.connected) inner = `<p>Terhubung ke ${maskNumber(wa.number) ?? 'nomor tidak diketahui'}.</p>`
  else if (wa.qr) inner = `<p>WhatsApp di HP uji: Perangkat tertaut, Tautkan perangkat, lalu pindai.</p><img alt="QR WhatsApp" src="${await qrImage(wa.qr)}">`
  const refresh = wa.connected ? '' : '<meta http-equiv="refresh" content="5">'
  return `<!doctype html><html lang="id"><head><meta charset="utf-8">${refresh}<title>Astria WA bridge</title></head><body style="font-family:sans-serif;text-align:center"><h1>Astria WA bridge (uji)</h1>${inner}</body></html>`
}

async function kirim(req, res) {
  if (!tokenMatches(req.headers.authorization, token)) return send(res, 401, { error: 'token_tidak_valid' })
  const { ke, teks } = parseKirim(await readBody(req))
  try {
    return send(res, 200, { id: await wa.send(ke, teks) })
  } catch (err) {
    if (err instanceof NotConnected) return send(res, 503, { error: 'belum_terhubung' })
    log.warn({ ke: maskNumber(ke), err: err?.constructor?.name }, 'kirim_gagal')
    return send(res, 502, { error: 'gagal_kirim' })
  }
}

const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://bridge').pathname
  try {
    if (req.method === 'GET' && path === '/status') return send(res, 200, status())
    if (req.method === 'GET' && path === '/qr') {
      const csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
      return send(res, 200, await qrPage(), 'text/html; charset=utf-8', { 'content-security-policy': csp, 'x-frame-options': 'DENY' })
    }
    if (req.method === 'POST' && path === '/kirim') return await kirim(req, res)
    return send(res, 404, { error: 'tidak_ada' })
  } catch (err) {
    if (err instanceof InputError) return send(res, err.message === 'body_terlalu_besar' ? 413 : 400, { error: err.message })
    log.error({ err: err?.constructor?.name, path }, 'galat_internal')
    if (!res.headersSent) send(res, 500, { error: 'galat_internal' })
  }
})
server.requestTimeout = 30_000
server.headersTimeout = 10_000

const port = Number(env.WA_BRIDGE_PORT || 3000)
const host = env.WA_BRIDGE_HOST || '127.0.0.1'
server.listen(port, host, () => log.info({ host, port, gateway: Boolean(inboundUrl) }, 'mendengar'))
wa.start().catch((err) => {
  log.fatal({ err: err.message }, 'whatsapp_gagal_mulai')
  process.exit(1)
})

function shutdown(signal) {
  log.info({ signal }, 'berhenti')
  wa.stop()
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(0), 5000).unref()
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
