// Controlled-input -> outgoing JSON -> saved readback. Synthetic local server only.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { chromium } from 'playwright'

const root = resolve(process.env.NAHLA_COUPON_TEST_DIST || 'dist')
let saved = [], received = [], edits = []
const json = (res, value, status = 200) => {
  res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value))
}
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname
  if (path === '/coupons' && req.method === 'POST') {
    let body = ''; for await (const part of req) body += part
    const payload = JSON.parse(body); received.push(payload)
    saved.push({ ...payload, id: saved.length + 1, source_type: 'manual', origin: 'manual',
      active: true, usages: 0, sync_status: 'local' })
    json(res, saved.at(-1), 201); return
  }
  if (path === '/coupons/1' && req.method === 'PATCH') {
    let body = ''; for await (const part of req) body += part
    const payload = JSON.parse(body); edits.push(payload)
    if (edits.length === 1) {
      json(res, { updated: false, accepted: true, sync_status: 'failed', sync_error: 'provider_terms_unconfirmed' })
      return
    }
    saved[0] = { ...saved[0], value: payload.value, expires: '2027-03-01T21:00:00Z', sync_status: 'synced' }
    json(res, { updated: true, accepted: true, sync_status: 'synced' }); return
  }
  if (path === '/coupons' && !String(req.headers.accept).includes('text/html')) {
    json(res, { coupons: saved, rules: [], levels: [], global_defaults: {}, ai_policy: {} }); return
  }
  if (path === '/integrations/salla/status') { json(res, { api_sync_enabled: false }); return }
  if (path.startsWith('/auth/')) {
    json(res, { role: 'merchant', tenant_id: 11, user_id: 11, is_active: true }); return
  }
  if (!String(req.headers.accept).includes('text/html') && !extname(path)) { json(res, {}); return }
  let file = resolve(root, '.' + path)
  if (!file.startsWith(root + sep)) file = resolve(root, 'index.html')
  try {
    const bytes = await readFile(file)
    res.writeHead(200, { 'Content-Type': { '.js': 'text/javascript', '.css': 'text/css' }[extname(file)] || 'text/html' })
    res.end(bytes)
  } catch { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(await readFile(resolve(root, 'index.html'))) }
})
await new Promise(done => server.listen(0, '127.0.0.1', done))
const origin = `http://127.0.0.1:${server.address().port}`
let browser
try {
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) })
  for (const [timezoneId, code, expected] of [
    ['UTC', 'DATEUTC', '2027-03-02T00:30:00.000Z'],
    ['Asia/Riyadh', 'DATERIYADH', '2027-03-01T21:30:00.000Z'],
  ]) {
    saved = []; received = []
    const context = await browser.newContext({ timezoneId })
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
    await context.addInitScript(({ origin }) => {
      const claims = { sub: 'synthetic@example.test', role: 'merchant', tenant_id: 11, user_id: 11, exp: Math.floor(Date.now() / 1000) + 72 * 3600 }
      const jwt = `synthetic.${btoa(JSON.stringify(claims))}.synthetic`
      localStorage.setItem('nahla_api_base_override', origin)
      localStorage.setItem('nahla_auth', '1'); localStorage.setItem('nahla_token', jwt)
      localStorage.setItem('nahla_tenant_id', '11'); localStorage.setItem('nahla_role', 'merchant')
    }, { origin })
    const page = await context.newPage()
    await page.goto(origin + '/coupons')
    await page.locator('button.btn-primary').first().click()
    const form = page.locator('form').filter({ has: page.locator('input[type=date]') })
    await form.locator('input[placeholder="NH33K7P9"]').fill(code)
    await form.locator('input[type=number]').first().fill('1')
    await form.locator('input[type=date]').fill('2027-03-02')
    await form.locator('input[type=time]').fill('00:30')
    await form.locator('input[type=time]').press('Tab')
    // Change another controlled field to force an additional render.
    await form.locator('input[type=number]').first().fill('2')
    assert.equal(await form.locator('input[type=date]').inputValue(), '2027-03-02')
    assert.equal(await form.locator('input[type=time]').inputValue(), '00:30')
    await form.locator('button[type=submit]').click()
    await form.waitFor({ state: 'detached' })
    assert.equal(received.length, 1, 'one successful submission only')
    assert.equal(received[0].expires, expected)
    const readback = await page.request.get(origin + '/coupons', { headers: { Accept: 'application/json' } })
    assert.equal((await readback.json()).coupons[0].expires, expected)
    await page.reload()
    assert.equal(saved.length, 1, 'reload creates nothing')
    assert.equal(saved[0].expires, expected)
    await context.close()
    console.log(JSON.stringify({ timezoneId, controlled_date: true, controlled_time: true, payload_matches: true, saved_readback_matches: true }))
  }
  saved = [{ id: 1, code: 'EDITBOUND', type: 'percentage', value: '1',
    expires: '2027-02-28T21:00:00Z', source_type: 'manual', origin: 'manual',
    salla_coupon_id: '101', active: true, usages: 0, sync_status: 'synced' }]
  edits = []
  const context = await browser.newContext({ timezoneId: 'Asia/Riyadh' })
  await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await context.addInitScript(({ origin }) => {
    const claims = { sub: 'synthetic@example.test', role: 'merchant', tenant_id: 11, user_id: 11, exp: Math.floor(Date.now() / 1000) + 72 * 3600 }
    localStorage.setItem('nahla_api_base_override', origin)
    localStorage.setItem('nahla_auth', '1')
    localStorage.setItem('nahla_token', `synthetic.${btoa(JSON.stringify(claims))}.synthetic`)
    localStorage.setItem('nahla_tenant_id', '11')
    localStorage.setItem('nahla_role', 'merchant')
  }, { origin })
  const page = await context.newPage()
  await page.goto(origin + '/coupons')
  await page.locator('tr').filter({ hasText: 'EDITBOUND' }).locator('button').filter({ hasText: /تعديل/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('#coupon-terms-value').fill('2.5')
  await dialog.locator('#coupon-terms-expires').fill('2027-03-02')
  await dialog.locator('button[type=submit]').click()
  await dialog.getByRole('status').waitFor()
  assert.equal(await dialog.isVisible(), true, 'failed provider confirmation keeps editor open')
  assert.deepEqual(edits[0], { value: '2.5', expires: '2027-03-02' })
  assert.equal(saved[0].value, '1', 'failed provider confirmation leaves local value unchanged')
  await dialog.locator('button[type=submit]').click()
  await dialog.waitFor({ state: 'detached' })
  assert.deepEqual(edits[1], edits[0])
  assert.equal(saved[0].value, '2.5')
  assert.equal(saved[0].expires, '2027-03-01T21:00:00Z')
  await context.close()
  console.log('COUPON_TERMS_BROWSER_PASS failed readback retained editor; confirmed readback closed it')
  console.log('COUPON_EXPIRY_BROWSER_PASS local synthetic persistence; backend persistence tested separately')
} finally {
  if (browser) await browser.close()
  await new Promise(done => server.close(done))
}
