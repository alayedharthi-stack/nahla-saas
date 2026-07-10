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
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import {
  AlertTriangle, CheckCircle2, Loader2,
  Package, RefreshCw, ToggleLeft, ToggleRight, XCircle,
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
import WabaCatalogLinkStatusCard from '../components/catalog/WabaCatalogLinkStatusCard'
import CatalogSummaryCard from '../components/catalog/CatalogSummaryCard'
import CatalogChannelsCard from '../components/catalog/CatalogChannelsCard'
import CatalogAdvancedSection, { AdvancedSubSection } from '../components/catalog/CatalogAdvancedSection'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import type { Lang, Translations } from '../i18n/types'

function localeTag(lang: Lang): string {
  return lang === 'en' ? 'en-US' : 'ar-SA'
}

function fmtCount(n: number, lang: Lang): string {
  return n.toLocaleString(localeTag(lang))
}

function fmtImportAt(iso: string | null, lang: Lang): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(localeTag(lang))
  } catch {
    return iso
  }
}

type CatalogSourceKey = keyof Translations['catalogMgmt']['sources']

const SOURCE_STYLES: Record<string, { bg: string; text: string }> = {
  salla:   { bg: 'bg-orange-50  border-orange-200',  text: 'text-orange-700' },
  zid:     { bg: 'bg-violet-50  border-violet-200',  text: 'text-violet-700' },
  meta:    { bg: 'bg-blue-50    border-blue-200',    text: 'text-blue-700'   },
  manual:  { bg: 'bg-sky-50     border-sky-200',     text: 'text-sky-700'    },
  unknown: { bg: 'bg-slate-50   border-slate-200',   text: 'text-slate-600'  },
  mixed:   { bg: 'bg-amber-50   border-amber-200',   text: 'text-amber-700'  },
}

function sourceStyle(source: ProductSource | DominantSource) {
  return SOURCE_STYLES[source] ?? SOURCE_STYLES.unknown
}

