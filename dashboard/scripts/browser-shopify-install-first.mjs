// Real Chromium form-navigation proof, isolated local server/profile; external traffic is aborted.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { chromium } from 'playwright'

const root = resolve('dist')
let received
const server = createServer(async (req, res) => {
  if (req.method === 'POST' && req.url === '/merchant/integrations/shopify/install/start') {
    let body = ''
    for await (const chunk of req) body += chunk
    received = { origin: req.headers.origin, referer: req.headers.referer, body: new URLSearchParams(body) }
    res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('Synthetic install accepted'); return
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
  const context = await browser.newContext()
  await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await context.addInitScript(value => localStorage.setItem('nahla_api_base_override', value), origin)
  const page = await context.newPage()
  await page.goto(origin + '/app/shopify?shop=demo.myshopify.com&hmac=synthetic-launch-hint')
  await page.waitForURL('**/merchant/integrations/shopify/install/start', { timeout: 20_000 })
  assert.equal(received.origin, origin, 'top-level form must send the expected Origin, not null')
  assert.equal(received.body.get('shop'), 'demo.myshopify.com')
  assert.match(received.body.get('browser'), /^[a-f0-9]{64}$/)
  assert.equal(received.referer, origin + '/', 'origin-only referrer contains no launch parameters')
  assert.equal(await page.textContent('body'), 'Synthetic install accepted')
  console.log('PASS real Chromium: scrubbed install page posts exact Origin and browser binding; no external traffic')
} finally {
  if (browser) await browser.close()
  await new Promise(done => server.close(done))
}
