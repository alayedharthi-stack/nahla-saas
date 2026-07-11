import { CheckCircle2, Loader2, RefreshCw } from 'lucide-react'
import type { CatalogDiagnostics } from '../../api/catalog'
import { useLanguage } from '../../i18n/context'

function WhatsAppIcon({ className = 'w-5 h-5' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.435 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" />
    </svg>
  )
}

export type WhatsAppCatalogActivationCTAProps = {
  diagnostics: CatalogDiagnostics
  busy?: boolean
  verifying?: boolean
  onActivate: () => void
  onManage: () => void
  onRecheck: () => void
}

export default function WhatsAppCatalogActivationCTA(props: WhatsAppCatalogActivationCTAProps) {
  const { tStatic } = useLanguage()
  const copy = tStatic(tr => tr.catalogMgmt.whatsappActivation)
  const d = props.diagnostics

  const waConnected = d.catalog.whatsapp_connected
  const catalogActive = d.catalog.catalog_enabled && d.catalog.catalog_id_present
  const catalogReady = d.readiness.catalog_ready

  if (props.verifying) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 flex items-center gap-3">
        <Loader2 className="w-5 h-5 text-slate-500 animate-spin shrink-0" />
        <p className="text-sm text-slate-600">{copy.verifying}</p>
      </div>
    )
  }

  if (!waConnected) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 space-y-2">
        <p className="text-sm font-semibold text-slate-800">{copy.notConnectedTitle}</p>
        <p className="text-xs text-slate-600 leading-relaxed">{copy.notConnectedDesc}</p>
      </div>
    )
  }

  if (catalogActive && catalogReady) {
    return (
      <div className="rounded-xl border border-[#25D366]/40 bg-[#25D366]/5 p-4 space-y-3">
        <div className="flex flex-wrap items-start gap-3">
          <span className="inline-flex items-center justify-center w-11 h-11 rounded-full bg-[#25D366] text-white shrink-0">
            <WhatsAppIcon className="w-6 h-6" />
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="text-sm font-bold text-slate-900">{copy.activeTitle}</h4>
              <span className="inline-flex items-center gap-1 text-[11px] font-bold text-emerald-800 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">
                <CheckCircle2 className="w-3.5 h-3.5" />
                {copy.activeBadge}
              </span>
            </div>
            <p className="text-xs text-slate-600 leading-relaxed">{copy.activeDesc}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={props.onManage}
            className="inline-flex items-center gap-2 text-xs font-bold text-slate-700 bg-white hover:bg-slate-50 border border-slate-200 px-4 py-2 rounded-xl transition"
          >
            {copy.manageBtn}
          </button>
          <button
            type="button"
            onClick={props.onRecheck}
            disabled={props.busy}
            className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg px-3 py-2 transition disabled:opacity-50"
          >
            {props.busy
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <RefreshCw className="w-3.5 h-3.5" />}
            {copy.recheckBtn}
          </button>
        </div>
      </div>
    )
  }

  if (catalogActive && !catalogReady) {
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 space-y-3">
        <div className="flex items-start gap-3">
          <span className="inline-flex items-center justify-center w-11 h-11 rounded-full bg-[#25D366] text-white shrink-0">
            <WhatsAppIcon className="w-6 h-6" />
          </span>
          <div className="min-w-0 space-y-1">
            <h4 className="text-sm font-bold text-slate-900">{copy.enabledNeedsSetupTitle}</h4>
            <p className="text-xs text-slate-600 leading-relaxed">{copy.enabledNeedsSetupDesc}</p>
          </div>
        </div>
        <button
          type="button"
          onClick={props.onManage}
          className="inline-flex items-center gap-2 text-xs font-bold text-slate-700 bg-white hover:bg-slate-50 border border-slate-200 px-4 py-2 rounded-xl transition"
        >
          {copy.manageBtn}
        </button>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-[#25D366]/30 bg-gradient-to-br from-[#25D366]/10 to-emerald-50 p-4 space-y-3">
      <div className="flex flex-wrap items-start gap-3">
        <span className="inline-flex items-center justify-center w-11 h-11 rounded-full bg-[#25D366] text-white shrink-0 shadow-sm">
          <WhatsAppIcon className="w-6 h-6" />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <h4 className="text-sm font-bold text-slate-900">{copy.activateTitle}</h4>
          <p className="text-xs text-slate-600 leading-relaxed">{copy.activateDesc}</p>
        </div>
      </div>
      <button
        type="button"
        onClick={props.onActivate}
        disabled={props.busy}
        className="w-full sm:w-auto inline-flex items-center justify-center gap-2.5 bg-[#25D366] hover:bg-[#128C7E] disabled:opacity-60 text-white font-bold px-6 py-3 rounded-xl text-sm shadow-md transition"
      >
        {props.busy
          ? <Loader2 className="w-5 h-5 animate-spin" />
          : <WhatsAppIcon className="w-5 h-5" />}
        {copy.activateBtn}
      </button>
    </div>
  )
}
