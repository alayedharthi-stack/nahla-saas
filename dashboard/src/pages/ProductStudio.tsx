/**
 * ProductStudio.tsx
 * ─────────────────
 * Meta-Commerce-Manager-style Product Studio for Nahla Catalog
 * (May 2026 #15 — Phase 1).
 *
 * Mounted inside the Catalog page as its primary content. Owns:
 *
 *   • The product grid — thumbnail-first table with search +
 *     filters + per-row source/readiness badges.
 *   • The product detail drawer — opens on row click, edits with
 *     live counters + autosave + readiness panel.
 *   • The channel-spec registry cache — loaded once, drives
 *     live counter limits + tooltips across every drawer mount.
 *
 * Design contract
 * ───────────────
 * 1. Pure consumer of `catalogApi`. No DB knowledge, no hardcoded
 *    limits (Meta 200 / Google 150 etc.) — those come from the
 *    backend channel registry via `/channels`.
 * 2. Drawer state is independent of grid state. Closing the drawer
 *    does NOT refetch the grid; refetch only on actual mutations
 *    or filter changes.
 * 3. Readiness preview is debounced at 280ms on every keystroke
 *    so the live counters/colors are interactive without flooding
 *    the API.
 * 4. The drawer autosaves persisted rows into the central Nahla
 *    catalog while keeping readiness preview debounced separately.
 *
 * Phase 1 scope (this file):
 *   ✅ Grid + filters + pagination
 *   ✅ Drawer (read + live counters + readiness panel + autosave)
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Search, X, Image as ImageIcon, ExternalLink, Loader2,
  AlertTriangle, CheckCircle2, XCircle, Package,
  Filter as FilterIcon, ChevronLeft, ChevronRight, ChevronDown,
  Bot, MessageCircle, Megaphone, ShoppingBag, Sparkles, Layers, EyeOff, RotateCcw, Trash2,
} from 'lucide-react'
import { resolveRowDisplaySource } from '../components/catalog/catalogDisplay'
import {
  catalogApi,
  type CatalogProductDiagRow,
  type CatalogProductVariantRow,
  type CatalogVariantsSummary,
  type ChannelReadiness,
  type ChannelSpecResponse,
  type ProductDetailResponse,
  type ProductSource,
  type ProductPublicationStatus,
  type ReadinessFieldStatus,
  type MetaSyncPreviewResponse,
  type MetaSyncConfirmResponse,
  type ReadinessPreviewBody,
  type StudioFilters,
  type StudioProduct,
  type CatalogVisibility,
} from '../api/catalog'
import { CatalogProductPriceCell } from '../components/catalog/CatalogProductPriceCell'
import { ProductThumbnail } from '../components/catalog/ProductThumbnail'
import { useLanguage } from '../i18n/context'
import type { Lang, Translations } from '../i18n/types'

function localeTag(lang: Lang): string {
  return lang === 'en' ? 'en-US' : 'ar-SA'
}

function fmtCount(n: number, lang: Lang): string {
  return n.toLocaleString(localeTag(lang))
}

type CatalogSourceKey = keyof Translations['catalogMgmt']['sources']

const SOURCE_STYLES: Record<string, { bg: string; text: string }> = {
  salla:        { bg: 'bg-orange-50 border-orange-200', text: 'text-orange-700' },
  zid:          { bg: 'bg-violet-50 border-violet-200', text: 'text-violet-700' },
  meta:         { bg: 'bg-blue-50   border-blue-200',   text: 'text-blue-700'   },
  manual:       { bg: 'bg-sky-50    border-sky-200',    text: 'text-sky-700'    },
  nahla_native: { bg: 'bg-sky-50    border-sky-200',    text: 'text-sky-700'    },
  unknown:      { bg: 'bg-slate-50  border-slate-200', text: 'text-slate-600'  },
}

function isMerchantEditableSource(source: string): boolean {
  return source === 'nahla_native' || source === 'manual'
}

function isExternalManagedSource(source: string): boolean {
  return source === 'salla' || source === 'zid' || source === 'shopify'
}

// Channel icon → lucide-react element. Keeps Studio rendering channel
// metadata symmetrical regardless of which channel the registry adds
// next.
const CHANNEL_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  whatsapp:        MessageCircle,
  meta_catalog:    Package,
  ai:              Bot,
  campaigns:       Megaphone,
  google_merchant: ShoppingBag,
}

const CHANNEL_ICON_COLOR: Record<string, string> = {
  whatsapp:        'text-emerald-600',
  meta_catalog:    'text-blue-600',
  ai:              'text-violet-600',
  campaigns:       'text-rose-600',
  google_merchant: 'text-amber-500',
}


function SourcePill({ source, metaRetailerId }: { source: ProductSource | string; metaRetailerId?: string | null }) {
  const { t } = useLanguage()
  const resolved = resolveRowDisplaySource(source as ProductSource, metaRetailerId ?? null)
  const style = SOURCE_STYLES[resolved] ?? SOURCE_STYLES.unknown
  const key = (resolved in SOURCE_STYLES ? resolved : 'unknown') as CatalogSourceKey
  const label = t(tr => tr.catalogMgmt.sources[key]) // i18n-static: allow — key is CatalogSourceKey
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border font-semibold text-[11px] px-2 py-0.5 ${style.bg} ${style.text}`}>
      {label}
    </span>
  )
}


function ReadinessPill({ row }: { row: CatalogProductDiagRow }) {
  const { t } = useLanguage()
  const rd = t(tr => tr.catalogMgmt.studio.readiness)
  const b = row.readiness_badge
  if (!b) return <span className="text-[11px] text-slate-400">—</span>
  const isReady = b.blocking_count === 0
    && b.warn_count === 0
    && b.ready_count >= b.enabled_total
  const palette = isReady
    ? { bg: 'bg-emerald-50 border-emerald-200', text: 'text-emerald-700', dot: 'bg-emerald-500' }
    : { bg: 'bg-amber-50 border-amber-200', text: 'text-amber-700', dot: 'bg-amber-500' }
  const label = isReady ? rd.readySimple : rd.needsCompletion
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border text-[11px] px-2 py-0.5 ${palette.bg} ${palette.text}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${palette.dot}`} />
      {label}
    </span>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Filters bar
// ─────────────────────────────────────────────────────────────────────

function FiltersBar(props: {
  filters: StudioFilters
  onChange: (f: StudioFilters) => void
  totalShown: number
  total: number
}) {
  const { t, lang } = useLanguage()
  const f = t(tr => tr.catalogMgmt.studio.filters)
  const sources = t(tr => tr.catalogMgmt.sources)

  const set = (patch: Partial<StudioFilters>) => props.onChange({ ...props.filters, ...patch })
  const clear = () => props.onChange({})

  const activeCount = Object.values(props.filters).filter(v => v !== undefined && v !== '').length

  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={props.filters.q ?? ''}
            onChange={e => set({ q: e.target.value || undefined })}
            placeholder={f.searchPlaceholder}
            className="w-full rounded-xl border border-slate-200 pr-9 pl-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none"
          />
        </div>
        <select
          value={props.filters.source ?? ''}
          onChange={e => set({ source: (e.target.value || undefined) as ProductSource | undefined })}
          className="rounded-xl border border-slate-200 px-3 py-2 text-sm bg-white"
        >
          <option value="">{f.allSources}</option>
          <option value="salla">{sources.salla}</option>
          <option value="meta">{sources.meta}</option>
          <option value="manual">{sources.manual}</option>
          <option value="nahla_native">{sources.nahla_native}</option>
          <option value="zid">{sources.zid}</option>
          <option value="unknown">{sources.unknown}</option>
        </select>
        <select
          value={
            props.filters.has_image === true ? 'yes'
            : props.filters.has_image === false ? 'no'
            : ''
          }
          onChange={e =>
            set({
              has_image:
                e.target.value === 'yes' ? true
                : e.target.value === 'no' ? false
                : undefined,
            })
          }
          className="rounded-xl border border-slate-200 px-3 py-2 text-sm bg-white"
        >
          <option value="">{f.imageAll}</option>
          <option value="yes">{f.imageYes}</option>
          <option value="no">{f.imageNo}</option>
        </select>
        <select
          value={
            props.filters.has_retailer_id === true ? 'yes'
            : props.filters.has_retailer_id === false ? 'no'
            : ''
          }
          onChange={e =>
            set({
              has_retailer_id:
                e.target.value === 'yes' ? true
                : e.target.value === 'no' ? false
                : undefined,
            })
          }
          className="rounded-xl border border-slate-200 px-3 py-2 text-sm bg-white"
        >
          <option value="">{f.retailerIdAll}</option>
          <option value="yes">{f.retailerIdYes}</option>
          <option value="no">{f.retailerIdNo}</option>
        </select>
        <select
          value={
            props.filters.in_stock === true ? 'yes'
            : props.filters.in_stock === false ? 'no'
            : ''
          }
          onChange={e =>
            set({
              in_stock:
                e.target.value === 'yes' ? true
                : e.target.value === 'no' ? false
                : undefined,
            })
          }
          className="rounded-xl border border-slate-200 px-3 py-2 text-sm bg-white"
        >
          <option value="">{f.stockAll}</option>
          <option value="yes">{f.stockYes}</option>
          <option value="no">{f.stockNo}</option>
        </select>
        <select
          value={props.filters.catalog_visibility ?? 'active'}
          onChange={e => {
            const v = e.target.value as CatalogVisibility
            set({ catalog_visibility: v === 'active' ? undefined : v })
          }}
          className="rounded-xl border border-slate-200 px-3 py-2 text-sm bg-white"
        >
          <option value="active">{f.visibilityAll}</option>
          <option value="hidden">{f.visibilityHidden}</option>
          <option value="removed">{f.visibilityRemoved}</option>
          <option value="archived">{f.visibilityArchived}</option>
          <option value="all">{f.visibilityEvery}</option>
        </select>

        {activeCount > 0 && (
          <button
            type="button"
            onClick={clear}
            className="inline-flex items-center gap-1 text-xs text-slate-600 hover:text-rose-600 px-2 py-1 rounded-lg hover:bg-slate-50"
          >
            <X className="w-3.5 h-3.5" /> {f.clear.replace('{count}', fmtCount(activeCount, lang))}
          </button>
        )}
      </div>

      <div className="flex items-center justify-between text-xs text-slate-500">
        <div className="flex items-center gap-1.5">
          <FilterIcon className="w-3.5 h-3.5" />
          {f.showing
            .replace('{shown}', fmtCount(props.totalShown, lang))
            .replace('{total}', fmtCount(props.total, lang))}
        </div>
      </div>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Product grid — Meta-style table
// ─────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────
// Variants summary header — five-counter pill bar.
// Surfaced from the diag endpoint's ``variants_summary`` block
// (migration 0064 Phase 4). Reads tenant-wide totals regardless of
// the active grid filter so merchants see the true catalog shape.
// ─────────────────────────────────────────────────────────────────────

function VariantsSummaryBar(props: { summary?: CatalogVariantsSummary | null }) {
  const { t, lang } = useLanguage()
  const vs = t(tr => tr.catalogMgmt.studio.variantsSummary)
  const s = props.summary
  if (!s) return null
  const pills: Array<{
    label:  string
    value:  number
    Icon:   typeof Package
    tone:   string
  }> = [
    { label: vs.products, value: s.products,
      Icon: Package, tone: 'text-slate-700 bg-slate-50 border-slate-200' },
    { label: vs.whatsappReady, value: s.whatsapp_ready,
      Icon: MessageCircle, tone: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
    { label: vs.needsReview, value: Math.max(0, s.products - s.whatsapp_ready),
      Icon: Layers, tone: 'text-amber-700 bg-amber-50 border-amber-200' },
  ]
  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {pills.map(p => (
          <span
            key={p.label}
            className={`inline-flex items-center gap-1.5 ${p.tone} border rounded-full px-3 py-1 text-xs font-semibold`}
          >
            <p.Icon className="w-3.5 h-3.5" />
            <span className="font-mono tabular-nums">{fmtCount(p.value, lang)}</span>
            <span>{p.label}</span>
          </span>
        ))}
      </div>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Inline variants drawer — rendered under the expanded parent row.
// Mirrors the shape of the per-row variant rows the diag endpoint
// returns. Read-only: a per-variant publish action will land later
// as part of the channel-listings work.
// ─────────────────────────────────────────────────────────────────────

function VariantsDrawer(props: { variants: CatalogProductVariantRow[] }) {
  const { t } = useLanguage()
  const vd = t(tr => tr.catalogMgmt.studio.variantsDrawer)
  const real = props.variants.filter(v => !v.is_default)
  if (real.length === 0) {
    return (
      <div className="bg-slate-50 border-t border-slate-200 px-6 py-3 text-xs text-slate-500">
        {vd.noVariants}
      </div>
    )
  }
  return (
    <div className="bg-slate-50 border-t border-slate-200 px-6 py-3">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.option}</th>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.sku}</th>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.price}</th>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.stock}</th>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.retailerId}</th>
              <th className="text-right py-1.5 px-2 font-semibold">{vd.status}</th>
            </tr>
          </thead>
          <tbody>
            {real.map(v => (
              <tr key={v.id} className="border-t border-slate-200">
                <td className="py-1.5 px-2 text-slate-700">
                  {v.option_summary || (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="py-1.5 px-2 font-mono text-[11px] text-slate-500" dir="ltr">
                  {v.sku ?? '—'}
                </td>
                <td className="py-1.5 px-2 font-medium text-slate-700">
                  {v.price ? `${v.price} ${v.currency ?? ''}`.trim() : '—'}
                </td>
                <td className="py-1.5 px-2 text-slate-600 tabular-nums">
                  {v.stock_quantity ?? '—'}
                </td>
                <td className="py-1.5 px-2 font-mono text-[11px] text-slate-500 max-w-[160px] truncate" dir="ltr" title={v.retailer_id ?? ''}>
                  {v.retailer_id ?? <span className="text-amber-600">{vd.missing}</span>}
                </td>
                <td className="py-1.5 px-2">
                  {v.in_stock
                    ? <span className="inline-flex items-center gap-1 text-emerald-700"><CheckCircle2 className="w-3 h-3" /> {vd.inStock}</span>
                    : <span className="inline-flex items-center gap-1 text-rose-700"><XCircle className="w-3 h-3" /> {vd.outOfStock}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


function ProductGrid(props: {
  rows: CatalogProductDiagRow[]
  loading: boolean
  onSelect: (id: number) => void
  onImportMeta?: () => void
  onAddManual?: () => void
  showEmptyActions?: boolean
}) {
  const { t } = useLanguage()
  const g = t(tr => tr.catalogMgmt.studio.grid)

  if (props.loading) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 p-16 text-center text-slate-500">
        <Loader2 className="w-7 h-7 animate-spin mx-auto mb-3 text-emerald-500" />
        <p className="text-sm">{g.loading}</p>
      </div>
    )
  }
  if (props.rows.length === 0) {
    // Full-width empty state with actionable CTAs (May 2026 UI revamp).
    // The previous version was a 12-row gray box that read like an
    // error; merchants asked for a Meta-style empty state that
    // points to the next step (import / add manually).
    return (
      <div className="bg-white rounded-2xl border border-slate-200 p-16 text-center">
        <div className="mx-auto w-16 h-16 rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center mb-4">
          <Package className="w-8 h-8 text-emerald-500" />
        </div>
        <h3 className="text-base font-bold text-slate-800 mb-1">
          {g.emptyTitle}
        </h3>
        <p className="text-sm text-slate-500 max-w-md mx-auto mb-5 leading-relaxed">
          {g.emptyDesc}
        </p>
        {props.showEmptyActions !== false && (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {props.onImportMeta ? (
            <button
              type="button"
              onClick={props.onImportMeta}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-4 py-2 rounded-xl text-sm transition"
            >
              {g.importFromMeta}
            </button>
          ) : (
            <a
              href="#meta-import-section"
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-4 py-2 rounded-xl text-sm transition"
            >
              {g.importFromMeta}
            </a>
          )}
          {props.onAddManual ? (
            <button
              type="button"
              onClick={props.onAddManual}
              className="inline-flex items-center gap-2 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-4 py-2 rounded-xl text-sm transition"
            >
              {g.addManual}
            </button>
          ) : (
            <a
              href="#manual-product-section"
              className="inline-flex items-center gap-2 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-4 py-2 rounded-xl text-sm transition"
            >
              {g.addManual}
            </a>
          )}
        </div>
        )}
      </div>
    )
  }

  return (
    <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs text-slate-600 uppercase tracking-wide">
            <tr>
              <th className="text-right py-3 px-3 font-semibold w-10"></th>
              <th className="text-right py-3 px-3 font-semibold">{g.colProduct}</th>
              <th className="text-right py-3 px-3 font-semibold">{g.colSource}</th>
              <th className="text-right py-3 px-3 font-semibold">{g.colPrice}</th>
              <th className="text-right py-3 px-3 font-semibold">{g.colStock}</th>
              <th className="text-right py-3 px-3 font-semibold">{g.colRetailerId}</th>
              <th className="text-right py-3 px-3 font-semibold">{g.colReadiness}</th>
              <th className="text-right py-3 px-3 font-semibold w-12"></th>
            </tr>
          </thead>
          <tbody>
            {props.rows.map(row => (
              <ProductGridRow
                key={row.id}
                row={row}
                onSelect={props.onSelect}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Single parent row + expandable variants drawer (migration 0064).
// We split the row into its own component so the open/closed state
// stays local — opening one row never re-renders the whole table.
// ─────────────────────────────────────────────────────────────────────

function ProductGridRow(props: {
  row: CatalogProductDiagRow
  onSelect: (id: number) => void
}) {
  const { t, lang } = useLanguage()
  const g = t(tr => tr.catalogMgmt.studio.grid)
  const { row } = props
  const [expanded, setExpanded] = useState(false)
  const realVariantCount = row.sellable_variants_count ?? (
    (row.variants ?? []).filter(v => !v.is_default).length
  )
  const hasExpandable = realVariantCount > 0
  return (
    <>
      <tr
        onClick={() => props.onSelect(row.id)}
        className="border-t border-slate-100 hover:bg-emerald-50/30 cursor-pointer transition"
      >
        <td
          className="py-3 px-3 text-slate-400"
          onClick={e => {
            e.stopPropagation()
            if (hasExpandable) setExpanded(v => !v)
          }}
          title={hasExpandable
            ? (expanded
                ? g.hideVariants
                : g.showVariants.replace('{count}', fmtCount(realVariantCount, lang)))
            : g.noVariantsTooltip}
        >
          {hasExpandable ? (
            expanded
              ? <ChevronDown className="w-4 h-4" />
              : <ChevronLeft className="w-4 h-4" />
          ) : (
            <span className="w-4 h-4 inline-block" />
          )}
        </td>
        <td className="py-3 px-3">
          <div className="flex items-center gap-3">
            <div className="shrink-0 w-12 h-12 rounded-lg bg-slate-100 border border-slate-200 overflow-hidden flex items-center justify-center">
              <ProductThumbnail row={row} />
            </div>
            <div className="min-w-0">
              <div className="font-semibold text-slate-900 text-sm truncate max-w-[320px]" title={row.title}>
                {row.title}
              </div>
              <div className="text-[11px] text-slate-400 font-mono flex items-center gap-2" dir="ltr">
                <span>#{row.id}</span>
                {row.sku ? <span>· {row.sku}</span> : null}
                {hasExpandable && (
                  <span className="inline-flex items-center gap-1 text-indigo-600">
                    <Layers className="w-3 h-3" />
                    {realVariantCount} variants
                  </span>
                )}
              </div>
            </div>
          </div>
        </td>
        <td className="py-3 px-3"><SourcePill source={row.source} metaRetailerId={row.meta_retailer_id} /></td>
        <td className="py-3 px-3 text-slate-700 font-medium">
          <CatalogProductPriceCell row={row} />
        </td>
        <td className="py-3 px-3">
          {row.in_stock
            ? <span className="text-xs text-emerald-700 inline-flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" /> {g.inStock}</span>
            : <span className="text-xs text-rose-700 inline-flex items-center gap-1"><XCircle className="w-3.5 h-3.5" /> {g.outOfStock}</span>}
        </td>
        <td className="py-3 px-3 font-mono text-[11px] text-slate-500 max-w-[140px] truncate" dir="ltr" title={row.effective_retailer_id ?? ''}>
          {row.effective_retailer_id ?? <span className="text-amber-600">{g.missing}</span>}
        </td>
        <td className="py-3 px-3"><ReadinessPill row={row} /></td>
        <td className="py-3 px-3 text-slate-400">
          <ChevronLeft className="w-4 h-4" />
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={8} className="p-0">
            <VariantsDrawer variants={row.variants ?? []} />
          </td>
        </tr>
      )}
    </>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Pagination bar
// ─────────────────────────────────────────────────────────────────────

function Pagination(props: {
  offset: number
  limit: number
  total: number
  onChange: (offset: number) => void
}) {
  const { t, lang } = useLanguage()
  const pg = t(tr => tr.catalogMgmt.studio.pagination)
  const page = Math.floor(props.offset / props.limit) + 1
  const totalPages = Math.max(1, Math.ceil(props.total / props.limit))
  const prev = () => props.onChange(Math.max(0, props.offset - props.limit))
  const next = () => props.onChange(Math.min((totalPages - 1) * props.limit, props.offset + props.limit))
  if (props.total <= props.limit) return null
  return (
    <div className="flex items-center justify-between text-xs text-slate-600">
      <div>{pg.pageOf
        .replace('{page}', fmtCount(page, lang))
        .replace('{totalPages}', fmtCount(totalPages, lang))}
      </div>
      <div className="flex items-center gap-1">
        <button
          onClick={prev}
          disabled={page === 1}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <ChevronRight className="w-3.5 h-3.5" /> {pg.prev}
        </button>
        <button
          onClick={next}
          disabled={page >= totalPages}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {pg.next} <ChevronLeft className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Field shell — live counter + warning state for ONE form field
// ─────────────────────────────────────────────────────────────────────
//
// Reads from the per-channel readiness map to figure out which channel
// imposes the strictest limit on THIS field, and renders:
//   • the merchant-typed value (controlled),
//   • a live counter "X/limit",
//   • a color shift at the soft-warn threshold and at the hard limit,
//   • a per-channel tooltip listing each channel's limit on this field
//     so the merchant understands WHY the strictest limit applies.
//
// Constraints are computed from the live readiness payload (not from
// hardcoded numbers) — this means a future channel registered on the
// backend automatically influences the counter without a frontend
// release. Pure consumer of the registry.

function isNativePriceDraftValid(price: string | undefined): boolean {
  const v = (price ?? '').trim()
  if (!v) return true
  return /^\d+(?:[.,]\d+)?$/.test(v.replace(/,/g, ''))
}

function FieldShell(props: {
  fieldName: string
  label: string
  required?: boolean
  multiline?: boolean
  value: string
  onChange: (v: string) => void
  perChannel: ChannelReadiness[]
  placeholder?: string
  dir?: 'rtl' | 'ltr'
  disabled?: boolean
  inputMode?: 'text' | 'decimal' | 'numeric'
  type?: string
  helperText?: string
  errorText?: string
}) {
  const statuses: Array<{ channel: string; label_ar: string; fs: ReadinessFieldStatus }> =
    props.perChannel
      .filter(c => c.enabled)
      .map(c => ({ channel: c.channel, label_ar: c.label_ar, fs: c.fields.find(f => f.field === props.fieldName)! }))
      .filter(s => !!s.fs)

  // Pick the strictest limit (smallest max_length across enabled channels).
  const limited = statuses.filter(s => typeof s.fs.limit === 'number').map(s => s.fs.limit as number)
  const strictest = limited.length > 0 ? Math.min(...limited) : null

  // Worst state across enabled channels for THIS field — drives border color.
  const stateRank: Record<string, number> = { ok: 0, warn: 1, missing: 2, error: 3 }
  const worst = statuses.reduce(
    (w, s) => (stateRank[s.fs.state] > stateRank[w] ? s.fs.state : w),
    'ok',
  )

  const borderClass =
    worst === 'error' || worst === 'missing'
      ? 'border-rose-300 focus-within:border-rose-400 focus-within:ring-rose-100'
      : worst === 'warn'
        ? 'border-amber-300 focus-within:border-amber-400 focus-within:ring-amber-100'
        : 'border-slate-200 focus-within:border-emerald-400 focus-within:ring-emerald-100'

  const counterColor =
    strictest !== null && props.value.length > strictest
      ? 'text-rose-600 font-bold'
      : strictest !== null && props.value.length > strictest * 0.85
        ? 'text-amber-600'
        : 'text-slate-400'

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <label className="text-xs font-semibold text-slate-700">
          {props.label} {props.required && <span className="text-rose-500">*</span>}
        </label>
        {strictest !== null && (
          <span
            className={`text-[11px] font-mono ${counterColor}`}
            title={statuses.map(s => `${s.label_ar}: ${s.fs.limit ?? '∞'}`).join(' · ')}
          >
            {props.value.length}/{strictest}
          </span>
        )}
      </div>
      <div className={`rounded-xl border bg-white transition focus-within:ring-1 ${borderClass}`}>
        {props.multiline ? (
          <textarea
            value={props.value}
            onChange={e => props.onChange(e.target.value)}
            placeholder={props.placeholder}
            dir={props.dir}
            rows={4}
            disabled={props.disabled}
            className="w-full bg-transparent px-3 py-2 text-sm outline-none resize-none disabled:opacity-60 disabled:cursor-not-allowed"
          />
        ) : (
          <input
            value={props.value}
            onChange={e => props.onChange(e.target.value)}
            placeholder={props.placeholder}
            dir={props.dir}
            disabled={props.disabled}
            inputMode={props.inputMode}
            type={props.type}
            className="w-full bg-transparent px-3 py-2 text-sm outline-none disabled:opacity-60 disabled:cursor-not-allowed"
          />
        )}
      </div>
      {props.helperText && !props.errorText && (
        <p className="text-[11px] text-slate-500 leading-relaxed">{props.helperText}</p>
      )}
      {props.errorText && (
        <p className="text-[11px] text-rose-700 leading-relaxed">{props.errorText}</p>
      )}
      {/* Inline warnings — show only the strictest message */}
      {statuses
        .filter(s => s.fs.state === 'warn' || s.fs.state === 'error' || (s.fs.state === 'missing' && s.fs.required))
        .slice(0, 1)
        .map(s => (
          <p
            key={s.channel}
            className={`text-[11px] leading-relaxed ${
              s.fs.state === 'warn' ? 'text-amber-700' : 'text-rose-700'
            }`}
          >
            <span className="font-semibold">{s.label_ar}:</span> {s.fs.message || s.fs.rationale}
          </p>
        ))}
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Readiness Panel — per-channel badges with warning list
// ─────────────────────────────────────────────────────────────────────

