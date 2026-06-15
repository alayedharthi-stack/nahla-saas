/**
 * PR-B dashboard screenshots — vite preview + Playwright API mock from pr-b-evidence.json
 */
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(__dirname, '..')
const DASHBOARD = path.join(REPO, 'dashboard')
const OUT = path.join(REPO, 'docs', 'evidence', 'pr-b', 'screenshots')
const EVIDENCE = JSON.parse(
  fs.readFileSync(path.join(REPO, 'docs/evidence/pr-b/pr-b-evidence.json'), 'utf8'),
)
const { chromium } = require(path.join(DASHBOARD, 'node_modules', 'playwright'))

const TENANT_ID = EVIDENCE.full_api_trace.tenant_id
const branchIds = EVIDENCE.full_api_trace.branch_ids
const riyadhId = branchIds[0]
const listResponse = EVIDENCE.api_evidence['GET /operations-center/branches'].response

function contactsForBranch(branchId) {
  return EVIDENCE.full_api_trace.calls
    .filter((c) => c.method === 'POST' && c.path === `/operations-center/branches/${branchId}/contacts`)
    .map((c) => c.response)
}

function stepsForBranch(branchId) {
  return EVIDENCE.full_api_trace.calls
    .filter((c) => c.method === 'POST' && c.path.startsWith(`/operations-center/branches/${branchId}/escalation-steps`))
    .map((c) => c.response)
    .sort((a, b) => a.escalation_level - b.escalation_level)
}

function branchById(id) {
  return listResponse.branches.find((b) => b.id === id)
}

function installApiMock(page) {
  page.route('**/*', async (route) => {
    const req = route.request()
    const url = req.url()
    const method = req.method()

    if (url.includes('/operations-center/branches') && method === 'GET') {
      const m = url.match(/\/operations-center\/branches\/(\d+)(?:\/|$)/)
      if (m) {
        const id = Number(m[1])
        if (url.includes('/contacts')) {
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ contacts: contactsForBranch(id) }),
          })
        }
        if (url.includes('/escalation-steps')) {
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ steps: stepsForBranch(id) }),
          })
        }
        const branch = branchById(id)
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(branch),
        })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(listResponse),
      })
    }

    if (/\/auth\/|\/billing\/|\/notifications\/|\/support-access\/|\/whatsapp\//.test(url)) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, items: [], status: 'active', trial: false }),
      })
    }

    return route.continue()
  })
}

async function seedAuth(page) {
  await page.addInitScript(({ tenantId }) => {
    localStorage.setItem('nahla_auth', '1')
    localStorage.setItem('nahla_token', 'evidence-jwt-stub')
    localStorage.setItem('nahla_role', 'merchant')
    localStorage.setItem('nahla_email', 'evidence@test.nahla.ai')
    localStorage.setItem('nahla_tenant_id', String(tenantId))
    localStorage.setItem('nahla_user_id', '1')
    localStorage.setItem('nahla_store_name', 'متجر الأدلة')
    localStorage.setItem('nahla_api_base_override', 'http://127.0.0.1:8765')
  }, { tenantId: TENANT_ID })
}

function startPreview() {
  return new Promise((resolve, reject) => {
    const proc = spawn('npm', ['run', 'preview', '--', '--port', '4173', '--host', '127.0.0.1'], {
      cwd: DASHBOARD,
      shell: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let ready = false
    const onData = (buf) => {
      const s = buf.toString()
      if (!ready && s.includes('Local:')) {
        ready = true
        resolve(proc)
      }
    }
    proc.stdout.on('data', onData)
    proc.stderr.on('data', onData)
    proc.on('error', reject)
    setTimeout(() => {
      if (!ready) reject(new Error('vite preview timeout'))
    }, 60000)
  })
}

async function shot(page, name, url, waitMs = 1500) {
  await page.goto(url, { waitUntil: 'networkidle' })
  await page.waitForTimeout(waitMs)
  const file = path.join(OUT, `${name}.png`)
  await page.screenshot({ path: file, fullPage: true })
  console.log('screenshot:', file)
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const preview = await startPreview()
  const base = 'http://127.0.0.1:4173'

  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  installApiMock(page)
  await seedAuth(page)

  try {
    await shot(page, '01-operations-center-list', `${base}/operations-center`, 2000)
    await shot(page, '02-branch-info-tab', `${base}/operations-center/branches/${riyadhId}`, 2000)
    await page.getByRole('button', { name: 'جهات التواصل' }).click()
    await page.waitForTimeout(1000)
    await page.screenshot({ path: path.join(OUT, '03-branch-contacts-tab.png'), fullPage: true })
    console.log('screenshot:', path.join(OUT, '03-branch-contacts-tab.png'))
    await page.getByRole('button', { name: 'التصعيد' }).click()
    await page.waitForTimeout(1000)
    await page.screenshot({ path: path.join(OUT, '04-branch-escalation-tab.png'), fullPage: true })
    console.log('screenshot:', path.join(OUT, '04-branch-escalation-tab.png'))
  } finally {
    await browser.close()
    preview.kill('SIGTERM')
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
