import { useEffect, useRef } from 'react'

/**
 * Nahla unified dashboard polling:
 * — One in-flight HTTP per ``pollKey`` (cross-route / cross-component)
 * — No ticks while the browser tab is hidden (when ``visibleOnly``)
 * — Exponential backoff on transport / server errors after a failed tick
 * — AbortSignal passed to ``run``; subscriber cleanup aborts the active flight
 *
 * Prefer stable ``pollKey`` strings like ``GET:/path`` for endpoints that must
 * not overlap with concurrent duplicate calls elsewhere.
 */

const flightMap = new Map<string, AbortController>()

/** Test hook — clears coordinators. */
export function __resetDashboardPollingForTests(): void {
  flightMap.clear()
}

export function dashboardPollAcquire(pollKey: string): AbortController | null {
  if (flightMap.has(pollKey)) return null
  const c = new AbortController()
  flightMap.set(pollKey, c)
  return c
}

export function dashboardPollRelease(pollKey: string, c: AbortController): void {
  if (flightMap.get(pollKey) === c) flightMap.delete(pollKey)
}

export type UseDashboardPollArgs = {
  pollKey: string
  intervalMs: number
  enabled?: boolean
  visibleOnly?: boolean
  /** Fire immediately when toggled enabled (still respects ``visibleOnly``). */
  leading?: boolean
  backoffBaseMs?: number
  backoffMaxMs?: number
  run: (signal: AbortSignal) => Promise<void>
}

export function useDashboardPoll(opts: UseDashboardPollArgs): void {
  const {
    pollKey,
    intervalMs,
    enabled = true,
    visibleOnly = true,
    leading = false,
    backoffBaseMs = 2_000,
    backoffMaxMs = 60_000,
    run,
  } = opts

  const runRef = useRef(run)
  runRef.current = run
  const failures = useRef(0)
  const nextAllowed = useRef(0)

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return

    let stopped = false
    let timer: ReturnType<typeof setInterval> | undefined

    /** Active controller handed to ``run`` for this hook instance — abort on unmount. */
    let localFlight: AbortController | null = null

    const tick = async (): Promise<void> => {
      if (stopped) return
      if (
        visibleOnly &&
        typeof document !== 'undefined' &&
        document.visibilityState !== 'visible'
      ) {
        return
      }
      if (Date.now() < nextAllowed.current) return

      const ac = dashboardPollAcquire(pollKey)
      if (!ac) return

      localFlight = ac
      try {
        await runRef.current(ac.signal)
        failures.current = 0
        nextAllowed.current = 0
      } catch (e: unknown) {
        const aborted =
          ac.signal.aborted ||
          (e instanceof DOMException && e.name === 'AbortError') ||
          (e instanceof Error && /abort|timed out|signal/i.test(e.message))
        if (aborted) {
          /* intentional abort — do not advance backoff */
        } else {
          failures.current += 1
          const delay = Math.min(
            backoffMaxMs,
            backoffBaseMs * 2 ** Math.min(failures.current, 8),
          )
          nextAllowed.current = Date.now() + delay
        }
      } finally {
        dashboardPollRelease(pollKey, ac)
        if (localFlight === ac) localFlight = null
      }
    }

    const onVis = (): void => {
      if (!visibleOnly) return
      if (document.visibilityState === 'visible') void tick()
    }

    if (leading) void tick()

    timer = window.setInterval(() => void tick(), intervalMs)
    if (visibleOnly) document.addEventListener('visibilitychange', onVis)

    return () => {
      stopped = true
      if (visibleOnly) document.removeEventListener('visibilitychange', onVis)
      if (timer !== undefined) clearInterval(timer)
      localFlight?.abort()
    }
  }, [
    pollKey,
    intervalMs,
    enabled,
    visibleOnly,
    leading,
    backoffBaseMs,
    backoffMaxMs,
  ])
}