function ChannelBadge(props: { c: ChannelReadiness }) {
  const { t, lang } = useLanguage()
  const cb = t(tr => tr.catalogMgmt.studio.channelBadge)
  const Icon = CHANNEL_ICON[props.c.channel] ?? Sparkles
  const iconColor = CHANNEL_ICON_COLOR[props.c.channel] ?? 'text-slate-500'

  let pillBg = 'bg-slate-50 border-slate-200', pillText = 'text-slate-600'
  let pillIcon = <span className="w-1.5 h-1.5 rounded-full bg-slate-300" />
  let label = cb.planned
  if (props.c.enabled) {
    if (props.c.blocking_count > 0) {
      pillBg = 'bg-rose-50 border-rose-200';     pillText = 'text-rose-700'
      pillIcon = <XCircle className="w-3 h-3" />
      label = cb.missing.replace('{count}', fmtCount(props.c.blocking_count, lang))
    } else if (props.c.warnings_count > 0) {
      pillBg = 'bg-amber-50 border-amber-200';   pillText = 'text-amber-700'
      pillIcon = <AlertTriangle className="w-3 h-3" />
      label = cb.readyWarn
    } else {
      pillBg = 'bg-emerald-50 border-emerald-200'; pillText = 'text-emerald-700'
      pillIcon = <CheckCircle2 className="w-3 h-3" />
      if (props.c.channel === 'whatsapp') {
        label = cb.readyWhatsappData
      } else if (props.c.channel === 'meta_catalog') {
        label = cb.readyDataOnly
      } else {
        label = cb.ready
      }
    }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon className={`w-4 h-4 shrink-0 ${iconColor}`} />
          <span className="text-sm font-bold text-slate-800 truncate">{props.c.label_ar}</span>
        </div>
        <span className={`inline-flex items-center gap-1 rounded-full border text-[11px] font-semibold px-2 py-0.5 ${pillBg} ${pillText}`}>
          {pillIcon}
          {label}
        </span>
      </div>
      {/* Progress bar */}
      <div className="mt-2">
        <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
          <div
            className={`h-full transition-all ${
              !props.c.enabled ? 'bg-slate-300'
              : props.c.blocking_count > 0 ? 'bg-rose-400'
              : props.c.warnings_count > 0 ? 'bg-amber-400'
              : 'bg-emerald-500'
            }`}
            style={{ width: `${props.c.score_pct}%` }}
          />
        </div>
        <div className="flex items-center justify-between text-[10px] text-slate-500 mt-1">
          <span>{props.c.score_pct}%</span>
          {!props.c.enabled && <span>{cb.futureChannel}</span>}
        </div>
      </div>
    </div>
  )
}


