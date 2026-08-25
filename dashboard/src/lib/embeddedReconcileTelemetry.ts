/**
 * Server-observable telemetry for the Salla embedded reconcile CTA only.
 * Payloads are flat, path-only, and must never include tokens or query values.
 */
import { getApiBase } from '../auth'

export type SallaReconcileTelemetryEvent =
  | 'SALLA_RECONCILE_CTA_CLICK'
  | 'SALLA_EMBEDDED_SDK_STATE'
  | 'SALLA_RECONCILE_NAV_ATTEMPT'

export interface SallaReconcileTelemetryPayload {
  event: SallaReconcileTelemetryEvent
  correlation_id: string
  sdk_loaded?: boolean
  sdk_initialized?: boolean
  attempted_method?: string
  fallback_stage?: string
  destination_path?: string
  ts?: number
}

export function createReconcileCorrelationId(): string {
  return `src_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

export function extractDestinationPath(url: string): string {
  try {
    return new URL(url).pathname
  } catch {
    return '(invalid)'
  }
}

function trySendBeaconFallback(endpoint: string, serialized: string): void {
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      const blob = new Blob([serialized], { type: 'application/json' })
      // Best-effort only — return value is not proof the server received telemetry.
      navigator.sendBeacon(endpoint, blob)
    }
  } catch {
    /* best effort only */
  }
}

export function emitSallaReconcileTelemetry(payload: SallaReconcileTelemetryPayload): void {
  const body: SallaReconcileTelemetryPayload = {
    ...payload,
    ts: payload.ts ?? Date.now(),
  }
  // eslint-disable-next-line no-console
  console.info('[SallaReconcileTelemetry]', body)
  const endpoint = `${getApiBase()}/api/salla/embedded/reconcile-telemetry`
  const serialized = JSON.stringify(body)
  try {
    void fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: serialized,
      keepalive: true,
      credentials: 'omit',
    }).catch(() => {
      trySendBeaconFallback(endpoint, serialized)
    })
  } catch {
    trySendBeaconFallback(endpoint, serialized)
  }
}
