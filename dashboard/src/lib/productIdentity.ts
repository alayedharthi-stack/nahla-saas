export type IdentityLanguage = 'ar' | 'en'

export interface MerchantIdentityFields {
  store_name?: string
  store_name_ar?: string
  store_name_en?: string
  store_logo_url?: string
}

export const PLATFORM_BRAND = {
  logoUrl: '/logo-nahla.png',
  name: {
    ar: 'نحلة',
    en: 'Nahlah',
  },
  accessibleName: {
    ar: 'نحلة AI',
    en: 'Nahlah AI',
  },
} as const

export function resolveMerchantName(
  store: MerchantIdentityFields | null,
  lang: IdentityLanguage,
): string {
  const primary = lang === 'ar' ? store?.store_name_ar : store?.store_name_en
  const secondary = lang === 'ar' ? store?.store_name_en : store?.store_name_ar
  const values = [primary, secondary, store?.store_name]

  for (const value of values) {
    const normalized = value?.trim()
    if (normalized) return normalized
  }

  return lang === 'ar' ? 'المتجر' : 'Store'
}
