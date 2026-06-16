import { useState } from 'react'
import { Clock, Megaphone } from 'lucide-react'

import { customersApi } from '../../api/customers'
import { useLanguage } from '../../i18n/context'

export type CampaignExcludeVariant = 'menu' | 'button' | 'inline-chip'

function normalizePhoneDigits(phone: string): string {
  return (phone || '').trim().replace(/^\+/, '').replace(/[\s-]/g, '')
}

async function resolveCustomerIdByPhone(phone: string): Promise<number | null> {
  const digits = normalizePhoneDigits(phone)
  if (!digits) return null
  const res = await customersApi.list({ search: phone, perPage: 20, page: 1 })
  const match = res.customers.find((c) => {
    const cd = normalizePhoneDigits(c.phone)
    return cd === digits || cd.endsWith(digits.slice(-9)) || digits.endsWith(cd.slice(-9))
  })
  return match?.id ?? null
}

export interface CampaignExcludeControlProps {
  customerId?: number | null
  phone?: string
  optedOut: boolean
  customerLabel: string
  variant: CampaignExcludeVariant
  onSuccess?: (nextOptedOut: boolean) => void
  onMenuClose?: () => void
  disabled?: boolean
}

export default function CampaignExcludeControl({
  customerId,
  phone,
  optedOut,
  customerLabel,
  variant,
  onSuccess,
  onMenuClose,
  disabled = false,
}: CampaignExcludeControlProps) {
  const { t, dir } = useLanguage()
  const cp = t((tr) => tr.conversationsPage)

  const [modal, setModal] = useState<'exclude' | 're-enable' | null>(null)
  const [busy, setBusy] = useState(false)

  const openModal = (e?: React.MouseEvent) => {
    e?.stopPropagation()
    if (disabled || busy) return
    onMenuClose?.()
    setModal(optedOut ? 're-enable' : 'exclude')
  }

  const closeModal = () => {
    if (!busy) setModal(null)
  }

  const resolveId = async (): Promise<number> => {
    if (customerId) return customerId
    if (!phone) throw new Error(cp.errors.customerNotFound)
    const id = await resolveCustomerIdByPhone(phone)
    if (!id) throw new Error(cp.errors.customerNotFound)
    return id
  }

  const confirm = async () => {
    if (!modal) return
    setBusy(true)
    try {
      const id = await resolveId()
      const nextOptedOut = modal === 'exclude'
      await customersApi.updateMarketingPreferences(id, {
        marketing_opt_out_manual: nextOptedOut,
      })
      setModal(null)
      onSuccess?.(nextOptedOut)
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.excludeFailed)
    } finally {
      setBusy(false)
    }
  }

  const trigger = (() => {
    if (variant === 'menu') {
      return (
        <button
          type="button"
          className={
            optedOut
              ? 'w-full flex items-center gap-2 px-3 py-2.5 text-sm text-violet-700 hover:bg-violet-50'
              : 'w-full flex items-center gap-2 px-3 py-2.5 text-sm text-slate-700 hover:bg-slate-50'
          }
          onClick={openModal}
          disabled={disabled || busy}
        >
          <Megaphone className="w-4 h-4 text-violet-500" />
          {optedOut ? cp.actions.excludedFromCampaigns : cp.actions.excludeCampaigns}
        </button>
      )
    }

    if (variant === 'inline-chip') {
      return (
        <button
          type="button"
          onClick={openModal}
          disabled={disabled || busy}
          className={
            'text-[11px] font-medium inline-flex items-center gap-1 px-2 py-0.5 rounded-md border transition ' +
            (optedOut
              ? 'border-purple-400 bg-purple-100 text-purple-800 hover:bg-purple-200'
              : 'border-purple-300 bg-purple-50 text-purple-700 hover:bg-purple-100')
          }
        >
          <Megaphone className="w-3 h-3" />
          {optedOut ? cp.actions.removeExclusionShort : cp.actions.excludeCampaigns}
        </button>
      )
    }

    return (
      <div className="space-y-2">
        {optedOut && (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-violet-700 bg-violet-50 border border-violet-200 rounded-lg px-2.5 py-1">
            <Megaphone className="w-3.5 h-3.5" />
            {cp.actions.excludedFromCampaigns}
          </span>
        )}
        <button
          type="button"
          onClick={openModal}
          disabled={disabled || busy}
          className={
            optedOut
              ? 'w-full inline-flex items-center justify-center gap-2 text-sm border border-violet-300 text-violet-700 bg-white hover:bg-violet-50 rounded-lg py-2 font-medium transition-colors disabled:opacity-50'
              : 'w-full inline-flex items-center justify-center gap-2 text-sm bg-violet-600 hover:bg-violet-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-50'
          }
        >
          <Megaphone className="w-4 h-4" />
          {optedOut ? cp.actions.removeExclusion : cp.actions.excludeCampaigns}
        </button>
      </div>
    )
  })()

  const isExclude = modal === 'exclude'

  return (
    <>
      {trigger}

      {modal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-sm p-4"
          onClick={closeModal}
        >
          <div
            className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6 space-y-4"
            dir={dir}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-violet-100 rounded-xl flex items-center justify-center shrink-0">
                <Megaphone className="w-5 h-5 text-violet-600" />
              </div>
              <div className="min-w-0">
                <h3 className="text-base font-semibold text-slate-900">
                  {isExclude ? cp.banners.excludeModalTitle : cp.banners.reEnableModalTitle}
                </h3>
                <p className="text-xs text-slate-500 mt-0.5 truncate">{customerLabel}</p>
              </div>
            </div>

            <p className="text-sm text-slate-600 leading-relaxed">
              {isExclude ? cp.banners.excludeModalBody : cp.banners.reEnableModalBody}
            </p>

            <div className="flex gap-3 pt-1">
              <button
                type="button"
                onClick={closeModal}
                disabled={busy}
                className="flex-1 btn-secondary text-sm"
              >
                {cp.actions.cancel}
              </button>
              <button
                type="button"
                onClick={() => { void confirm() }}
                disabled={busy}
                className="flex-1 inline-flex items-center justify-center gap-2 text-sm bg-violet-600 hover:bg-violet-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {busy ? (
                  <Clock className="w-4 h-4 animate-spin" />
                ) : (
                  <Megaphone className="w-4 h-4" />
                )}
                {busy
                  ? (isExclude ? cp.actions.excluding : cp.actions.reEnabling)
                  : (isExclude ? cp.actions.exclude : cp.actions.reEnable)}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
