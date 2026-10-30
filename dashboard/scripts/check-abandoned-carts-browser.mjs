// Synthetic loopback-only smoke test. No merchant session or provider transport.
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'

const root = resolve(process.cwd())
const server = await createServer({
  configFile: false, root, plugins: [react()],
  cacheDir: '.vite-cart-review',
  server: { host: '127.0.0.1', port: 0, strictPort: true },
})
let browser
try {
  await server.listen()
  const address = server.httpServer.address()
  assert(address && typeof address !== 'string')
  const origin = `http://127.0.0.1:${address.port}`
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort())
  await page.goto(origin + '/cart-preview.html')
  await page.getByRole('button', { name: 'عرض المراحل' }).first().click()
  await page.getByText('قُبل لدى المزود', { exact: true }).waitFor()
  assert.equal(await page.locator('[data-stage-state]').count(), 4)
  assert.equal(await page.locator('[data-stage-state=complete]').count(), 1)
  assert.equal(await page.locator('[aria-current=step]').count(), 1)
  await mkdir(resolve('cart-review-artifacts'), { recursive: true })
  await page.screenshot({ path: 'cart-review-artifacts/desktop.png', fullPage: true })
  await page.getByText('تفاصيل محاولات الإرسال', { exact: true }).click()
  await page.getByText('synthetic-81', { exact: false }).waitFor()
  await page.getByRole('textbox').fill('عطور')
  assert.equal(await page.locator('tbody tr').count(), 1)
  await page.getByRole('button', { name: 'عرض المراحل' }).click()
  await page.locator('[data-stage-state=failed]').waitFor()
  assert.equal(await page.locator('[data-stage-state=complete]').count(), 0)
  await page.getByRole('textbox').fill('ملابس')
  await page.getByRole('button', { name: 'عرض المراحل' }).click()
  await page.locator('[data-stage-state=unknown]').waitFor()
  assert.equal(await page.locator('[data-stage-state]').count(), 2)
  assert.equal(await page.locator('[data-stage-state=complete]').count(), 0)
  await page.getByRole('textbox').fill('أحذية')
  await page.getByRole('button', { name: 'عرض المراحل' }).click()
  await page.getByText('قُبل لدى المزود', { exact: true }).waitFor()
  await page.getByRole('textbox').fill('')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('details').evaluate(element => { element.open = false })
  await page.screenshot({ path: 'cart-review-artifacts/mobile.png', fullPage: true })
  assert.deepEqual(errors, [])
  console.log('PASS cart browser: 4/2 configured stages, current, details, search, failed/unknown, mobile; external traffic blocked')
} finally {
  if (browser) await browser.close()
  await server.close()
}
