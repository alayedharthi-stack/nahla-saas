/**
 * WhatsAppCatalog.tsx
 * ───────────────────
 * Merchant settings page for the WhatsApp Catalog integration.
 *
 * Renders three blocks:
 *  1) Status banner — driven by /merchant/catalog/status.advice
 *  2) Configuration form — catalog_enabled toggle + meta_catalog_id input
 *  3) Test send panel — pick a product (by title or sample) and exercise
 *     the full dispatch chain via /merchant/catalog/test-send
 *
 * Why a dedicated page (not a tab in Integrations)
 * ────────────────────────────────────────────────
 * Catalog wire-up is failure-rich (eligibility may flip on any of:
 * Meta Commerce Manager id, catalog approval state, retailer_id
 * coverage on products, provider catalog_management permission).
 * Each of those needs its own diagnostic surface — collapsing them
 * into the generic Integrations row would hide the actionable bits.
 */
import { useEffect, useState } from 'react'
import {
  AlertTriangle, BookOpen, CheckCircle2, ExternalLink, Loader2,
  Package, Send, ShieldCheck, ToggleLeft, ToggleRight, XCircle,
} from 'lucide-react'
import {
  catalogApi,
  type CatalogStatus,
  type CatalogTestSendResult,
} from '../api/catalog'

// ── small UI primitives, scoped to this page ─────────────────────────

