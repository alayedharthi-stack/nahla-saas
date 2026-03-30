/**
 * Language context for Nahla SaaS dashboard.
 *
 * Arabic is the default language. The selected language is persisted in
 * localStorage so it survives page refreshes.
 *
 * Usage:
 *   const { lang, dir, isRTL, setLang, t } = useLanguage()
 *
 *   // Type-safe translation access — full autocomplete in IDEs:
 *   t(tr => tr.nav.items.overview)          // → 'نظرة عامة' | 'Overview'
 *   t(tr => tr.actions.newCoupon)           // → 'كوبون جديد' | 'New Coupon'
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import type { Lang, Translations } from './types'
import ar from './ar'
import en from './en'

// ── Translation map ───────────────────────────────────────────────────────────

const TRANSLATIONS: Record<Lang, Translations> = { ar, en }

const STORAGE_KEY  = 'nahla-lang'
const DEFAULT_LANG: Lang = 'ar'

// ── Context shape ─────────────────────────────────────────────────────────────

interface LangContextValue {
  lang:    Lang
  dir:     'rtl' | 'ltr'
  isRTL:   boolean
  setLang: (lang: Lang) => void
  /** Type-safe accessor — accepts a selector function over the translation tree. */
  t: <T>(selector: (tr: Translations) => T) => T
}

const LangContext = createContext<LangContextValue | null>(null)

// ── Provider ──────────────────────────────────────────────────────────────────

function getInitialLang(): Lang {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'ar' || stored === 'en') return stored
  } catch {
    // localStorage unavailable (SSR / privacy mode)
  }
  return DEFAULT_LANG
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(getInitialLang)

  const tr   = TRANSLATIONS[lang]
  const dir  = tr.meta.dir
  const isRTL = dir === 'rtl'

  // Sync <html> attributes on every language change
  useEffect(() => {
    const root = document.documentElement
    root.lang = lang
    root.dir  = dir
  }, [lang, dir])

  const setLang = (next: Lang) => {
    setLangState(next)
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
  }

  const t = <T,>(selector: (tr: Translations) => T) => selector(tr)

  return (
    <LangContext.Provider value={{ lang, dir, isRTL, setLang, t }}>
      {children}
    </LangContext.Provider>
  )
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useLanguage(): LangContextValue {
  const ctx = useContext(LangContext)
  if (!ctx) throw new Error('useLanguage must be used inside <LanguageProvider>')
  return ctx
}
