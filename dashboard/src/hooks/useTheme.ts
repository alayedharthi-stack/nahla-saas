/**
 * useTheme — Centralized light/dark/system theme for Nahla dashboard.
 * ────────────────────────────────────────────────────────────────────
 * Persisted to localStorage (`nahla-theme`).  When mode === 'system'
 * we mirror `prefers-color-scheme` live.
 *
 * Side effects (run once per render of the hook):
 *   • Toggles the `dark` class on <html>           ← Tailwind `darkMode: 'class'`
 *   • Sets `data-theme="light|dark"` on <html>     ← inline-style consumers
 *   • Sets `color-scheme` on <html>                ← native form controls
 *
 * To avoid FOUC, an inline pre-React snippet in `index.html` (or `main.tsx`)
 * should apply the same logic synchronously before React mounts.  See
 * `applyThemeEarly()` below for the shared implementation.
 */
import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'nahla-theme'
const DEFAULT_MODE: ThemeMode = 'system'

// ── Cross-tab + same-tab change broadcast ───────────────────────────────────
// We use a custom event because the native `storage` event only fires across
// different tabs — useTheme instances inside the same tab need to stay in sync.
const CHANGE_EVENT = 'nahla:theme-change'

function readStoredMode(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch { /* localStorage blocked */ }
  return DEFAULT_MODE
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch { return false }
}

export function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return mode
}

/**
 * Apply the theme to <html> immediately.  Safe to call anywhere, including
 * synchronously before React renders to prevent flash-of-wrong-theme.
 */
export function applyThemeEarly(): void {
  try {
    const mode     = readStoredMode()
    const resolved = resolveTheme(mode)
    const root     = document.documentElement
    root.classList.toggle('dark', resolved === 'dark')
    root.setAttribute('data-theme', resolved)
    root.style.colorScheme = resolved
  } catch { /* DOM not ready, will retry from React */ }
}

interface UseThemeReturn {
  /** User-selected mode (may be 'system'). */
  mode: ThemeMode
  /** Resolved theme — always 'light' or 'dark'. */
  theme: 'light' | 'dark'
  isDark: boolean
  setMode: (mode: ThemeMode) => void
  toggle: () => void
}

export function useTheme(): UseThemeReturn {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode)
  const [theme, setTheme]    = useState<'light' | 'dark'>(() => resolveTheme(readStoredMode()))

  // Apply to DOM whenever mode (or system preference for `system` mode) changes
  useEffect(() => {
    const resolved = resolveTheme(mode)
    setTheme(resolved)
    const root = document.documentElement
    root.classList.toggle('dark', resolved === 'dark')
    root.setAttribute('data-theme', resolved)
    root.style.colorScheme = resolved
  }, [mode])

  // Track OS-level changes only while in 'system' mode
  useEffect(() => {
    if (mode !== 'system') return
    let mql: MediaQueryList
    try { mql = window.matchMedia('(prefers-color-scheme: dark)') }
    catch { return }
    const onChange = () => {
      const resolved = mql.matches ? 'dark' : 'light'
      setTheme(resolved)
      const root = document.documentElement
      root.classList.toggle('dark', resolved === 'dark')
      root.setAttribute('data-theme', resolved)
      root.style.colorScheme = resolved
    }
    mql.addEventListener?.('change', onChange)
    return () => mql.removeEventListener?.('change', onChange)
  }, [mode])

  // Stay in sync across hook instances (Header toggle + Embedded screen, etc.)
  useEffect(() => {
    const onChange = (e: Event) => {
      const next = (e as CustomEvent<ThemeMode>).detail
      if (next === 'light' || next === 'dark' || next === 'system') {
        setModeState(next)
      } else {
        setModeState(readStoredMode())
      }
    }
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setModeState(readStoredMode())
    }
    window.addEventListener(CHANGE_EVENT, onChange)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(CHANGE_EVENT, onChange)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
    try { window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: next })) } catch { /* ignore */ }
  }, [])

  const toggle = useCallback(() => {
    setMode(theme === 'dark' ? 'light' : 'dark')
  }, [setMode, theme])

  return { mode, theme, isDark: theme === 'dark', setMode, toggle }
}
