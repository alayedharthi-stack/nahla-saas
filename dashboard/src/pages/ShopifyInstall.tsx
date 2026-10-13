import { useEffect, useState } from 'react'
import { getApiBase, getSessionBindingKey, getToken, persistShopifySession } from '../auth'
import { isShopifyConnectionUiEnabled } from '../lib/platformFeatureFlags'
import { installReturn } from '../lib/shopifyInstallBoot'

const KEY = 'nahla_shopify_install_browser_v1'
const enabled = isShopifyConnectionUiEnabled() && import.meta.env.VITE_SHOPIFY_INSTALL_FIRST_ENABLED === 'true'
const shopRE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com$/
const base = () => `${getApiBase()}/merchant/integrations/shopify/install`
let boot: Promise<string> | undefined
let redeeming: Promise<string> | undefined
let confirmationBinding: string | undefined
let confirmationShop = ''

async function sessionDigest(binding = getSessionBindingKey()) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(binding))
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('')
}

function pending() {
  const raw = JSON.parse(sessionStorage.getItem(KEY) || 'null')
  if (!raw || typeof raw.browser !== 'string' || !/^[a-f0-9]{64}$/.test(raw.browser)
      || typeof raw.binding !== 'string' || typeof raw.shop !== 'string' || !shopRE.test(raw.shop)
      || typeof raw.expires !== 'number' || raw.expires <= Date.now()) throw new Error('expired')
  return raw as { browser: string; binding: string; shop: string; switchConfirmed: boolean; expires: number }
}

async function redeem(totp?: string): Promise<string> {
  try {
    const p = pending()
    const binding = getSessionBindingKey()
    if (p.binding !== await sessionDigest(binding) || binding !== getSessionBindingKey()) return 'session_changed'
    if (getToken() && p.switchConfirmed !== true) return 'session_changed'
    const res = await fetch(`${base()}/redeem`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'omit', cache: 'no-store',
      body: JSON.stringify({ browser: p.browser, handle: installReturn.handle, totp }),
      signal: AbortSignal.timeout(25_000),
    })
    const data = await res.json()
    if (!res.ok) return ['totp_required', 'totp_invalid', 'totp_locked'].includes(data.error) ? data.error : 'refused'
    if (binding !== getSessionBindingKey() || data.shop !== p.shop) return 'session_changed'
    persistShopifySession(data.access_token, binding)
    sessionStorage.removeItem(KEY)
    window.location.replace('/overview')
    return 'working'
  } catch { return 'refused' }
}

function singleRedeem(totp?: string) {
  if (!redeeming) redeeming = redeem(totp).finally(() => { redeeming = undefined })
  return redeeming
}

async function begin(confirmedBinding?: string): Promise<string> {
  if (!enabled) return 'disabled'
  if (installReturn.error) return 'refused'
  try {
    const isReturn = /^[A-Za-z0-9_-]{43}$/.test(installReturn.handle)
    if (!isReturn && !shopRE.test(installReturn.shop)) return 'refused'
    const binding = getSessionBindingKey()
    if (confirmedBinding !== undefined && confirmedBinding !== binding) return 'session_changed'
    const hashed = await sessionDigest(binding)
    if (binding !== getSessionBindingKey()) return 'session_changed'
    const p = isReturn ? pending() : undefined
    if (p && p.binding !== hashed) return 'session_changed'
    if (getToken() && p?.switchConfirmed !== true && confirmedBinding === undefined) {
      confirmationBinding = binding
      confirmationShop = p?.shop || installReturn.shop
      return 'confirm_switch'
    }
    if (p) {
      p.switchConfirmed = true
      sessionStorage.setItem(KEY, JSON.stringify(p))
      return singleRedeem()
    }
    const browser = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('')
    sessionStorage.setItem(KEY, JSON.stringify({ browser, binding: hashed, shop: installReturn.shop,
      switchConfirmed: !getToken() || confirmedBinding === binding, expires: Date.now() + 900_000 }))
    // Top-level POST establishes the HttpOnly API cookie; no third-party cookie dependency.
    const form = document.createElement('form')
    form.method = 'POST'
    form.action = `${base()}/start`
    for (const [name, value] of Object.entries({ shop: installReturn.shop, browser })) {
      const input = document.createElement('input')
      input.type = 'hidden'; input.name = name; input.value = value
      form.appendChild(input)
    }
    document.body.appendChild(form)
    // The URL has already been scrubbed. Navigate-mode POST needs an Origin:
    // no-referrer would produce Origin:null and must not weaken the server check.
    const policy = document.createElement('meta')
    policy.name = 'referrer'; policy.content = 'strict-origin'
    document.head.appendChild(policy)
    form.submit()
    return 'working'
  } catch { return 'refused' }
}

export default function ShopifyInstall() {
  const [state, setState] = useState('working')
  const [otp, setOtp] = useState('')
  useEffect(() => {
    let live = true
    boot ??= begin()
    void boot.then(value => { if (live) setState(value) })
    return () => { live = false }
  }, [])
  const mfa = state === 'totp_required' || state === 'totp_invalid'
  return <main dir="rtl" data-install-state={state} className="max-w-lg mx-auto my-20 p-6 space-y-4">
    {state === 'confirm_switch' && <section role="dialog" aria-label="تأكيد تبديل الجلسة" className="space-y-3">
      <p>لديك جلسة حالية في نحلة. المتابعة ستستبدلها بجلسة مالك متجر Shopify بعد التحقق من هويته، ولن تربط المتجر بحسابك الحالي.</p>
      <p dir="ltr">{confirmationShop}</p>
      <button className="btn-primary" onClick={() => {
        const expected = confirmationBinding
        if (expected === undefined || expected !== getSessionBindingKey()) { setState('session_changed'); return }
        setState('working')
        void begin(expected).then(setState)
      }}>متابعة وتبديل الجلسة</button>
      <button className="btn-secondary" onClick={() => {
        sessionStorage.removeItem(KEY)
        window.location.replace('/overview')
      }}>إلغاء والاحتفاظ بالجلسة الحالية</button>
    </section>}
    <h1 className="text-xl font-bold">ربط متجر Shopify بنحلة</h1>
    <p role="status" hidden={state === 'confirm_switch'}>{state === 'working' ? 'جارٍ التحقق من المتجر وفتح حسابه في نحلة…'
      : state === 'disabled' ? 'الربط مع Shopify غير متاح حاليًا.'
      : mfa ? 'أدخل رمز التحقق الثنائي لحساب نحلة.'
      : state === 'session_changed' ? 'تغيّر الحساب في هذا المتصفح. افتح التطبيق مجددًا من Shopify.'
      : 'تعذّر إكمال الدخول بأمان. افتح التطبيق مجددًا من Shopify بحساب مالك المتجر.'}</p>
    {mfa && <form onSubmit={event => {
      event.preventDefault(); setState('working')
      void singleRedeem(otp).then(setState)
    }} className="space-y-3">
      <label className="block">رمز التحقق
        <input autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" required maxLength={6}
          value={otp} onChange={e => setOtp(e.target.value)} className="input block" />
      </label>
      <button className="btn-primary" type="submit">متابعة</button>
    </form>}
  </main>
}