function SourceBadge({ source, size = 'sm' }: { source: ProductSource | DominantSource; size?: 'sm' | 'md' }) {
  const { tStatic } = useLanguage()
  const style = sourceStyle(source)
  const key = (source in SOURCE_STYLES ? source : 'unknown') as CatalogSourceKey
  const sources = tStatic(tr => tr.catalogMgmt.sources)
  const label = sources[key]
  const cls = size === 'md' ? 'text-xs px-2.5 py-1' : 'text-[11px] px-2 py-0.5'
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border font-semibold ${cls} ${style.bg} ${style.text}`}>
      {label}
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
  const { tStatic, lang, dir } = useLanguage()
  const cm = tStatic(tr => tr.catalogMgmt)
  void UI_ONLY_GUARD

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

  // Bumps ProductStudio after import / resync / manual add.
  const [productsRefresh, setProductsRefresh] = useState(0)
  const bumpProductList = () => setProductsRefresh(v => v + 1)

  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [manualFormOpen, setManualFormOpen] = useState(false)
  const [metaImportBusy, setMetaImportBusy] = useState(false)
  const metaImportRef = useRef<MetaImportHandle>(null)

  const openAdvanced = () => {
    setAdvancedOpen(true)
    requestAnimationFrame(() => {
      document.getElementById('catalog-advanced-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

  const openManualForm = () => {
    setManualFormOpen(true)
    openAdvanced()
    requestAnimationFrame(() => {
      document.getElementById('catalog-manual-product-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

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
        cm.messages.resyncSuccess
          .replace('{assigned}', fmtCount(r.report.retailer_id_set, lang))
          .replace('{synthetic}', fmtCount(r.report.synthetic_assigned, lang)),
      )
      // Refresh status + products + diagnostics to reflect new coverage.
      await refresh()
      await loadProducts()
      await loadDiagnostics()
      bumpProductList()
    } catch (e: any) {
      setError(e?.message ?? cm.messages.resyncFailed)
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
      setError(e?.message ?? cm.messages.loadFailed)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    void loadProducts()
    void loadDiagnostics()
  }, [])

  const catalogIdMissingForEnable = enabled && !catalogId.trim()

  const onToggleEnabled = () => {
    if (!enabled && !catalogId.trim()) {
      setError(cm.messages.catalogIdRequired)
      setSuccess(null)
      return
    }
    setError(null)
    setEnabled(prev => !prev)
  }

  const onSave = async () => {
    if (enabled && !catalogId.trim()) {
      setError(cm.messages.catalogIdRequired)
      setSuccess(null)
      return
    }
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
        setSuccess(cm.messages.settingsAlreadySaved)
      } else {
        setSuccess(cm.messages.settingsSaved)
      }
    } catch (e: any) {
      if (
        e?.code === 'catalog_id_required'
        || e?.error === 'catalog_id_required'
        || (e?.message ?? '').includes('Catalog ID')
        || (e?.message ?? '').includes('Meta Catalog ID')
      ) {
        setError(cm.messages.catalogIdRequired)
      } else {
        setError(e?.message ?? cm.messages.saveFailed)
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
      setError(e?.message ?? cm.messages.testFailed)
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-2 text-slate-500" dir={dir}>
        <Loader2 className="w-5 h-5 animate-spin" /> {cm.loading}
      </div>
    )
  }

  return (
    <div className="space-y-6 w-full" dir={dir}>
      {diagnostics && (
        <CatalogSummaryCard
          diagnostics={diagnostics}
          showMetaImport={diagnostics.catalog.catalog_id_present}
          metaImportBusy={metaImportBusy}
          onImportMeta={() => void metaImportRef.current?.runImport()}
          onAddManual={openManualForm}
          onOpenAdvanced={openAdvanced}
        />
      )}

      {diagnostics && <CatalogChannelsCard diagnostics={diagnostics} />}

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

      <section
        id="product-studio"
        className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden"
      >
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Package className="w-5 h-5 text-emerald-600" />
          <div className="flex-1 min-w-0">
            <h3 className="text-base font-bold text-slate-800">{cm.studioSection.title}</h3>
            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">
              {cm.studioSection.intro}
            </p>
          </div>
        </div>
        <div className="p-5">
          <ProductStudio
            refreshTrigger={productsRefresh}
            onImportMeta={
              diagnostics?.catalog.catalog_id_present
                ? () => void metaImportRef.current?.runImport()
                : undefined
            }
            onAddManual={openManualForm}
          />
        </div>
      </section>

      <CatalogAdvancedSection open={advancedOpen} onOpenChange={setAdvancedOpen}>
        {diagnostics && (
          <AdvancedSubSection title={cm.advanced.structureTitle}>
            <HubDiagramCard diagnostics={diagnostics} merchantLabels />
          </AdvancedSubSection>
        )}

        {diagnostics && (
          <AdvancedSubSection title={cm.advanced.catalogStatusTitle}>
            <DiagnosticsDetailPanel diagnostics={diagnostics} />
          </AdvancedSubSection>
        )}

        <AdvancedSubSection title={cm.advanced.commerceDiagnosticsTitle}>
          {status && (
            <div className="space-y-3 mb-4">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill ok={status.eligibility.ok} reason={status.eligibility.reason} />
                <span className="text-xs text-slate-500">
                  {cm.connectionStatus.whatsappLabel}{' '}
                  {status.connection.found ? (status.connection.status ?? '—') : cm.connectionStatus.notLinked}
                </span>
                <span className="text-xs text-slate-500">
                  {cm.connectionStatus.catalogIdLabel}{' '}
                  {status.connection.meta_catalog_id ? status.connection.meta_catalog_id : '—'}
                </span>
                <span className="text-xs text-slate-500">
                  {cm.connectionStatus.retailerCoverageLabel}{' '}
                  {status.coverage.with_retailer_id} / {status.coverage.sample_size}
                </span>
              </div>
              <p className="text-sm text-slate-700 bg-slate-50 border border-slate-100 rounded-lg p-3">
                {status.advice}
              </p>
            </div>
          )}
          {diagnostics && (
            <div className={`rounded-xl border p-3 ${
              diagnostics.whatsapp_readiness.ready
                ? 'bg-emerald-50 border-emerald-200'
                : 'bg-amber-50 border-amber-200'
            }`}>
              <div className="flex items-center gap-2">
                {diagnostics.whatsapp_readiness.ready
                  ? <CheckCircle2 className="w-5 h-5 text-emerald-600" />
                  : <AlertTriangle className="w-5 h-5 text-amber-600" />}
                <span className="text-sm font-bold text-slate-800">
                  {diagnostics.whatsapp_readiness.ready
                    ? cm.diagnostics.commerceReadyTitle
                    : cm.diagnostics.commerceNotReadyTitle}
                </span>
              </div>
              <ul className="mt-2 space-y-1">
                {diagnostics.whatsapp_readiness.checks.map((check) => {
                  const labelKey = check.key as keyof typeof cm.diagnostics.checkLabels
                  const label = cm.diagnostics.checkLabels[labelKey] ?? check.key
                  return (
                    <li key={check.key} className="flex items-center gap-2 text-xs text-slate-700">
                      {check.ok
                        ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
                        : <XCircle className="w-3.5 h-3.5 text-rose-500 shrink-0" />}
                      <span>{label}</span>
                      {check.key === 'products_with_retailer_id' && typeof check.count === 'number' && (
                        <span className="text-slate-500">({fmtCount(check.count, lang)})</span>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
        </AdvancedSubSection>

        <AdvancedSubSection title={cm.advanced.linkStatusTitle} lazyMount>
          <WabaCatalogLinkStatusCard />
        </AdvancedSubSection>

        <AdvancedSubSection title={cm.advanced.bindingSettingsTitle}>
          <div className="space-y-4">
            <div className="text-xs leading-relaxed text-slate-600 bg-slate-50 border border-slate-100 rounded-lg p-3">
              {cm.channelBinding.intro}
            </div>
            <div>
              <label className="block text-sm font-semibold text-slate-700 mb-1">
                {cm.channelBinding.catalogIdLabel}
              </label>
              <input
                type="text"
                dir="ltr"
                value={catalogId}
                onChange={e => setCatalogId(e.target.value)}
                placeholder={cm.channelBinding.catalogIdPh}
                className="w-full rounded-xl border border-slate-200 px-4 py-2.5 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
              <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
                {cm.channelBinding.catalogIdHint}
              </p>
              {catalogIdMissingForEnable && (
                <p className="text-xs text-amber-700 mt-1.5 leading-relaxed">
                  {cm.messages.catalogIdRequired}
                </p>
              )}
            </div>
            <div className="flex items-center justify-between gap-3 bg-slate-50 border border-slate-100 rounded-xl p-4">
              <div>
                <p className="font-semibold text-sm text-slate-800">{cm.channelBinding.enableTitle}</p>
                <p className="text-xs text-slate-500 mt-0.5">{cm.channelBinding.enableDesc}</p>
              </div>
              <button
                type="button"
                onClick={onToggleEnabled}
                className={`shrink-0 inline-flex items-center gap-1 px-3 py-1.5 rounded-full text-sm font-semibold transition ${enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-200 text-slate-500'}`}
                aria-pressed={enabled}
              >
                {enabled ? <ToggleRight className="w-4 h-4" /> : <ToggleLeft className="w-4 h-4" />}
                {enabled ? cm.channelBinding.enabled : cm.channelBinding.disabled}
              </button>
            </div>
            <div className="flex justify-end">
              <button
                onClick={onSave}
                disabled={saving || catalogIdMissingForEnable}
                className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2.5 rounded-xl text-sm transition"
              >
                {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                {cm.channelBinding.save}
              </button>
            </div>
          </div>
        </AdvancedSubSection>

        <AdvancedSubSection title={cm.advanced.catalogToolsTitle}>
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 bg-slate-50 border border-slate-100 rounded-xl p-4">
              <div className="space-y-1">
                <p className="text-sm font-semibold text-slate-800">
                  {cm.tools.coverageLabel}{' '}
                  <span className="text-emerald-700">{products ? products.coverage.with_rid : '—'}</span>
                  <span className="text-slate-400"> / </span>
                  <span className="text-slate-700">{products ? products.coverage.total : '—'}</span>
                </p>
                <p className="text-xs text-slate-500 leading-relaxed">{cm.tools.coverageDesc}</p>
              </div>
              <button
                onClick={onResync}
                disabled={resyncing}
                className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-4 py-2 rounded-xl text-sm transition shrink-0"
              >
                {resyncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                {cm.tools.resyncBtn}
              </button>
            </div>
            {resyncReport && (
              <div className="bg-emerald-50/60 border border-emerald-100 rounded-xl p-4 text-xs text-emerald-900 space-y-1">
                <p className="font-bold">{cm.tools.reportTitle}</p>
                <ul className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-1">
                  <li>{cm.tools.scanned} {resyncReport.scanned}</li>
                  <li>{cm.tools.assigned} {resyncReport.retailer_id_set}</li>
                  <li>{cm.tools.alreadySet} {resyncReport.already_set}</li>
                  <li>{cm.tools.synthetic} {resyncReport.synthetic_assigned}</li>
                  <li>{cm.tools.published} {resyncReport.published_stamped}</li>
                  <li>{cm.tools.errors} {resyncReport.errors}</li>
                </ul>
              </div>
            )}
          </div>
        </AdvancedSubSection>

        <AdvancedSubSection title={cm.advanced.manualProductsTitle}>
          <div id="catalog-manual-product-section">
            <ManualProductsSection
              currentSource={diagnostics?.products.dominant_source ?? 'unknown'}
              formOpen={manualFormOpen}
              onFormOpenChange={setManualFormOpen}
              hideAddButton
              onChanged={async () => {
                await loadProducts()
                await loadDiagnostics()
                bumpProductList()
              }}
            />
          </div>
        </AdvancedSubSection>

        {diagnostics?.catalog.catalog_id_present && (
          <AdvancedSubSection title={cm.advanced.metaImportTitle}>
            <MetaImportSection
              ref={metaImportRef}
              hideImportButton
              onBusyChange={setMetaImportBusy}
              onChanged={async () => {
                await loadProducts()
                await loadDiagnostics()
                bumpProductList()
              }}
            />
          </AdvancedSubSection>
        )}

        <AdvancedSubSection title={cm.advanced.testSendTitle}>
          <div className="space-y-3">
            <p className="text-xs text-slate-500 leading-relaxed">{cm.testSend.intro}</p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <input
                dir="ltr"
                placeholder={cm.testSend.phonePlaceholder}
                value={testTo}
                onChange={e => setTestTo(e.target.value)}
                className="rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
              />
              <input
                placeholder={cm.testSend.titlePlaceholder}
                value={testTitle}
                onChange={e => setTestTitle(e.target.value)}
                className="rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
              />
              <input
                dir="ltr"
                placeholder={cm.testSend.productIdPlaceholder}
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
                {cm.testSend.sendBtn}
              </button>
            </div>
            {testResult && (
              <div className="mt-3 bg-slate-50 border border-slate-100 rounded-xl p-4 text-sm space-y-2">
                <p className="font-bold text-slate-800 flex items-center gap-2">
                  {testResult.ok
                    ? <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                    : <XCircle className="w-4 h-4 text-rose-600" />}
                  {cm.testSend.resultTitle}{' '}
                  <code className="text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded text-xs">
                    {testResult.final_mode}
                  </code>
                </p>
                <p className="text-xs text-slate-600">
                  {cm.testSend.productLabel} <strong>{testResult.product.title}</strong>
                  {' '}(retailer_id: <code>{testResult.product.retailer_id ?? '—'}</code>)
                </p>
              </div>
            )}
          </div>
        </AdvancedSubSection>
      </CatalogAdvancedSection>
    </div>
  )
}


function DiagnosticsDetailPanel(props: { diagnostics: CatalogDiagnostics }) {
  const { tStatic, lang } = useLanguage()
  const cm = tStatic(tr => tr.catalogMgmt)
  const diagnostics = props.diagnostics

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
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
              ? cm.diagnostics.readyTitle
              : cm.diagnostics.notReadyTitle}
          </span>
        </div>
        <p className="text-xs text-slate-600 mt-1.5 leading-relaxed">
          {diagnostics.catalog.catalog_id_present
            ? (() => {
                const parts = cm.diagnostics.metaLinked.split('{catalogId}')
                return (
                  <>
                    {parts[0]}
                    <code dir="ltr" className="bg-slate-100 px-1 rounded">{diagnostics.catalog.catalog_id}</code>
                    {parts[1] ?? ''}
                  </>
                )
              })()
            : cm.diagnostics.metaNotLinked}
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
        <div className="flex items-center gap-2">
          <Store className="w-5 h-5 text-slate-500" />
          <span className="text-sm font-bold text-slate-800">{cm.diagnostics.productSource}</span>
          <SourceBadge source={diagnostics.products.dominant_source} size="md" />
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {(Object.entries(diagnostics.products.source_breakdown) as Array<[ProductSource, number]>)
            .filter(([, n]) => n > 0)
            .map(([src, n]) => {
              const srcKey = (src in SOURCE_STYLES ? src : 'unknown') as CatalogSourceKey
              const srcLabel = cm.sources[srcKey]
              return (
                <span
                  key={src}
                  className="inline-flex items-center gap-1 rounded-full bg-white border border-slate-200 text-[11px] px-2 py-0.5 text-slate-600"
                >
                  <SourceBadge source={src} /> × {fmtCount(n, lang)}
                  <span className="sr-only">{srcLabel}</span>
                </span>
              )
            })}
        </div>
      </div>

      <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
        <div className="flex items-center gap-2">
          <Package className="w-5 h-5 text-slate-500" />
          <span className="text-sm font-bold text-slate-800">{cm.diagnostics.coverageTitle}</span>
        </div>
        <p className="text-xs text-slate-600 mt-1.5">
          {cm.diagnostics.coverageDesc
            .replace('{with}', fmtCount(diagnostics.products.with_effective_retailer_id, lang))
            .replace('{total}', fmtCount(diagnostics.products.total, lang))
            .replace('{pct}', String(diagnostics.products.coverage_pct))}
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
        <div className="flex items-center gap-2">
          <Clock className="w-5 h-5 text-slate-500" />
          <span className="text-sm font-bold text-slate-800">{cm.diagnostics.importTitle}</span>
        </div>
        {!diagnostics.import.status ? (
          <p className="text-xs text-slate-600 mt-1.5">{cm.diagnostics.importNever}</p>
        ) : (
          <p className="text-xs text-slate-600 mt-1.5">
            {diagnostics.import.last_at && cm.diagnostics.importLastAt.replace(
              '{at}',
              fmtImportAt(diagnostics.import.last_at, lang),
            )}
          </p>
        )}
      </div>
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

const NODE_STATUS_STYLE: Record<NodeStatus, { pill: string; dot: string }> = {
  live:      { pill: 'bg-emerald-50 border-emerald-200', dot: 'bg-emerald-500' },
  active:    { pill: 'bg-emerald-50 border-emerald-200', dot: 'bg-emerald-500' },
  available: { pill: 'bg-blue-50    border-blue-200',    dot: 'bg-blue-500'    },
  unused:    { pill: 'bg-slate-50   border-slate-200',   dot: 'bg-slate-300'   },
  planned:   { pill: 'bg-amber-50   border-amber-200',   dot: 'bg-amber-400'   },
}

function HubNode(props: {
  icon: React.ReactNode
  title: string
  subtitle?: string
  status: NodeStatus
}) {
  const { tStatic } = useLanguage()
  const s = NODE_STATUS_STYLE[props.status]
  const nodeStatus = tStatic(tr => tr.catalogMgmt.hub.nodeStatus)
  const label = nodeStatus[props.status]
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
      <div className="absolute top-1.5 start-1.5 flex items-center gap-1">
        <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
        <span className="text-[10px] text-slate-600">{label}</span>
      </div>
    </div>
  )
}

function HubDiagramCard(props: { diagnostics: CatalogDiagnostics; merchantLabels?: boolean }) {
  const { tStatic, lang, dir } = useLanguage()
  const hub = tStatic(tr => tr.catalogMgmt.hub)
  const d = props.diagnostics
  const breakdown = d.products.source_breakdown
  const cardTitle = props.merchantLabels ? hub.advancedTitle : hub.title
  const inputsLabel = props.merchantLabels ? hub.sourcesLabel : hub.inputsLabel
  const outputsLabel = props.merchantLabels ? hub.channelsLabel : hub.outputsLabel

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
    <Card title={cardTitle} icon={<Database className="w-5 h-5 text-emerald-600" />}>
      <div dir={dir}>
      <p className="text-xs text-slate-600 leading-relaxed mb-4">
        {hub.intro}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-[1fr_auto_1fr_auto_1fr] gap-3 items-center">
        {/* ── Sources column ── */}
        <div className="space-y-2">
          <div className="text-xs font-bold text-slate-500 uppercase tracking-wide text-center mb-2">
            {inputsLabel}
          </div>
          <HubNode
            icon={<Store className="w-4 h-4 text-orange-600" />}
            title={hub.sources.salla}
            subtitle={sallaCount > 0
              ? hub.subtitles.sallaCount.replace('{count}', fmtCount(sallaCount, lang))
              : hub.subtitles.sallaUnlinked}
            status={sallaStatus}
          />
          <HubNode
            icon={<Package className="w-4 h-4 text-blue-600" />}
            title={hub.sources.meta}
            subtitle={metaCount > 0
              ? hub.subtitles.metaImported.replace('{count}', fmtCount(metaCount, lang))
              : (d.catalog.catalog_id_present ? hub.subtitles.metaReadyToImport : hub.subtitles.metaNotLinked)}
            status={metaStatus}
          />
          <HubNode
            icon={<Store className="w-4 h-4 text-sky-600" />}
            title={hub.sources.manual}
            subtitle={manualCount > 0
              ? hub.subtitles.manualCount.replace('{count}', fmtCount(manualCount, lang))
              : hub.subtitles.manualAlwaysAvailable}
            status={manualStatus}
          />
          <HubNode
            icon={<Clock className="w-4 h-4 text-amber-500" />}
            title={hub.sources.shopifyPlanned}
            subtitle={hub.subtitles.shopifyPlanned}
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
          <div className="font-black text-sm text-emerald-900">{hub.nahlaCatalog}</div>
          <div className="text-xs text-emerald-800 mt-1">
            {hub.productCount.replace('{count}', fmtCount(d.products.total, lang))}
          </div>
          <div className="text-[11px] text-emerald-700 mt-1">
            {hub.unifiedSource}
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
            {outputsLabel}
          </div>
          <HubNode
            icon={<MessageCircle className="w-4 h-4 text-emerald-600" />}
            title={hub.channels.whatsapp}
            subtitle={waStatus === 'live'
              ? hub.subtitles.whatsappReady
              : (waStatus === 'available' ? hub.subtitles.whatsappNeedsCatalogId : hub.subtitles.whatsappConnectFirst)}
            status={waStatus}
          />
          <HubNode
            icon={<Bot className="w-4 h-4 text-violet-600" />}
            title={hub.channels.ai}
            subtitle={aiStatus === 'live' ? hub.subtitles.aiReadsCatalog : hub.subtitles.aiNeedsProducts}
            status={aiStatus}
          />
          <HubNode
            icon={<Megaphone className="w-4 h-4 text-rose-600" />}
            title={hub.channels.campaigns}
            subtitle={campaignsStatus === 'available'
              ? hub.subtitles.campaignsAvailable
              : hub.subtitles.campaignsNeedsProducts}
            status={campaignsStatus}
          />
          <HubNode
            icon={<ShoppingBag className="w-4 h-4 text-amber-500" />}
            title={hub.channels.googlePlanned}
            subtitle={hub.subtitles.googlePlanned}
            status="planned"
          />
        </div>
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

export type MetaImportHandle = { runImport: () => Promise<void> }

const MetaImportSection = forwardRef<MetaImportHandle, {
  onChanged: () => Promise<void>
  hideImportButton?: boolean
  onBusyChange?: (busy: boolean) => void
}>(function MetaImportSection(props, ref) {
  const { tStatic, dir } = useLanguage()
  const mi = tStatic(tr => tr.catalogMgmt.metaImport)
  const msgs = tStatic(tr => tr.catalogMgmt.messages)

  const [busy, setBusy]               = useState(false)
  const [error, setError]             = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<Record<string, any> | null>(null)
  const [showDetail, setShowDetail]   = useState(false)
  const [report, setReport]           = useState<MetaImportReport | null>(null)

  const errorCopy = (code: string | undefined): string => {
    const errs = mi.errors
    switch (code) {
      case 'connection_not_found':       return errs.connection_not_found
      case 'catalog_id_missing':         return errs.catalog_id_missing
      case 'access_token_missing':       return errs.access_token_missing
      case 'meta_access_token_missing':  return errs.meta_access_token_missing
      case 'catalog_not_found':          return errs.catalog_not_found
      case 'catalog_type_unsupported':   return errs.catalog_type_unsupported
      case 'meta_http_error':            return errs.meta_http_error
      default:
        return code
          ? errs.defaultUnexpected.replace('{code}', code)
          : msgs.unexpectedImport
    }
  }

  const onImport = useCallback(async () => {
    setBusy(true); props.onBusyChange?.(true); setError(null); setErrorDetail(null); setShowDetail(false); setReport(null)
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
      props.onBusyChange?.(false)
    }
  }, [props.onChanged, props.onBusyChange])

  useImperativeHandle(ref, () => ({ runImport: onImport }), [onImport])

  return (
    <div id="meta-import-section">
      <div className="space-y-3" dir={dir}>
        <p className="text-xs text-slate-600 leading-relaxed">
          {mi.intro}
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
                  {showDetail ? mi.hideDetail : mi.showDetail}
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
            <p className="font-bold">{mi.reportTitle}</p>
            <ul className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-1">
              <li>{mi.scanned} {report.scanned}</li>
              <li>{mi.created} {report.created}</li>
              <li>{mi.updated} {report.updated}</li>
              <li>{mi.skippedManual} {report.skipped_manual}</li>
              <li>{mi.reportErrors} {report.errors}</li>
              <li>{mi.pages} {report.pages_fetched}</li>
            </ul>
            {report.truncated && (
              <p className="text-amber-700 mt-1">
                {mi.truncated}
              </p>
            )}
          </div>
        )}

        {!props.hideImportButton && (
        <button
          type="button"
          onClick={onImport}
          disabled={busy}
          className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm transition"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
          {mi.importBtn}
        </button>
        )}
      </div>
    </div>
  )
})


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
  formOpen?: boolean
  onFormOpenChange?: (open: boolean) => void
  hideAddButton?: boolean
}) {
  const { tStatic, dir } = useLanguage()
  const manual = tStatic(tr => tr.catalogMgmt.manual)
  const msgs = tStatic(tr => tr.catalogMgmt.messages)

  const [internalOpen, setInternalOpen] = useState(false)
  const open = props.formOpen ?? internalOpen
  const setOpen = (next: boolean) => {
    props.onFormOpenChange?.(next)
    if (props.formOpen === undefined) setInternalOpen(next)
  }
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
      const productTitle = title.trim()
      if (!productTitle) {
        setError(manual.nameRequired)
        setBusy(false)
        return
      }
      const created = await catalogApi.createManualProduct({
        title:            productTitle,
        price:            price.trim() || undefined,
        description:      description.trim() || undefined,
        image_url:        imageUrl.trim() || undefined,
        product_url:      productUrl.trim() || undefined,
        meta_retailer_id: metaRid.trim() || undefined,
      })
      setSuccess(msgs.addProductSuccess.replace('{title}', created.title))
      reset()
      setOpen(false)
      await props.onChanged()
    } catch (e: any) {
      setError(e?.message ?? msgs.addProductFailed)
    } finally {
      setBusy(false)
    }
  }

  // Explainer copy adapts to the current dominant source: a Salla
  // merchant gets a different tone ("هذا اختياري") than a no-store
  // merchant ("ابدأ من هنا").
  const explainer =
    props.currentSource === 'salla' || props.currentSource === 'zid'
      ? manual.explainerStore
      : manual.explainerNoStore

  return (
    <div id="manual-product-section">
      <div className="space-y-4" dir={dir}>
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

        {!open && !props.hideAddButton && (
          <button
            type="button"
            onClick={() => { setOpen(true); setSuccess(null) }}
            className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-4 py-2 rounded-xl text-sm transition"
          >
            <Package className="w-4 h-4" />
            {manual.addNew}
          </button>
        )}

        {open && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="md:col-span-2">
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.productName} <span className="text-rose-500">*</span></label>
              <input
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder={manual.productNamePh}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.price}</label>
              <input
                value={price}
                onChange={e => setPrice(e.target.value)}
                placeholder={manual.pricePh}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.imageUrl}</label>
              <input
                value={imageUrl}
                onChange={e => setImageUrl(e.target.value)}
                placeholder="https://..."
                dir="ltr"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.productUrl}</label>
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
                {manual.metaRidLabel}
              </label>
              <input
                value={metaRid}
                onChange={e => setMetaRid(e.target.value)}
                placeholder={manual.metaRidPh}
                dir="ltr"
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
              />
              <p className="text-[11px] text-slate-500 mt-1 leading-relaxed">
                {manual.metaRidHint}
              </p>
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.description}</label>
              <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                rows={3}
                placeholder={manual.descriptionPh}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none resize-none"
              />
            </div>
            <div className="md:col-span-2 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => { setOpen(false); reset(); setError(null) }}
                className="px-4 py-2 rounded-xl text-sm font-semibold text-slate-600 hover:bg-slate-100 transition"
              >
                {manual.cancel}
              </button>
              <button
                type="button"
                onClick={onSubmit}
                disabled={busy}
                className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm transition"
              >
                {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                {manual.save}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