function StatusPill({ ok, reason }: { ok: boolean; reason: string }) {
  const cls = ok
    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
    : 'bg-amber-50 border-amber-200 text-amber-700'
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-full border ${cls}`}>
      {ok
        ? <CheckCircle2 className="w-3.5 h-3.5" />
        : <AlertTriangle className="w-3.5 h-3.5" />}
      {reason}
    </span>
  )
}

function Card(props: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm">
      <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
        {props.icon}
        <h3 className="text-base font-bold text-slate-800">{props.title}</h3>
      </div>
      <div className="p-5">{props.children}</div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────

export default function WhatsAppCatalog() {
  const [status, setStatus]         = useState<CatalogStatus | null>(null)
  const [loading, setLoading]       = useState(true)
  const [saving, setSaving]         = useState(false)
  const [error, setError]           = useState<string | null>(null)
  const [success, setSuccess]       = useState<string | null>(null)

  // form state mirrors the connection columns
  const [enabled, setEnabled]       = useState(false)
  const [catalogId, setCatalogId]   = useState('')

  // test-send form
  const [testTo, setTestTo]         = useState('')
  const [testTitle, setTestTitle]   = useState('')
  const [testProductId, setTestProductId] = useState<number | ''>('')
  const [testing, setTesting]       = useState(false)
  const [testResult, setTestResult] = useState<CatalogTestSendResult | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await catalogApi.status()
      setStatus(s)
      setEnabled(s.connection.catalog_enabled)
      setCatalogId(s.connection.meta_catalog_id ?? '')
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر جلب حالة الكتالوج.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void refresh() }, [])

  const onSave = async () => {
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const res = await catalogApi.patch({
        meta_catalog_id: catalogId.trim(),
        catalog_enabled: enabled,
      })
      setStatus(res.status)
      setEnabled(res.status.connection.catalog_enabled)
      setCatalogId(res.status.connection.meta_catalog_id ?? '')
      if (Object.keys(res.applied_changes).length === 0) {
        setSuccess('الإعدادات محدّثة مسبقاً.')
      } else {
        setSuccess('تم حفظ إعدادات الكتالوج.')
      }
    } catch (e: any) {
      // Surface the structured 400 (catalog_id_required) clearly.
      if (e?.code === 'catalog_id_required' || (e?.message ?? '').includes('Catalog ID')) {
        setError('لا يمكن تفعيل الكتالوج بدون إدخال Catalog ID صحيح من Meta Commerce Manager.')
      } else {
        setError(e?.message ?? 'تعذّر حفظ الإعدادات.')
      }
    } finally {
      setSaving(false)
    }
  }

  const onTestSend = async () => {
    setTesting(true)
    setTestResult(null)
    setError(null)
    try {
      const result = await catalogApi.testSend({
        to:            testTo.trim(),
        product_id:    typeof testProductId === 'number' ? testProductId : undefined,
        product_title: testTitle.trim() || undefined,
        mode:          'auto',
      })
      setTestResult(result)
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر تنفيذ الإرسال التجريبي.')
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-2 text-slate-500">
        <Loader2 className="w-5 h-5 animate-spin" /> جاري تحميل حالة الكتالوج...
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto" dir="rtl">
      {/* ── Header ──────────────────────────────────────────────── */}
      <div>
        <h1 className="text-2xl font-black text-slate-900 flex items-center gap-2">
          <Package className="w-7 h-7 text-emerald-600" />
          كتالوج واتساب
        </h1>
        <p className="text-sm text-slate-500 mt-1">
          اربط كتالوج Meta Commerce Manager بحساب واتساب الأعمال ليتمكن
          الذكاء من إرسال كرت منتج رسمي (صورة + سعر + زر شراء) بدلاً
          من رابط نصي.
        </p>
      </div>

      {/* ── Error / Success banners ─────────────────────────────── */}
      {error && (
        <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded-xl px-4 py-3 flex items-start gap-2">
          <XCircle className="w-5 h-5 shrink-0 mt-0.5" />
          <p className="text-sm">{error}</p>
        </div>
      )}
      {success && (
        <div className="bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-xl px-4 py-3 flex items-start gap-2">
          <CheckCircle2 className="w-5 h-5 shrink-0 mt-0.5" />
          <p className="text-sm">{success}</p>
        </div>
      )}

      {/* ── Status snapshot ─────────────────────────────────────── */}
      {status && (
        <Card title="حالة الربط" icon={<ShieldCheck className="w-5 h-5 text-emerald-600" />}>
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill ok={status.eligibility.ok} reason={status.eligibility.reason} />
              <span className="text-xs text-slate-500">
                واتساب: {status.connection.found ? (status.connection.status ?? '—') : 'غير مربوط'}
              </span>
              <span className="text-xs text-slate-500">
                Catalog ID: {status.connection.meta_catalog_id ? status.connection.meta_catalog_id : '—'}
              </span>
              <span className="text-xs text-slate-500">
                تغطية retailer_id: {status.coverage.with_retailer_id} / {status.coverage.sample_size}
              </span>
            </div>
            <p className="text-sm text-slate-700 bg-slate-50 border border-slate-100 rounded-lg p-3">
              {status.advice}
            </p>
          </div>
        </Card>
      )}

      {/* ── Configuration form ──────────────────────────────────── */}
      <Card title="إعدادات الكتالوج" icon={<BookOpen className="w-5 h-5 text-emerald-600" />}>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Catalog ID من Meta Commerce Manager
            </label>
            <input
              type="text"
              dir="ltr"
              value={catalogId}
              onChange={e => setCatalogId(e.target.value)}
              placeholder="مثال: 1234567890123456"
              className="w-full rounded-xl border border-slate-200 px-4 py-2.5 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
            />
            <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
              افتح{' '}
              <a
                href="https://business.facebook.com/commerce/"
                target="_blank" rel="noopener noreferrer"
                className="text-emerald-600 hover:underline inline-flex items-center gap-0.5"
              >
                Meta Commerce Manager <ExternalLink className="w-3 h-3" />
              </a>
              {' '}→ Catalog → Settings → انسخ قيمة <code className="bg-slate-100 px-1 rounded">Catalog ID</code>
              {' '}والصقها هنا. تأكد أن الكتالوج مرتبط برقم واتساب الأعمال نفسه.
            </p>
          </div>

          <div className="flex items-center justify-between gap-3 bg-slate-50 border border-slate-100 rounded-xl p-4">
            <div>
              <p className="font-semibold text-sm text-slate-800">تفعيل إرسال المنتج عبر الكتالوج</p>
              <p className="text-xs text-slate-500 mt-0.5">
                عند التفعيل، يستخدم الذكاء كرت المنتج الرسمي من واتساب
                بدلاً من رابط بسيط — لتجربة أقرب للمتجر الإلكتروني.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setEnabled(p => !p)}
              className={`shrink-0 inline-flex items-center gap-1 px-3 py-1.5 rounded-full text-sm font-semibold transition ${enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-200 text-slate-500'}`}
              aria-pressed={enabled}
            >
              {enabled ? <ToggleRight className="w-4 h-4" /> : <ToggleLeft className="w-4 h-4" />}
              {enabled ? 'مُفعّل' : 'مُعطّل'}
            </button>
          </div>

          <div className="flex justify-end">
            <button
              onClick={onSave}
              disabled={saving}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2.5 rounded-xl text-sm transition"
            >
              {saving && <Loader2 className="w-4 h-4 animate-spin" />}
              حفظ الإعدادات
            </button>
          </div>
        </div>
      </Card>

      {/* ── Product sample ───────────────────────────────────────── */}
      {status && status.products_sample.length > 0 && (
        <Card title="عيّنة المنتجات وتغطية retailer_id" icon={<Package className="w-5 h-5 text-emerald-600" />}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 uppercase">
                <tr className="border-b border-slate-100">
                  <th className="text-right py-2 pl-2">ID</th>
                  <th className="text-right py-2 pl-2">المنتج</th>
                  <th className="text-right py-2 pl-2">external_id</th>
                  <th className="text-right py-2 pl-2">effective retailer_id</th>
                </tr>
              </thead>
              <tbody>
                {status.products_sample.map(p => (
                  <tr key={p.id} className="border-b border-slate-50 last:border-0">
                    <td className="py-2 pl-2 text-slate-500">{p.id}</td>
                    <td className="py-2 pl-2 text-slate-800 truncate max-w-[280px]">{p.title}</td>
                    <td className="py-2 pl-2 text-slate-500 font-mono text-xs">{p.external_id ?? '—'}</td>
                    <td className="py-2 pl-2">
                      {p.effective_retailer_id
                        ? <code className="bg-emerald-50 text-emerald-700 text-xs px-1.5 py-0.5 rounded">{p.effective_retailer_id}</code>
                        : <span className="text-xs text-amber-700">مفقود</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* ── Test send ────────────────────────────────────────────── */}
      <Card title="إرسال تجريبي" icon={<Send className="w-5 h-5 text-emerald-600" />}>
        <div className="space-y-3">
          <p className="text-xs text-slate-500 leading-relaxed">
            أرسل منتجاً اختبارياً إلى رقم واتساب (يفضّل رقمك الشخصي)
            لمعاينة الكرت الذي سيظهر للعملاء. يمر الإرسال عبر نفس المسار
            الذي يستخدمه الذكاء — كتالوج، أو صورة + زر، أو رابط CTA.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <input
              dir="ltr"
              placeholder="رقم الاستلام (9665XXXXXXXX)"
              value={testTo}
              onChange={e => setTestTo(e.target.value)}
              className="rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
            />
            <input
              placeholder="عنوان المنتج (مثال: عسل السمر)"
              value={testTitle}
              onChange={e => setTestTitle(e.target.value)}
              className="rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
            />
            <input
              dir="ltr"
              placeholder="أو product_id"
              value={testProductId}
              onChange={e => {
                const v = e.target.value.trim()
                setTestProductId(v === '' ? '' : Number.isFinite(Number(v)) ? Number(v) : '')
              }}
              className="rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
            />
          </div>
          <div className="flex justify-end">
            <button
              onClick={onTestSend}
              disabled={testing || !testTo.trim() || (!testTitle.trim() && testProductId === '')}
              className="inline-flex items-center gap-2 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white font-semibold px-5 py-2.5 rounded-xl text-sm transition"
            >
              {testing && <Loader2 className="w-4 h-4 animate-spin" />}
              إرسال تجريبي
            </button>
          </div>

          {testResult && (
            <div className="mt-3 bg-slate-50 border border-slate-100 rounded-xl p-4 text-sm space-y-2">
              <p className="font-bold text-slate-800 flex items-center gap-2">
                {testResult.ok
                  ? <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                  : <XCircle className="w-4 h-4 text-rose-600" />}
                نتيجة الإرسال:{' '}
                <code className="text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded text-xs">
                  {testResult.final_mode}
                </code>
              </p>
              <p className="text-xs text-slate-600">
                المنتج: <strong>{testResult.product.title}</strong>
                {' '}(retailer_id: <code>{testResult.product.retailer_id ?? '—'}</code>)
              </p>
              <ul className="text-xs text-slate-600 space-y-1">
                <li>• كتالوج: {testResult.catalog.attempted ? (testResult.catalog.succeeded ? 'نجح ✅' : `فشل (${testResult.catalog.reason})`) : 'لم يُحاول'}</li>
                <li>• صورة + زر: {testResult.image_cta.attempted ? (testResult.image_cta.image_ok ? 'نجح ✅' : 'فشل') : 'لم يُحاول'}</li>
                <li>• CTA فقط: {testResult.cta_only.attempted ? (testResult.cta_only.ok ? 'نجح ✅' : 'فشل') : 'لم يُحاول'}</li>
              </ul>
            </div>
          )}
        </div>
      </Card>
    </div>
  )
}
