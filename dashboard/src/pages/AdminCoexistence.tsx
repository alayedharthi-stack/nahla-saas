import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Copy,
  Check,
  Edit3,
  Hammer,
  Inbox,
  KeyRound,
  Link,
  MessageSquare,
  Phone,
  PlugZap,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
  Smartphone,
  Store,
  TestTube2,
  User,
  Webhook,
  XCircle,
  Zap,
} from 'lucide-react'
import {
  adminApi,
  type CoexistenceRequest,
  type CoexistenceActivatePayload,
  type CoexistenceTestWebhookResult,
  type CoexistenceVerifyWebhookResult,
  type CoexistenceAutoConfigureResult,
  type CoexistenceDiagnoseResult,
} from '../api/admin'

// ── Status badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    request_submitted:  { label: 'طلب جديد',        cls: 'bg-amber-100 text-amber-700' },
    pending_activation: { label: 'جارٍ التفعيل',    cls: 'bg-blue-100 text-blue-700' },
    action_required:    { label: 'يحتاج تدخل',      cls: 'bg-red-100 text-red-700' },
    connected:          { label: 'مفعّل',            cls: 'bg-emerald-100 text-emerald-700' },
    not_connected:      { label: 'غير مربوط',       cls: 'bg-slate-100 text-slate-500' },
  }
  const { label, cls } = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>
      {label}
    </span>
  )
}

// ── Canonical 360dialog webhook URLs ──────────────────────────────────────────
// Channel = customer messages + statuses (always required).
// Coexistence = device_sync, pairing, phone_app_handover, mobile_app state, …
// Status = account / channel health callbacks.

const CHANNEL_WEBHOOK_URL     = 'https://api.nahlah.ai/webhook/whatsapp/360dialog'
const COEXISTENCE_WEBHOOK_URL = 'https://api.nahlah.ai/webhook/whatsapp/360dialog/coexistence'
const STATUS_WEBHOOK_URL      = 'https://api.nahlah.ai/webhook/whatsapp/360dialog/status'

function CopyableUrl({ url, tone = 'dark' }: { url: string; tone?: 'dark' | 'light' }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  if (tone === 'light') {
    return (
      <div className="rounded-lg bg-white border border-slate-200 px-3 py-2 flex items-center gap-2">
        <Link className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" />
        <span className="flex-1 text-xs font-mono text-slate-700 truncate" dir="ltr">{url}</span>
        <button
          type="button"
          onClick={copy}
          title="نسخ الرابط"
          className="flex-shrink-0 p-1 rounded hover:bg-slate-100 transition text-slate-400 hover:text-slate-700"
        >
          {copied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
        </button>
      </div>
    )
  }
  return (
    <div className="rounded-lg bg-slate-900 border border-slate-700 px-3 py-2.5 flex items-center gap-2">
      <Link className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" />
      <span className="flex-1 text-xs font-mono text-emerald-300 truncate" dir="ltr">{url}</span>
      <button
        type="button"
        onClick={copy}
        title="نسخ الرابط"
        className="flex-shrink-0 p-1 rounded hover:bg-slate-700 transition text-slate-400 hover:text-white"
      >
        {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
    </div>
  )
}

function WebhookUrlBox() {
  return <CopyableUrl url={CHANNEL_WEBHOOK_URL} />
}

// ── Webhook management panel (per connected tenant) ───────────────────────────
// Surfaces all three URLs Nahla supports and gives the operator one-click
// Test / Verify / Auto-Configure tooling so a 360dialog mis-config can be
// diagnosed without leaving the owner panel.

function WebhookStatusPill({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    verified:     { label: 'مفعّل',          cls: 'bg-emerald-100 text-emerald-700' },
    unknown:      { label: 'غير مفحوص',      cls: 'bg-slate-100 text-slate-500' },
    unverified:   { label: 'غير مفعّل',       cls: 'bg-amber-100 text-amber-700' },
    url_mismatch: { label: 'الرابط مختلف',    cls: 'bg-amber-100 text-amber-700' },
    failed:       { label: 'فشل',             cls: 'bg-red-100 text-red-700' },
  }
  const { label, cls } = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${cls}`}>
      {label}
    </span>
  )
}

// Tri-state mini badge: shows a single dimension (reachable / registered /
// received) with three values: yes / no / unknown. Lets the operator see
// "the URL is reachable and registered but no real message has arrived"
// vs "URL registered but Nahla never received anything" at a glance —
// previously all three were collapsed into a single pill that read
// "verified" or "failed" without explaining which dimension failed.
function FacetPill({
  label, value, tone, hint,
}: {
  label: string
  value: 'yes' | 'no' | 'unknown'
  tone?: 'positive' | 'caution' | 'negative'
  hint?: string
}) {
  const palette: Record<string, string> = {
    yes_positive: 'bg-emerald-100 text-emerald-700 border-emerald-200',
    yes_caution:  'bg-amber-100 text-amber-700 border-amber-200',
    yes_negative: 'bg-red-100 text-red-700 border-red-200',
    no_positive:  'bg-slate-100 text-slate-500 border-slate-200',
    no_caution:   'bg-amber-50 text-amber-600 border-amber-200',
    no_negative:  'bg-red-50 text-red-600 border-red-200',
    unknown:      'bg-slate-100 text-slate-500 border-slate-200',
  }
  const key = value === 'unknown' ? 'unknown' : `${value}_${tone ?? 'positive'}`
  const cls = palette[key] ?? palette.unknown
  const valueLabel = value === 'yes' ? 'نعم' : value === 'no' ? 'لا' : '؟'
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold ${cls}`}
      title={hint}
    >
      <span className="font-normal text-[9px] opacity-80">{label}</span>
      <span>{valueLabel}</span>
    </span>
  )
}

