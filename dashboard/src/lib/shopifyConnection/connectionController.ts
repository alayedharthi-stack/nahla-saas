/**
 * Shopify connection card state (status, start, local disconnect, reconnect).
 *
 * Framework-free so the races are checked in CI without React:
 *
 *  * Status reads are sequenced: a slower, older read never overwrites a
 *    newer one, and a disconnect invalidates any read that started before it.
 *  * Start is single-flight (repeated clicks are ignored while starting or
 *    redirecting). The authorize URL is validated against the requested shop
 *    and handed straight to ``navigate``; it is never kept in state.
 *  * Back from Shopify (bfcache ``pageshow``) re-enables the form and re-reads
 *    the status instead of leaving a stuck "redirecting" button.
 *  * Every late result is dropped when the session (tenant, user, JWT id)
 *    changed since the request was sent.
 *  * Support impersonation is read-only (the backend refuses those mutations).
 */
import {
  type MessageKey,
  type ShopifyConnectionSummary,
  type ShopifyStatus,
  canDisconnect,
  canonicalShopDomain,
  isSafeAuthorizeUrl,
  messageKeyForFailure,
  normalizeShopInput,
  normalizeStatus,
  parseDisconnectResponse,
  parseStartResponse,
  projectApiFailure,
} from './model'

export type LoadState = 'idle' | 'loading' | 'ready' | 'unavailable' | 'hidden' | 'failed'

export type StartState =
  | { phase: 'idle' }
  | { phase: 'starting'; shop: string }
  | { phase: 'redirecting'; shop: string }
  | { phase: 'error'; message: MessageKey }

export type DisconnectState =
  | { phase: 'idle' }
  | { phase: 'working'; shop: string }
  | { phase: 'done'; shop: string }
  | { phase: 'error'; shop: string; message: MessageKey }

export interface ConnectionState {
  load: LoadState
  status: ShopifyStatus | null
  readOnly: boolean
  start: StartState
  disconnect: DisconnectState
}

export interface ConnectionDeps {
  enabled(): boolean
  readOnly(): boolean
  sessionKey(): string
  status(signal: AbortSignal): Promise<unknown>
  start(shop: string, signal: AbortSignal): Promise<unknown>
  disconnect(shop: string, signal: AbortSignal): Promise<unknown>
  navigate(url: string): void
}

export interface ConnectionController {
  getSnapshot(): ConnectionState
  subscribe(listener: () => void): () => void
  refresh(): void
  start(rawShop: string): void
  disconnect(shop: string): void
  clearMessages(): void
  onPageShow(persisted: boolean): void
  onSessionMaybeChanged(): void
  dispose(): void
}

