/**
 * Capture preview evidence from the real WaBubblePreview + resolveOrderUpdatePreview,
 * using the same API fixture as test_preview_follows_pending_image_over_active_text.
 *
 * Run: npx --yes tsx@4 scripts/render-order-updates-preview-evidence.mts
 */
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { createServer } from 'node:net'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = join(ROOT, '..')
const OUT_DIR = join(REPO_ROOT, 'docs', 'evidence')
const OUT_FILE = join(OUT_DIR, 'pr976-order-confirmation-preview-r3.png')

async function pickFreePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = createServer()
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        reject(new Error('could not allocate preview evidence port'))
        return
      }
      const port = address.port
      server.close(err => (err ? reject(err) : resolve(port)))
    })
  })
}

const VITE_PORT = await pickFreePort()
const APP_URL = `http://localhost:${VITE_PORT}/evidence-order-updates-preview.html`

function waitForViteReady(child: ReturnType<typeof spawn>, timeoutMs = 90000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout waiting for vite dev server')), timeoutMs)
    const onData = (chunk: Buffer | string) => {
      const text = String(chunk)
      if (text.includes('ready in') || text.includes('Local:')) {
        clearTimeout(timer)
        child.stdout?.off('data', onData)
        child.stderr?.off('data', onData)
        resolve()
      }
      if (text.toLowerCase().includes('error')) process.stderr.write(text)
    }
    child.stdout?.on('data', onData)
    child.stderr?.on('data', onData)
  })
}

const viteBin = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js')
const vite = spawn(process.execPath, [viteBin, '--port', String(VITE_PORT), '--strictPort'], {
  cwd: ROOT,
  stdio: 'pipe',
  env: { ...process.env, BROWSER: 'none' },
})

try {
  await waitForViteReady(vite)
  await new Promise(resolve => setTimeout(resolve, 800))
  await mkdir(OUT_DIR, { recursive: true })

  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 420, height: 520 } })
  await page.goto(APP_URL)
  await page.waitForSelector('[data-testid="wa-bubble-preview"] img', { timeout: 20000 })
  await page.waitForFunction(() => {
    const img = document.querySelector('[data-testid="wa-bubble-preview"] img') as HTMLImageElement | null
    return Boolean(img && img.complete && img.naturalWidth > 0)
  }, { timeout: 30000 })
  await page.waitForTimeout(400)
  await page.locator('[data-testid="wa-bubble-preview"]').screenshot({ path: OUT_FILE })
  await browser.close()
  console.log(`render-order-updates-preview-evidence: wrote ${OUT_FILE}`)
} finally {
  if (!vite.killed) {
    vite.kill('SIGTERM')
    await new Promise(resolve => setTimeout(resolve, 500))
  }
}
