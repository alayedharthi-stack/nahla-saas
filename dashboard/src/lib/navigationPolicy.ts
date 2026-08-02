export const NAVIGATION_PATHS = {
  structuredContacts: '/sales-channels/branches',
  /** Canonical merchant settings entry — modern SettingsHub (not legacy /settings). */
  merchantSettings: '/settings-hub',
  securitySettings: '/settings/security',
} as const

export const INTEGRATION_MANAGEMENT_PATHS = {
  store: '/store-integration',
  whatsapp: '/whatsapp-connect',
} as const

interface ProfileSettingsContext {
  platformOwner: boolean
  impersonating: boolean
}

/**
 * Platform owners can manage their own 2FA without entering merchant settings.
 * During impersonation, settings stay scoped to the merchant being supported.
 */
export function resolveProfileSettingsPath({
  platformOwner,
  impersonating,
}: ProfileSettingsContext): string {
  return platformOwner && !impersonating
    ? NAVIGATION_PATHS.securitySettings
    : NAVIGATION_PATHS.merchantSettings
}
