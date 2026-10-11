/**
 * Completion of a Shopify authorization on ``/integrations/shopify/complete``.
 *
 * One controller per page load (module singleton in the page). It is
 * framework-free so the state machine is checked in CI without React:
 *
 *  * ``attach`` / ``detach`` are StrictMode-safe: the double mount re-attaches
 *    in the same tick (the deferred check finds it attached again) and the
 *    one-shot capture is taken exactly once. A genuine leave (still detached
 *    when the deferred check runs) aborts any request, drops the handle and
 *    the capture, and resets: coming back never resubmits.
 *  * One ``POST /complete`` at a time; repeated clicks and re-mounts join it.
 *  * The handle stays in memory only while a retry is meaningful
 *    (``exchange_in_progress``, which the backend leaves usable) and an armed
 *    timer drops it at its TTL even if nothing else happens; it is also
 *    dropped on success, refusal, cancel, session change and leave.
 *  * A result is committed only if the session (tenant, user, JWT id) is the
 *    one that sent the request; otherwise it is discarded.
 *  * A ``#shopify_connection=connected`` fragment is never trusted: only the
 *    authenticated POST response or a fresh status read can show a connection.
 *  * An unknown outcome (network, timeout, 5xx) is never retried with the
 *    handle: the merchant checks the status instead.
 */
import { COMPLETION_TTL_MS, type TakenReturn } from './returnCapture'
import {
  type MessageKey,
  type ShopifyConnectionSummary,
  type ShopifyStatus,
  messageKeyForCode,
  messageKeyForFailure,
  normalizeStatus,
  parseCompleteResponse,
  projectApiFailure,
} from './model'

export type CompletionPhase =
  | { phase: 'idle' }
  | { phase: 'feature_off' }
  | { phase: 'nothing' }
  | { phase: 'invalid' }
  | { phase: 'expired' }
  | { phase: 'completing' }
  | { phase: 'connected'; connection: ShopifyConnectionSummary }
  | { phase: 'unconfirmed' }
  | { phase: 'refused'; message: MessageKey; retryable: boolean }
  | { phase: 'uncertain' }
  | { phase: 'session_changed' }
  | { phase: 'cancelled' }

export type StatusCheck =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'done'; status: ShopifyStatus }
  | { kind: 'failed' }

export interface CompletionState {
  view: CompletionPhase
  check: StatusCheck
}

export interface CompletionDeps {
  enabled(): boolean
  sessionKey(): string
  now(): number
  take(): TakenReturn
  discard(): void
  complete(handle: string, signal: AbortSignal): Promise<unknown>
  status(signal: AbortSignal): Promise<unknown>
  /** ``setTimeout``-like; returns a cancel function. */
  schedule(fn: () => void, ms: number): () => void
}

export interface CompletionController {
  getSnapshot(): CompletionState
  subscribe(listener: () => void): () => void
  attach(): void
  detach(): void
  retry(): void
  cancel(): void
  checkStatus(): void
  onSessionMaybeChanged(): void
  /** Test/diagnostic: whether a completion handle is still held in memory. */
  holdsHandle(): boolean
}

const CHECK_IDLE: StatusCheck = { kind: 'idle' }