function ReadinessPanel(props: { perChannel: ChannelReadiness[] }) {
  const { t, lang } = useLanguage()
  const rp = t(tr => tr.catalogMgmt.studio.readinessPanel)
  const issues = useMemo(
    () =>
      props.perChannel
        .filter(c => c.enabled)
        .flatMap(c =>
          c.fields
            .filter(f => f.state === 'error' || f.state === 'warn' || (f.state === 'missing' && f.required))
            .map(f => ({ channel: c.label_ar, fs: f })),
        ),
    [props.perChannel],
  )
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {props.perChannel.map(c => <ChannelBadge key={c.channel} c={c} />)}
      </div>
      {issues.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/50 p-3">
          <h4 className="text-xs font-bold text-amber-800 mb-2">{rp.issuesTitle}</h4>
          <ul className="text-[11px] text-slate-700 space-y-1">
            {issues.slice(0, 8).map((i, idx) => (
              <li key={idx} className="flex items-start gap-1.5">
                <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${
                  i.fs.state === 'warn' ? 'bg-amber-500' : 'bg-rose-500'
                }`} />
                <span>
                  <span className="font-semibold">{i.channel}</span> · {i.fs.message || i.fs.rationale}
                </span>
              </li>
            ))}
            {issues.length > 8 && (
              <li className="text-[11px] text-slate-500">
                {rp.moreIssues.replace('{count}', fmtCount(issues.length - 8, lang))}
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Product Detail Drawer
// ─────────────────────────────────────────────────────────────────────

function MetaSyncStatusPanel(props: {
  product: StudioProduct
  publication: ProductPublicationStatus
  dr: Translations['catalogMgmt']['studio']['drawer']
  onRetry: () => void
  retryBusy: boolean
}) {
  const status = (props.product.sync_status || '').toLowerCase()
  const pub = props.publication
  let label = props.dr.metaSyncPending
  let tone = 'text-slate-700 bg-slate-50 border-slate-200'
  const syncedBadges: { label: string; tone: string }[] = []

  if (status === 'syncing') {
    label = props.dr.metaSyncSyncing
    tone = 'text-sky-700 bg-sky-50 border-sky-200'
  } else if (status === 'blocked') {
    label = props.dr.metaSyncBlocked
    tone = 'text-amber-800 bg-amber-50 border-amber-200'
  } else if (status === 'failed' || status === 'sync_failed') {
    label = props.dr.metaSyncStateFailed
    tone = 'text-rose-700 bg-rose-50 border-rose-200'
  } else if (status === 'synced') {
    if (pub.meta_catalog_synced) {
      syncedBadges.push({
        label: props.dr.metaSyncSynced,
        tone: 'text-emerald-700 bg-emerald-50 border-emerald-200',
      })
    }
    if (pub.waba_catalog_linked === true) {
      syncedBadges.push({
        label: props.dr.metaSyncWabaLinked,
        tone: 'text-emerald-700 bg-emerald-50 border-emerald-200',
      })
    } else if (pub.waba_catalog_linked === false || pub.waba_catalog_linked === null) {
      syncedBadges.push({
        label: props.dr.metaSyncWabaUncertain,
        tone: 'text-amber-800 bg-amber-50 border-amber-200',
      })
    }
    if (pub.visible_in_whatsapp) {
      syncedBadges.push({
        label: props.dr.metaSyncVisibleWhatsapp,
        tone: 'text-emerald-700 bg-emerald-50 border-emerald-200',
      })
    } else {
      syncedBadges.push({
        label: props.dr.metaSyncWhatsappNotVerified,
        tone: 'text-amber-800 bg-amber-50 border-amber-200',
      })
    }
  }

  const showRetry = Boolean(props.product.retry_allowed) && status !== 'syncing'

  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-bold text-slate-800">{props.dr.metaSyncStatusTitle}</h3>
          {syncedBadges.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {syncedBadges.map((badge) => (
                <span
                  key={badge.label}
                  className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold ${badge.tone}`}
                >
                  {badge.label}
                </span>
              ))}
            </div>
          ) : (
            <span className={`inline-flex mt-2 items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold ${tone}`}>
              {label}
            </span>
          )}
          {props.product.sync_error_summary && (
            <p className="text-[11px] text-rose-700 mt-2">{props.product.sync_error_summary}</p>
          )}
        </div>
        {showRetry && (
          <button
            type="button"
            disabled={props.retryBusy}
            onClick={props.onRetry}
            className="inline-flex items-center gap-1 text-xs font-semibold text-sky-700 hover:bg-sky-50 border border-sky-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50 shrink-0"
          >
            {props.retryBusy
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <RotateCcw className="w-3.5 h-3.5" />}
            {props.retryBusy ? props.dr.metaSyncRetryRunning : props.dr.metaSyncRetryBtn}
          </button>
        )}
      </div>
    </div>
  )
}

