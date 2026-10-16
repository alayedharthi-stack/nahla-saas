// Real Chromium form-navigation proof, isolated local server/profile; external traffic is aborted.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { chromium } from 'playwright'
import { createHash } from 'node:crypto'

const root = resolve('dist')
const pilotCode = 'f'.repeat(64) // synthetic admission credential, local server only
let received
let starts = 0
let redeems = 0
let releaseRedeem
let holdRedeem = false
const token = claims => `synthetic.${Buffer.from(JSON.stringify(claims)).toString('base64url')}.synthetic`
const oldClaims = { sub: 'neutral@example.test', role: 'merchant', tenant_id: 11, user_id: 11, jti: 'old', exp: Math.floor(Date.now() / 1000) + 259200 }
const oldToken = token(oldClaims)
const otherToken = token({ ...oldClaims, tenant_id: 33, user_id: 33, jti: 'changed' })
const shopifyToken = token({ ...oldClaims, tenant_id: 22, user_id: 22, jti: 'new', auth_source: 'shopify',
  shopify_session: 'synthetic-flow', shopify_shop: 'demo.myshopify.com', exp: Math.floor(Date.now() / 1000) + 300 })
// A second account's Shopify session for another shop (cross-tab switch).
const alphaToken = token({ ...oldClaims, tenant_id: 44, user_id: 44, jti: 'alpha', auth_source: 'shopify',
  shopify_session: 'synthetic-alpha', shopify_shop: 'alpha.myshopify.com', exp: Math.floor(Date.now() / 1000) + 300 })
const binding = createHash('sha256').update('11|11|merchant|old|').digest('hex')
const pending = (confirmed, key = binding) => ({ browser: 'b'.repeat(64), binding: key, shop: 'demo.myshopify.com',
  switchConfirmed: confirmed, expires: Date.now() + 900000 })