function WebhookRow({
  icon, title, hint, url, status, lastReceivedAt,
  registeredFacet, receivedFacet,
}: {
  icon: React.ReactNode
  title: string
  hint: string
  url: string
  status: string
  lastReceivedAt: string | null
  // New: explicit per-dimension state. When omitted, the row falls back
  // to the legacy single-pill behaviour for non-channel webhooks.
  registeredFacet?: 'yes' | 'no' | 'unknown'
  receivedFacet?: 'yes' | 'no' | 'unknown'
}) {
  const fmtAgo = (iso: string | null) => {
    if (!iso) return 'لم يصل أي حدث بعد'
    const ts = new Date(iso).getTime()
    if (Number.isNaN(ts)) return iso
    const sec = Math.floor((Date.now() - ts) / 1000)
    if (sec < 60)        return `منذ ${sec} ث`
    if (sec < 3600)      return `منذ ${Math.floor(sec / 60)} د`
    if (sec < 86_400)    return `منذ ${Math.floor(sec / 3600)} س`
    return `منذ ${Math.floor(sec / 86_400)} يوم`
  }
  const showFacets = registeredFacet !== undefined || receivedFacet !== undefined
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-7 h-7 rounded-lg bg-violet-50 text-violet-600 flex items-center justify-center flex-shrink-0">
            {icon}
          </div>
          <div className="min-w-0">
            <p className="text-xs font-bold text-slate-700 truncate">{title}</p>
            <p className="text-[10px] text-slate-400 truncate">{hint}</p>
          </div>
        </div>
        <WebhookStatusPill status={status} />
      </div>
      <CopyableUrl url={url} tone="light" />
      <p className="text-[10px] text-slate-400 flex items-center gap-1">
        <Clock className="w-3 h-3" /> آخر حدث: {fmtAgo(lastReceivedAt)}
      </p>
      {showFacets && (
        <div className="flex flex-wrap gap-1 pt-1 border-t border-slate-100">
          <FacetPill
            label="مسجَّل لدى 360dialog"
            value={registeredFacet ?? 'unknown'}
            tone={registeredFacet === 'yes' ? 'positive' : registeredFacet === 'no' ? 'caution' : undefined}
            hint="هل URL المسجَّل لدى 360dialog يطابق رابط نحلة الرسمي؟"
          />
          <FacetPill
            label="رسائل حقيقية وصلت"
            value={receivedFacet ?? 'unknown'}
            tone={receivedFacet === 'yes' ? 'positive' : receivedFacet === 'no' ? 'caution' : undefined}
            hint="هل استلمت نحلة فعلاً webhook على هذه القناة خلال آخر أسبوع؟"
          />
        </div>
      )}
    </div>
  )
}

// ── Diagnose panel sub-components ────────────────────────────────────────────
// Surface the three independent signals so an operator never confuses
// "API key rejected by 360dialog management API" with "URL mismatch" or
// "real customer message never arrived". Previously these collapsed
// into a single ambiguous "failed" pill on the channel webhook row.

function DiagnoseSignalCard({
  icon, label, value, hint, tone,
}: {
  icon: React.ReactNode
  label: string
  value: string
  hint?: string
  tone: 'ok' | 'warn' | 'error' | 'neutral'
}) {
  const tones: Record<string, string> = {
    ok:      'bg-emerald-50 text-emerald-700 border-emerald-200',
    warn:    'bg-amber-50 text-amber-700 border-amber-200',
    error:   'bg-red-50 text-red-700 border-red-200',
    neutral: 'bg-slate-50 text-slate-700 border-slate-200',
  }
  return (
    <div className={`rounded-xl border p-3 ${tones[tone]}`}>
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <p className="text-[10px] font-semibold opacity-80">{label}</p>
      </div>
      <p className="text-sm font-bold leading-tight">{value}</p>
      {hint && <p className="text-[10px] mt-1 opacity-70 leading-snug">{hint}</p>}
    </div>
  )
}

function DuplicatesPanel({
  rows, label,
}: {
  rows: CoexistenceDiagnoseResult['duplicates']['by_phone_number_id']
  label: string
}) {
  if (!rows.length) return null
  const others = rows.filter(r => !r.is_this_tenant)
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-2 space-y-1">
      <p className="text-[10px] font-bold text-slate-600">{label}</p>
      <ul className="space-y-1">
        {rows.map(r => (
          <li key={`${r.tenant_id}-${r.connection_id}`}
              className={`text-[10px] flex items-center gap-2 rounded px-1.5 py-1 ${
                r.is_this_tenant ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'
              }`}>
            <span className="font-bold">tenant={r.tenant_id}</span>
            <span>conn={r.connection_id}</span>
            <span>status={r.status ?? '—'}</span>
            <span>phone_id={r.phone_number_id ?? '—'}</span>
            {r.is_this_tenant
              ? <span className="ms-auto font-bold">← هذا التاجر</span>
              : <span className="ms-auto font-bold">⚠ تاجر آخر</span>}
          </li>
        ))}
      </ul>
      {others.length > 0 && (
        <p className="text-[10px] text-red-600 font-bold">
          يوجد {others.length} اتصال آخر يستخدم نفس القيمة — تسرّب محتمل عبر التجار.
        </p>
      )}
    </div>
  )
}