export function createConnectionController(deps: ConnectionDeps): ConnectionController {
  let state: ConnectionState = {
    load: 'idle',
    status: null,
    readOnly: deps.readOnly(),
    start: { phase: 'idle' },
    disconnect: { phase: 'idle' },
  }
  let session = deps.sessionKey()
  let readSeq = 0
  let readFlight: AbortController | null = null
  let startFlight: AbortController | null = null
  let disconnectFlight: AbortController | null = null
  const listeners = new Set<() => void>()

  function emit(patch: Partial<ConnectionState>): void {
    state = { ...state, ...patch }
    listeners.forEach((l) => l())
  }

  function sameSession(sentWith: string): boolean {
    return deps.sessionKey() === sentWith && sentWith === session
  }

  function refresh(): void {
    if (!deps.enabled()) {
      emit({ load: 'hidden' })
      return
    }
    readFlight?.abort()
    const flight = new AbortController()
    readFlight = flight
    const seq = ++readSeq
    const sentWith = deps.sessionKey()
    if (state.load !== 'ready' && state.load !== 'unavailable') emit({ load: 'loading' })
    deps.status(flight.signal).then(
      (raw) => {
        if (seq !== readSeq || !sameSession(sentWith)) return
        readFlight = null
        const status = normalizeStatus(raw)
        if (!status) {
          emit({ load: 'failed' })
          return
        }
        emit({ status, load: status.available ? 'ready' : 'unavailable' })
      },
      (err) => {
        if (seq !== readSeq || !sameSession(sentWith)) return
        readFlight = null
        const failure = projectApiFailure(err)
        if (failure.kind === 'feature_off') emit({ load: 'hidden', status: null })
        else if (failure.kind === 'refused' && failure.code === 'platform_session_refused') emit({ load: 'hidden', status: null })
        else emit({ load: 'failed' })
      },
    )
  }

  function start(rawShop: string): void {
    if (state.start.phase === 'starting' || state.start.phase === 'redirecting') return
    if (state.readOnly || deps.readOnly()) {
      emit({ start: { phase: 'error', message: 'merchantOnly' } })
      return
    }
    const shop = normalizeShopInput(rawShop)
    if (!shop) {
      emit({ start: { phase: 'error', message: 'shopInvalid' } })
      return
    }
    const flight = new AbortController()
    startFlight = flight
    const sentWith = deps.sessionKey()
    emit({ start: { phase: 'starting', shop }, disconnect: { phase: 'idle' } })
    deps.start(shop, flight.signal).then(
      (raw) => {
        if (startFlight !== flight) return
        startFlight = null
        if (!sameSession(sentWith)) return onSessionMaybeChanged()
        const url = parseStartResponse(raw)
        if (!url || !isSafeAuthorizeUrl(url, shop)) {
          emit({ start: { phase: 'error', message: 'unexpectedResponse' } })
          return
        }
        emit({ start: { phase: 'redirecting', shop } })
        deps.navigate(url)
      },
      (err) => {
        if (startFlight !== flight) return
        startFlight = null
        if (!sameSession(sentWith)) return onSessionMaybeChanged()
        const failure = projectApiFailure(err)
        if (failure.kind === 'feature_off') {
          emit({ load: 'hidden', status: null, start: { phase: 'idle' } })
          return
        }
        emit({ start: { phase: 'error', message: messageKeyForFailure(failure) } })
        // A pending authorization or an ownership refusal may have changed what the status shows.
        if (failure.kind === 'uncertain' || failure.kind === 'refused') refresh()
      },
    )
  }

  function applySummary(summary: ShopifyConnectionSummary): void {
    if (!state.status) return
    const connections = state.status.connections.map((c) => (c.shopDomain === summary.shopDomain ? summary : c))
    emit({ status: { ...state.status, connections } })
  }

  function disconnect(rawShop: string): void {
    const shop = canonicalShopDomain(rawShop)
    if (!shop || shop !== rawShop) return
    if (state.disconnect.phase === 'working') return
    if (state.readOnly || deps.readOnly()) {
      emit({ disconnect: { phase: 'error', shop, message: 'merchantOnly' } })
      return
    }
    const target = state.status?.connections.find((c) => c.shopDomain === shop)
    if (!target || !canDisconnect(target)) return
    // Any status read already in flight predates the disconnect: drop it.
    readSeq += 1
    readFlight?.abort()
    readFlight = null
    const flight = new AbortController()
    disconnectFlight = flight
    const sentWith = deps.sessionKey()
    emit({ disconnect: { phase: 'working', shop }, start: { phase: 'idle' } })
    deps.disconnect(shop, flight.signal).then(
      (raw) => {
        if (disconnectFlight !== flight) return
        disconnectFlight = null
        if (!sameSession(sentWith)) return onSessionMaybeChanged()
        const summary = parseDisconnectResponse(raw)
        if (summary && summary.shopDomain === shop) applySummary(summary)
        emit({ disconnect: { phase: 'done', shop } })
        refresh()
      },
      (err) => {
        if (disconnectFlight !== flight) return
        disconnectFlight = null
        if (!sameSession(sentWith)) return onSessionMaybeChanged()
        const failure = projectApiFailure(err)
        if (failure.kind === 'feature_off') {
          // Either the flag went off or the connection is already gone; the status read decides.
          emit({ disconnect: { phase: 'idle' } })
        } else {
          emit({ disconnect: { phase: 'error', shop, message: messageKeyForFailure(failure) } })
        }
        refresh()
      },
    )
  }

  function abortAll(): void {
    readSeq += 1
    readFlight?.abort()
    startFlight?.abort()
    disconnectFlight?.abort()
    readFlight = null
    startFlight = null
    disconnectFlight = null
  }

  function onSessionMaybeChanged(): void {
    const current = deps.sessionKey()
    if (current === session) return
    session = current
    abortAll()
    emit({
      load: 'idle',
      status: null,
      readOnly: deps.readOnly(),
      start: { phase: 'idle' },
      disconnect: { phase: 'idle' },
    })
    if (current) refresh()
    else emit({ load: 'hidden' })
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    refresh,
    start,
    disconnect,
    clearMessages() {
      const patch: Partial<ConnectionState> = {}
      if (state.start.phase === 'error') patch.start = { phase: 'idle' }
      if (state.disconnect.phase === 'error' || state.disconnect.phase === 'done') patch.disconnect = { phase: 'idle' }
      if (Object.keys(patch).length) emit(patch)
    },
    onPageShow(persisted) {
      if (!persisted) return
      startFlight?.abort()
      startFlight = null
      emit({ start: { phase: 'idle' } })
      onSessionMaybeChanged()
      refresh()
    },
    onSessionMaybeChanged,
    dispose: abortAll,
  }
}
