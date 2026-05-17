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
  Store, MessageCircle, Database, ArrowDown, Bot, Megaphone, ShoppingBag,
  Download, Clock,
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
  type MetaImportReport,
} from '../api/catalog'
import ProductStudio from './ProductStudio'

// Source → Arabic label + colour palette mapping. Single source of
// truth for the source badges that appear on the diagnostics card AND
// inside the product mapping table.
const SOURCE_META: Record<DominantSource, { label: string; bg: string; text: string }> = {
  salla:   { label: 'سلة',    bg: 'bg-orange-50  border-orange-200',  text: 'text-orange-700' },
  zid:     { label: 'زد',     bg: 'bg-violet-50  border-violet-200',  text: 'text-violet-700' },
  meta:    { label: 'Meta',   bg: 'bg-blue-50    border-blue-200',    text: 'text-blue-700'   },
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

  // ── Top-header import button (May 2026 UI revamp) ──────────────────
  // The full-fledged Meta-import section still lives lower in the
  // page (with its diagnostics, error copy, and report card), but
  // merchants expect a "استيراد من Meta" CTA next to the page title
  // — the way Meta Commerce Manager places its Add/Import buttons in
  // the page header. We anchor-scroll to the same section instead of
  // duplicating logic, so the source of truth stays single.
  const scrollToMetaImport = () => {
    const el = document.getElementById('meta-import-section')
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }

  // Layout note: this page DELIBERATELY does NOT clamp itself to
  // ``max-w-4xl mx-auto`` — the catalog is now a daily-driver page
  // whose product grid needs the full app width (12+ columns of
  // product metadata). The outer ``<main>`` already applies the
  // right horizontal padding; we just let the content fill what's
  // left after the sidebar.
  return (
    <div className="space-y-6 w-full" dir="rtl">
      {/* ── Header: title + primary CTAs (Meta-style command bar) ──
            Pinned at the top so merchants always see the import +
            add-product actions, regardless of how far down the
            page they scroll. The lower-down sections (sub-cards)
            still hold the full configuration UX. */}
      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm px-5 py-5 flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-black text-slate-900 flex items-center gap-2">
            <Package className="w-7 h-7 text-emerald-600" />
            كتالوج المنتجات
            {diagnostics && (
              <span className="text-base font-bold text-slate-500 bg-slate-100 rounded-full px-3 py-0.5">
                {diagnostics.products.total} منتج
              </span>
            )}
          </h1>
          <p className="text-sm text-slate-500 mt-1.5 leading-relaxed max-w-3xl">
            كتالوج المنتجات هو المصدر الموحّد لمنتجاتك داخل نحلة.
            يمكن جلبه تلقائياً من سلة، أو إضافته يدوياً، أو استيراده
            من Meta، ثم استخدامه في واتساب والذكاء والحملات. يستخدم
            الذكاء هذا الكتالوج لإرسال كرت منتج رسمي (صورة + سعر +
            زر شراء) بدلاً من رابط نصي.
          </p>
        </div>
        {/* Primary CTAs — visible above the fold. The Meta import
            button is conditional on the channel being wired,
            matching the behaviour of the section below. */}
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          {diagnostics?.catalog.catalog_id_present && (
            <button
              type="button"
              onClick={scrollToMetaImport}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-4 py-2.5 rounded-xl text-sm transition shadow-sm"
            >
              <Download className="w-4 h-4" />
              استيراد من Meta
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              const el = document.getElementById('manual-product-section')
              if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
            }}
            className="inline-flex items-center gap-2 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-4 py-2.5 rounded-xl text-sm transition"
          >
            <Package className="w-4 h-4" />
            إضافة منتج يدوي
          </button>
          <button
            type="button"
            onClick={onResync}
            disabled={resyncing}
            className="inline-flex items-center gap-2 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-4 py-2.5 rounded-xl text-sm transition disabled:opacity-50"
          >
            {resyncing
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <RefreshCw className="w-4 h-4" />}
            إعادة مزامنة
          </button>
        </div>
      </div>

      {/* ── Hub diagram: Sources → Catalog → Channels ─────────────
            Visual mental model: Nahla Catalog is the central hub.
            Sources feed it (Manual / Salla / Meta-import / future);
            Channels consume it (WhatsApp / AI / Campaigns / future).
            The diagram reads the same diagnostics payload that the
            "حالة الكتالوج" card uses — single source of truth. */}
      {diagnostics && (
        <HubDiagramCard diagnostics={diagnostics} />
      )}

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

      {/* ── Product Studio (PROMOTED to above the fold) ──────────────
            Daily-driver content. Moved above the channel-binding card
            so the table is the FIRST thing merchants see after the
            status banner — matches Meta Commerce Manager's IA where
            the products table is the page, and the settings are tabs
            beside it. The settings cards below still hold the full
            configuration UX (channel binding / manual add / Meta
            import / test send). */}
      <section
        id="product-studio"
        className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden"
      >
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Package className="w-5 h-5 text-emerald-600" />
          <div className="flex-1 min-w-0">
            <h3 className="text-base font-bold text-slate-800">استوديو المنتجات</h3>
            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">
              اضغط على أي منتج لفتحه في نافذة جانبية ومعاينة جاهزيته للنشر عبر القنوات
              (واتساب / Meta / الذكاء / الحملات / Google قريباً) مع عدّادات الحدود الحيّة.
            </p>
          </div>
        </div>
        <div className="p-5">
          <ProductStudio />
        </div>
      </section>

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

      {/* ── Catalog tools (resync + retailer_id coverage) ───────────
            Catalog-level operations live above the Studio. Per-product
            actions live INSIDE the Studio (the grid + drawer). */}
      <Card title="أدوات الكتالوج" icon={<RefreshCw className="w-5 h-5 text-emerald-600" />}>
        <div className="space-y-4">
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

      {/* ── Import from Meta (Hub: Meta → Nahla) ────────────────────
            Optional source for merchants who already have products
            inside Meta Commerce Manager. Only renders when the Meta
            channel is wired (we need a catalog_id + token to pull). */}
      {diagnostics?.catalog.catalog_id_present && (
        <MetaImportSection
          onChanged={async () => {
            await loadProducts()
            await loadDiagnostics()
          }}
        />
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


// ─────────────────────────────────────────────────────────────────────
// Hub diagram (May 2026 #14 — Hub architecture)
// ─────────────────────────────────────────────────────────────────────
//
// Visual representation of the catalog hub: Sources on the left feed
// the central Catalog box, which then feeds Channels on the right.
// Each Source / Channel renders an availability state read from the
// existing diagnostics payload — no new endpoint required.
//
// Source states:
//   • ``active``   — at least one product on this source is in Nahla
//   • ``available`` — wired but no products imported yet (e.g. Meta
//                     catalog_id present but no Meta-source rows)
//   • ``unused``   — not wired
//
// Channel states:
//   • ``live``     — channel is wired AND has data to send
//   • ``available`` — wired but no products to consume yet
//   • ``planned``  — future feature (Google Merchant, Checkout)
//   • ``unused``   — not wired

type NodeStatus = 'live' | 'active' | 'available' | 'unused' | 'planned'

const STATUS_STYLES: Record<NodeStatus, { pill: string; dot: string; label: string }> = {
  live:      { pill: 'bg-emerald-50 border-emerald-200', dot: 'bg-emerald-500',   label: 'مُفعّل' },
  active:    { pill: 'bg-emerald-50 border-emerald-200', dot: 'bg-emerald-500',   label: 'يغذّي الكتالوج' },
  available: { pill: 'bg-blue-50    border-blue-200',    dot: 'bg-blue-500',      label: 'متاح' },
  unused:    { pill: 'bg-slate-50   border-slate-200',   dot: 'bg-slate-300',     label: 'غير مربوط' },
  planned:   { pill: 'bg-amber-50   border-amber-200',   dot: 'bg-amber-400',     label: 'قريباً' },
}

function HubNode(props: {
  icon: React.ReactNode
  title: string
  subtitle?: string
  status: NodeStatus
}) {
  const s = STATUS_STYLES[props.status]
  return (
    <div className={`relative rounded-xl border p-3 ${s.pill}`}>
      <div className="flex items-center gap-2">
        <div className="shrink-0">{props.icon}</div>
        <div className="min-w-0">
          <div className="font-bold text-sm text-slate-800 truncate">{props.title}</div>
          {props.subtitle && (
            <div className="text-[11px] text-slate-500 truncate">{props.subtitle}</div>
          )}
        </div>
      </div>
      <div className="absolute top-1.5 left-1.5 flex items-center gap-1">
        <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
        <span className="text-[10px] text-slate-600">{s.label}</span>
      </div>
    </div>
  )
}

function HubDiagramCard(props: { diagnostics: CatalogDiagnostics }) {
  const d = props.diagnostics
  const breakdown = d.products.source_breakdown

  // Source availability — derived purely from the diagnostics payload.
  const sallaCount  = breakdown.salla  ?? 0
  const metaCount   = breakdown.meta   ?? 0
  const manualCount = breakdown.manual ?? 0

  const sallaStatus:  NodeStatus = sallaCount > 0 ? 'active' : 'unused'
  const metaStatus:   NodeStatus =
    metaCount > 0 ? 'active'
    : d.catalog.catalog_id_present ? 'available'
    : 'unused'
  const manualStatus: NodeStatus = manualCount > 0 ? 'active' : 'available'

  // Channel availability.
  const waStatus: NodeStatus =
    d.readiness.catalog_ready ? 'live'
    : d.catalog.whatsapp_connected ? 'available'
    : 'unused'
  const aiStatus: NodeStatus = d.products.total > 0 ? 'live' : 'available'
  const campaignsStatus: NodeStatus = d.products.total > 0 ? 'available' : 'unused'

  return (
    <Card title="بنية الكتالوج (Hub)" icon={<Database className="w-5 h-5 text-emerald-600" />}>
      <p className="text-xs text-slate-600 leading-relaxed mb-4">
        كتالوج نحلة هو المصدر المركزي للمنتجات. <strong>المصادر</strong> تغذّي الكتالوج،
        و<strong>القنوات</strong> تستهلك منه. الذكاء يقرأ من كتالوج نحلة فقط — أبداً
        من سلة أو Meta مباشرة.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-[1fr_auto_1fr_auto_1fr] gap-3 items-center">
        {/* ── Sources column ── */}
        <div className="space-y-2">
          <div className="text-xs font-bold text-slate-500 uppercase tracking-wide text-center mb-2">
            المصادر (Inputs)
          </div>
          <HubNode
            icon={<Store className="w-4 h-4 text-orange-600" />}
            title="سلة"
            subtitle={sallaCount > 0 ? `${sallaCount} منتج` : 'غير مربوط'}
            status={sallaStatus}
          />
          <HubNode
            icon={<Package className="w-4 h-4 text-blue-600" />}
            title="Meta Catalog"
            subtitle={metaCount > 0 ? `${metaCount} منتج (مستورد)` : (d.catalog.catalog_id_present ? 'جاهز للاستيراد' : 'لم يُربط بعد')}
            status={metaStatus}
          />
          <HubNode
            icon={<Store className="w-4 h-4 text-sky-600" />}
            title="إدخال يدوي"
            subtitle={manualCount > 0 ? `${manualCount} منتج` : 'متاح دائماً'}
            status={manualStatus}
          />
          <HubNode
            icon={<Clock className="w-4 h-4 text-amber-500" />}
            title="Shopify / CSV / Zid"
            subtitle="قريباً"
            status="planned"
          />
        </div>

        {/* Arrow Sources → Hub */}
        <div className="hidden md:flex flex-col items-center text-slate-400">
          <ArrowDown className="w-6 h-6 -rotate-90" />
        </div>
        <div className="flex md:hidden justify-center">
          <ArrowDown className="w-5 h-5 text-slate-400" />
        </div>

        {/* ── Catalog Hub (center) ── */}
        <div className="bg-gradient-to-br from-emerald-50 to-emerald-100 border-2 border-emerald-300 rounded-2xl p-4 text-center">
          <Database className="w-8 h-8 text-emerald-700 mx-auto mb-1" />
          <div className="font-black text-sm text-emerald-900">كتالوج نحلة</div>
          <div className="text-xs text-emerald-800 mt-1">
            {d.products.total} منتج
          </div>
          <div className="text-[11px] text-emerald-700 mt-1">
            المصدر الموحّد
          </div>
        </div>

        {/* Arrow Hub → Channels */}
        <div className="hidden md:flex flex-col items-center text-slate-400">
          <ArrowDown className="w-6 h-6 -rotate-90" />
        </div>
        <div className="flex md:hidden justify-center">
          <ArrowDown className="w-5 h-5 text-slate-400" />
        </div>

        {/* ── Channels column ── */}
        <div className="space-y-2">
          <div className="text-xs font-bold text-slate-500 uppercase tracking-wide text-center mb-2">
            القنوات (Outputs)
          </div>
          <HubNode
            icon={<MessageCircle className="w-4 h-4 text-emerald-600" />}
            title="WhatsApp Catalog"
            subtitle={waStatus === 'live' ? 'جاهز للإرسال' : (waStatus === 'available' ? 'يحتاج Meta Catalog ID' : 'اربط واتساب أولاً')}
            status={waStatus}
          />
          <HubNode
            icon={<Bot className="w-4 h-4 text-violet-600" />}
            title="الذكاء (AI)"
            subtitle={aiStatus === 'live' ? 'يقرأ من الكتالوج' : 'يحتاج منتجات'}
            status={aiStatus}
          />
          <HubNode
            icon={<Megaphone className="w-4 h-4 text-rose-600" />}
            title="الحملات"
            subtitle={campaignsStatus === 'available' ? 'متاح للاستخدام' : 'يحتاج منتجات'}
            status={campaignsStatus}
          />
          <HubNode
            icon={<ShoppingBag className="w-4 h-4 text-amber-500" />}
            title="Google Merchant / الدفع"
            subtitle="قريباً"
            status="planned"
          />
        </div>
      </div>
    </Card>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Meta import section (Path 4)
// ─────────────────────────────────────────────────────────────────────
//
// Trigger ``/merchant/catalog/import/meta``. Only mounted when the
// Meta channel is wired (catalog_id present) — otherwise the import
// would fail preflight anyway, so showing a button would be confusing.

function MetaImportSection(props: { onChanged: () => Promise<void> }) {
  const [busy, setBusy]               = useState(false)
  const [error, setError]             = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<Record<string, any> | null>(null)
  const [showDetail, setShowDetail]   = useState(false)
  const [report, setReport]           = useState<MetaImportReport | null>(null)

  const errorCopy = (code: string | undefined): string => {
    switch (code) {
      case 'connection_not_found':       return 'لا يوجد ربط واتساب حالياً. يرجى ربط واتساب أولاً.'
      case 'catalog_id_missing':         return 'يرجى إدخال Meta Catalog ID في قسم "ربط الكتالوج بواتساب وMeta" أعلاه.'
      case 'access_token_missing':       return 'الرمز المطلوب للوصول إلى Meta غير متوفر. أعد ربط واتساب لتجديده.'
      case 'meta_access_token_missing':  return 'الاتصال الحالي عبر 360dialog ولا يحمل توكِن Meta Graph. أعد ربط واتساب عبر Meta Embedded Signup مع منح صلاحية catalog_management، أو اطلب من الدعم تفعيل توكِن النظام.'
      case 'catalog_not_found':          return 'الـ Catalog ID المُدخل غير موجود في Meta. انسخه من Meta Commerce Manager → Catalog → Settings والصقه مرة أخرى. تأكد أنه Catalog ID وليس Commerce Account ID.'
      case 'catalog_type_unsupported':   return 'نوع الكتالوج غير مدعوم في نحلة (حالياً ندعم Commerce/Products فقط، أما السيارات/الفنادق/الطيران/الوظائف فتحتاج مستوردًا منفصلاً).'
      case 'meta_http_error':            return 'تعذّر الاتصال بـ Meta Catalog. الـ Catalog ID صحيح من حيث الشكل لكن Meta رفض الطلب — غالباً لأن التوكِن لا يملك صلاحية catalog_management على هذا الكتالوج، أو لأن الكتالوج في Business Manager مختلف عن BM رقم واتساب. راجع التفاصيل أدناه.'
      default:                           return code ? `خطأ غير متوقع: ${code}` : 'تعذّر تنفيذ الاستيراد.'
    }
  }

  const onImport = async () => {
    setBusy(true); setError(null); setErrorDetail(null); setShowDetail(false); setReport(null)
    try {
      const r = await catalogApi.importFromMeta()
      setReport(r.report)
      await props.onChanged()
    } catch (e: any) {
      // The API wrapper surfaces the FastAPI ``detail`` as ``e.code``
      // and stashes the full structured detail on ``e.validation``
      // (when present) or on ``e.detail``. We capture both so the
      // collapsible diagnostic block can show what Meta actually
      // said (token_source / provider / meta_message / fbtrace_id).
      const code  = e?.code ?? e?.detail ?? e?.message
      setError(errorCopy(code))
      const detail = (e && typeof e === 'object') ? {
        code:           code,
        status:         e?.status,
        provider:       e?.provider,
        token_source:   e?.token_source,
        catalog_id:     e?.catalog_id,
        hint:           e?.hint,
        meta_code:      e?.meta_code ?? e?.discovery?.error?.meta_code,
        meta_message:   e?.meta_message ?? e?.discovery?.error?.meta_message,
        fbtrace_id:     e?.fbtrace_id ?? e?.discovery?.error?.fbtrace_id,
        stage:          e?.stage,
      } : null
      // Drop empty entries so the panel only shows what actually has a value.
      const compact = detail
        ? Object.fromEntries(Object.entries(detail).filter(([, v]) => v !== undefined && v !== null && v !== ''))
        : null
      setErrorDetail(compact && Object.keys(compact).length > 0 ? compact : null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div id="meta-import-section">
    <Card title="استيراد المنتجات من Meta" icon={<Download className="w-5 h-5 text-emerald-600" />}>
      <div className="space-y-3">
        <p className="text-xs text-slate-600 leading-relaxed">
          إذا كانت منتجاتك جاهزة بالفعل في Meta Commerce Manager، يمكنك استيرادها مباشرة
          إلى كتالوج نحلة. الاستيراد آمن وقابل للإعادة (idempotent) — تشغيله مرّة أخرى
          يحدّث البيانات بدون تكرار. المنتجات اليدوية محميّة من الكتابة فوقها.
        </p>

        {error && (
          <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded-xl px-3 py-2 text-sm space-y-2">
            <div className="flex items-start gap-2">
              <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span className="leading-relaxed">{error}</span>
            </div>
            {errorDetail && (
              <div className="ms-6">
                <button
                  type="button"
                  onClick={() => setShowDetail(s => !s)}
                  className="text-[11px] underline text-rose-700 hover:text-rose-900"
                >
                  {showDetail ? 'إخفاء التفاصيل التقنية' : 'عرض التفاصيل التقنية للدعم'}
                </button>
                {showDetail && (
                  <pre dir="ltr" className="mt-2 bg-white border border-rose-100 rounded-lg p-2 text-[11px] text-slate-700 overflow-x-auto whitespace-pre-wrap leading-relaxed">
{JSON.stringify(errorDetail, null, 2)}
                  </pre>
                )}
              </div>
            )}
          </div>
        )}

        {report && (
          <div className="bg-emerald-50/60 border border-emerald-100 rounded-xl p-3 text-xs text-emerald-900 space-y-1">
            <p className="font-bold">تقرير الاستيراد</p>
            <ul className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-1">
              <li>تم المسح: {report.scanned}</li>
              <li>جديد: {report.created}</li>
              <li>محدّث: {report.updated}</li>
              <li>محمي (يدوي): {report.skipped_manual}</li>
              <li>أخطاء: {report.errors}</li>
              <li>صفحات: {report.pages_fetched}</li>
            </ul>
            {report.truncated && (
              <p className="text-amber-700 mt-1">
                تنبيه: تم بلوغ حد الصفحات. شغّل الاستيراد مرة أخرى لجلب الباقي.
              </p>
            )}
          </div>
        )}

        <button
          type="button"
          onClick={onImport}
          disabled={busy}
          className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm transition"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
          استيراد الآن من Meta
        </button>
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
  // ``id="manual-product-section"`` is used by the top-bar CTA so
  // clicking "إضافة منتج يدوي" up there scrolls down here.
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
    <div id="manual-product-section">
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
    </div>
  )
}
