// ── 2FA API client ───────────────────────────────────────────────────────────
// Phase 2A Sprint 1 — wraps the four `/auth/2fa/*` endpoints exposed by
// `backend/routers/twofa.py`. The shape of each function intentionally
// mirrors the JSON contract on the server so type drift is loud.

import { apiCall } from './client'

export interface TwoFactorStatus {
  enabled:                   boolean
  enrolled_at:               string | null
  last_used_at:              string | null
  recovery_codes_remaining:  number
  /** Backend build marker — present on success and on the structured 500 body too. */
  build_marker?:             string
}

export interface TwoFactorSetupStart {
  /** Short-lived (10 min) JWT carrying the pending secret. */
  setup_token:  string
  /** Plain base32 secret — shown next to the QR for users who can't scan. */
  secret_b32:   string
  /** `otpauth://totp/...` URL ready to be rendered as a QR. */
  otpauth_url:  string
  issuer:       string
  account:      string
  expires_in:   number
}

export interface TwoFactorSetupConfirm {
  enabled:        true
  enrolled_at:    string
  /** 10 plaintext recovery codes — shown ONCE. */
  recovery_codes: string[]
  warning:        string
}

export interface TwoFactorDisableResult {
  enabled:     false
  disabled_at: number
}

export async function getTwoFactorStatus(): Promise<TwoFactorStatus> {
  return apiCall<TwoFactorStatus>('/auth/2fa/status', { method: 'GET' })
}

export async function startTwoFactorSetup(): Promise<TwoFactorSetupStart> {
  return apiCall<TwoFactorSetupStart>('/auth/2fa/setup/start', { method: 'POST' })
}

export async function confirmTwoFactorSetup(args: {
  setupToken: string
  otp:        string
}): Promise<TwoFactorSetupConfirm> {
  return apiCall<TwoFactorSetupConfirm>('/auth/2fa/setup/confirm', {
    method: 'POST',
    body: JSON.stringify({
      setup_token: args.setupToken,
      otp:         args.otp,
    }),
  })
}

export async function disableTwoFactor(args: {
  password: string
  otp:      string
}): Promise<TwoFactorDisableResult> {
  return apiCall<TwoFactorDisableResult>('/auth/2fa/disable', {
    method: 'POST',
    body: JSON.stringify(args),
  })
}
