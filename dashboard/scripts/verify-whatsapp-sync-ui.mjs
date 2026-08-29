/**
 * Local UI verification for WhatsApp catalog sync — test data only.
 * Never points at production. Aborts any request to nahlah.ai / railway.
 */
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SHOTS = join(ROOT, 'tmp-whatsapp-sync-ui')
const MOCK_PORT = 18765
const VITE_PORT = 3017
const MOCK_ORIGIN = `http://127.0.0.1:${MOCK_PORT}`
const APP_ORIGIN = `http://127.0.0.1:${VITE_PORT}`
const PROD_HOST_RE = /(nahlah\.ai|railway\.app)$/i

mkdirSync(SHOTS, { recursive: true })

let scenario = 'blocked'
const postSyncHits = { count: 0 }
const statusHits = { count: 0 }
const unknownPaths = []

function cors(res, extra = {}) {
  const headers = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type, X-Tenant-ID',
    'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
    ...extra,
  }
  return headers
}

function json(res, code, body) {
  res.writeHead(code, cors(res, { 'Content-Type': 'application/json; charset=utf-8' }))
  res.end(JSON.stringify(body))
}

function empty(res, code) {
  res.writeHead(code, cors(res))
  res.end()
}

function verification() {
  return {
    lookup_fields: ['id', 'retailer_id', 'name', 'price', 'currency', 'availability'],
    identity_fields: ['id', 'retailer_id', 'name'],
    content_fields: ['price', 'currency', 'availability'],
    not_verified_fields: ['image_url', 'whatsapp_storefront_visibility'],
    note_ar: 'النشر يُختم فقط بعد تطابق السعر والعملة والتوفر مع Graph. وجود retailer_id وحده ليس إثبات مزامنة المحتوى.',
  }
}

function counts(overrides) {
  return {
    eligible: 3,
    pending: 0,
    syncing: 0,
    synced: 0,
    failed: 0,
    blocked: 0,
    skipped_ineligible: 0,
    pending_verification: 0,
    ...overrides,
  }
}

function syncStatus() {
  const base = {
    ok: true,
    tenant_id: 9,
    verification: verification(),
    failures: [],
    last_success_at: null,
    auto_sync_enabled: scenario !== 'auto_sync_off',
    auto_sync_flag: 'NAHLA_WHATSAPP_CATALOG_AUTO_SYNC',
  }
  if (scenario === 'auto_sync_off') {
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: 'queued',
      counts: counts({ pending: 2, synced: 1 }),
    }
  }
  if (scenario === 'poll_progress') {
    const progressed = statusHits.count >= 2
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: progressed ? 'published' : 'queued',
      last_success_at: progressed ? '2026-08-28T18:00:00+00:00' : null,
      counts: progressed
        ? counts({ synced: 3 })
        : counts({ pending: 2, synced: 1 }),
    }
  }
  if (scenario === 'poll_long_verify') {
    const progressed = statusHits.count >= 26
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: progressed ? 'published' : 'pending_verification',
      last_success_at: progressed ? '2026-08-28T18:00:00+00:00' : null,
      counts: progressed
        ? counts({ synced: 3 })
        : counts({ pending_verification: 2, synced: 1 }),
    }
  }
  if (scenario === 'blocked') {
    return {
      ...base,
      ready: false,
      blocker_code: 'catalog_disabled',
      message_ar: 'ربط الكتالوج بواتساب غير مفعّل.',
      action_ar: 'فعّل الكتالوج من إعدادات الربط.',
      phase: 'blocked',
      counts: counts(),
    }
  }
  if (scenario === 'permissions') {
    return {
      ...base,
      ready: false,
      blocker_code: 'access_token_missing',
      message_ar: 'صلاحيات Meta غير مكتملة أو منتهية.',
      action_ar: 'أعد ربط واتساب لتجديد صلاحيات الكتالوج.',
      phase: 'blocked',
      counts: counts({ eligible: 3 }),
    }
  }
  if (scenario === 'queued') {
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: 'queued',
      counts: counts({ pending: 2, synced: 1 }),
    }
  }
  if (scenario === 'syncing') {
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: 'syncing',
      counts: counts({ syncing: 1, synced: 1, pending: 0 }),
    }
  }
  if (scenario === 'pending_verification') {
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: 'pending_verification',
      counts: counts({ pending_verification: 2, synced: 1 }),
    }
  }
  if (scenario === 'failed') {
    return {
      ...base,
      ready: true,
      blocker_code: null,
      message_ar: null,
      action_ar: null,
      phase: 'needs_attention',
      counts: counts({ synced: 1, failed: 1, blocked: 1 }),
      failures: [
        { product_id: 201, title: 'قميص قطني أزرق', sync_status: 'failed', error_summary: 'meta_http_error' },
      ],
    }
  }
  return {
    ...base,
    ready: true,
    blocker_code: null,
    message_ar: null,
    action_ar: null,
    phase: 'published',
    last_success_at: '2026-08-28T18:00:00+00:00',
    counts: counts({ synced: 3 }),
  }
}

