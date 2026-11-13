import assert from 'node:assert/strict'
import { test } from 'node:test'
import { forwardInbound, InputError, jidToDigits, maskNumber, MAX_TEXT_CHARS, normalizeInbound, parseKirim, tokenMatches } from '../src/lib.js'

const TOKEN = 'uji-token-bukan-rahasia'

test('tokenMatches: hanya Bearer dengan token yang sama', () => {
  assert.equal(tokenMatches(`Bearer ${TOKEN}`, TOKEN), true)
  assert.equal(tokenMatches(`Bearer ${TOKEN}x`, TOKEN), false)
  assert.equal(tokenMatches(TOKEN, TOKEN), false)
  assert.equal(tokenMatches(undefined, TOKEN), false)
  assert.equal(tokenMatches('Bearer ', ''), false)
})

test('parseKirim: menerima ke 8-15 digit dan teks', () => {
  assert.deepEqual(parseKirim('{"ke":"6281200000001","teks":"halo"}'), { ke: '6281200000001', teks: 'halo' })
})

test('parseKirim: menolak input tidak valid', () => {
  const bad = [
    'bukan json',
    '[]',
    'null',
    '{"ke":"+6281200000001","teks":"x"}',
    '{"ke":"1234567","teks":"x"}',
    '{"ke":"1234567890123456","teks":"x"}',
    '{"ke":"6281200000001@s.whatsapp.net","teks":"x"}',
    '{"ke":"6281200000001","teks":"   "}',
    '{"ke":"6281200000001","teks":5}',
    `{"ke":"6281200000001","teks":"${'a'.repeat(MAX_TEXT_CHARS + 1)}"}`,
    '{"ke":"6281200000001","teks":"x","grup":true}',
  ]
  for (const raw of bad) assert.throws(() => parseKirim(raw), InputError, raw.slice(0, 40))
})

test('maskNumber menyamarkan bagian tengah', () => {
  assert.equal(maskNumber('6281234565678'), '62812****5678')
  assert.equal(maskNumber('12345678'), '12****78')
  assert.equal(maskNumber(null), null)
})

test('jidToDigits hanya untuk chat pribadi', () => {
  assert.equal(jidToDigits('6281200000001@s.whatsapp.net'), '6281200000001')
  assert.equal(jidToDigits('6281200000001:12@s.whatsapp.net'), '6281200000001')
  assert.equal(jidToDigits('120363000000000000@g.us'), null)
  assert.equal(jidToDigits('status@broadcast'), null)
})

const msg = (key, message = { conversation: '/status' }, ts = 1762790400) => ({
  key: { id: 'ABCD1234', fromMe: false, remoteJid: '6281200000001@s.whatsapp.net', ...key },
  message,
  messageTimestamp: ts,
})

test('normalizeInbound: pesan teks pribadi', () => {
  assert.deepEqual(normalizeInbound(msg({})), {
    id: 'ABCD1234',
    dari: '6281200000001',
    teks: '/status',
    waktu: '2025-11-10T16:00:00.000Z',
  })
  const ext = normalizeInbound(msg({}, { extendedTextMessage: { text: '/kartu IC-1' } }, { toNumber: () => 1762790400 }))
  assert.equal(ext.teks, '/kartu IC-1')
  assert.equal(ext.waktu, '2025-11-10T16:00:00.000Z')
  assert.equal(normalizeInbound(msg({}, { ephemeralMessage: { message: { conversation: '/bantuan' } } })).teks, '/bantuan')
  assert.equal(normalizeInbound(msg({}, { conversation: 'x'.repeat(5000) })).teks.length, MAX_TEXT_CHARS)
})

test('normalizeInbound: LID memakai remoteJidAlt atau senderPn', () => {
  const v7 = normalizeInbound(msg({ remoteJid: '123456789012345@lid', remoteJidAlt: '6281200000003@s.whatsapp.net' }))
  assert.equal(v7.dari, '6281200000003')
  const lid = normalizeInbound(msg({ remoteJid: '123456789012345@lid', senderPn: '6281200000002@s.whatsapp.net' }))
  assert.equal(lid.dari, '6281200000002')
  assert.equal(normalizeInbound(msg({ remoteJid: '123456789012345@lid' })), null)
})

test('normalizeInbound: abaikan diri sendiri, grup, status, dan non-teks', () => {
  assert.equal(normalizeInbound(msg({ fromMe: true })), null)
  assert.equal(normalizeInbound(msg({ remoteJid: '120363000000000000@g.us', participant: '6281200000001@s.whatsapp.net' })), null)
  assert.equal(normalizeInbound(msg({ remoteJid: 'status@broadcast' })), null)
  assert.equal(normalizeInbound(msg({ remoteJid: '120363000000000000@newsletter' })), null)
  assert.equal(normalizeInbound(msg({}, { imageMessage: { caption: 'foto' } })), null)
  assert.equal(normalizeInbound(msg({}, { conversation: '  ' })), null)
  assert.equal(normalizeInbound({}), null)
})

test('forwardInbound: ulang pada galat jaringan dan 5xx, berhenti pada 2xx/4xx', async () => {
  const calls = []
  const waits = []
  const replies = [new Error('ECONNREFUSED'), { status: 503 }, { status: 202 }]
  const fetchFn = async (url, init) => {
    calls.push({ url, init })
    const next = replies.shift()
    if (next instanceof Error) throw next
    return next
  }
  const payload = { id: 'A', dari: '6281200000001', teks: '/status', waktu: '2026-11-10T00:00:00Z' }
  const status = await forwardInbound('http://gw/internal/v1/whatsapp/masuk', TOKEN, payload, { fetchFn, wait: async (ms) => waits.push(ms) })
  assert.equal(status, 202)
  assert.equal(calls.length, 3)
  assert.deepEqual(waits, [1000, 2000])
  assert.equal(calls[0].init.headers.authorization, `Bearer ${TOKEN}`)
  assert.deepEqual(JSON.parse(calls[0].init.body), payload)

  const once = []
  const final = await forwardInbound('http://gw/x', TOKEN, payload, { fetchFn: async () => (once.push(1), { status: 401 }), wait: async () => {} })
  assert.equal(final, 401)
  assert.equal(once.length, 1)

  const always = await forwardInbound('http://gw/x', TOKEN, payload, { fetchFn: async () => ({ status: 500 }), attempts: 3, wait: async () => {} })
  assert.equal(always, 500)
})
