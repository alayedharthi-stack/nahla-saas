/** FB.login options for Meta WhatsApp Embedded Signup (System User / code flow). */
export function buildEmbeddedSignupFbLoginOptions(configId: string) {
  return {
    config_id: configId,
    response_type: 'code' as const,
    // Force code flow — without this the JS SDK defaults to response_type=token,
    // which System User Token Embedded Signup configs reject.
    override_default_response_type: true,
    extras: {
      setup: {},
      feature: 'whatsapp_embedded_signup',
      sessionInfoVersion: '3',
    },
  }
}

/** FB.login options for WhatsApp Business App coexistence onboarding. */
export function buildCoexistenceEmbeddedSignupFbLoginOptions(configId: string) {
  return {
    config_id: configId,
    response_type: 'code' as const,
    override_default_response_type: true,
    extras: {
      setup: {},
      featureType: 'whatsapp_business_app_onboarding',
      sessionInfoVersion: '3',
    },
  }
}

export const EMBEDDED_SIGNUP_ALLOWED_ORIGINS = [
  'https://www.facebook.com',
  'https://web.facebook.com',
] as const

export type EmbeddedSignupSessionEvent =
  | 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING'
  | 'FINISH_OBO_MIGRATION'
  | 'CANCEL'
  | 'ERROR'
  | string

export interface ParsedEmbeddedSignupMessage {
  event?: EmbeddedSignupSessionEvent
  waba_id?: string
  phone_number_id?: string
  current_step?: string
  error_message?: string
}

const UNSAFE_ERROR_PATTERNS = [
  /\bmigrat/i,
  /\bdelete\b/i,
  /\bremove\b/i,
  /\bdisconnect\b/i,
  /حذف/,
  /ترحيل/,
  /فصل/,
  /إزالة/,
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asNonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

/** Parse WA_EMBEDDED_SIGNUP postMessage payload; returns null for non-JSON or invalid shapes. */
export function parseEmbeddedSignupWindowMessage(data: unknown): ParsedEmbeddedSignupMessage | null {
  let raw: unknown = data
  if (typeof data === 'string') {
    const trimmed = data.trim()
    if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null
    try {
      raw = JSON.parse(trimmed)
    } catch {
      return null
    }
  }
  if (!isRecord(raw) || raw.type !== 'WA_EMBEDDED_SIGNUP') return null

  // Official Meta payload nests asset IDs under `data`.
  const nested = isRecord(raw.data) ? raw.data : {}
  const event = asNonEmptyString(raw.event)
  const waba_id = asNonEmptyString(nested.waba_id) || asNonEmptyString(raw.waba_id)
  const phone_number_id = asNonEmptyString(nested.phone_number_id) || asNonEmptyString(raw.phone_number_id)
  const current_step = asNonEmptyString(nested.current_step) || asNonEmptyString(raw.current_step)
  const error_message = asNonEmptyString(nested.error_message) || asNonEmptyString(raw.error_message)

  if (!event && !waba_id && !phone_number_id && !current_step && !error_message) {
    return null
  }

  return { event, waba_id, phone_number_id, current_step, error_message }
}

export function isCoexistenceFinishEvent(event: string | undefined): boolean {
  return event === 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING'
}

export function isMigrationOrUnsafeEvent(
  event: string | undefined,
  errorMessage?: string,
): boolean {
  if (event === 'FINISH_OBO_MIGRATION') return true
  const haystack = (errorMessage || '').trim()
  if (!haystack) return false
  return UNSAFE_ERROR_PATTERNS.some(pattern => pattern.test(haystack))
}

export function subscribeEmbeddedSignupSessionListener(
  handler: (message: ParsedEmbeddedSignupMessage) => void,
): () => void {
  const listener = (event: MessageEvent) => {
    if (!EMBEDDED_SIGNUP_ALLOWED_ORIGINS.includes(event.origin as typeof EMBEDDED_SIGNUP_ALLOWED_ORIGINS[number])) {
      return
    }
    const parsed = parseEmbeddedSignupWindowMessage(event.data)
    if (parsed) handler(parsed)
  }
  window.addEventListener('message', listener)
  return () => window.removeEventListener('message', listener)
}