function entitlements() {
  const features = {
    nahla_template_library: true,
    meta_template_sync: true,
    autopilot_order_confirmation: true,
    autopilot_order_notifications: true,
    autopilot_shipping_tracking: true,
    autopilot_full: true,
    autopilot_customer_recovery: true,
    autopilot_cod_confirmation: true,
    cart_recovery_stage_2: true,
    cart_recovery_stage_3: true,
    cart_recovery_advanced_coupon: true,
    abandoned_cart_basic_coupon: true,
    advanced_coupon_types: true,
    campaign_customer_segments: true,
    campaign_ai_optimization: true,
    predictive_reorder: true,
    vip_rewards: true,
    back_in_stock_alerts: true,
    new_products_alerts: true,
    seasonal_smart_offers: true,
    salary_offers: true,
    seasonal_calendar: true,
    smart_discount_popup: true,
    meta_catalog_sync: true,
    zid_integration: false,
    future_integrations: false,
    ai_performance_dashboard: true,
    conversion_funnel: true,
    advanced_ai_analytics: false,
    revenue_breakdown: false,
    top_products_analytics: false,
    order_sources_analytics: false,
    store_brain_advanced: false,
    full_ai_customization: false,
    advanced_discount_rules: false,
    escalation_rules: false,
    team_handoff_queue: false,
  }
  return {
    plan: 'growth',
    plan_name_ar: 'النمو',
    billing_status: 'active',
    is_active: true,
    is_blocked: false,
    features,
    limits: { monthly_conversations: 5000, campaigns_per_month: 20 },
    usage: { monthly_conversations: 12, campaigns_per_month: 0 },
  }
}

function mockDiagnostics() {
  const linked = scenario !== 'blocked'
  return {
    catalog: {
      catalog_id_present: linked,
      catalog_id: linked ? 'CAT-TEST-001' : '',
      catalog_enabled: linked,
      whatsapp_connected: true,
    },
    products: {
      total: 3,
      with_effective_retailer_id: 3,
      without_effective_retailer_id: 0,
      coverage_pct: 100,
      source_breakdown: { salla: 2, nahla_native: 1 },
      dominant_source: 'salla',
    },
    readiness: {
      catalog_ready: linked,
      whatsapp_commerce_ready: linked,
    },
    import: { status: null, last_at: null, last_error: null, last_report: null, token_source: null },
    whatsapp_readiness: {
      ready: linked,
      checks: [
        { key: 'whatsapp_connected', ok: true },
        { key: 'phone_number_id', ok: true },
        { key: 'catalog_management', ok: scenario !== 'permissions' },
      ],
      missing_requirements: scenario === 'blocked'
        ? ['catalog_enabled']
        : scenario === 'permissions'
          ? ['catalog_management']
          : [],
    },
  }
}