type AutoSaveStatus = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

function ProductDrawer(props: {
  productId: number
  onClose: () => void
  onMutated: () => void
}) {
  const { t, dir, lang } = useLanguage()
  const dr = t(tr => tr.catalogMgmt.studio.drawer)
  const fld = dr.fields
  const ph = dr.placeholders

  const [data, setData]       = useState<ProductDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft]     = useState<ReadinessPreviewBody>({})
  const [perChannel, setPerChannel] = useState<ChannelReadiness[]>([])
  const [actionBusy, setActionBusy] = useState(false)
  const [saveStatus, setSaveStatus] = useState<AutoSaveStatus>('idle')
  const [metaPreview, setMetaPreview] = useState<MetaSyncPreviewResponse | null>(null)
  const [metaPreviewBusy, setMetaPreviewBusy] = useState(false)
  const [metaPreviewError, setMetaPreviewError] = useState<string | null>(null)
  const [metaConfirmBusy, setMetaConfirmBusy] = useState(false)
  const [metaConfirmResult, setMetaConfirmResult] = useState<MetaSyncConfirmResponse | null>(null)
  const [metaConfirmError, setMetaConfirmError] = useState<string | null>(null)
  const [metaRetryBusy, setMetaRetryBusy] = useState(false)
  const [priceSaveError, setPriceSaveError] = useState<string | null>(null)
  const previewTimer = useRef<number | null>(null)
  const saveTimer = useRef<number | null>(null)
  const saveVersion = useRef(0)
  const pendingSaveDraft = useRef<ReadinessPreviewBody | null>(null)
  const activeProductId = useRef(props.productId)

  useEffect(() => {
    activeProductId.current = props.productId
    return () => {
      if (previewTimer.current) window.clearTimeout(previewTimer.current)
      if (saveTimer.current) window.clearTimeout(saveTimer.current)
      const pending = pendingSaveDraft.current
      pendingSaveDraft.current = null
      if (pending) {
        void catalogApi.updateProduct(props.productId, pending)
          .then(() => props.onMutated())
          .catch(() => { /* close should not block on a late autosave */ })
      }
    }
  }, [props.productId, props.onMutated])

  // Initial load.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setSaveStatus('idle')
    setMetaPreview(null)
    setMetaPreviewError(null)
    setMetaConfirmResult(null)
    setMetaConfirmError(null)
    setPriceSaveError(null)
    pendingSaveDraft.current = null
    saveVersion.current += 1
    catalogApi.productDetail(props.productId)
      .then(d => {
        if (cancelled) return
        setData(d)
        setPerChannel(d.per_channel)
        // Seed the draft from the loaded product so live counters
        // start with the persisted values.
        setDraft({
          title:             d.product.title || '',
          description:       d.product.description || '',
          price:             d.product.price || '',
          sale_price:        d.product.sale_price || '',
          currency:          d.product.currency || '',
          sku:               d.product.sku || '',
          external_id:       d.product.external_id || '',
          meta_retailer_id:  d.product.meta_retailer_id || '',
          image_url:         d.product.image_url || '',
          product_url:       d.product.product_url || '',
          additional_images: d.product.additional_images || [],
          availability:      d.product.availability || '',
          brand:             d.product.brand || '',
          category:          d.product.category || '',
          condition:         d.product.condition || '',
          gtin:              d.product.gtin || '',
          mpn:               d.product.mpn || '',
          in_stock:          d.product.in_stock,
          stock_quantity:    d.product.stock_quantity ?? undefined,
        })
      })
      .catch(() => { /* soft-fail */ })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [props.productId])

  // Debounced readiness preview on draft change.
  const recompute = useCallback((d: ReadinessPreviewBody) => {
    if (previewTimer.current) window.clearTimeout(previewTimer.current)
    previewTimer.current = window.setTimeout(() => {
      catalogApi.readinessPreview(d)
        .then(r => setPerChannel(r.per_channel))
        .catch(() => { /* keep last good */ })
    }, 280)
  }, [])

  const persistDraft = useCallback(async (d: ReadinessPreviewBody, version: number) => {
    if (!data || !isMerchantEditableSource(data.product.source)) return
    if (!isNativePriceDraftValid(d.price)) {
      setSaveStatus('error')
      setPriceSaveError(dr.priceInvalid)
      return
    }
    const productId = props.productId
    pendingSaveDraft.current = null
    setSaveStatus('saving')
    setPriceSaveError(null)
    try {
      const saved = await catalogApi.updateProduct(productId, d)
      if (version !== saveVersion.current || activeProductId.current !== productId) return
      setData(saved)
      setPerChannel(saved.per_channel)
      setSaveStatus('saved')
      props.onMutated()
    } catch (err: unknown) {
      if (version !== saveVersion.current || activeProductId.current !== productId) return
      setSaveStatus('error')
      const detail = (err as { detail?: { message_ar?: string; error_code?: string } })?.detail
      if (detail?.error_code === 'price_must_be_numeric') {
        setPriceSaveError(detail.message_ar || dr.priceInvalid)
      }
    }
  }, [props, data, dr.priceInvalid])

  const scheduleSave = useCallback((d: ReadinessPreviewBody) => {
    if (!data || !isMerchantEditableSource(data.product.source)) return
    if (!isNativePriceDraftValid(d.price)) {
      setSaveStatus('error')
      setPriceSaveError(dr.priceInvalid)
      return
    }
    setPriceSaveError(null)
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    const version = saveVersion.current + 1
    saveVersion.current = version
    pendingSaveDraft.current = d
    setSaveStatus('pending')
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null
      void persistDraft(d, version)
    }, 700)
  }, [persistDraft, data])

  const update = useCallback(<K extends keyof ReadinessPreviewBody>(k: K, v: ReadinessPreviewBody[K]) => {
    if (!data || !isMerchantEditableSource(data.product.source)) return
    setDraft(prev => {
      const next = { ...prev, [k]: v }
      recompute(next)
      scheduleSave(next)
      return next
    })
  }, [recompute, scheduleSave, data])

  // Escape closes the drawer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') props.onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [props])

  const catalogStatus = data?.product.catalog_status ?? 'active'
  const productSource = data?.product.source ?? 'unknown'
  const isEditable = isMerchantEditableSource(productSource)
  const isExternalManaged = isExternalManagedSource(productSource)
  const ownershipBanner = isEditable
    ? dr.ownershipNahlaManaged
    : isExternalManaged
      ? dr.ownershipExternalManaged
      : productSource === 'meta'
        ? dr.ownershipMetaReadonly
        : null
  const isMerchantHidden =
    catalogStatus === 'merchant_hidden' || Boolean(data?.product.merchant_hidden_at)
  const isRemovedMeta = catalogStatus === 'removed_from_meta'
  const autoSaveLabel =
    saveStatus === 'pending' ? dr.autoSavePending
    : saveStatus === 'saving' ? dr.autoSaveSaving
    : saveStatus === 'saved' ? dr.autoSaveSaved
    : saveStatus === 'error' ? dr.autoSaveFailed
    : dr.autoSaveIdle
  const autoSaveTone =
    saveStatus === 'error'
      ? 'text-rose-700 bg-rose-50 border-rose-100'
      : saveStatus === 'saved'
        ? 'text-emerald-700 bg-emerald-50 border-emerald-100'
        : 'text-slate-500 bg-slate-50 border-slate-100'

  const onDelete = async () => {
    if (!window.confirm(dr.deleteConfirm)) return
    setActionBusy(true)
    try {
      await catalogApi.deleteManualProduct(props.productId)
      props.onMutated()
      props.onClose()
    } catch {
      window.alert(dr.deleteFailed)
    } finally {
      setActionBusy(false)
    }
  }

  const onHide = async () => {
    if (!window.confirm(dr.hideConfirm)) return
    setActionBusy(true)
    try {
      await catalogApi.hideProduct(props.productId)
      props.onMutated()
      props.onClose()
    } catch {
      window.alert(dr.hideFailed)
    } finally {
      setActionBusy(false)
    }
  }

  const onRestore = async () => {
    setActionBusy(true)
    try {
      await catalogApi.restoreProduct(props.productId)
      props.onMutated()
      props.onClose()
    } catch {
      window.alert(dr.restoreFailed)
    } finally {
      setActionBusy(false)
    }
  }

  const onMetaSyncPreview = async () => {
    setMetaPreviewBusy(true)
    setMetaPreviewError(null)
    setMetaPreview(null)
    setMetaConfirmResult(null)
    setMetaConfirmError(null)
    try {
      const result = await catalogApi.metaSyncPreview(props.productId)
      setMetaPreview(result)
    } catch (err: unknown) {
      const detail = (err as { detail?: MetaSyncPreviewResponse | string })?.detail
      if (detail && typeof detail === 'object' && 'message_ar' in detail) {
        setMetaPreview(detail)
      } else {
        setMetaPreviewError(dr.metaSyncFailed)
      }
    } finally {
      setMetaPreviewBusy(false)
    }
  }

  const canMetaConfirm =
    Boolean(
      metaPreview?.eligible
      && (metaPreview.fatal_errors?.length ?? 0) === 0
    )

  const onMetaSyncConfirm = async () => {
    if (!window.confirm(dr.metaSyncConfirmModal)) return
    setMetaConfirmBusy(true)
    setMetaConfirmError(null)
    setMetaConfirmResult(null)
    try {
      const result = await catalogApi.metaSyncConfirm(props.productId)
      setMetaConfirmResult(result)
      props.onMutated()
    } catch (err: unknown) {
      const detail = (err as { detail?: MetaSyncConfirmResponse | string })?.detail
      if (detail && typeof detail === 'object' && 'message_ar' in detail) {
        setMetaConfirmError(detail.message_ar || dr.metaSyncConfirmFailed)
      } else {
        setMetaConfirmError(dr.metaSyncConfirmFailed)
      }
    } finally {
      setMetaConfirmBusy(false)
    }
  }

  const onMetaSyncRetry = async () => {
    setMetaRetryBusy(true)
    try {
      await catalogApi.metaSyncRetry(props.productId)
      const detail = await catalogApi.productDetail(props.productId)
      setData(detail)
      setPerChannel(detail.per_channel)
      props.onMutated()
    } catch {
      window.alert(dr.metaSyncStateFailed)
    } finally {
      setMetaRetryBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex" dir={dir}>
      <div className="flex-1 bg-slate-900/40" onClick={props.onClose} />
      <div className="w-full md:w-[680px] bg-slate-50 h-full overflow-y-auto shadow-2xl border-r border-slate-200">
        {/* Drawer header */}
        <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-5 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <Package className="w-5 h-5 text-emerald-600 shrink-0" />
            <h3 className="font-bold text-slate-900 truncate">
              {loading ? dr.loading : (data?.product.title || dr.defaultTitle)}
            </h3>
            {data && <SourcePill source={data.product.source} />}
            {isRemovedMeta && (
              <span className="text-[11px] font-semibold text-rose-700 bg-rose-50 border border-rose-200 rounded-full px-2 py-0.5">
                {dr.statusRemovedMeta}
              </span>
            )}
            {isMerchantHidden && (
              <span className="text-[11px] font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5">
                {dr.statusHidden}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {data && isEditable && canMetaConfirm && (
              <button
                type="button"
                disabled={actionBusy || metaConfirmBusy || metaPreviewBusy}
                onClick={() => void onMetaSyncConfirm()}
                className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 border border-emerald-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50"
              >
                {metaConfirmBusy
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <ShoppingBag className="w-3.5 h-3.5" />}
                {metaConfirmBusy ? dr.metaSyncConfirmRunning : dr.metaSyncConfirmBtn}
              </button>
            )}
            {data && isEditable && (
              <button
                type="button"
                disabled={actionBusy || metaPreviewBusy || metaConfirmBusy}
                onClick={() => void onMetaSyncPreview()}
                className="inline-flex items-center gap-1 text-xs font-semibold text-sky-700 hover:bg-sky-50 border border-sky-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50"
              >
                {metaPreviewBusy
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <ShoppingBag className="w-3.5 h-3.5" />}
                {metaPreviewBusy ? dr.metaSyncRunning : dr.metaSyncBtn}
              </button>
            )}
            {data && isEditable && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => void onDelete()}
                className="inline-flex items-center gap-1 text-xs font-semibold text-rose-700 hover:bg-rose-50 border border-rose-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50"
              >
                <Trash2 className="w-3.5 h-3.5" /> {dr.deleteBtn}
              </button>
            )}
            {data && !isMerchantHidden && !isRemovedMeta && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => void onHide()}
                className="inline-flex items-center gap-1 text-xs font-semibold text-rose-700 hover:bg-rose-50 border border-rose-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50"
              >
                <EyeOff className="w-3.5 h-3.5" /> {dr.hideBtn}
              </button>
            )}
            {data && isMerchantHidden && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => void onRestore()}
                className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 border border-emerald-200 rounded-lg px-2.5 py-1.5 disabled:opacity-50"
              >
                <RotateCcw className="w-3.5 h-3.5" /> {dr.restoreBtn}
              </button>
            )}
          <button
            onClick={props.onClose}
            className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500"
          >
            <X className="w-5 h-5" />
          </button>
          </div>
        </div>

        {loading || !data ? (
          <div className="p-8 text-center text-slate-500">
            <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
            {dr.loadingData}
          </div>
        ) : (
          <div className="p-5 space-y-5">
            {ownershipBanner && (
              <div className={`rounded-xl border px-3 py-2 text-sm leading-relaxed ${
                isEditable
                  ? 'bg-sky-50 border-sky-200 text-sky-800'
                  : 'bg-amber-50 border-amber-200 text-amber-900'
              }`}>
                {ownershipBanner}
              </div>
            )}
            {isExternalManaged && (
              <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                {dr.metaSyncBlockedExternal}
              </p>
            )}
            {isEditable && metaPreview && (
              <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h3 className="text-sm font-bold text-slate-900">{dr.metaSyncTitle}</h3>
                    <p className="text-xs text-slate-500 mt-0.5">{dr.metaSyncDryRunNote}</p>
                  </div>
                  {metaPreview.eligible && metaPreview.dry_run && (
                    <span className="text-[11px] font-semibold text-sky-700 bg-sky-50 border border-sky-200 rounded-full px-2 py-0.5 shrink-0">
                      dry-run
                    </span>
                  )}
                </div>
                {!metaPreview.eligible && metaPreview.message_ar && (
                  <p className="text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2">
                    {metaPreview.message_ar}
                  </p>
                )}
                {metaPreview.eligible && (
                  <>
                    <div className="flex flex-wrap gap-2 text-[11px] text-slate-600">
                      {metaPreview.meta_catalog_id && (
                        <span className="bg-slate-50 border border-slate-200 rounded-full px-2 py-0.5">
                          {dr.metaSyncCatalogId}: <code dir="ltr" className="font-mono">{metaPreview.meta_catalog_id}</code>
                        </span>
                      )}
                      {metaPreview.retailer_id && (
                        <span className="bg-slate-50 border border-slate-200 rounded-full px-2 py-0.5">
                          {dr.metaSyncRetailerId}: <code dir="ltr" className="font-mono">{metaPreview.retailer_id}</code>
                        </span>
                      )}
                    </div>
                    {(metaPreview.fatal_errors?.length ?? 0) > 0 && (
                      <div className="rounded-lg border border-rose-200 bg-rose-50/60 p-3">
                        <h4 className="text-xs font-bold text-rose-800 mb-1.5">{dr.metaSyncFatalTitle}</h4>
                        <ul className="text-xs text-rose-900 space-y-1">
                          {metaPreview.fatal_errors!.map(item => (
                            <li key={item.code}>{item.message_ar}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {(metaPreview.warnings?.length ?? 0) > 0 && (
                      <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
                        <h4 className="text-xs font-bold text-amber-800 mb-1.5">{dr.metaSyncWarningsTitle}</h4>
                        <ul className="text-xs text-amber-900 space-y-1">
                          {metaPreview.warnings!.map(item => (
                            <li key={item.code}>{item.message_ar}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {metaPreview.payload && (
                      <pre
                        dir="ltr"
                        className="text-[10px] leading-relaxed bg-slate-900 text-slate-100 rounded-lg p-3 overflow-x-auto max-h-48"
                      >
                        {JSON.stringify(metaPreview.payload, null, 2)}
                      </pre>
                    )}
                    {canMetaConfirm && (
                      <button
                        type="button"
                        disabled={metaConfirmBusy || metaPreviewBusy}
                        onClick={() => void onMetaSyncConfirm()}
                        className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2 disabled:opacity-50"
                      >
                        {metaConfirmBusy
                          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          : <ShoppingBag className="w-3.5 h-3.5" />}
                        {metaConfirmBusy ? dr.metaSyncConfirmRunning : dr.metaSyncConfirmBtn}
                      </button>
                    )}
                  </>
                )}
              </div>
            )}
            {isEditable && metaConfirmResult?.ok && (
              <p className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
                {dr.metaSyncConfirmSuccess}
              </p>
            )}
            {isEditable && metaConfirmError && (
              <p className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2">
                {metaConfirmError}
              </p>
            )}
            {isEditable && metaPreviewError && (
              <p className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2">
                {metaPreviewError}
              </p>
            )}
            {/* Hero — image + meta */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4 flex gap-4">
              <div className="shrink-0 w-32 h-32 rounded-xl bg-slate-100 border border-slate-200 overflow-hidden flex items-center justify-center">
                <ProductThumbnail
                  row={{
                    image_url: draft.image_url ?? data.product.image_url,
                    additional_images: data.product.additional_images,
                  }}
                  iconClassName="w-8 h-8 text-slate-300"
                />
              </div>
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="text-xs text-slate-400 font-mono" dir="ltr">#{data.product.id}</div>
                <h2 className="text-base font-bold text-slate-900 leading-tight">{data.product.title}</h2>
                <div className="flex items-center gap-2 text-xs flex-wrap">
                  <span className="text-slate-700 font-semibold">{data.product.price ?? '—'}</span>
                  {data.product.sale_price && (
                    <span className="text-emerald-700">{dr.saleLabel} {data.product.sale_price}</span>
                  )}
                  {data.product.currency && <span className="text-slate-500">{data.product.currency}</span>}
                </div>
                <div className="flex items-center gap-1.5 text-[11px] flex-wrap">
                  <span className="inline-flex items-center gap-1 text-slate-600 bg-slate-50 border border-slate-200 rounded-full px-2 py-0.5">
                    retailer_id: <code dir="ltr" className="font-mono">{data.product.effective_retailer_id || '—'}</code>
                  </span>
                  {data.product.in_stock
                    ? <span className="inline-flex items-center gap-1 text-emerald-700"><CheckCircle2 className="w-3 h-3" /> {t(tr => tr.catalogMgmt.studio.grid.inStock)}</span>
                    : <span className="inline-flex items-center gap-1 text-rose-700"><XCircle className="w-3 h-3" /> {t(tr => tr.catalogMgmt.studio.grid.outOfStock)}</span>}
                  {data.product.product_url && (
                    <a
                      href={data.product.product_url}
                      target="_blank" rel="noopener noreferrer"
                      className="inline-flex items-center gap-0.5 text-emerald-700 hover:underline"
                    >
                      {dr.storePage} <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                </div>
              </div>
            </div>

            {data?.publication && isEditable && (
              <MetaSyncStatusPanel
                product={data.product}
                publication={data.publication}
                dr={dr}
                onRetry={() => void onMetaSyncRetry()}
                retryBusy={metaRetryBusy}
              />
            )}

            {/* Readiness panel */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4">
              <h3 className="text-sm font-bold text-slate-800 mb-3 flex items-center gap-2">
                <Sparkles className="w-4 h-4 text-emerald-600" />
                {dr.readinessTitle}
              </h3>
              <ReadinessPanel perChannel={perChannel} />
            </div>

            {/* Form — live counters drive feedback; autosave persists the central catalog row. */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4 space-y-3">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-bold text-slate-800 flex items-center gap-2">
                  <Package className="w-4 h-4 text-emerald-600" />
                  {dr.productDataTitle}
                </h3>
                <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${autoSaveTone}`}>
                  {saveStatus === 'saving' && <Loader2 className="w-3 h-3 animate-spin" />}
                  {isEditable ? autoSaveLabel : dr.readOnlyNote}
                </span>
              </div>
              <FieldShell
                fieldName="title" label={fld.title} required
                value={draft.title ?? ''} onChange={v => update('title', v)}
                perChannel={perChannel} disabled={!isEditable}
              />
              <FieldShell
                fieldName="description" label={fld.description} multiline
                value={draft.description ?? ''} onChange={v => update('description', v)}
                perChannel={perChannel} disabled={!isEditable}
              />
              <div className="grid grid-cols-2 gap-3">
                <FieldShell
                  fieldName="price" label={fld.price} required
                  value={draft.price ?? ''} onChange={v => update('price', v)}
                  perChannel={perChannel} disabled={!isEditable}
                  inputMode="decimal"
                  type="text"
                  helperText={isEditable ? dr.priceHelper : undefined}
                  errorText={isEditable ? priceSaveError ?? undefined : undefined}
                />
                <FieldShell
                  fieldName="sale_price" label={fld.salePrice}
                  value={draft.sale_price ?? ''} onChange={v => update('sale_price', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <FieldShell
                  fieldName="currency" label={fld.currency} dir="ltr"
                  value={draft.currency ?? ''} onChange={v => update('currency', v.toUpperCase())}
                  perChannel={perChannel} placeholder={ph.currency} disabled={!isEditable}
                />
                <FieldShell
                  fieldName="availability" label={fld.availability}
                  value={draft.availability ?? ''} onChange={v => update('availability', v)}
                  perChannel={perChannel} placeholder={ph.availability} disabled={!isEditable}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <FieldShell
                  fieldName="image_url" label={fld.imageUrl} dir="ltr"
                  value={draft.image_url ?? ''} onChange={v => update('image_url', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
                <FieldShell
                  fieldName="product_url" label={fld.productUrl} dir="ltr"
                  value={draft.product_url ?? ''} onChange={v => update('product_url', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <FieldShell
                  fieldName="brand" label={fld.brand}
                  value={draft.brand ?? ''} onChange={v => update('brand', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
                <FieldShell
                  fieldName="category" label={fld.category}
                  value={draft.category ?? ''} onChange={v => update('category', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
              </div>
              <div className="grid grid-cols-3 gap-3">
                <FieldShell
                  fieldName="condition" label={fld.condition}
                  value={draft.condition ?? ''} onChange={v => update('condition', v)}
                  perChannel={perChannel} placeholder={ph.condition} disabled={!isEditable}
                />
                <FieldShell
                  fieldName="gtin" label={fld.gtin} dir="ltr"
                  value={draft.gtin ?? ''} onChange={v => update('gtin', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
                <FieldShell
                  fieldName="mpn" label={fld.mpn} dir="ltr"
                  value={draft.mpn ?? ''} onChange={v => update('mpn', v)}
                  perChannel={perChannel} disabled={!isEditable}
                />
              </div>

              {isEditable && (
              <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-100 rounded-lg p-2 leading-relaxed">
                {dr.autoSaveNote}
              </div>
              )}
            </div>

            {/* Variants — read-only preview when present */}
            {data.product.variants.length > 0 && (
              <div className="bg-white rounded-2xl border border-slate-200 p-4">
                <h3 className="text-sm font-bold text-slate-800 mb-2">{dr.variantsTitle}</h3>
                <p className="text-[11px] text-slate-500 mb-3">
                  {dr.variantsPhase2Note.replace('{count}', fmtCount(data.product.variants.length, lang))}
                </p>
                <ul className="text-xs text-slate-700 space-y-1">
                  {data.product.variants.slice(0, 10).map((v, i) => (
                    <li key={i} className="bg-slate-50 rounded-lg px-2 py-1 font-mono text-[11px]" dir="ltr">
                      {JSON.stringify(v)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Public component — the Studio as one unit
// ─────────────────────────────────────────────────────────────────────

export default function ProductStudio(props: {
  refreshTrigger?: number
  onImportMeta?: () => void
  onAddManual?: () => void
  showEmptyActions?: boolean
}) {
  const [filters, setFilters] = useState<StudioFilters>({})
  const [offset, setOffset]   = useState(0)
  const [limit]               = useState(50)
  const [rows, setRows]       = useState<CatalogProductDiagRow[]>([])
  const [total, setTotal]     = useState(0)
  const [variantsSummary, setVariantsSummary] =
    useState<CatalogVariantsSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const r = await catalogApi.products(limit, offset, filters)
      setRows(r.rows)
      setTotal(r.total)
      setVariantsSummary(r.variants_summary ?? null)
    } catch {
      setRows([])
      setTotal(0)
      setVariantsSummary(null)
    } finally {
      setLoading(false)
    }
  }, [filters, offset, limit])

  // Reset offset whenever filters change (otherwise paging into page 3
  // then narrowing to 5 results would show an empty grid).
  useEffect(() => { setOffset(0) }, [filters])
  useEffect(() => { void reload() }, [reload])
  useEffect(() => {
    if (props.refreshTrigger === undefined || props.refreshTrigger === 0) return
    void reload()
  }, [props.refreshTrigger, reload])

  return (
    <div className="space-y-4">
      <VariantsSummaryBar summary={variantsSummary} />
      <FiltersBar
        filters={filters}
        onChange={setFilters}
        totalShown={rows.length}
        total={total}
      />
      <ProductGrid
        rows={rows}
        loading={loading}
        onSelect={setSelectedId}
        onImportMeta={props.onImportMeta}
        onAddManual={props.onAddManual}
        showEmptyActions={props.showEmptyActions}
      />
      <Pagination offset={offset} limit={limit} total={total} onChange={setOffset} />

      {selectedId !== null && (
        <ProductDrawer
          productId={selectedId}
          onClose={() => setSelectedId(null)}
          onMutated={() => { void reload() }}
        />
      )}
    </div>
  )
}