function WebhookManagementPanel({ tenantId }: { tenantId: number }) {
  const [webhooks, setWebhooks] = useState<CoexistenceVerifyWebhookResult['webhooks'] | null>(null)
  const [diag, setDiag] = useState<CoexistenceDiagnoseResult | null>(null)
  const [reachable, setReachable] = useState<'unknown' | 'yes' | 'no'>('unknown')
  const [busy, setBusy] = useState<'test' | 'verify' | 'configure' | 'diagnose' | null>(null)
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)

  const fmt = (label: string, payload: unknown): string => {
    try { return `${label}: ${JSON.stringify(payload)}` } catch { return label }
  }

  const runTest = async () => {
    setBusy('test'); setFeedback(null)
    try {
      const res: CoexistenceTestWebhookResult = await adminApi.testCoexistenceWebhook(tenantId)
      const failed = Object.entries(res.results).filter(([, v]) => !v.ok).map(([k]) => k)
      setReachable(res.all_ok ? 'yes' : 'no')
      setFeedback(res.all_ok
        ? { kind: 'ok',  text: 'جميع روابط Webhook تستجيب بشكل سليم (الوصول من الإنترنت يعمل).' }
        : { kind: 'err', text: `فشل بعض الروابط: ${failed.join('، ')}` }
      )
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'تعذّر إجراء الاختبار' })
    } finally { setBusy(null) }
  }

  const runVerify = async () => {
    setBusy('verify'); setFeedback(null)
    try {
      const res = await adminApi.verifyCoexistenceWebhook(tenantId)
      setWebhooks(res.webhooks)
      if (res.verify_error) {
        setFeedback({ kind: 'err', text: `فشل التحقق من 360dialog (مهلة/شبكة): ${res.verify_error}` })
      } else {
        setFeedback(res.matches
          ? { kind: 'ok',   text: 'الرابط المسجّل لدى 360dialog مطابق للرابط الرسمي لنحلة.' }
          : { kind: 'info', text: res.remote_url
              ? `الرابط لدى 360dialog مختلف: ${res.remote_url}`
              : 'لم يتم تسجيل أي Webhook في 360dialog بعد.' }
        )
      }
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'فشل التحقق' })
    } finally { setBusy(null) }
  }

  const runAutoConfigure = async () => {
    setBusy('configure'); setFeedback(null)
    try {
      const res: CoexistenceAutoConfigureResult = await adminApi.autoConfigureCoexistenceWebhook(tenantId)
      setWebhooks(res.webhooks)
      setFeedback(res.ok
        ? { kind: 'ok',  text: 'تم تسجيل الـ Webhook لدى 360dialog بنجاح.' }
        : { kind: 'err', text: fmt('فشل التسجيل', res.result) }
      )
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'فشل الإعداد التلقائي' })
    } finally { setBusy(null) }
  }

  const runDiagnose = async () => {
    setBusy('diagnose'); setFeedback(null)
    try {
      const res = await adminApi.diagnoseCoexistence(tenantId)
      setDiag(res)
      setWebhooks(res.webhooks)
      // Surface a one-line summary on the feedback strip so the operator
      // sees the headline finding without scrolling.
      const tc = res.token_check
      const reg = res.registration
      const inb = res.inbound_evidence
      const tokenStr = tc.verdict === 'valid' ? 'مفتاح API صالح'
                     : tc.verdict === 'rejected' ? 'مفتاح API مرفوض من 360dialog'
                     : tc.verdict === 'transport_error' ? 'تعذّر الاتصال بـ 360dialog'
                     : 'لا يوجد مفتاح API'
      const regStr = reg.channel_matches || reg.waba_matches
        ? 'URL مسجَّل في 360dialog'
        : reg.channel_remote_url || reg.waba_remote_url
          ? 'URL مختلف لدى 360dialog'
          : 'URL غير مسجَّل في 360dialog'
      const inbStr = inb.channel_received_recently || inb.coexistence_received_recently
        ? 'استلام إنباند فعلي حديث'
        : inb.any_inbound_ever
          ? 'استلام إنباند قديم (لم يصل شيء حديث)'
          : 'لا يوجد إنباند فعلي أبداً'
      const kind: 'ok' | 'err' | 'info' =
        tc.verdict === 'rejected' ? 'err'
        : (!reg.channel_matches && !reg.waba_matches) ? 'info'
        : (inb.channel_received_recently || inb.coexistence_received_recently) ? 'ok'
        : 'info'
      setFeedback({
        kind,
        text: `${tokenStr} • ${regStr} • ${inbStr}` + (
          res.duplicates.has_duplicates ? ' • ⚠ يوجد سجلات مكرّرة' : ''
        ),
      })
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'فشل التشخيص' })
    } finally { setBusy(null) }
  }

  const w = webhooks
  // Derive the per-row facet states from the diagnose result when we
  // have it. Falls back to 'unknown' so the row never claims certainty
  // it does not have.
  const channelRegistered: 'yes' | 'no' | 'unknown' =
    diag === null ? 'unknown'
    : diag.registration.channel_matches ? 'yes' : 'no'
  const channelReceived: 'yes' | 'no' | 'unknown' =
    diag === null ? 'unknown'
    : diag.inbound_evidence.channel_received_recently ? 'yes' : 'no'
  const coexReceived: 'yes' | 'no' | 'unknown' =
    diag === null ? 'unknown'
    : diag.inbound_evidence.coexistence_received_recently ? 'yes' : 'no'
  const statusReceived: 'yes' | 'no' | 'unknown' =
    diag === null ? 'unknown'
    : diag.inbound_evidence.status_received_recently ? 'yes' : 'no'
  const wabaRegistered: 'yes' | 'no' | 'unknown' =
    diag === null ? 'unknown'
    : diag.registration.waba_matches ? 'yes' : 'no'

  return (
    <div className="rounded-xl border border-violet-200 bg-violet-50/40 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Webhook className="w-4 h-4 text-violet-700" />
        <p className="text-sm font-bold text-violet-800">إدارة Webhooks لـ 360dialog</p>
      </div>
      <p className="text-xs text-slate-500">
        نحلة تدعم ثلاث نقاط Webhook منفصلة: الرسائل العادية (Channel) — أحداث التعايش
        (Coexistence) — حالة القناة (Status). يمكنك تسجيل أي منها يدويًا في 360dialog
        أو استخدام «الإعداد التلقائي». استخدم «تشخيص شامل» قبل أي إجراء لمعرفة
        السبب الحقيقي للفشل: مفتاح API مرفوض، أم URL مختلف، أم رسائل العملاء لا تصل.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <WebhookRow
          icon={<MessageSquare className="w-3.5 h-3.5" />}
          title="Channel Webhook"
          hint="رسائل العملاء + statuses"
          url={w?.channel_url ?? CHANNEL_WEBHOOK_URL}
          status={w?.channel_status ?? 'unknown'}
          lastReceivedAt={w?.channel_last_received_at ?? null}
          registeredFacet={channelRegistered}
          receivedFacet={channelReceived}
        />
        <WebhookRow
          icon={<Smartphone className="w-3.5 h-3.5" />}
          title="Coexistence Webhook"
          hint="device sync — pairing — handover"
          url={w?.coexistence_url ?? COEXISTENCE_WEBHOOK_URL}
          status={w?.coexistence_status ?? 'unknown'}
          lastReceivedAt={w?.coexistence_last_received_at ?? null}
          registeredFacet={wabaRegistered}
          receivedFacet={coexReceived}
        />
        <WebhookRow
          icon={<ShieldCheck className="w-3.5 h-3.5" />}
          title="Status Webhook"
          hint="account health + quality"
          url={w?.status_url ?? STATUS_WEBHOOK_URL}
          status={w?.status_status ?? 'unknown'}
          lastReceivedAt={w?.status_last_received_at ?? null}
          registeredFacet={wabaRegistered}
          receivedFacet={statusReceived}
        />
      </div>

      {diag && (
        <div className="space-y-2 rounded-xl bg-white border border-slate-200 p-3">
          <div className="flex items-center justify-between">
            <p className="text-xs font-bold text-slate-700">نتيجة التشخيص الشامل</p>
            <p className="text-[10px] text-slate-400">request_id={diag.request_id}</p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <DiagnoseSignalCard
              icon={<KeyRound className="w-3.5 h-3.5" />}
              label="1) مفتاح API"
              value={
                diag.token_check.verdict === 'valid'   ? 'صالح ✓'
              : diag.token_check.verdict === 'rejected' ? `مرفوض (${diag.token_check.channel_status_code ?? diag.token_check.waba_status_code ?? '—'})`
              : diag.token_check.verdict === 'transport_error' ? 'تعذّر الاتصال'
              :                                            'غير موجود'}
              hint={`API key tail: ${diag.connection.api_key_tail}`}
              tone={
                diag.token_check.verdict === 'valid' ? 'ok'
              : diag.token_check.verdict === 'rejected' ? 'error'
              : 'warn'}
            />
            <DiagnoseSignalCard
              icon={<Link className="w-3.5 h-3.5" />}
              label="2) تسجيل URL في 360dialog"
              value={
                diag.registration.channel_matches && diag.registration.waba_matches ? 'مسجَّل وصحيح ✓'
              : diag.registration.channel_matches || diag.registration.waba_matches ? 'مسجَّل جزئيًا'
              : diag.registration.channel_remote_url || diag.registration.waba_remote_url ? 'مسجَّل لكنه مختلف'
              :                                            'غير مسجَّل'}
              hint={diag.registration.channel_remote_url
                ? `Channel: ${diag.registration.channel_remote_url}`
                : 'لم يُسترجع URL من 360dialog'}
              tone={
                diag.registration.channel_matches && diag.registration.waba_matches ? 'ok'
              : (diag.registration.channel_remote_url || diag.registration.waba_remote_url) ? 'warn'
              : 'error'}
            />
            <DiagnoseSignalCard
              icon={<Inbox className="w-3.5 h-3.5" />}
              label="3) إنباند فعلي"
              value={
                diag.inbound_evidence.channel_received_recently ? 'رسائل حقيقية وصلت ✓'
              : diag.inbound_evidence.any_inbound_ever          ? 'وصول قديم فقط'
              :                                                   'لم يصل أي webhook'}
              hint={
                `channel: ${diag.inbound_evidence.freshness_seconds.channel ?? '∅'} ث، ` +
                `coex: ${diag.inbound_evidence.freshness_seconds.coexistence ?? '∅'} ث، ` +
                `status: ${diag.inbound_evidence.freshness_seconds.status ?? '∅'} ث`
              }
              tone={
                diag.inbound_evidence.channel_received_recently ? 'ok'
              : diag.inbound_evidence.any_inbound_ever          ? 'warn'
              :                                                   'error'}
            />
          </div>

          {reachable !== 'unknown' && (
            <p className="text-[11px] text-slate-500">
              <strong>الوصول من الإنترنت:</strong>{' '}
              {reachable === 'yes' ? '✓ كل عناوين Nahla تستجيب' : '✗ بعض العناوين لا تستجيب'}
              {' '}— استخدم «Test Coexistence Webhook» في أي وقت لإعادة فحص ذلك.
            </p>
          )}

          {diag.token_check.verdict === 'rejected' && (
            <div className="rounded-lg bg-red-50 border border-red-200 p-2 text-[11px] text-red-700">
              <strong>تشخيص:</strong> 360dialog يرفض مفتاح API المخزَّن في نحلة
              ({diag.connection.api_key_tail}) — جواب الـ Management API:{' '}
              <code className="bg-white/60 px-1">{diag.token_check.channel_body_preview}</code>.
              السبب الأكثر شيوعًا: تم تجديد المفتاح في 360dialog ولم يُحدَّث هنا.
              افتح «تعديل الحقول» أعلى وألصق المفتاح الجديد.
            </div>
          )}

          {diag.registration.phone_id_drift && (
            <div className="rounded-lg bg-amber-50 border border-amber-200 p-2 text-[11px] text-amber-700">
              <strong>انحراف phone_number_id:</strong> الـ phone_number_id لدى نحلة
              ({diag.connection.phone_number_id}) لا يطابق أي رقم على هذا الـ WABA لدى 360dialog
              ({diag.registration.numbers_on_this_waba.join(', ') || '—'}). أعد المزامنة
              عبر «Sync / Repair Integration Record».
            </div>
          )}

          {diag.duplicates.has_duplicates && (
            <details className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-[11px]">
              <summary className="cursor-pointer font-bold text-amber-800">
                ⚠ سجلات اتصال مكرّرة — قد تتسبّب في تسرّب الرسائل
              </summary>
              <div className="mt-2 space-y-2">
                <DuplicatesPanel rows={diag.duplicates.by_phone_number_id} label="حسب phone_number_id" />
                <DuplicatesPanel rows={diag.duplicates.by_channel_id}     label="حسب channel_id" />
                <DuplicatesPanel rows={diag.duplicates.by_display_phone}  label="حسب رقم العرض" />
              </div>
            </details>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <button
          onClick={runDiagnose}
          disabled={!!busy}
          className="inline-flex items-center gap-2 rounded-lg bg-slate-800 px-3 py-2 text-xs font-bold text-white hover:bg-slate-700 disabled:opacity-50"
        >
          <Search className="w-3.5 h-3.5" />
          {busy === 'diagnose' ? 'جارٍ التشخيص…' : 'تشخيص شامل'}
        </button>
        <button
          onClick={runTest}
          disabled={!!busy}
          className="inline-flex items-center gap-2 rounded-lg bg-white border border-slate-200 px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          <TestTube2 className="w-3.5 h-3.5" />
          {busy === 'test' ? 'جارٍ الاختبار…' : 'Test Coexistence Webhook'}
        </button>
        <button
          onClick={runVerify}
          disabled={!!busy}
          className="inline-flex items-center gap-2 rounded-lg bg-white border border-slate-200 px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          <ShieldCheck className="w-3.5 h-3.5" />
          {busy === 'verify' ? 'جارٍ التحقق…' : 'Verify'}
        </button>
        <button
          onClick={runAutoConfigure}
          disabled={!!busy}
          className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-3 py-2 text-xs font-bold text-white hover:bg-violet-500 disabled:opacity-50"
        >
          <PlugZap className="w-3.5 h-3.5" />
          {busy === 'configure' ? 'جارٍ الإعداد…' : 'Auto Configure'}
        </button>
      </div>

      {feedback && (
        <div className={`rounded-lg p-2.5 text-xs font-semibold flex items-start gap-2 ${
          feedback.kind === 'ok'  ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' :
          feedback.kind === 'err' ? 'bg-red-50 text-red-700 border border-red-200' :
                                    'bg-amber-50 text-amber-700 border border-amber-200'
        }`}>
          {feedback.kind === 'ok'  && <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          {feedback.kind === 'err' && <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          {feedback.kind === 'info' && <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          <span>{feedback.text}</span>
        </div>
      )}
    </div>
  )
}

// ── Activate form ─────────────────────────────────────────────────────────────

function ActivateForm({
  req,
  onSuccess,
  onCancel,
}: {
  req: CoexistenceRequest
  onSuccess: () => void
  onCancel: () => void
}) {
  const [form, setForm] = useState<Partial<CoexistenceActivatePayload>>({
    tenant_id: req.tenant_id,
    phone_number: req.requested_phone ?? '',
    display_name: req.display_name ?? '',
    phone_number_id: '',
    api_key: '',
    configure_webhook: true,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<Record<string, unknown> | null>(null)

  const set = (k: keyof CoexistenceActivatePayload, v: string | boolean) =>
    setForm(f => ({ ...f, [k]: v }))

  const _translateError = (msg: string): string => {
    if (!msg) return 'فشل التفعيل'
    if (msg.toLowerCase().includes('phone_number_id')) return 'حقل phone_number_id مطلوب — أدخله من صفحة الرقم في 360dialog'
    if (msg.toLowerCase().includes('api_key'))         return 'حقل مفتاح API مطلوب'
    if (msg.toLowerCase().includes('phone_number'))    return 'حقل رقم واتساب مطلوب'
    if (msg.toLowerCase().includes('tenant_id'))       return 'معرّف المتجر (tenant_id) مطلوب'
    if (msg.toLowerCase().includes('unauthorized') || msg.includes('401')) return 'غير مصرح — يرجى تسجيل الدخول من جديد'
    if (msg.toLowerCase().includes('not found') || msg.includes('404'))    return 'المتجر غير موجود'
    return msg
  }

  const submit = async () => {
    if (!form.phone_number?.trim()) {
      setError('رقم واتساب التاجر مطلوب.')
      return
    }
    if (!form.api_key?.trim()) {
      setError('مفتاح API من 360dialog مطلوب.')
      return
    }
    if (!form.phone_number_id?.trim()) {
      setError('حقل phone_number_id مطلوب — انسخه من صفحة الرقم في 360dialog.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const res = await adminApi.activateCoexistence(form as CoexistenceActivatePayload)
      setResult(res)
      onSuccess()
    } catch (e: any) {
      const raw: string = e?.message ?? e?.detail ?? String(e)
      setError(_translateError(raw))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="rounded-xl bg-emerald-50 border border-emerald-200 p-4 text-center space-y-2">
        <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto" />
        <p className="font-bold text-emerald-700">تم التفعيل بنجاح</p>
        <p className="text-xs text-emerald-600">الحالة: {String(result.status ?? 'connected')}</p>
      </div>
    )
  }

  return (
    <div className="mt-4 border border-violet-200 rounded-xl bg-violet-50 p-4 space-y-4" dir="rtl">
      <div className="flex items-center gap-2">
        <Zap className="w-4 h-4 text-violet-700" />
        <p className="font-bold text-violet-800 text-sm">تفعيل عبر 360dialog</p>
      </div>

      {/* Step guide */}
      <div className="rounded-lg bg-white border border-violet-100 p-3 text-xs text-slate-600 space-y-1.5">
        <p className="font-bold text-slate-700 mb-2">خطوات التفعيل:</p>
        <p>① ادخل <a href="https://app.360dialog.io" target="_blank" rel="noreferrer" className="text-violet-600 underline font-semibold">app.360dialog.io</a> → الحساب المرتبط بـ Nahlah AI</p>
        <p>② اضغط <strong>Add Number</strong> → أدخل رقم واتساب التاجر → أكمل التحقق عبر OTP</p>
        <p>③ بعد إضافة الرقم → افتح الرقم → انسخ <strong>Phone Number ID</strong> من الصفحة</p>
        <p>④ اضغط <strong>Generate API Key</strong> → انسخ المفتاح → الصقه أدناه</p>
        <p>⑤ إذا لم يُضبط Webhook تلقائيًا → ضع الرابط أدناه يدويًا في 360dialog</p>
      </div>

      {/* Webhook URL */}
      <div className="space-y-1">
        <p className="text-xs font-semibold text-slate-600 flex items-center gap-1">
          <Link className="w-3 h-3" />
          Webhook URL لـ 360dialog
        </p>
        <WebhookUrlBox />
        <p className="text-xs text-slate-400">ضع هذا الرابط في إعدادات 360dialog إذا لم يُضبط Webhook تلقائيًا</p>
      </div>

      {/* Required fields */}
      <div className="space-y-3">
        <div>
          <label className="block text-xs font-bold text-slate-700 mb-1">
            رقم واتساب التاجر <span className="text-red-500">*</span>
          </label>
          <input
            value={form.phone_number ?? ''}
            onChange={e => set('phone_number', e.target.value)}
            placeholder="+9665XXXXXXXX"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm"
            dir="ltr"
          />
        </div>

        <div>
          <label className="block text-xs font-bold text-slate-700 mb-1">
            Phone Number ID <span className="text-red-500">*</span>
          </label>
          <input
            value={form.phone_number_id ?? ''}
            onChange={e => set('phone_number_id', e.target.value)}
            placeholder="مثال: 123456789012345"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-mono"
            dir="ltr"
          />
          <p className="text-xs text-slate-400 mt-1">من صفحة الرقم في 360dialog — مطلوب لربط القناة بشكل صحيح</p>
        </div>

        <div>
          <label className="block text-xs font-bold text-slate-700 mb-1">
            مفتاح API من 360dialog <span className="text-red-500">*</span>
          </label>
          <input
            value={form.api_key ?? ''}
            onChange={e => set('api_key', e.target.value)}
            placeholder="D360-XXXXXXXXXXXXXXXXXXXXXXXX"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-mono"
            dir="ltr"
          />
          <p className="text-xs text-slate-400 mt-1">من صفحة الرقم في 360dialog → Generate API Key</p>
        </div>
      </div>

      {/* Optional fields (collapsible) */}
      <details className="group">
        <summary className="cursor-pointer text-xs font-semibold text-slate-500 hover:text-slate-700 list-none flex items-center gap-1 select-none">
          <span className="group-open:hidden">▸</span>
          <span className="hidden group-open:inline">▾</span>
          حقول اختيارية (WABA ID — Channel ID — Client ID — اسم النشاط)
        </summary>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">WABA ID</label>
            <input
              value={form.waba_id ?? ''}
              onChange={e => set('waba_id', e.target.value)}
              placeholder="WhatsApp Business Account ID"
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-mono"
              dir="ltr"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">Channel ID</label>
            <input
              value={form.channel_id ?? ''}
              onChange={e => set('channel_id', e.target.value)}
              placeholder="Channel ID من 360dialog"
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-mono"
              dir="ltr"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">Client ID</label>
            <input
              value={form.client_id ?? ''}
              onChange={e => set('client_id', e.target.value)}
              placeholder="Client ID"
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-mono"
              dir="ltr"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">اسم النشاط (Display Name)</label>
            <input
              value={form.display_name ?? ''}
              onChange={e => set('display_name', e.target.value)}
              placeholder="اسم المتجر أو النشاط"
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
            />
          </div>
        </div>
      </details>

      {/* Webhook toggle */}
      <label className="flex items-center gap-2.5 text-sm text-slate-700 cursor-pointer bg-white border border-violet-100 rounded-lg px-3 py-2.5">
        <input
          type="checkbox"
          checked={!!form.configure_webhook}
          onChange={e => set('configure_webhook', e.target.checked)}
          className="rounded accent-violet-600 w-4 h-4"
        />
        <span>
          <span className="font-semibold">إعداد Webhook تلقائيًا</span>
          <span className="text-xs text-slate-400 block">نحلة ستُسجّل الرابط أعلاه تلقائيًا لدى 360dialog لاستقبال الرسائل</span>
        </span>
      </label>

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-red-700 text-xs font-semibold">
          {error}
        </div>
      )}

      <div className="flex gap-3 pt-1">
        <button
          onClick={submit}
          disabled={busy}
          className="flex-1 rounded-xl bg-violet-600 py-2.5 text-sm font-bold text-white hover:bg-violet-500 disabled:opacity-60 transition"
        >
          {busy ? 'جارٍ التفعيل...' : '⚡ تفعيل الآن'}
        </button>
        <button
          onClick={onCancel}
          className="px-4 rounded-xl border border-slate-200 text-sm text-slate-500 hover:bg-slate-50 transition"
        >
          إلغاء
        </button>
      </div>
    </div>
  )
}

// ── Integration integrity banner ─────────────────────────────────────────────
// Surfaces the SAME completeness verdict the merchant page uses, so the owner
// panel can never show "مفعّل" while the merchant sees "غير متصل فعليًا".
// The verdict comes straight from the backend (single source of truth) — we
// just render it.

const _MISSING_FIELD_LABELS: Record<string, string> = {
  waba_id:         'WABA ID',
  phone_number_id: 'Phone Number ID',
  phone_number:    'رقم واتساب',
  api_key:         'مفتاح API',
}

function IntegrationIntegrityBanner({ req }: { req: CoexistenceRequest }) {
  const ic = req.integration_complete
  if (ic.truly_connected) {
    return (
      <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-3 text-xs text-emerald-700 flex items-start gap-2">
        <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
        <div>
          <p className="font-bold">سجل الربط مكتمل</p>
          <p className="text-emerald-600">
            صفحة التاجر ولوحة المالك تعرضان نفس الحالة. لا يحتاج هذا التاجر إلى أي إصلاح.
          </p>
        </div>
      </div>
    )
  }
  const missingLabels = (ic.missing_fields || []).map(f => _MISSING_FIELD_LABELS[f] || f)
  return (
    <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700 flex items-start gap-2">
      <XCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
      <div className="flex-1">
        <p className="font-bold">عدم تطابق بين لوحة المالك وصفحة التاجر</p>
        <p className="text-red-600 mt-0.5">
          سبب: <span className="font-mono">{ic.reason_code || 'incomplete'}</span>
          {missingLabels.length ? ` — مفقود: ${missingLabels.join('، ')}` : ''}
        </p>
        <p className="text-red-500 mt-1">
          استخدم زر «Sync / Repair Integration Record» أدناه أو افتح «تعديل الحقول» لإكمال السجل يدويًا.
        </p>
      </div>
    </div>
  )
}

// ── Integration record fields panel ──────────────────────────────────────────
// Read view + Edit view + Sync/Repair button. Operates entirely against the
// new admin endpoints (sync-record, edit-record).

function IntegrationFieldsPanel({
  req, onRefresh,
}: {
  req: CoexistenceRequest
  onRefresh: () => void
}) {
  const [editing, setEditing]   = useState(false)
  const [syncing, setSyncing]   = useState(false)
  const [saving,  setSaving]    = useState(false)
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
  const [form, setForm] = useState({
    waba_id:         req.waba_id ?? '',
    phone_number_id: req.phone_number_id ?? '',
    phone_number:    req.requested_phone ?? '',
    channel_id:      req.channel_id ?? '',
    client_id:       req.client_id ?? '',
    api_key:         '',
    display_name:    req.display_name ?? '',
    promote_to_connected: true,
  })

  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
    setForm(f => ({ ...f, [k]: v }))

  const runSync = async () => {
    setSyncing(true); setFeedback(null)
    try {
      const res = await adminApi.syncCoexistenceRecord(req.tenant_id)
      const after = res.after
      if (after.truly_connected) {
        setFeedback({ kind: 'ok', text: 'تم إصلاح السجل — جميع الحقول مكتملة الآن.' })
      } else {
        const missing = (after.missing_fields || [])
          .map(f => _MISSING_FIELD_LABELS[f] || f).join('، ')
        setFeedback({
          kind: 'info',
          text: `لم تُحلّ كل الحقول تلقائيًا. ما زال ناقصًا: ${missing}. أكمِل من «تعديل الحقول».`,
        })
      }
      // Free the spinner immediately; the list refresh fires in the
      // background so the operator never waits on a second round-trip.
      setSyncing(false)
      void Promise.resolve()
        .then(() => onRefresh())
        .catch(err => console.warn('[AdminCoexistence] background refresh failed', err))
      return
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'فشلت عملية المزامنة' })
    } finally { setSyncing(false) }
  }

  const submitEdit = async () => {
    setSaving(true); setFeedback(null)
    try {
      // Build the payload with ONLY the fields the operator actually
      // touched: undefined = "leave alone", "" = "clear field". The api_key
      // field is never sent unchanged.
      const payload: Parameters<typeof adminApi.editCoexistenceRecord>[0] = {
        tenant_id: req.tenant_id,
        promote_to_connected: form.promote_to_connected,
      }
      if (form.waba_id !== (req.waba_id ?? ''))                payload.waba_id = form.waba_id
      if (form.phone_number_id !== (req.phone_number_id ?? '')) payload.phone_number_id = form.phone_number_id
      if (form.phone_number !== (req.requested_phone ?? ''))   payload.phone_number = form.phone_number
      if (form.channel_id !== (req.channel_id ?? ''))          payload.channel_id = form.channel_id
      if (form.client_id !== (req.client_id ?? ''))            payload.client_id = form.client_id
      if (form.display_name !== (req.display_name ?? ''))      payload.display_name = form.display_name
      if (form.api_key.trim())                                  payload.api_key = form.api_key.trim()

      const res = await adminApi.editCoexistenceRecord(payload)
      if (res.integration_complete.truly_connected) {
        setFeedback({ kind: 'ok', text: 'تم حفظ التعديلات وسجل الربط الآن مكتمل.' })
      } else {
        const missing = (res.integration_complete.missing_fields || [])
          .map(f => _MISSING_FIELD_LABELS[f] || f).join('، ')
        setFeedback({ kind: 'info', text: `تم الحفظ لكن لا يزال ناقصًا: ${missing}` })
      }
      setEditing(false)
      // Save is the source of truth — release the spinner immediately so
      // the operator sees confirmation. The list refresh runs in the
      // background and updates the row when it returns; we never block
      // the Save button on a recheck/probe round-trip.
      setSaving(false)
      void Promise.resolve()
        .then(() => onRefresh())
        .catch(err => console.warn('[AdminCoexistence] background refresh failed', err))
      return
    } catch (e: unknown) {
      setFeedback({ kind: 'err', text: e instanceof Error ? e.message : 'فشل الحفظ' })
    } finally { setSaving(false) }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-slate-600" />
          <p className="text-sm font-bold text-slate-700">حقول سجل الربط</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={runSync}
            disabled={syncing || saving}
            title="يقرأ بيانات القناة من 360dialog ويملأ الحقول الناقصة"
            className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-500 disabled:opacity-50"
          >
            <Hammer className="w-3.5 h-3.5" />
            {syncing ? 'جارٍ الإصلاح…' : 'Sync / Repair Integration Record'}
          </button>
          <button
            onClick={() => { setEditing(e => !e); setFeedback(null) }}
            disabled={syncing || saving}
            className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            <Edit3 className="w-3.5 h-3.5" />
            {editing ? 'إلغاء' : 'تعديل الحقول'}
          </button>
        </div>
      </div>

      {/* Read view */}
      {!editing && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
          {([
            ['WABA ID',         req.waba_id,         'waba_id'],
            ['Phone Number ID', req.phone_number_id, 'phone_number_id'],
            ['رقم واتساب',       req.requested_phone, 'phone_number'],
            ['Channel ID',      req.channel_id,      'channel_id'],
            ['Client ID',       req.client_id,       'client_id'],
            ['API Key',         req.has_api_key ? '✓ مخزّن' : null, 'api_key'],
          ] as Array<[string, string | null, string]>).map(([label, value, key]) => {
            const optional = key === 'client_id'
            const missing = !value && !optional
            return (
            <div
              key={key}
              className={`rounded-lg border px-3 py-2 ${
                value ? 'border-slate-200 bg-slate-50' : missing ? 'border-red-200 bg-red-50' : 'border-slate-200 bg-slate-50'
              }`}
            >
              <p className="text-[10px] font-semibold text-slate-400 mb-0.5">{label}</p>
              <p className={`font-mono text-xs truncate ${value ? 'text-slate-700' : missing ? 'text-red-600' : 'text-slate-500'}`} dir="ltr">
                {value || (optional ? '— (اختياري)' : '— مفقود —')}
              </p>
            </div>
            )
          })}
        </div>
      )}

      {/* Edit view */}
      {editing && (
        <div className="space-y-3 pt-1" dir="rtl">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">WABA ID</label>
              <input
                value={form.waba_id}
                onChange={e => set('waba_id', e.target.value)}
                placeholder="123456789012345"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-mono"
                dir="ltr"
              />
            </div>
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">Phone Number ID</label>
              <input
                value={form.phone_number_id}
                onChange={e => set('phone_number_id', e.target.value)}
                placeholder="123456789012345"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-mono"
                dir="ltr"
              />
            </div>
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">رقم واتساب</label>
              <input
                value={form.phone_number}
                onChange={e => set('phone_number', e.target.value)}
                placeholder="+9665XXXXXXXX"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
                dir="ltr"
              />
            </div>
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">Channel ID</label>
              <input
                value={form.channel_id}
                onChange={e => set('channel_id', e.target.value)}
                placeholder="ZVXPG8CH"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-mono"
                dir="ltr"
              />
            </div>
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">Client ID</label>
              <input
                value={form.client_id}
                onChange={e => set('client_id', e.target.value)}
                placeholder="(اختياري)"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-mono"
                dir="ltr"
              />
            </div>
            <div>
              <label className="block text-[11px] font-bold text-slate-600 mb-1">اسم النشاط</label>
              <input
                value={form.display_name}
                onChange={e => set('display_name', e.target.value)}
                placeholder="اسم المتجر"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs"
              />
            </div>
            <div className="sm:col-span-2">
              <label className="block text-[11px] font-bold text-slate-600 mb-1">
                مفتاح API لـ 360dialog {req.has_api_key && <span className="text-emerald-600 font-normal">(مخزّن — اتركه فارغًا للإبقاء)</span>}
              </label>
              <input
                value={form.api_key}
                onChange={e => set('api_key', e.target.value)}
                placeholder="D360-XXXXXXXXXXXXXXXXXXXXXXXX"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-mono"
                dir="ltr"
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
            <input
              type="checkbox"
              checked={form.promote_to_connected}
              onChange={e => set('promote_to_connected', e.target.checked)}
              className="rounded accent-violet-600 w-4 h-4"
            />
            <span>ترقية الحالة إلى <strong>مفعّل</strong> تلقائيًا إذا اكتملت الحقول</span>
          </label>

          <div className="flex gap-2">
            <button
              onClick={submitEdit}
              disabled={saving}
              className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2 text-xs font-bold text-white hover:bg-violet-500 disabled:opacity-50"
            >
              <Save className="w-3.5 h-3.5" />
              {saving ? 'جارٍ الحفظ…' : 'Save & Recheck'}
            </button>
            <button
              onClick={() => setEditing(false)}
              disabled={saving}
              className="rounded-lg border border-slate-200 px-4 py-2 text-xs font-bold text-slate-500 hover:bg-slate-50 disabled:opacity-50"
            >
              إلغاء
            </button>
          </div>
        </div>
      )}

      {feedback && (
        <div className={`rounded-lg p-2.5 text-xs font-semibold flex items-start gap-2 ${
          feedback.kind === 'ok'  ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' :
          feedback.kind === 'err' ? 'bg-red-50 text-red-700 border border-red-200' :
                                    'bg-amber-50 text-amber-700 border border-amber-200'
        }`}>
          {feedback.kind === 'ok'  && <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          {feedback.kind === 'err' && <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          {feedback.kind === 'info' && <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />}
          <span>{feedback.text}</span>
        </div>
      )}
    </div>
  )
}

// ── Request card ──────────────────────────────────────────────────────────────

function RequestCard({ req, onRefresh }: { req: CoexistenceRequest; onRefresh: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [activating, setActivating] = useState(false)

  const isNew = req.wa_status === 'request_submitted'

  const fmt = (iso: string | null) => {
    if (!iso) return '—'
    const d = new Date(iso)
    return d.toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh', dateStyle: 'medium', timeStyle: 'short' })
  }

  return (
    <div
      className={`rounded-2xl border shadow-sm transition ${isNew ? 'border-amber-300 bg-amber-50' : 'border-slate-200 bg-white'}`}
    >
      {/* Header */}
      <div className="p-4 flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 ${isNew ? 'bg-amber-500' : 'bg-slate-200'}`}>
            <Smartphone className={`w-5 h-5 ${isNew ? 'text-white' : 'text-slate-400'}`} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-bold text-slate-800 text-sm">
                {req.display_name || req.tenant_name || `Tenant #${req.tenant_id}`}
              </span>
              <StatusBadge status={req.wa_status} />
            </div>
            <div className="flex items-center gap-4 mt-1 text-xs text-slate-500 flex-wrap">
              <span className="flex items-center gap-1"><Store className="w-3 h-3" /> #{req.tenant_id}</span>
              {req.merchant_email && <span className="flex items-center gap-1"><User className="w-3 h-3" />{req.merchant_email}</span>}
              {req.requested_phone && <span className="flex items-center gap-1" dir="ltr"><Phone className="w-3 h-3" />{req.requested_phone}</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {req.submitted_at && (
            <span className="text-xs text-slate-400 flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {fmt(req.submitted_at)}
            </span>
          )}
          <button onClick={() => setExpanded(e => !e)} className="p-1.5 rounded-lg hover:bg-slate-100 transition text-slate-400">
            {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-slate-100 pt-3" dir="rtl">
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-xs">
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">رقم واتساب التاجر</p>
              <p className="font-mono text-slate-700 dir-ltr" dir="ltr">{req.requested_phone || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">اسم النشاط</p>
              <p className="text-slate-700">{req.display_name || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">البريد الإلكتروني</p>
              <p className="text-slate-700">{req.merchant_email || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">تطبيق WA Business</p>
              <p className="text-slate-700">{req.has_whatsapp_business_app ? 'نعم ✓' : 'لا'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">وقت تقديم الطلب</p>
              <p className="text-slate-700">{fmt(req.submitted_at)}</p>
            </div>
            {req.connected_at && (
              <div>
                <p className="text-slate-400 font-semibold mb-0.5">وقت التفعيل</p>
                <p className="text-slate-700">{fmt(req.connected_at)}</p>
              </div>
            )}
          </div>

          {req.notes && (
            <div className="rounded-lg bg-slate-100 p-3">
              <p className="text-xs font-semibold text-slate-500 mb-1 flex items-center gap-1">
                <MessageSquare className="w-3 h-3" /> ملاحظات التاجر
              </p>
              <p className="text-sm text-slate-700 whitespace-pre-wrap">{req.notes}</p>
            </div>
          )}

          {req.last_error && (
            <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700">
              آخر خطأ: {req.last_error}
            </div>
          )}

          {/* Activate button (only for pending requests) */}
          {(req.wa_status === 'request_submitted' || req.wa_status === 'action_required') && !activating && (
            <button
              onClick={() => setActivating(true)}
              className="w-full mt-2 rounded-xl bg-violet-600 py-2.5 text-sm font-bold text-white hover:bg-violet-500 transition"
            >
              تفعيل هذا التاجر
            </button>
          )}

          {activating && (
            <ActivateForm
              req={req}
              onSuccess={() => { setActivating(false); onRefresh() }}
              onCancel={() => setActivating(false)}
            />
          )}

          {/* Integrity banner — shows ONLY when the merchant page would
              currently show "غير متصل فعليًا". This is the cure for the
              owner panel and merchant page disagreeing. */}
          <IntegrationIntegrityBanner req={req} />

          {/* Editable integration record — Sync/Repair button + manual
              field overrides + Save & Recheck. */}
          <IntegrationFieldsPanel req={req} onRefresh={onRefresh} />

          {/* Webhook tooling — always visible. For tenants still in
              `request_submitted` the buttons will fail with a clear error
              ("API key not stored yet"), which is the expected feedback. */}
          <WebhookManagementPanel tenantId={req.tenant_id} />
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdminCoexistence() {
  const [requests, setRequests] = useState<CoexistenceRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [statusFilter, setStatusFilter] = useState('request_submitted')

  const load = useCallback(() => {
    setLoading(true)
    setLoadError('')
    adminApi.coexistenceRequests(statusFilter)
      .then(data => setRequests(data.requests))
      .catch((e: unknown) => {
        setRequests([])
        setLoadError(e instanceof Error ? e.message : 'فشل تحميل الطلبات')
      })
      .finally(() => setLoading(false))
  }, [statusFilter])

  useEffect(() => { load() }, [load])

  const pending   = requests.filter(r => r.wa_status === 'request_submitted').length
  const activated = requests.filter(r => r.wa_status === 'connected').length

  return (
    <div className="p-6 space-y-5" dir="rtl">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-600 flex items-center justify-center shadow-lg shadow-violet-500/30">
            <Smartphone className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">طلبات واتساب الجوال + الذكاء</h1>
            <p className="text-slate-400 text-xs">إدارة وتفعيل طلبات التاجر لخدمة 360dialog Coexistence</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-2 rounded-xl border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-50 transition"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'إجمالي الطلبات', value: requests.length, color: 'text-slate-700' },
          { label: 'طلبات جديدة',    value: pending,          color: 'text-amber-600' },
          { label: 'مفعّلون',        value: activated,        color: 'text-emerald-600' },
        ].map(s => (
          <div key={s.label} className="bg-white rounded-2xl border border-slate-100 shadow-sm p-4">
            <p className={`text-2xl font-black ${s.color}`}>{s.value}</p>
            <p className="text-xs text-slate-400 mt-0.5">{s.label}</p>
          </div>
        ))}
      </div>

      {/* Filter */}
      <div className="flex gap-2 flex-wrap">
        {[
          { key: 'request_submitted',  label: 'طلبات جديدة' },
          { key: 'connected',          label: 'مفعّلون' },
          { key: 'action_required',    label: 'يحتاج تدخل' },
          { key: 'all',                label: 'الكل' },
        ].map(f => (
          <button
            key={f.key}
            onClick={() => setStatusFilter(f.key)}
            className={`rounded-full px-4 py-1.5 text-xs font-semibold transition ${
              statusFilter === f.key
                ? 'bg-violet-600 text-white'
                : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* List */}
      {loading ? (
        <div className="text-center py-16 text-slate-400 text-sm">جارٍ التحميل...</div>
      ) : loadError ? (
        <div className="rounded-2xl border border-red-200 bg-red-50 p-6 text-center">
          <p className="text-red-700 font-semibold text-sm">فشل تحميل الطلبات</p>
          <p className="text-red-500 text-xs mt-1 font-mono">{loadError}</p>
          <button
            onClick={load}
            className="mt-3 px-4 py-2 rounded-xl bg-red-600 text-white text-xs font-bold hover:bg-red-500 transition"
          >
            إعادة المحاولة
          </button>
        </div>
      ) : requests.length === 0 ? (
        <div className="text-center py-16 text-slate-400">
          <Smartphone className="w-12 h-12 mx-auto mb-3 opacity-20" />
          <p className="text-sm">لا توجد طلبات {statusFilter !== 'all' ? 'بهذه الحالة' : ''}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {requests.map(req => (
            <RequestCard key={req.tenant_id} req={req} onRefresh={load} />
          ))}
        </div>
      )}
    </div>
  )
}