const mock = createServer((req, res) => {
  const url = new URL(req.url || '/', MOCK_ORIGIN)
  if (req.method === 'OPTIONS') {
    empty(res, 204)
    return
  }
  if (url.pathname === '/__qa/scenario' && req.method === 'POST') {
    let raw = ''
    req.on('data', (c) => { raw += c })
    req.on('end', () => {
      try {
        scenario = JSON.parse(raw || '{}').scenario || 'blocked'
      } catch {
        scenario = 'blocked'
      }
      json(res, 200, { scenario })
    })
    return
  }
  if (url.pathname === '/merchant/catalog/whatsapp-sync/status') {
    statusHits.count += 1
    json(res, 200, syncStatus())
    return
  }
  if (url.pathname === '/merchant/catalog/whatsapp-sync' && req.method === 'POST') {
    postSyncHits.count += 1
    if (scenario === 'blocked' || scenario === 'permissions') {
      json(res, 409, {
        detail: {
          queued: false,
          phase: 'blocked',
          message_ar: syncStatus().message_ar,
          action_ar: syncStatus().action_ar,
        },
      })
      return
    }
    json(res, 200, {
      ok: true,
      queued: true,
      phase: 'queued',
      trigger: 'manual',
      enqueued: 3,
      eligible: 3,
    })
    return
  }
  if (url.pathname === '/merchant/catalog/diagnostics') {
    json(res, 200, mockDiagnostics())
    return
  }
  if (url.pathname === '/merchant/catalog/status') {
    json(res, 200, {
      tenant_id: 9,
      connection: {
        found: true,
        phone_id_tail: '0000',
        status: 'connected',
        catalog_enabled: scenario !== 'blocked',
        meta_catalog_id: scenario === 'blocked' ? null : 'CAT-TEST-001',
      },
      eligibility: { ok: scenario !== 'blocked' && scenario !== 'permissions', reason: scenario === 'blocked' ? 'catalog_disabled' : 'ok' },
      products_sample: [],
      coverage: { with_retailer_id: 3, without_retailer_id: 0, sample_size: 3 },
      advice: 'test',
    })
    return
  }
  if (url.pathname === '/merchant/catalog/channels') {
    json(res, 200, { channels: [] })
    return
  }
  if (url.pathname.startsWith('/merchant/catalog/products')) {
    json(res, 200, {
      rows: [],
      total: 0,
      limit: 50,
      offset: 0,
      coverage: { with_rid: 0, missing_rid: 0, published: 0, unpublished: 0, total: 0 },
    })
    return
  }
  if (url.pathname === '/billing/entitlements') {
    json(res, 200, entitlements())
    return
  }
  if (url.pathname === '/billing/status') {
    json(res, 200, {
      has_subscription: true,
      is_trial: false,
      trial_expired: false,
      subscription_expired: false,
      lifecycle_status: 'paid_active',
      plan: 'growth',
    })
    return
  }
  if (url.pathname === '/settings' || url.pathname === '/settings/') {
    json(res, 200, {
      store: { store_name: 'متجر تجريبي عام', store_logo_url: '' },
      ai: { store_ai_enabled: true, store_ai_mode: 'on' },
      notifications: {},
    })
    return
  }
  if (url.pathname === '/merchant/support-access') {
    json(res, 200, { enabled: false, expires_at: null })
    return
  }
  if (url.pathname.startsWith('/merchant/notifications')) {
    json(res, 200, { notifications: [] })
    return
  }
  if (url.pathname.startsWith('/merchant/access-requests')) {
    json(res, 200, { requests: [] })
    return
  }
  if (url.pathname === '/auth/session/refresh' && req.method === 'POST') {
    json(res, 200, {
      access_token: fakeJwt(),
      role: 'merchant',
      tenant_id: 9,
      user_id: 1,
    })
    return
  }
  unknownPaths.push(`${req.method} ${url.pathname}`)
  json(res, 200, { ok: true })
})

function jwtPart(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64')
}

function fakeJwt() {
  const header = jwtPart({ alg: 'none', typ: 'JWT' })
  const payload = jwtPart({
    sub: 'qa-local@example.test',
    role: 'merchant',
    tenant_id: 9,
    user_id: 1,
    exp: Math.floor(Date.now() / 1000) + 60 * 60 * 24 * 400,
  })
  return `${header}.${payload}.local-test`
}

async function setScenario(name) {
  scenario = name
  await fetch(`${MOCK_ORIGIN}/__qa/scenario`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario: name }),
  })
}

await new Promise((resolve, reject) => {
  mock.listen(MOCK_PORT, '127.0.0.1', resolve)
  mock.on('error', reject)
})

