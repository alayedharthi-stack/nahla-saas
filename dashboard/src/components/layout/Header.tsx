import { useState, useRef, useEffect } from 'react'
import { Bell, Search, ChevronDown, Menu, LogOut, User, Shield, ShieldOff } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useLanguage } from '../../i18n/context'
import {
  logout,
  getEmail,
  getRole,
  getStoreName,
  isImpersonating,
  getImpersonation,
  stopImpersonation,
  isPlatformOwner,
} from '../../auth'
import type { Lang } from '../../i18n/types'

interface HeaderProps {
  title:        string
  subtitle?:    string
  onMenuClick?: () => void
}

/** Derive a readable display name from an email or store name. */
function _displayName(email: string, storeName: string, role: string): string {
  if (storeName) return storeName
  if (email) {
    // Take the part before @ and clean it up
    const local = email.split('@')[0].replace(/[-_.]/g, ' ')
    return local.charAt(0).toUpperCase() + local.slice(1)
  }
  if (role === 'admin' || role === 'owner') return 'المالك'
  return 'التاجر'
}

/** First letter of display name for the avatar. */
function _avatarLetter(name: string): string {
  return name.trim().charAt(0).toUpperCase() || 'م'
}

/** Color class for the avatar based on role. */
function _avatarColor(role: string): string {
  if (role === 'admin' || role === 'owner' || role === 'super_admin') return 'bg-rose-500'
  return 'bg-brand-500'
}

export default function Header({ title, subtitle, onMenuClick }: HeaderProps) {
  const { lang, setLang, t } = useLanguage()
  const navigate = useNavigate()
  const [profileOpen, setProfileOpen] = useState(false)
  const profileRef = useRef<HTMLDivElement>(null)

  // Read session info from localStorage (updated on every login/session change)
  const email      = getEmail()
  const role       = getRole()
  const storeName  = getStoreName()
  const impersonating = isImpersonating()
  const impersonInfo  = getImpersonation()

  const displayName  = _displayName(email, storeName, role)
  const avatarLetter = _avatarLetter(displayName)
  const avatarColor  = _avatarColor(role)

  const roleLabel = (() => {
    if (impersonating)                                        return 'دعم فني — وصول مؤقت'
    if (role === 'admin' || role === 'super_admin')           return 'مالك المنصة'
    if (role === 'owner')                                     return 'مالك'
    if (role === 'staff')                                     return 'موظف'
    return 'تاجر'
  })()

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setProfileOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  const handleStopImpersonation = () => {
    stopImpersonation()
    navigate('/admin')
    setProfileOpen(false)
  }

  return (
    <header className="h-14 md:h-16 bg-white border-b border-slate-200 flex items-center justify-between px-3 md:px-6 sticky top-0 z-20 pt-safe-top">

      {/* Left side: hamburger (mobile) + page title */}
      <div className="flex items-center gap-3">
        {/* Hamburger — visible only on mobile */}
        <button
          className="lg:hidden w-9 h-9 flex items-center justify-center rounded-lg hover:bg-slate-50 text-slate-500 transition-colors"
          onClick={onMenuClick}
          aria-label="فتح القائمة"
        >
          <Menu className="w-5 h-5" />
        </button>

        <div>
          <h1 className="text-base font-semibold text-slate-900 leading-none">{title}</h1>
          {subtitle && <p className="text-xs text-slate-400 mt-0.5">{subtitle}</p>}
        </div>
      </div>

      {/* Right-side actions */}
      <div className="flex items-center gap-2">

        {/* Impersonation banner — shown only to support agents */}
        {impersonating && (
          <div className="hidden md:flex items-center gap-1.5 px-2.5 py-1 bg-amber-50 border border-amber-200 rounded-lg text-xs font-medium text-amber-700">
            <Shield className="w-3.5 h-3.5" />
            دعم فني: {impersonInfo?.storeName || 'متجر'}
          </div>
        )}

        {/* Search */}
        <div className="relative hidden md:block">
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
              aria-label={code === 'ar' ? 'التبديل إلى العربية' : 'التبديل إلى الإنجليزية'}
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

        {/* Profile dropdown */}
        <div ref={profileRef} className="relative">
          <button
            onClick={() => setProfileOpen(o => !o)}
            className={`flex items-center gap-2 ps-2 pe-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors ${
              impersonating ? 'ring-2 ring-amber-300' : ''
            }`}
          >
            <div className={`w-7 h-7 ${avatarColor} rounded-full flex items-center justify-center`}>
              <span className="text-white text-xs font-semibold">{avatarLetter}</span>
            </div>
            <div className="hidden md:flex flex-col items-start leading-tight">
              <span className="text-sm font-medium text-slate-700 max-w-[120px] truncate">
                {displayName}
              </span>
              <span className="text-[10px] text-slate-400">{roleLabel}</span>
            </div>
            <ChevronDown className={`w-3.5 h-3.5 text-slate-400 hidden md:block transition-transform ${profileOpen ? 'rotate-180' : ''}`} />
          </button>

          {profileOpen && (
            <div className="absolute end-0 top-full mt-1 w-52 max-w-[calc(100vw-1rem)] bg-white rounded-xl shadow-lg border border-slate-100 py-1 z-50">
              {/* User info */}
              <div className="px-3 py-2.5 border-b border-slate-100">
                <p className="text-xs font-semibold text-slate-900 truncate">{displayName}</p>
                <p className="text-xs text-slate-400 truncate">{email || '—'}</p>
                <span className={`mt-1 inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full ${
                  impersonating
                    ? 'bg-amber-100 text-amber-700'
                    : isPlatformOwner()
                      ? 'bg-rose-100 text-rose-700'
                      : 'bg-emerald-100 text-emerald-700'
                }`}>
                  {roleLabel}
                </span>
              </div>

              {/* Settings */}
              <button
                onClick={() => { setProfileOpen(false); navigate('/settings') }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 transition-colors"
              >
                <User className="w-4 h-4 text-slate-400" />
                الإعدادات
              </button>

              {/* Exit impersonation — shown only when support is active */}
              {impersonating && (
                <button
                  onClick={handleStopImpersonation}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-amber-600 hover:bg-amber-50 transition-colors border-t border-slate-100"
                >
                  <ShieldOff className="w-4 h-4" />
                  إنهاء وصول الدعم
                </button>
              )}

              {/* Logout */}
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50 transition-colors border-t border-slate-100"
              >
                <LogOut className="w-4 h-4" />
                تسجيل الخروج
              </button>
            </div>
          )}
        </div>

      </div>
    </header>
  )
}