export function createCompletionController(deps: CompletionDeps): CompletionController {
  let state: CompletionState = { view: { phase: 'idle' }, check: CHECK_IDLE }
  let started = false
  let attached = 0
  let handle: string | null = null
  let handleExpiresAt = 0
  let cancelExpiry: (() => void) | null = null
  let boundSession = ''
  let inflight: AbortController | null = null
  let statusFlight: AbortController | null = null
  const listeners = new Set<() => void>()

  function emit(next: CompletionState): void {
    state = next
    listeners.forEach((l) => l())
  }
  function setView(view: CompletionPhase): void {
    emit({ ...state, view })
  }
  function dropHandle(): void {
    handle = null
    handleExpiresAt = 0
    cancelExpiry?.()
    cancelExpiry = null
  }
  /** Drop the handle from memory at its TTL, whatever else happens. */
  function armExpiry(): void {
    cancelExpiry?.()
    cancelExpiry = deps.schedule(() => {
      cancelExpiry = null
      if (handle === null) return
      dropHandle()
      // An in-flight request decides its own outcome; a waiting retry expires.
      if (!inflight && state.view.phase === 'refused' && state.view.retryable) setView({ phase: 'expired' })
    }, Math.max(0, handleExpiresAt - deps.now()))
  }
  function abortAll(): void {
    inflight?.abort()
    inflight = null
    statusFlight?.abort()
    statusFlight = null
  }

  function begin(): void {
    if (started) return
    started = true
    boundSession = deps.sessionKey()
    if (!deps.enabled()) {
      deps.discard()
      setView({ phase: 'feature_off' })
      return
    }
    const taken = deps.take()
    switch (taken.kind) {
      case 'none':
        setView({ phase: 'nothing' })
        return
      case 'invalid':
        setView({ phase: 'invalid' })
        return
      case 'expired':
        setView({ phase: 'expired' })
        return
      case 'result':
        if (taken.code === 'connected') {
          // A fragment is a hint only; prove it with a fresh status read.
          setView({ phase: 'unconfirmed' })
          checkStatus()
        } else {
          setView({ phase: 'refused', message: messageKeyForCode(taken.code), retryable: false })
        }
        return
      case 'handle':
        if (!boundSession) {
          setView({ phase: 'session_changed' })
          return
        }
        handle = taken.handle
        handleExpiresAt = taken.capturedAt + COMPLETION_TTL_MS
        armExpiry()
        void submit()
    }
  }

  async function submit(): Promise<void> {
    if (inflight || handle === null) return
    if (deps.sessionKey() !== boundSession) {
      dropHandle()
      setView({ phase: 'session_changed' })
      return
    }
    if (deps.now() >= handleExpiresAt) {
      dropHandle()
      setView({ phase: 'expired' })
      return
    }
    const flight = new AbortController()
    inflight = flight
    const sent = handle
    setView({ phase: 'completing' })
    let ok = false
    let body: unknown = null
    let error: unknown = null
    try {
      body = await deps.complete(sent, flight.signal)
      ok = true
    } catch (err) {
      error = err
    }
    if (inflight !== flight) return // cancelled, reset or superseded meanwhile
    inflight = null
    if (deps.sessionKey() !== boundSession) {
      dropHandle()
      deps.discard()
      setView({ phase: 'session_changed' })
      return
    }
    if (ok) {
      dropHandle()
      const connection = parseCompleteResponse(body)
      setView(connection ? { phase: 'connected', connection } : { phase: 'uncertain' })
      return
    }
    const failure = projectApiFailure(error)
    if (failure.kind === 'refused' && failure.code === 'exchange_in_progress' && deps.now() < handleExpiresAt) {
      // The backend left the completion usable; an explicit retry may succeed.
      setView({ phase: 'refused', message: 'inProgress', retryable: true })
      return
    }
    dropHandle()
    if (failure.kind === 'uncertain') setView({ phase: 'uncertain' })
    else if (failure.kind === 'feature_off') setView({ phase: 'feature_off' })
    else if (failure.kind === 'session') setView({ phase: 'session_changed' })
    else setView({ phase: 'refused', message: messageKeyForFailure(failure), retryable: false })
  }

  function checkStatus(): void {
    if (statusFlight || !deps.enabled()) return
    const session = deps.sessionKey()
    if (!session || session !== boundSession) {
      onSessionMaybeChanged()
      return
    }
    const flight = new AbortController()
    statusFlight = flight
    emit({ ...state, check: { kind: 'loading' } })
    deps.status(flight.signal).then(
      (raw) => {
        if (statusFlight !== flight) return
        statusFlight = null
        if (deps.sessionKey() !== session) return onSessionMaybeChanged()
        const status = normalizeStatus(raw)
        emit({ ...state, check: status ? { kind: 'done', status } : { kind: 'failed' } })
      },
      () => {
        if (statusFlight !== flight) return
        statusFlight = null
        if (deps.sessionKey() !== session) return onSessionMaybeChanged()
        emit({ ...state, check: { kind: 'failed' } })
      },
    )
  }

  /**
   * Genuine leave (not the StrictMode re-mount): abort, forget and reset.
   * An aborted completion has an unknown outcome; it is never resubmitted —
   * a later visit finds nothing to complete and offers the status check.
   */
  function release(): void {
    if (attached > 0) return
    dropHandle()
    deps.discard()
    abortAll()
    started = false
    boundSession = ''
    emit({ view: { phase: 'idle' }, check: CHECK_IDLE })
  }

  function onSessionMaybeChanged(): void {
    if (!started) return
    if (deps.sessionKey() === boundSession) return
    const hadWork = handle !== null || inflight !== null || state.view.phase !== 'idle'
    dropHandle()
    deps.discard()
    abortAll()
    if (hadWork) emit({ view: { phase: 'session_changed' }, check: CHECK_IDLE })
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    attach() {
      attached += 1
      begin()
    },
    detach() {
      attached = Math.max(0, attached - 1)
      // StrictMode re-attaches synchronously; a genuine leave is still detached here.
      deps.schedule(() => {
        if (attached === 0) release()
      }, 0)
    },
    retry() {
      if (state.view.phase === 'refused' && state.view.retryable) void submit()
    },
    cancel() {
      dropHandle()
      deps.discard()
      abortAll()
      emit({ view: { phase: 'cancelled' }, check: CHECK_IDLE })
    },
    checkStatus,
    onSessionMaybeChanged,
    holdsHandle: () => handle !== null,
  }
}
