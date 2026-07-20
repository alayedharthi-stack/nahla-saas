import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { settingsApi, type StoreSettings } from '../api/settings'
import { getRole, getTenantId, isPlatformStaffRole } from '../auth'
import { useLanguage } from '../i18n/context'
import { resolveMerchantName } from '../lib/productIdentity'

interface MerchantIdentity {
  name: string
  logoUrl: string
  tenantId: number | null
  loading: boolean
}

const MerchantIdentityContext = createContext<MerchantIdentity | null>(null)

export function MerchantIdentityProvider({ children }: { children: ReactNode }) {
  const { lang } = useLanguage()
  const role = getRole()
  const tenantId = getTenantId()
  const merchantScoped = !isPlatformStaffRole(role)
  const [store, setStore] = useState<StoreSettings | null>(null)
  const [loading, setLoading] = useState(merchantScoped)

  useEffect(() => {
    let cancelled = false
    setStore(null)

    if (!merchantScoped) {
      setLoading(false)
      return () => { cancelled = true }
    }

    setLoading(true)
    settingsApi.getAll()
      .then(settings => {
        if (!cancelled) setStore(settings.store)
      })
      .catch(() => {
        if (!cancelled) setStore(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => { cancelled = true }
  }, [merchantScoped, tenantId])

  const identity = useMemo<MerchantIdentity>(() => ({
    name: resolveMerchantName(store, lang),
    logoUrl: store?.store_logo_url?.trim() ?? '',
    tenantId,
    loading,
  }), [lang, loading, store, tenantId])

  return (
    <MerchantIdentityContext.Provider value={identity}>
      {children}
    </MerchantIdentityContext.Provider>
  )
}

export function useMerchantIdentity(): MerchantIdentity {
  const identity = useContext(MerchantIdentityContext)
  if (!identity) {
    throw new Error('useMerchantIdentity must be used within MerchantIdentityProvider')
  }
  return identity
}
