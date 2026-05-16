/**
 * WhatsAppCatalog.tsx  →  rendered at /catalog (canonical) AND
 *                        /whatsapp-catalog (legacy alias).
 * ──────────────────────────────────────────────────────────────────
 * Merchant settings page for the **Nahla Product Catalog**.
 *
 * Mental model: the catalog is a first-class asset INSIDE Nahla.
 * Product sources fill it (Salla sync / manual entry / future
 * Shopify/Woo/CSV) and channels consume it (WhatsApp catalog
 * messages, AI [PRODUCT:...] resolver, campaigns, future checkout).
 * WhatsApp + Meta Commerce Manager are CHANNELS, not the catalog
 * itself — that's why the Meta-side config (Catalog ID, enabled
 * toggle) sits inside a dedicated "ربط الكتالوج بواتساب وMeta"
 * sub-section instead of being the whole page.
 *
 * Sections (top → bottom):
 *  1) Diagnostics snapshot (catalog-source + readiness pill) —
 *     /merchant/catalog/diagnostics
 *  2) Status / advice banner — /merchant/catalog/status
 *  3) "ربط الكتالوج بواتساب وMeta" — config form (Meta Catalog ID
 *     + catalog_enabled toggle + Save)
 *  4) Products mapping table + Resync + Source badge per row
 *  5) Test send panel
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
  Package, RefreshCw, Send, ShieldCheck, ToggleLeft, ToggleRight, XCircle,
  Store, MessageCircle, Database,
} from 'lucide-react'
import {
  catalogApi,
  type CatalogDiagnostics,
  type CatalogProductDiagResponse,
  type CatalogResyncReport,
  type CatalogStatus,
  type CatalogTestSendResult,
  type ProductSource,
  type DominantSource,
} from '../api/catalog'

// Source → Arabic label + colour palette mapping. Single source of
// truth for the source badges that appear on the diagnostics card AND
// inside the product mapping table.
const SOURCE_META: Record<DominantSource, { label: string; bg: string; text: string }> = {
  salla:   { label: 'سلة',    bg: 'bg-orange-50  border-orange-200',  text: 'text-orange-700' },
  zid:     { label: 'زد',     bg: 'bg-violet-50  border-violet-200',  text: 'text-violet-700' },
  manual:  { label: 'يدوي',   bg: 'bg-sky-50     border-sky-200',     text: 'text-sky-700'    },
  unknown: { label: 'غير محدد', bg: 'bg-slate-50  border-slate-200',  text: 'text-slate-600'  },
  mixed:   { label: 'مختلط',  bg: 'bg-amber-50   border-amber-200',   text: 'text-amber-700'  },
}

function SourceBadge({ source, size = 'sm' }: { source: ProductSource | DominantSource; size?: 'sm' | 'md' }) {
  const meta = SOURCE_META[source] ?? SOURCE_META.unknown
  const cls = size === 'md' ? 'text-xs px-2.5 py-1' : 'text-[11px] px-2 py-0.5'
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border font-semibold ${cls} ${meta.bg} ${meta.text}`}>
      {meta.label}
    </span>
  )
}

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

  // Product diagnostic + resync
  const [products, setProducts]     = useState<CatalogProductDiagResponse | null>(null)
  const [productsLoading, setProductsLoading] = useState(false)
  const [resyncing, setResyncing]   = useState(false)
  const [resyncReport, setResyncReport] = useState<CatalogResyncReport | null>(null)

  // Source-agnostic diagnostics snapshot (driven by
  // /merchant/catalog/diagnostics). Optional — null on initial load
  // and on network failure. The page renders WITHOUT the diagnostics
  // card if this is null, so the legacy status surface keeps working.
  const [diagnostics, setDiagnostics] = useState<CatalogDiagnostics | null>(null)

  const loadDiagnostics = async () => {
    try {
      const d = await catalogApi.diagnostics()
      setDiagnostics(d)
    } catch {
      // soft-fail — the rest of the page still works
    }
  }

  const loadProducts = async () => {
    setProductsLoading(true)
    try {
      const r = await catalogApi.products(100, 0)
      setProducts(r)
    } catch {
      // soft-fail — the page is still useful without this section
    } finally {
      setProductsLoading(false)
    }
  }

  const onResync = async () => {
    setResyncing(true)
    setError(null)
    setSuccess(null)
    setResyncReport(null)
    try {
      const r = await catalogApi.resync()
      setResyncReport(r.report)
      setSuccess(
        `تمت إعادة المزامنة: تم تعيين retailer_id لـ ${r.report.retailer_id_set} منتج، ` +
        `${r.report.synthetic_assigned} منها بمعرّف اصطناعي.`,
      )
      // Refresh status + products + diagnostics to reflect new coverage.
      await refresh()
      await loadProducts()
      await loadDiagnostics()
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر تنفيذ إعادة المزامنة.')
    } finally {
      setResyncing(false)
    }
  }

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

  useEffect(() => {
    void refresh()
    void loadProducts()
    void loadDiagnostics()
  }, [])

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
          كتالوج المنتجات
        </h1>
        <p className="text-sm text-slate-500 mt-1 leading-relaxed">
          كتالوج المنتجات هو المصدر الموحّد لمنتجاتك داخل نحلة.
          يمكن جلبه تلقائياً من سلة، أو إضافته يدوياً، ثم استخدامه في
          واتساب والذكاء والحملات. يستخدم الذكاء هذا الكتالوج لإرسال
          كرت منتج رسمي (صورة + سعر + زر شراء) بدلاً من رابط نصي.
        </p>
      </div>

      {/* ── Diagnostics snapshot (source-agnostic) ──────────────── */}
      {diagnostics && (
        <Card title="حالة الكتالوج" icon={<Database className="w-5 h-5 text-emerald-600" />}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {/* Readiness pill */}
            <div className={`rounded-xl border p-3 ${
              diagnostics.readiness.catalog_ready
                ? 'bg-emerald-50 border-emerald-200'
                : 'bg-amber-50 border-amber-200'
            }`}>
              <div className="flex items-center gap-2">
                {diagnostics.readiness.catalog_ready
                  ? <CheckCircle2 className="w-5 h-5 text-emerald-600" />
                  : <AlertTriangle className="w-5 h-5 text-amber-600" />}
                <span className="text-sm font-bold text-slate-800">
                  {diagnostics.readiness.catalog_ready
                    ? 'الكتالوج جاهز للإرسال عبر واتساب'
                    : 'الكتالوج غير جاهز بعد'}
                </span>
              </div>
              <p className="text-xs text-slate-600 mt-1.5 leading-relaxed">
                {diagnostics.catalog.catalog_id_present
                  ? <>تم ربط Meta Catalog بـ ID <code dir="ltr" className="bg-slate-100 px-1 rounded">{diagnostics.catalog.catalog_id}</code></>
                  : 'لم يتم ربط Meta Catalog بعد — راجع قسم "ربط الكتالوج بواتساب وMeta" أدناه.'}
              </p>
            </div>

            {/* Product source */}
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="flex items-center gap-2">
                <Store className="w-5 h-5 text-slate-500" />
                <span className="text-sm font-bold text-slate-800">مصدر المنتجات</span>
                <SourceBadge source={diagnostics.products.dominant_source} size="md" />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {(Object.entries(diagnostics.products.source_breakdown) as Array<[ProductSource, number]>)
                  .filter(([, n]) => n > 0)
                  .map(([src, n]) => (
                    <span
                      key={src}
                      className="inline-flex items-center gap-1 rounded-full bg-white border border-slate-200 text-[11px] px-2 py-0.5 text-slate-600"
                      title={`${n} منتج من ${SOURCE_META[src]?.label ?? src}`}
                    >
                      <SourceBadge source={src} /> × {n}
                    </span>
                  ))}
                {diagnostics.products.total === 0 && (
                  <span className="text-[11px] text-slate-500">لا توجد منتجات بعد — أضف منتجات يدوياً أو اربط متجر سلة.</span>
                )}
              </div>
            </div>

            {/* Coverage */}
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="flex items-center gap-2">
                <Package className="w-5 h-5 text-slate-500" />
                <span className="text-sm font-bold text-slate-800">تغطية retailer_id</span>
              </div>
              <p className="text-xs text-slate-600 mt-1.5">
                {diagnostics.products.with_effective_retailer_id} من {diagnostics.products.total} منتج
                ({diagnostics.products.coverage_pct}%) لديها معرّف صالح للإرسال عبر Meta Catalog.
              </p>
              {diagnostics.products.without_effective_retailer_id > 0 && (
                <p className="text-[11px] text-amber-700 mt-1">
                  استخدم زر "إعادة مزامنة وربط" أدناه لتعيين معرّفات للمنتجات غير المربوطة.
                </p>
              )}
            </div>

            {/* WhatsApp channel readiness */}
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="flex items-center gap-2">
                <MessageCircle className="w-5 h-5 text-slate-500" />
                <span className="text-sm font-bold text-slate-800">قناة الإرسال</span>
              </div>
              <p className="text-xs text-slate-600 mt-1.5">
                {diagnostics.catalog.whatsapp_connected
                  ? 'واتساب الأعمال متصل وجاهز للإرسال.'
                  : 'لم يتم ربط واتساب الأعمال بعد — اربطه من صفحة "واتساب".'}
              </p>
            </div>
          </div>
        </Card>
      )}

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

      {/* ── Channel binding (WhatsApp + Meta) ────────────────────────
            Catalog itself is channel-agnostic; this sub-section is
            specifically the WhatsApp/Meta channel wire-up. */}
      <Card
        title="ربط الكتالوج بواتساب وMeta"
        icon={<MessageCircle className="w-5 h-5 text-emerald-600" />}
      >
        <div className="space-y-4">
          <div className="text-xs leading-relaxed text-slate-600 bg-slate-50 border border-slate-100 rounded-lg p-3">
            هذا القسم خاص بقناة واتساب فقط. الكتالوج نفسه مستقل ويمكن
            استخدامه لاحقاً في قنوات أخرى (الحملات، الذكاء، الدفع).
            هنا فقط نربط منتجات نحلة بـ Meta Commerce Manager ليتمكن
            واتساب الأعمال من إرسال كرت منتج رسمي.
          </div>
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

      {/* ── Product mapping (resync + full coverage table) ────────── */}
      <Card title="ربط المنتجات بالكتالوج" icon={<Package className="w-5 h-5 text-emerald-600" />}>
        <div className="space-y-4">
          {/* Coverage summary + resync button */}
          <div className="flex flex-wrap items-center justify-between gap-3 bg-slate-50 border border-slate-100 rounded-xl p-4">
            <div className="space-y-1">
              <p className="text-sm font-semibold text-slate-800">
                تغطية retailer_id:{' '}
                <span className="text-emerald-700">
                  {products ? products.coverage.with_rid : '—'}
                </span>
                <span className="text-slate-400"> / </span>
                <span className="text-slate-700">
                  {products ? products.coverage.total : '—'}
                </span>
                {products && products.coverage.total > 0 && (
                  <span className="text-xs text-slate-500 mr-2">
                    ({Math.round(products.coverage.with_rid / products.coverage.total * 100)}%)
                  </span>
                )}
              </p>
              <p className="text-xs text-slate-500 leading-relaxed">
                إعادة المزامنة تربط كل منتج بمعرّف Meta retailer_id تلقائيًا
                (عبر external_id، أو معرّف اصطناعي إذا لزم). آمنة وتشغّل أكثر من مرة.
              </p>
            </div>
            <button
              onClick={onResync}
              disabled={resyncing}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-4 py-2 rounded-xl text-sm transition shrink-0"
            >
              {resyncing
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <RefreshCw className="w-4 h-4" />}
              إعادة مزامنة وربط
            </button>
          </div>

          {resyncReport && (
            <div className="bg-emerald-50/60 border border-emerald-100 rounded-xl p-4 text-xs text-emerald-900 space-y-1">
              <p className="font-bold">تقرير المزامنة</p>
              <ul className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-1">
                <li>تم المسح: {resyncReport.scanned}</li>
                <li>تم التعيين: {resyncReport.retailer_id_set}</li>
                <li>كان معيّن مسبقًا: {resyncReport.already_set}</li>
                <li>معرّف اصطناعي: {resyncReport.synthetic_assigned}</li>
                <li>تم نشره: {resyncReport.published_stamped}</li>
                <li>أخطاء: {resyncReport.errors}</li>
              </ul>
            </div>
          )}

          {/* Products table */}
          {productsLoading && (
            <div className="text-sm text-slate-500 flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> جاري تحميل المنتجات...
            </div>
          )}

          {products && products.rows.length > 0 && (
            <div className="overflow-x-auto -mx-2">
              <table className="w-full text-sm">
                <thead className="text-xs text-slate-500 uppercase">
                  <tr className="border-b border-slate-100">
                    <th className="text-right py-2 px-2">ID</th>
                    <th className="text-right py-2 px-2">المنتج</th>
                    <th className="text-right py-2 px-2">المصدر</th>
                    <th className="text-right py-2 px-2">external_id</th>
                    <th className="text-right py-2 px-2">meta_retailer_id</th>
                    <th className="text-right py-2 px-2">retailer_id فعّال</th>
                    <th className="text-right py-2 px-2">الحالة</th>
                  </tr>
                </thead>
                <tbody>
                  {products.rows.map(p => (
                    <tr key={p.id} className="border-b border-slate-50 last:border-0">
                      <td className="py-2 px-2 text-slate-500">{p.id}</td>
                      <td className="py-2 px-2 text-slate-800 truncate max-w-[260px]" title={p.title}>{p.title}</td>
                      <td className="py-2 px-2"><SourceBadge source={p.source} /></td>
                      <td className="py-2 px-2 text-slate-500 font-mono text-xs" dir="ltr">{p.external_id ?? '—'}</td>
                      <td className="py-2 px-2 text-slate-500 font-mono text-xs" dir="ltr">{p.meta_retailer_id ?? '—'}</td>
                      <td className="py-2 px-2">
                        {p.effective_retailer_id
                          ? <code className="bg-emerald-50 text-emerald-700 text-xs px-1.5 py-0.5 rounded" dir="ltr">{p.effective_retailer_id}</code>
                          : <span className="text-xs text-amber-700">مفقود</span>}
                      </td>
                      <td className="py-2 px-2">
                        <span className={
                          'text-xs px-2 py-0.5 rounded-full font-semibold ' +
                          (p.publish_status === 'published'
                            ? 'bg-emerald-100 text-emerald-700'
                            : p.publish_status === 'ready'
                              ? 'bg-blue-50 text-blue-700'
                              : 'bg-amber-50 text-amber-700')
                        }>
                          {p.publish_status === 'published' ? 'منشور'
                            : p.publish_status === 'ready' ? 'جاهز'
                              : 'بحاجة لمزامنة'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {products.total > products.rows.length && (
                <p className="text-xs text-slate-400 mt-2 text-center">
                  عرض {products.rows.length} من أصل {products.total} منتج. استخدم Meta Commerce Manager لإدارة الباقي.
                </p>
              )}
            </div>
          )}

          {products && products.rows.length === 0 && (
            <p className="text-sm text-slate-500 bg-slate-50 border border-slate-100 rounded-xl p-4 text-center leading-relaxed">
              لا توجد منتجات بعد. اربط متجر سلة لجلب المنتجات تلقائياً،
              أو أضف منتجاتك يدوياً من القسم أدناه إذا لم يكن لديك متجر.
            </p>
          )}
        </div>
      </Card>

      {/* ── Manual products (Path 3) ─────────────────────────────────
            Inline editor for merchants without a synced store. Wires
            to /merchant/catalog/products/manual. We render it for ALL
            tenants — even Salla merchants may want to add a one-off
            promotional product that doesn't exist in their store. */}
      <ManualProductsSection
        currentSource={diagnostics?.products.dominant_source ?? 'unknown'}
        onChanged={async () => {
          await loadProducts()
          await loadDiagnostics()
        }}
      />

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


// ─────────────────────────────────────────────────────────────────────
// Manual products section (Path 3)
// ─────────────────────────────────────────────────────────────────────
//
// Standalone block — pulled out of the main component so the page
// stays readable. Renders:
//   • a brief explainer about when to use manual entry,
//   • a single inline form (collapsed by default) for adding a new
//     manual product,
//   • a flash success/error region.
//
// We do NOT render a full list of manual products here — the main
// catalog table above already lists EVERY product with a source badge,
// so manual rows are visible there. Edit/delete UX for an individual
// manual product is a follow-up PR (right now this section is "create
// only", which covers the immediate need: a no-Salla merchant being
// able to seed their catalog).

function ManualProductsSection(props: {
  currentSource: DominantSource
  onChanged: () => Promise<void>
}) {
  const [open, setOpen]       = useState(false)
  const [busy, setBusy]       = useState(false)
  const [error, setError]     = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const [title, setTitle]               = useState('')
  const [price, setPrice]               = useState('')
  const [description, setDescription]   = useState('')
  const [imageUrl, setImageUrl]         = useState('')
  const [productUrl, setProductUrl]     = useState('')
  const [metaRid, setMetaRid]           = useState('')

  const reset = () => {
    setTitle(''); setPrice(''); setDescription('')
    setImageUrl(''); setProductUrl(''); setMetaRid('')
  }

  const onSubmit = async () => {
    setBusy(true); setError(null); setSuccess(null)
    try {
      const t = title.trim()
      if (!t) {
        setError('اسم المنتج مطلوب.')
        setBusy(false)
        return
      }
      const created = await catalogApi.createManualProduct({
        title:            t,
        price:            price.trim() || undefined,
        description:      description.trim() || undefined,
        image_url:        imageUrl.trim() || undefined,
        product_url:      productUrl.trim() || undefined,
        meta_retailer_id: metaRid.trim() || undefined,
      })
      setSuccess(`تم إضافة "${created.title}" بنجاح إلى الكتالوج.`)
      reset()
      setOpen(false)
      await props.onChanged()
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر إضافة المنتج.')
    } finally {
      setBusy(false)
    }
  }

  // Explainer copy adapts to the current dominant source: a Salla
  // merchant gets a different tone ("هذا اختياري") than a no-store
  // merchant ("ابدأ من هنا").
  const explainer =
    props.currentSource === 'salla' || props.currentSource === 'zid'
      ? 'منتجاتك من المتجر تُجلب تلقائياً. استخدم هذا القسم فقط لإضافة منتج خاص بنحلة (مثلاً عرض ترويجي لا يوجد في المتجر).'
      : 'إذا لم يكن لديك متجر سلة أو زد، أضف منتجاتك يدوياً هنا ليتمكن الذكاء وواتساب من استخدامها.'

  return (
    <Card title="إضافة منتج يدوي" icon={<Store className="w-5 h-5 text-emerald-600" />}>
      <div className="space-y-4">
        <div className="text-xs text-slate-600 bg-slate-50 border border-slate-100 rounded-lg p-3 leading-relaxed">
          {explainer}
        </div>

        {error && (
          <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded-xl px-3 py-2 text-sm flex items-start gap-2">
            <XCircle className="w-4 h-4 shrink-0 mt-0.5" /> {error}
          </div>
        )}
        {success && (
          <div className="bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-xl px-3 py-2 text-sm flex items-start gap-2">
            <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" /> {success}
          </div>
        )}

        {!open && (
          <button
            type="button"
            onClick={() => { setOpen(true); setSuccess(null) }}
            className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-4 py-2 rounded-xl text-sm transition"
          >
            <Package className="w-4 h-4" />
            إضافة منتج جديد
          </button>
        )}

        {open && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="md:col-span-2">
              <label className="block text-xs font-semibold text-slate-700 mb-1">اسم المنتج <span className="text-rose-500">*</span></label>
              <input
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder="مثال: عسل سدر فاخر 500 جرام"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">السعر</label>
              <input
                value={price}
                onChange={e => setPrice(e.target.value)}
                placeholder="مثال: 95 ر.س"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">رابط الصورة</label>
              <input
                value={imageUrl}
                onChange={e => setImageUrl(e.target.value)}
                placeholder="https://..."
                dir="ltr"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">رابط المنتج</label>
              <input
                value={productUrl}
                onChange={e => setProductUrl(e.target.value)}
                placeholder="https://..."
                dir="ltr"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Meta retailer_id (اختياري)
              </label>
              <input
                value={metaRid}
                onChange={e => setMetaRid(e.target.value)}
                placeholder="اتركه فارغاً ليتم توليده تلقائيًا"
                dir="ltr"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
              <p className="text-[11px] text-slate-500 mt-1 leading-relaxed">
                املأ هذا فقط إذا نشرت المنتج في Meta Commerce Manager بمعرّف مخصّص.
                وإلا، سيستخدم النظام معرّفاً اصطناعياً لعرضه عبر واتساب.
              </p>
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs font-semibold text-slate-700 mb-1">الوصف</label>
              <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                rows={3}
                placeholder="وصف مختصر يظهر مع كرت المنتج."
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none resize-none"
              />
            </div>
            <div className="md:col-span-2 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => { setOpen(false); reset(); setError(null) }}
                className="px-4 py-2 rounded-xl text-sm font-semibold text-slate-600 hover:bg-slate-100 transition"
              >
                إلغاء
              </button>
              <button
                type="button"
                onClick={onSubmit}
                disabled={busy}
                className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm transition"
              >
                {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                حفظ المنتج
              </button>
            </div>
          </div>
        )}
      </div>
    </Card>
  )
}
