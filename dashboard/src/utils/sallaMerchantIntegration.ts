/**
 * Merchant-facing Salla integration copy and status helpers.
 * No tokens, store IDs, or internal field names in merchant UI.
 */

export const SALLA_MERCHANT_COPY = {
  storeTitle: 'سلة',
  fullApiTitle: 'ربط API الكامل',
  couponSyncTitle: 'مزامنة الكوبونات',
  storeConnected: 'متصل',
  storeNeedsReauth: 'يحتاج إعادة ربط',
  storeDisconnected: 'غير متصل',
  fullApiComplete: 'مكتمل',
  fullApiIncomplete: 'غير مكتمل',
  couponSyncReady: 'جاهزة',
  couponSyncNeedsApi: 'تحتاج ربط API الكامل',
  openedFromSalla: 'تم فتح تطبيق نحلة من سلة',
  completeLinkCta: 'إكمال ربط سلة',
  reconnectCta: 'إعادة ربط سلة',
  completeLinkHelper: 'مطلوب لمزامنة الكوبونات والطلبات والمنتجات مع سلة.',
  reauthMessage: 'انتهت صلاحية ربط سلة. أعد الربط لتفعيل المزامنة.',
  linkCompleteMessage: 'الربط مكتمل',
  couponSyncReadyMessage: 'مزامنة الكوبونات جاهزة.',
  couponSyncNeedsApiMessage: 'مزامنة الكوبونات مع سلة تتطلب إكمال ربط سلة.',
  testConnection: 'اختبار الاتصال',
  lastSyncLabel: 'آخر مزامنة',
  openFromSallaHint:
    'لإكمال الربط، افتح تطبيق نحلة من لوحة تطبيقات سلة (تطبيقاتي).',
} as const

export type SallaMerchantStatusTone = 'ok' | 'warn' | 'muted'

export interface SallaMerchantStatusLine {
  label: string
  tone: SallaMerchantStatusTone
  hint?: string
}

export interface SallaMerchantIntegrationView {
  store: SallaMerchantStatusLine
  fullApi: SallaMerchantStatusLine
  couponSync: SallaMerchantStatusLine
  showCompleteCta: boolean
  showReauthCta: boolean
  ctaLabel: string
  ctaHelper: string
  bannerMessage: string | null
  storeConnectedForSync: boolean
}

export interface SallaMerchantStatusInput {
  configured?: boolean
  enabled?: boolean
  needsReauth?: boolean
  apiSyncEnabled?: boolean
  embeddedConnected?: boolean
  storeName?: string
  lastSyncAt?: string | null
}

export function deriveSallaMerchantIntegrationView(
  input: SallaMerchantStatusInput,
): SallaMerchantIntegrationView {
  const configured = Boolean(input.configured && input.enabled)
  const needsReauth = Boolean(input.needsReauth)
  const apiComplete = Boolean(input.apiSyncEnabled) && !needsReauth
  const embeddedOk = input.embeddedConnected ?? configured

  const store: SallaMerchantStatusLine = needsReauth
    ? { label: SALLA_MERCHANT_COPY.storeNeedsReauth, tone: 'warn' }
    : embeddedOk || configured
      ? {
          label: SALLA_MERCHANT_COPY.storeConnected,
          tone: 'ok',
          hint: SALLA_MERCHANT_COPY.openedFromSalla,
        }
      : { label: SALLA_MERCHANT_COPY.storeDisconnected, tone: 'muted' }

  const fullApi: SallaMerchantStatusLine = needsReauth
    ? { label: SALLA_MERCHANT_COPY.storeNeedsReauth, tone: 'warn' }
    : apiComplete
      ? { label: SALLA_MERCHANT_COPY.fullApiComplete, tone: 'ok' }
      : { label: SALLA_MERCHANT_COPY.fullApiIncomplete, tone: 'warn' }

  const couponSync: SallaMerchantStatusLine = apiComplete
    ? { label: SALLA_MERCHANT_COPY.couponSyncReady, tone: 'ok' }
    : { label: SALLA_MERCHANT_COPY.couponSyncNeedsApi, tone: 'warn' }

  const showReauthCta = needsReauth
  const showCompleteCta = !apiComplete

  return {
    store,
    fullApi,
    couponSync,
    showCompleteCta,
    showReauthCta,
    ctaLabel: showReauthCta
      ? SALLA_MERCHANT_COPY.reconnectCta
      : SALLA_MERCHANT_COPY.completeLinkCta,
    ctaHelper: showReauthCta
      ? SALLA_MERCHANT_COPY.reauthMessage
      : SALLA_MERCHANT_COPY.completeLinkHelper,
    bannerMessage: apiComplete
      ? SALLA_MERCHANT_COPY.linkCompleteMessage
      : showReauthCta
        ? SALLA_MERCHANT_COPY.reauthMessage
        : null,
    storeConnectedForSync: configured && !needsReauth,
  }
}

/** Regression guard: merchant page must not expose credential forms. */
export const SALLA_MERCHANT_FORBIDDEN_UI_MARKERS = [
  'Webhook Secret',
  'مفتاح API',
  'Account Token',
  'Reveal token once',
  'partners.salla.sa',
  'refresh_token',
  'access token',
  'معرّف المتجر (Store ID)',
  'api_key_hint',
  'showApiKey',
] as const
