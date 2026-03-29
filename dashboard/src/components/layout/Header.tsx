import { Bell, Search, ChevronDown } from 'lucide-react'
import { useLanguage } from '../../i18n/context'
import type { Lang } from '../../i18n/types'

interface HeaderProps {
  title:     string
  subtitle?: string
}

export default function Header({ title, subtitle }: HeaderProps) {
  const { lang, setLang, t } = useLanguage()

  return (
    <header className="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-6 sticky top-0 z-20">

      {/* Page title */}
      <div>
        <h1 className="text-base font-semibold text-slate-900 leading-none">{title}</h1>
        {subtitle && <p className="text-xs text-slate-400 mt-0.5">{subtitle}</p>}
      </div>

      {/* Right-side actions */}
      <div className="flex items-center gap-2">

        {/* Search */}
        <div className="relative hidden md:block">
          {/*
           * start-3 = inset-inline-start: 12px
           * Icon sits on the starting side (right in RTL, left in LTR).
           */}
          <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input
            type="text"
            placeholder={t(tr => tr.topbar.searchPlaceholder)}
            className="ps-9 pe-4 py-1.5 text-sm bg-slate-50 border border-slate-200 rounded-lg w-52
                       focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        </div>

        {/* Language toggle — AR / EN */}
        <div className="flex items-center rounded-lg border border-slate-200 overflow-hidden">
          {(['ar', 'en'] as Lang[]).map((code) => (
            <button
              key={code}
              onClick={() => setLang(code)}
              aria-label={code === 'ar' ? 'التبديل إلى العربية' : 'Switch to English'}
              className={`px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                lang === code
                  ? 'bg-brand-500 text-white'
                  : 'text-slate-500 hover:bg-slate-50'
              }`}
            >
              {code === 'ar' ? 'AR' : 'EN'}
            </button>
          ))}
        </div>

        {/* Notifications */}
        <button
          className="relative w-9 h-9 flex items-center justify-center rounded-lg hover:bg-slate-50 text-slate-500 transition-colors"
          aria-label={t(tr => tr.topbar.notifications)}
        >
          <Bell className="w-4 h-4" />
          <span className="absolute top-1.5 end-1.5 w-2 h-2 bg-red-500 rounded-full ring-2 ring-white" />
        </button>

        {/* Profile */}
        <button className="flex items-center gap-2 ps-2 pe-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors">
          <div className="w-7 h-7 bg-brand-500 rounded-full flex items-center justify-center">
            <span className="text-white text-xs font-semibold">م</span>
          </div>
          <span className="text-sm font-medium text-slate-700 hidden md:block">
            {t(tr => tr.topbar.admin)}
          </span>
          <ChevronDown className="w-3.5 h-3.5 text-slate-400 hidden md:block" />
        </button>

      </div>
    </header>
  )
}
