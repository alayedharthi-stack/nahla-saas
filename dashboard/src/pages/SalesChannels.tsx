import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader2, MapPin, MessageCircle, Save, Store } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import { useLanguage } from '../i18n/context'
import { settingsApi, type StoreSettings } from '../api/settings'

type SalesChannelToggles = {
  online_store?: { enabled?: boolean }
  whatsapp_quick_order?: { enabled?: boolean }
  showroom_visit?: { enabled?: boolean }
}

type StoreSettingsWithChannels = StoreSettings & {
  sales_channels?: SalesChannelToggles
}

function Toggle({
  label,
  hint,
  value,
  onChange,
  disabled,
}: {
  label: string
  hint?: string
  value: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-3 border-b border-slate-100 last:border-0">
      <div>
        <p className="text-sm font-medium text-slate-800">{label}</p>
        {hint && <p className="text-xs text-slate-500 mt-0.5">{hint}</p>}
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-6 w-11 shrink-0 rounded-full transition-colors ${
          value ? 'bg-brand-500' : 'bg-slate-200'
        } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
        aria-pressed={value}
      >
        <span
          className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform mt-0.5 ${
            value ? 'translate-x-5' : 'translate-x-0.5'
          }`}
        />
      </button>
    </div>
  )
}

export default function SalesChannels() {
  const { t } = useLanguage()
  const sc = t(tr => tr.pages.salesChannels)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [storeSettings, setStoreSettings] = useState<StoreSettingsWithChannels | null>(null)
  const [storeUrl, setStoreUrl] = useState('')
  const [onlineEnabled, setOnlineEnabled] = useState(true)
  const [mapsLocation, setMapsLocation] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await settingsApi.getAll()
      const store = (data.store || {}) as StoreSettingsWithChannels
      setStoreSettings(store)
      setStoreUrl(store.store_url || '')
      setMapsLocation(store.google_maps_location || '')
      setOnlineEnabled(store.sales_channels?.online_store?.enabled ?? true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : sc.loadError)
    } finally {
      setLoading(false)
    }
  }, [sc.loadError])

  useEffect(() => { load() }, [load])

  const save = async () => {
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      if (!storeSettings) return
      await settingsApi.update({
        store: {
          ...storeSettings,
          store_url: storeUrl.trim(),
          sales_channels: {
            ...(storeSettings.sales_channels || {}),
            online_store: { enabled: onlineEnabled },
            whatsapp_quick_order: { enabled: true },
            showroom_visit: { enabled: true },
          },
        },
      })
      setSaved(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : sc.saveError)
    } finally {
      setSaving(false)
    }
  }

  const onlineAvailable = onlineEnabled && Boolean(storeUrl.trim())
  const showroomAvailable = Boolean(mapsLocation.trim())

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24 text-slate-400">
        <Loader2 className="w-6 h-6 animate-spin" />
      </div>
    )
  }

  return (
    <div className="max-w-3xl mx-auto space-y-6 pb-10">
      <PageHeader title={sc.title} subtitle={sc.subtitle} />

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}
      {saved && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
          {sc.saved}
        </div>
      )}

      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Store className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">{sc.onlineStore.title}</h2>
        </div>
        <div className="p-5 space-y-4">
          <Toggle
            label={sc.onlineStore.enableLabel}
            hint={sc.onlineStore.enableHint}
            value={onlineEnabled}
            onChange={setOnlineEnabled}
          />
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              {sc.onlineStore.urlLabel}
            </label>
            <input
              type="url"
              dir="ltr"
              className="input w-full"
              placeholder="https://your-store.example"
              value={storeUrl}
              onChange={e => setStoreUrl(e.target.value)}
            />
            <p className="text-xs text-slate-500 mt-1">{sc.onlineStore.urlHint}</p>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-slate-600">{sc.statusLabel}:</span>
            <Badge
              label={onlineAvailable ? sc.available : sc.notAvailable}
              variant={onlineAvailable ? 'green' : 'slate'}
            />
          </div>
        </div>
      </div>

      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <MessageCircle className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">{sc.whatsapp.title}</h2>
        </div>
        <div className="p-5">
          <p className="text-sm text-slate-600">{sc.whatsapp.description}</p>
          <div className="mt-3 flex items-center gap-2 text-sm">
            <span className="text-slate-600">{sc.statusLabel}:</span>
            <Badge label={sc.available} variant="green" />
          </div>
        </div>
      </div>

      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <MapPin className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">{sc.showroom.title}</h2>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-sm text-slate-600">{sc.showroom.description}</p>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-slate-600">{sc.statusLabel}:</span>
            <Badge
              label={showroomAvailable ? sc.available : sc.notAvailable}
              variant={showroomAvailable ? 'green' : 'slate'}
            />
          </div>
          <Link
            to="/settings"
            className="inline-flex text-sm text-brand-600 hover:text-brand-700"
          >
            {sc.showroom.mapsHint}
          </Link>
          {' · '}
          <Link
            to="/operations-center"
            className="inline-flex text-sm text-brand-600 hover:text-brand-700"
          >
            {sc.showroom.branchesLink}
          </Link>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="btn-primary inline-flex items-center gap-2"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {sc.save}
        </button>
      </div>
    </div>
  )
}
