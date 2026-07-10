import { CheckCircle2, Clock, Download, Loader2, MoreHorizontal, Package, Plus } from 'lucide-react'
import { useState } from 'react'
import type { CatalogDiagnostics } from '../../api/catalog'
import { useLanguage } from '../../i18n/context'
import type { Lang, Translations } from '../../i18n/types'
import { resolveCatalogDisplaySource } from './catalogDisplay'

type CatalogSourceKey = keyof Translations['catalogMgmt']['sources']

const SOURCE_STYLES: Record<string, { bg: string; text: string }> = {
  salla:   { bg: 'bg-orange-50  border-orange-200',  text: 'text-orange-700' },
  zid:     { bg: 'bg-violet-50  border-violet-200',  text: 'text-violet-700' },
  meta:    { bg: 'bg-blue-50    border-blue-200',    text: 'text-blue-700'   },
  manual:  { bg: 'bg-sky-50     border-sky-200',     text: 'text-sky-700'    },
  unknown: { bg: 'bg-slate-50   border-slate-200',   text: 'text-slate-600'  },
  mixed:   { bg: 'bg-amber-50   border-amber-200',   text: 'text-amber-700'  },
}

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

export default function CatalogSummaryCard(props: {
  diagnostics: CatalogDiagnostics
  showMetaImport: boolean
  metaImportBusy: boolean
  onImportMeta: () => void
  onAddManual: () => void
  onOpenAdvanced: () => void
}) {
  const { tStatic, lang } = useLanguage()
  const copy = tStatic(tr => tr.catalogMgmt.summary)
  const sources = tStatic(tr => tr.catalogMgmt.sources)
  const [menuOpen, setMenuOpen] = useState(false)

  const d = props.diagnostics
  const displaySource = resolveCatalogDisplaySource(d.products)
  const sourceKey = (displaySource in SOURCE_STYLES ? displaySource : 'unknown') as CatalogSourceKey
  const sourceStyle = SOURCE_STYLES[sourceKey] ?? SOURCE_STYLES.unknown

  const statusLabel = d.products.total === 0
    ? copy.statusEmpty
    : d.readiness.catalog_ready
      ? copy.statusReady
      : copy.statusNotReady

  const lastUpdate = d.import.last_at
    ? fmtImportAt(d.import.last_at, lang)
    : copy.lastImportNever

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0 space-y-3">
          <div>
            <h2 className="text-lg font-black text-slate-900 flex flex-wrap items-center gap-2">
              <Package className="w-6 h-6 text-emerald-600 shrink-0" />
              {copy.title}
            </h2>
            <p className="text-2xl font-black text-slate-900 mt-1">
              {copy.productCount.replace('{count}', fmtCount(d.products.total, lang))}
            </p>
          </div>
          <dl className="grid grid-cols-1 xs:grid-cols-2 gap-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <dt className="text-slate-500 font-semibold">{copy.sourceLabel}</dt>
              <dd>
                <span className={`inline-flex items-center rounded-full border font-semibold text-xs px-2.5 py-0.5 ${sourceStyle.bg} ${sourceStyle.text}`}>
                  {sources[sourceKey]}
                </span>
              </dd>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <dt className="text-slate-500 font-semibold">{copy.lastUpdateLabel}</dt>
              <dd className="text-slate-800 flex items-center gap-1">
                <Clock className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                <span>{lastUpdate}</span>
              </dd>
            </div>
            <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
              <dt className="text-slate-500 font-semibold">{copy.statusLabel}</dt>
              <dd>
                <span className={`inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full border ${
                  d.readiness.catalog_ready && d.products.total > 0
                    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : 'bg-amber-50 border-amber-200 text-amber-700'
                }`}>
                  {d.readiness.catalog_ready && d.products.total > 0
                    ? <CheckCircle2 className="w-3.5 h-3.5" />
                    : null}
                  {statusLabel}
                </span>
              </dd>
            </div>
          </dl>
        </div>

        <div className="flex flex-wrap items-center gap-2 shrink-0">
          {props.showMetaImport && (
            <button
              type="button"
              onClick={props.onImportMeta}
              disabled={props.metaImportBusy}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-4 py-2.5 rounded-xl text-sm transition shadow-sm"
            >
              {props.metaImportBusy
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Download className="w-4 h-4" />}
              {tStatic(tr => tr.catalogMgmt.page.importFromMeta)}
            </button>
          )}
          <button
            type="button"
            onClick={props.onAddManual}
            className="inline-flex items-center gap-2 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-4 py-2.5 rounded-xl text-sm transition"
          >
            <Plus className="w-4 h-4" />
            {tStatic(tr => tr.catalogMgmt.page.addManual)}
          </button>
          <div className="relative">
            <button
              type="button"
              onClick={() => setMenuOpen(v => !v)}
              className="inline-flex items-center gap-1.5 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 font-semibold px-3 py-2.5 rounded-xl text-sm transition"
              aria-expanded={menuOpen}
            >
              <MoreHorizontal className="w-4 h-4" />
              {copy.moreActions}
            </button>
            {menuOpen && (
              <div className="absolute end-0 mt-1 z-10 min-w-[10rem] bg-white border border-slate-200 rounded-xl shadow-lg py-1 text-sm">
                <button
                  type="button"
                  className="w-full text-start px-3 py-2 hover:bg-slate-50 text-slate-700"
                  onClick={() => { setMenuOpen(false); props.onOpenAdvanced() }}
                >
                  {tStatic(tr => tr.catalogMgmt.advanced.title)}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