const server = createServer(async (req, res) => {
  if (req.method === 'POST' && req.url === '/merchant/integrations/shopify/install/start') {
    let body = ''
    for await (const chunk of req) body += chunk
    received = { origin: req.headers.origin, referer: req.headers.referer, body: new URLSearchParams(body) }
    starts++
    res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('Synthetic install accepted'); return
  }
  if (req.method === 'POST' && req.url === '/merchant/integrations/shopify/install/redeem') {
    redeems++
    for await (const _chunk of req) { /* Consume synthetic request. */ }
    if (holdRedeem) await new Promise(done => { releaseRedeem = done })
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ access_token: shopifyToken, shop: 'demo.myshopify.com' })); return
  }
  if (req.url.startsWith('/auth/') || req.url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{}'); return
  }
  const path = new URL(req.url, 'http://local.test').pathname
  let file = resolve(root, '.' + path)
  if (!file.startsWith(root + sep)) file = resolve(root, 'index.html')
  try {
    const bytes = await readFile(file)
    const type = { '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.html': 'text/html' }[extname(file)] || 'application/octet-stream'
    res.writeHead(200, { 'Content-Type': type }); res.end(bytes)
  } catch {
    res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(await readFile(resolve(root, 'index.html')))
  }
})
await new Promise(done => server.listen(0, '127.0.0.1', done))
const origin = `http://127.0.0.1:${server.address().port}`
let browser
try {
  browser = await chromium.launch({ headless: true })
  async function isolated({ existing = false, flow, session = oldToken, clock = false } = {}) {
    const context = await browser.newContext()
    // One clock for every tab of the context; it keeps running until fast-forwarded.
    if (clock) await context.clock.install()
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
    await context.addInitScript(({ origin, existing, session, flow }) => {
      localStorage.setItem('nahla_api_base_override', origin)
      if (!localStorage.getItem('synthetic_seeded')) {
        localStorage.setItem('synthetic_seeded', '1')
        if (existing) {
          localStorage.setItem('nahla_auth', '1'); localStorage.setItem('nahla_token', session)
          localStorage.setItem('nahla_tenant_id', '11'); localStorage.setItem('nahla_role', 'merchant')
        }
        if (flow) sessionStorage.setItem('nahla_shopify_install_browser_v1', JSON.stringify(flow))
      }
    }, { origin, existing, session, flow })
    return { context, page: await context.newPage() }
  }
  const { context, page } = await isolated()
  await page.goto(origin + '/app/shopify?shop=demo.myshopify.com&hmac=synthetic-launch-hint')
  await page.locator('[data-install-state="pilot_required"]').waitFor()
  assert.equal(starts, 0, 'no automatic public admission')
  await page.locator('input[name="pilot_code"]').fill(pilotCode)
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))
  assert.equal(storage.includes(pilotCode), false, 'pilot code stays out of durable browser storage')
  assert.equal(page.url().includes(pilotCode), false)
  await page.locator('[data-install-state="pilot_required"] button[type="submit"]').click()
  await page.waitForURL('**/merchant/integrations/shopify/install/start', { timeout: 20_000 })
  assert.equal(received.origin, origin, 'top-level form must send the expected Origin, not null')
  assert.equal(received.body.get('shop'), 'demo.myshopify.com')
  assert.match(received.body.get('browser'), /^[a-f0-9]{64}$/)
  assert.equal(received.body.get('pilot_code'), pilotCode)
  assert.equal(received.referer, origin + '/', 'origin-only referrer contains no launch parameters')
  assert.equal(await page.textContent('body'), 'Synthetic install accepted')
  console.log('PASS real Chromium: pilot required before POST; code absent from URL/storage; exact Origin and browser binding; no external traffic')
  await context.close()

  const cancel = await isolated({ existing: true })
  await cancel.page.goto(origin + '/app/shopify?shop=demo.myshopify.com')
  await cancel.page.locator('[data-install-state="confirm_switch"]').waitFor()
  assert.equal(starts, 1)
  assert.equal(await cancel.page.evaluate(() => localStorage.getItem('nahla_token')), oldToken)
  await cancel.page.getByRole('dialog').getByRole('button').nth(1).click()
  await cancel.page.waitForURL('**/overview')
  assert.equal(await cancel.page.evaluate(() => localStorage.getItem('nahla_token')), oldToken)
  assert.equal(starts, 1)
  await cancel.context.close()

  const changed = await isolated({ existing: true })
  await changed.page.goto(origin + '/app/shopify?shop=demo.myshopify.com')
  await changed.page.locator('[data-install-state="confirm_switch"]').waitFor()
  await changed.page.evaluate(value => localStorage.setItem('nahla_token', value), otherToken)
  await changed.page.getByRole('dialog').getByRole('button').first().click()
  await changed.page.locator('[data-install-state="session_changed"]').waitFor()
  assert.equal(starts, 1)
  assert.equal(await changed.page.evaluate(() => localStorage.getItem('nahla_token')), otherToken)
  await changed.context.close()

  const approve = await isolated({ existing: true })
  await approve.page.goto(origin + '/app/shopify?shop=demo.myshopify.com')
  await approve.page.locator('[data-install-state="confirm_switch"]').waitFor()
  await approve.page.getByRole('dialog').getByRole('button').first().click()
  await approve.page.locator('[data-install-state="pilot_required"]').waitFor()
  await approve.page.locator('input[name="pilot_code"]').fill(pilotCode)
  await approve.page.locator('[data-install-state="pilot_required"] button[type="submit"]').click()
  await approve.page.waitForURL('**/merchant/integrations/shopify/install/start')
  assert.equal(starts, 2)
  assert.equal(await approve.page.evaluate(() => localStorage.getItem('nahla_token')), oldToken)
  assert.equal(await approve.page.evaluate(() => JSON.parse(sessionStorage.getItem('nahla_shopify_install_browser_v1')).switchConfirmed), true)
  await approve.context.close()

  const pilotRace = await isolated({ existing: true })
  await pilotRace.page.goto(origin + '/app/shopify?shop=demo.myshopify.com')
  await pilotRace.page.locator('[data-install-state="confirm_switch"]').waitFor()
  await pilotRace.page.getByRole('dialog').getByRole('button').first().click()
  await pilotRace.page.locator('[data-install-state="pilot_required"]').waitFor()
  await pilotRace.page.locator('input[name="pilot_code"]').fill(pilotCode)
  await pilotRace.page.evaluate(value => localStorage.setItem('nahla_token', value), otherToken)
  await pilotRace.page.locator('[data-install-state="pilot_required"] button[type="submit"]').click()
  await pilotRace.page.locator('[data-install-state="session_changed"]').waitFor()
  assert.equal(starts, 2, 'typing a pilot code cannot rebaseline prior session consent')
  assert.equal(await pilotRace.page.evaluate(() => localStorage.getItem('nahla_token')), otherToken)
  await pilotRace.context.close()

  const complete = await isolated({ existing: true, flow: pending(false) })
  await complete.page.goto(origin + '/app/shopify#install_handle=' + 'a'.repeat(43))
  await complete.page.locator('[data-install-state="confirm_switch"]').waitFor()
  assert.equal(redeems, 0)
  assert.equal(await complete.page.evaluate(() => localStorage.getItem('nahla_token')), oldToken)
  await complete.page.getByRole('dialog').getByRole('button').first().click()
  await complete.page.waitForURL('**/overview')
  assert.equal(redeems, 1)
  assert.equal(await complete.page.evaluate(() => localStorage.getItem('nahla_token')), shopifyToken)
  await complete.context.close()

  holdRedeem = true
  const race = await isolated({ existing: true, flow: pending(true) })
  const request = race.page.waitForRequest(r => r.url().endsWith('/install/redeem'))
  await race.page.goto(origin + '/app/shopify#install_handle=' + 'a'.repeat(43))
  await request
  await race.page.evaluate(value => localStorage.setItem('nahla_token', value), otherToken)
  // Wait for the local server to receive the request before completing its response.
  for (let i = 0; !releaseRedeem && i < 100; i++) await new Promise(done => setTimeout(done, 10))
  assert.equal(typeof releaseRedeem, 'function')
  releaseRedeem()
  await race.page.locator('[data-install-state="session_changed"]').waitFor()
  assert.equal(await race.page.evaluate(() => localStorage.getItem('nahla_token')), otherToken)
  await race.context.close()
  console.log('PASS real Chromium: explicit session transition, cancel preservation, consent binding, and late-response session race')

  // Cross-tab account switch. Tabs of one profile share localStorage, so the
  // session-refresh loop of an already open tab (A) must follow the session
  // another tab (B) leaves behind: re-authenticate the shop of the CURRENT
  // Shopify session, and never a shop of an account that is gone.
  holdRedeem = false
  const shopifyNavigations = page => {
    const shops = []
    page.on('request', r => {
      if (r.isNavigationRequest() && r.frame() === page.mainFrame() && new URL(r.url()).pathname === '/app/shopify')
        shops.push(new URL(r.url()).searchParams.get('shop'))
    })
    return shops
  }
  async function openTabA(session) {
    const tabs = await isolated({ existing: true, session, clock: true })
    const ready = tabs.page.waitForEvent('console', m => m.text().startsWith('[auth] session bootstrap'))
    await tabs.page.goto(origin + '/overview')
    await ready // ProtectedRoute accepted the session and installed its refresh loop
    return { ...tabs, shops: shopifyNavigations(tabs.page) }
  }
  async function installInTabB(context, key) {
    const tabB = await context.newPage()
    await tabB.addInitScript(flow => {
      if (!sessionStorage.getItem('synthetic_tab_b')) {
        sessionStorage.setItem('synthetic_tab_b', '1')
        sessionStorage.setItem('nahla_shopify_install_browser_v1', JSON.stringify(flow))
      }
    }, pending(false, key))
    await tabB.goto(origin + '/app/shopify#install_handle=' + 'a'.repeat(43))
    await tabB.locator('[data-install-state="confirm_switch"]').waitFor()
    await tabB.getByRole('dialog').getByRole('button').first().click()
    await tabB.waitForURL('**/overview')
    return tabB
  }
  const expire = tab => tab.context.clock.fastForward('06:00') // beyond the five-minute Shopify sessions
  const alphaKey = createHash('sha256').update('44|44|merchant|alpha|').digest('hex')

  // 1. A holds shop alpha's session; B completes the real install-first handoff for demo.
  const switched = await openTabA(alphaToken)
  const redeemsBefore = redeems
  await installInTabB(switched.context, alphaKey)
  assert.equal(redeems, redeemsBefore + 1)
  assert.equal(await switched.page.evaluate(() => localStorage.getItem('nahla_token')), shopifyToken)
  await expire(switched)
  await switched.page.waitForURL('**/app/shopify**')
  assert.deepEqual(switched.shops, ['demo.myshopify.com'], 'tab A re-authenticates the current shop, not alpha')
  await switched.context.close()

  // 2. A holds alpha's session; B signs out (the keys logout() removes). A's next
  //    15 s check must not start alpha's re-auth. Then B installs demo: the same
  //    loop, still running, now follows demo.
  const ended = await openTabA(alphaToken)
  const tabB = await ended.context.newPage()
  await tabB.goto(origin + '/landing')
  await tabB.evaluate(() => {
    for (const k of ['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email', 'nahla_tenant_id', 'nahla_user_id', 'nahla_store_name'])
      localStorage.removeItem(k)
  })
  await ended.context.clock.fastForward('00:20')
  await ended.page.waitForTimeout(2000)
  assert.deepEqual(ended.shops, [], 'no Shopify re-auth for a signed-out account')
  assert.equal(new URL(ended.page.url()).pathname, '/overview')
  await tabB.evaluate(value => { localStorage.setItem('nahla_auth', '1'); localStorage.setItem('nahla_token', value) }, oldToken)
  await installInTabB(ended.context, binding)
  await expire(ended)
  await ended.page.waitForURL('**/app/shopify**')
  assert.deepEqual(ended.shops, ['demo.myshopify.com'])
  await ended.context.close()

  // 3. A holds a password session (12 h refresh); B installs demo's five-minute session.
  const password = await openTabA(oldToken)
  await installInTabB(password.context, binding)
  await expire(password)
  await password.page.waitForURL('**/app/shopify**')
  assert.deepEqual(password.shops, ['demo.myshopify.com'], 'an open password tab follows a Shopify session from another tab')
  await password.context.close()
  console.log('PASS real Chromium: two tabs, shared storage and clock; refresh loop follows cross-tab account switch, sign-out and session-kind change')
} finally {
  if (releaseRedeem) releaseRedeem()
  if (browser) await browser.close()
  await new Promise(done => server.close(done))
}
