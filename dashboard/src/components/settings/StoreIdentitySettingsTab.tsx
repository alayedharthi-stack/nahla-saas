import { AlertCircle, CheckCircle, Loader2, Save, Type } from 'lucide-react'
import { useLanguage } from '../../i18n/context'
import type { StoreSettings } from '../../api/settings'

function externalSourceLabel(source: string | undefined, isArabic: boolean): string | null {
  if (!source?.startsWith('external:')) return null
  const providerKey = source.slice('external:'.length).toLowerCase()
  const provider = providerKey === 'salla'
    ? 'Salla'
    : providerKey === 'zid'
      ? 'Zid'
      : providerKey || 'integration'
  return isArabic
    ? `مستورد من ${provider}`
    : `Imported from ${provider}`
}

export interface StoreIdentitySettingsTabProps {
  data: StoreSettings
  onChange: (patch: Partial<StoreSettings>) => void
  onSave: () => void
  saving: boolean
  saved: boolean
  error: string | null
}

export default function StoreIdentitySettingsTab({
  data,
  onChange,
  onSave,
  saving,
  saved,
  error,
}: StoreIdentitySettingsTabProps) {
  const { lang, t } = useLanguage()
  const isArabic = lang === 'ar'

  return (
    <div className="space-y-5">
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Type className="w-4 h-4 text-brand-500 shrink-0" />
          <h2 className="text-sm font-semibold text-slate-900">
            {isArabic ? 'هوية المتجر' : 'Store Identity'}
          </h2>
        </div>
        <div className="p-5 space-y-4">
          <p className="text-sm text-slate-600 leading-relaxed">
            {isArabic
              ? 'سيُستخدم اسم المتجر المعتمد داخل المنصة وفي تواصل الذكاء مع العملاء.'
              : 'The approved store name will be used across the platform and in AI customer conversations.'}
          </p>
          <p className="text-sm text-slate-500 leading-relaxed">
            {isArabic
              ? 'يمكن أن يملأ اسم المتجر تلقائياً عند ربط سلة أو زد. التعديل اليدوي محمي ولن يُستبدل بالمزامنة. اللغة الناقصة لا تُترجم تلقائياً.'
              : 'Connecting Salla or Zid may prefill store names. Manual edits are protected from sync overwrites. Missing languages are not auto-translated.'}
          </p>
          <div>
            <label htmlFor="store-name-ar" className="block text-sm font-medium text-slate-700 mb-1">
              {isArabic ? 'اسم المتجر بالعربية' : 'Store Name in Arabic'}
            </label>
            <input
              id="store-name-ar"
              type="text"
              dir="rtl"
              className="input w-full"
              value={data.store_name_ar ?? ''}
              onChange={e => onChange({ store_name_ar: e.target.value })}
            />
            {externalSourceLabel(data.store_name_ar_source, isArabic) && (
              <p className="text-xs text-slate-500 mt-1">
                {externalSourceLabel(data.store_name_ar_source, isArabic)}
              </p>
            )}
          </div>
          <div>
            <label htmlFor="store-name-en" className="block text-sm font-medium text-slate-700 mb-1">
              {isArabic ? 'اسم المتجر بالإنجليزية' : 'Store Name in English'}
            </label>
            <input
              id="store-name-en"
              type="text"
              dir="ltr"
              className="input w-full"
              value={data.store_name_en ?? ''}
              onChange={e => onChange({ store_name_en: e.target.value })}
            />
            {externalSourceLabel(data.store_name_en_source, isArabic) && (
              <p className="text-xs text-slate-500 mt-1">
                {externalSourceLabel(data.store_name_en_source, isArabic)}
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className="btn-primary inline-flex items-center gap-2 text-sm"
        >
          {saving
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <Save className="w-4 h-4" />}
          {saving
            ? t(tr => tr.settings.saveBar.saving)
            : t(tr => tr.settings.saveBar.save)}
        </button>
        {saved && (
          <span className="flex items-center gap-1.5 text-sm text-emerald-600">
            <CheckCircle className="w-4 h-4" />
            {t(tr => tr.settings.saveBar.saved)}
          </span>
        )}
        {error && (
          <span className="flex items-center gap-1.5 text-sm text-red-600">
            <AlertCircle className="w-4 h-4" />
            {error}
          </span>
        )}
      </div>
    </div>
  )
}