const viteBin = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js')
const vite = spawn(
  process.execPath,
  [viteBin, '--host', '127.0.0.1', '--port', String(VITE_PORT), '--strictPort'],
  {
    cwd: ROOT,
    env: {
      ...process.env,
      VITE_API_BASE: MOCK_ORIGIN,
      VITE_API_BASE_URL: MOCK_ORIGIN,
      BROWSER: 'none',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  },
)

let viteReady = false
let viteErr = ''
vite.stdout.on('data', (buf) => {
  const text = String(buf)
  if (text.includes('Local:') || text.includes(String(VITE_PORT))) viteReady = true
})
vite.stderr.on('data', (buf) => {
  const text = String(buf)
  viteErr += text
  if (text.includes('Local:') || text.includes(String(VITE_PORT))) viteReady = true
})

const started = Date.now()
while (!viteReady && Date.now() - started < 60000) {
  await new Promise((r) => setTimeout(r, 250))
}
if (!viteReady) {
  vite.kill()
  mock.close()
  throw new Error(`vite did not start on ${APP_ORIGIN}. stderr=${viteErr.slice(0, 1500)}`)
}

const healthStarted = Date.now()
let healthOk = false
let healthErr = ''
while (!healthOk && Date.now() - healthStarted < 20000) {
  try {
    const res = await fetch(`${APP_ORIGIN}/catalog`)
    healthOk = res.ok || res.status === 200
    if (!healthOk) healthErr = `status=${res.status}`
  } catch (err) {
    healthErr = String(err)
    await new Promise((r) => setTimeout(r, 250))
  }
}
if (!healthOk) {
  vite.kill()
  mock.close()
  throw new Error(`vite health check failed for ${APP_ORIGIN}/catalog: ${healthErr}`)
}

const browser = await chromium.launch({ headless: true })
const notes = []
const observations = []

async function openPage(viewport) {
  const context = await browser.newContext({
    viewport,
    locale: 'ar-SA',
  })
  await context.route(/nahlah\.ai|railway\.app/i, async (route) => {
    observations.push(`aborted_production_host=${new URL(route.request().url()).hostname}`)
    await route.abort()
  })
  const page = await context.newPage()
  await page.addInitScript(({ token, apiBase }) => {
    localStorage.setItem('nahla_auth', '1')
    localStorage.setItem('nahla_token', token)
    localStorage.setItem('nahla_role', 'merchant')
    localStorage.setItem('nahla_tenant_id', '9')
    localStorage.setItem('nahla_user_id', '1')
    localStorage.setItem('nahla_api_base_override', apiBase)
  }, { token: fakeJwt(), apiBase: MOCK_ORIGIN })
  return { context, page }
}

async function shot(page, name) {
  const file = join(SHOTS, `${name}.png`)
  await page.screenshot({ path: file, fullPage: true })
  notes.push(file.replace(/\\/g, '/'))
  return file
}

async function waitForSyncCard(page) {
  await page.getByRole('button', { name: 'مزامنة الكتالوج مع واتساب' }).waitFor({ timeout: 20000 })
}

try {
  const desktop = { width: 1280, height: 800 }
  const mobile = { width: 390, height: 844 }
  const states = ['blocked', 'permissions', 'queued', 'syncing', 'failed', 'published', 'pending_verification', 'auto_sync_off']

  for (const [label, viewport] of [['desktop', desktop], ['mobile', mobile]]) {
    for (const name of states) {
      await setScenario(name)
      const { context, page } = await openPage(viewport)
      await page.goto(`${APP_ORIGIN}/catalog`, { waitUntil: 'domcontentloaded', timeout: 60000 })
      await waitForSyncCard(page)
      const btn = page.getByRole('button', { name: 'مزامنة الكتالوج مع واتساب' })
      const box = await btn.boundingBox()
      const disabled = await btn.isDisabled()
      const bg = await btn.evaluate((el) => getComputedStyle(el).backgroundColor)
      observations.push(`${label}-${name}: top=${box && Math.round(box.y)} disabled=${disabled} bg=${bg}`)
      await shot(page, `${label}-${name}`)
      if (name === 'auto_sync_off') {
        const body = await page.locator('body').innerText()
        if (!body.includes('NAHLA_WHATSAPP_CATALOG_AUTO_SYNC')) {
          throw new Error('auto_sync_off did not show the flag stop reason')
        }
        if (!disabled) throw new Error('sync button should stay disabled when auto-sync flag is off')
      }
      if (name === 'queued') {
        const more = page.getByRole('button', { name: 'المزيد' })
        if (await more.count()) {
          await more.first().click()
          await page.getByRole('button', { name: 'استيراد منتجات من Meta إلى كتالوج نحلة' }).waitFor({ timeout: 5000 })
          const syncStillGreen = await btn.evaluate((el) => getComputedStyle(el).backgroundColor)
          observations.push(`${label}-queued-import-menu: syncBg=${syncStillGreen}`)
          await shot(page, `${label}-queued-import-menu`)
        }
      }
      await context.close()
    }
  }

  postSyncHits.count = 0
  await setScenario('queued')
  const { context, page } = await openPage(desktop)
  await page.goto(`${APP_ORIGIN}/catalog`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await waitForSyncCard(page)
  const btn = page.getByRole('button', { name: 'مزامنة الكتالوج مع واتساب' })
  await btn.evaluate((el) => { el.click(); el.click() })
  await btn.click()
  await page.getByText('دخلت المهمة الطابور', { exact: false }).waitFor({ timeout: 10000 })
  const body = await page.locator('body').innerText()
  const noticeOk = body.includes('دخلت المهمة الطابور') && body.includes('ليس تأكيد نشر')
  const falseSuccess = /مكتمل النشر|تم النشر بنجاح/.test(body)
  if (!noticeOk) throw new Error('queued click did not show queue copy')
  if (falseSuccess) throw new Error('queued click showed completed-publish copy')
  await shot(page, 'desktop-queued-after-click')
  observations.push(`post_sync_hits_after_double_click=${postSyncHits.count}`)
  observations.push(`button_disabled_after_click=${await btn.isDisabled()}`)
  await context.close()

  statusHits.count = 0
  postSyncHits.count = 0
  await setScenario('poll_progress')
  const pollSession = await openPage(desktop)
  await pollSession.page.goto(`${APP_ORIGIN}/catalog`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await waitForSyncCard(pollSession.page)
  const clicksBeforePoll = postSyncHits.count
  await pollSession.page.getByText('تطابق المحتوى في Meta', { exact: false }).waitFor({ timeout: 12000 })
  if (postSyncHits.count !== clicksBeforePoll) {
    throw new Error('status follow used the sync button instead of polling')
  }
  await shot(pollSession.page, 'desktop-poll-queued-to-published')
  observations.push(`poll_status_hits=${statusHits.count}`)
  observations.push(`poll_post_hits=${postSyncHits.count}`)
  await pollSession.context.close()

  statusHits.count = 0
  postSyncHits.count = 0
  await setScenario('poll_long_verify')
  const longSession = await openPage(desktop)
  await longSession.page.clock.install()
  await longSession.page.goto(`${APP_ORIGIN}/catalog`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await waitForSyncCard(longSession.page)
  await longSession.page.getByText('بانتظار التحقق', { exact: false }).waitFor({ timeout: 12000 })
  const clicksBeforeLongPoll = postSyncHits.count
  for (let i = 0; i < 36; i += 1) {
    await longSession.page.clock.fastForward(2500)
    await new Promise((resolve) => setTimeout(resolve, 40))
  }
  await longSession.page.getByText('تطابق المحتوى في Meta', { exact: false }).waitFor({ timeout: 8000 })
  if (postSyncHits.count !== clicksBeforeLongPoll) {
    throw new Error('long status follow used the sync button instead of polling')
  }
  await shot(longSession.page, 'desktop-poll-long-verify')
  observations.push(`long_poll_status_hits=${statusHits.count}`)
  observations.push(`long_poll_post_hits=${postSyncHits.count}`)
  await longSession.context.close()

  const report = {
    ok: true,
    shots: notes,
    observations,
    unknown_paths: [...new Set(unknownPaths)].slice(0, 40),
    mock: MOCK_ORIGIN,
    app: APP_ORIGIN,
    production_used: observations.some((x) => x.startsWith('aborted_production_host=')),
  }
  writeFileSync(join(SHOTS, 'index.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
} finally {
  await browser.close()
  vite.kill()
  mock.close()
}
