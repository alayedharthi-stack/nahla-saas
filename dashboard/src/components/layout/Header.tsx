import { useState, useRef, useEffect, useCallback } from 'react'
import { Bell, ChevronDown, Menu, LogOut, User, Shield, ShieldOff, ShieldCheck, Clock, CheckCircle, XCircle, Headphones, AlertCircle, Sun, Moon, Monitor } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useLanguage } from '../../i18n/context'
import { useTheme, type ThemeMode } from '../../hooks/useTheme'
import {
  logout,
  getEmail,
  getRole,
  getStoreName,
  getTenantId,
  isImpersonating,
  getImpersonation,
  stopImpersonation,
  isPlatformOwner,
} from '../../auth'
import { API_BASE } from '../../api/client'
import { useDashboardPoll } from '../../lib/dashboardPolling'
import type { Lang } from '../../i18n/types'

interface HeaderProps {
  title:        string
  subtitle?:    string
  onMenuClick?: () => void
}

interface AccessRequest {
  id:           string
  requested_by: string
  requested_at: string
}

function _displayName(email: string, storeName: string): string {
  if (storeName) return storeName
  if (email) {
    const local = email.split('@')[0].replace(/[-_.]/g, ' ')
    return local.charAt(0).toUpperCase() + local.slice(1)
  }
  return ''
}

function _avatarLetter(name: string): string {
  return name.trim().charAt(0).toUpperCase() || 'م'
}

function _avatarColor(role: string): string {
  if (role === 'admin' || role === 'owner' || role === 'super_admin') return 'bg-rose-500'
  return 'bg-brand-500'
}

function useAccessRequests(role: string) {
  const [requests, setRequests]     = useState<AccessRequest[]>([])
  const [responding, setResponding] = useState<string | null>(null)
  const [approved, setApproved]     = useState<string | null>(null)
  // Track IDs already handled so polling never re-shows them
  const handledIds = useRef<Set<string>>(new Set())

  const load = useCallback(async (signal: AbortSignal) => {
    if (role === 'admin' || role === 'super_admin') return
    try {
      const res = await fetch(`${API_BASE}/merchant/access-requests`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
        signal,
      })
      if (res.ok) {
        const d = await res.json()
        // Filter out any IDs we've already handled locally — prevents
        // the brief window between local removal and DB confirmation
        const incoming: AccessRequest[] = (d.requests ?? []).filter(
          (r: AccessRequest) => !handledIds.current.has(r.id)
        )
        setRequests(incoming)
        const rawCount = d.count
        const cnt = typeof rawCount === 'number' ? rawCount : incoming.length
        window.dispatchEvent(new CustomEvent('nahla:access-requests-sync', { detail: { count: cnt } }))
      }
    } catch { /* ignore */ }
  }, [role])

  const reloadWithTimeout = useCallback(async () => {
    const ac = new AbortController()
    const tid = window.setTimeout(() => ac.abort(), 25_000)
    try {
      await load(ac.signal)
    } finally {
      clearTimeout(tid)
    }
  }, [load])

  useEffect(() => {
    void reloadWithTimeout()
  }, [reloadWithTimeout])

  useDashboardPoll({
    pollKey: 'GET:/merchant/access-requests',
    intervalMs: 45_000,
    leading: false,
    run: signal => load(signal),
  })

  const respond = async (reqId: string, approve: boolean, ttlHours = 4) => {
    if (responding) return
    setResponding(reqId)

    // Optimistic: remove immediately from view and mark handled
    handledIds.current.add(reqId)

    if (approve) {
      setApproved(reqId)
    } else {
      setRequests(prev => prev.filter(r => r.id !== reqId))
    }

    try {
      const res = await fetch(`${API_BASE}/merchant/access-requests/${reqId}/respond`, {
        method:  'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body: JSON.stringify({ approve, ttl_hours: ttlHours }),
      })

      if (res.ok) {
        if (approve) {
          // Notify Settings page to refresh support-access status
          window.dispatchEvent(new Event('nahla:support-access-changed'))
          // Brief success feedback, then remove
          setTimeout(() => {
            setApproved(null)
            setRequests(prev => prev.filter(r => r.id !== reqId))
          }, 2000)
        }
        // Reload from server after a short delay to sync state
        setTimeout(() => void reloadWithTimeout(), 1500)
      } else {
        // Server rejected — undo optimistic removal
        handledIds.current.delete(reqId)
        setApproved(null)
        await reloadWithTimeout()
      }
    } catch {
      // Network error — undo optimistic removal
      handledIds.current.delete(reqId)
      setApproved(null)
      await reloadWithTimeout()
    } finally {
      setResponding(null)
    }
  }

  return { requests, responding, respond, reload: reloadWithTimeout, approved }
}

// ── Admin bell: merchant help requests ────────────────────────────────────────
interface HelpReq {
  req_id: string; store_name: string; email: string
  reason: string; ttl_hours: number; requested_at: string; tenant_id: number
}

function useAdminBell(role: string) {
  const [helpReqs, setHelpReqs] = useState<HelpReq[]>([])
  const navigate = useNavigate()

  const load = useCallback(async (signal: AbortSignal) => {
    if (role !== 'admin' && role !== 'super_admin') return
    try {
      const res = await fetch(`${API_BASE}/admin/support-requests`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
        signal,
      })
      if (res.ok) {
        const d = await res.json()
        setHelpReqs(d.requests ?? [])
      }
    } catch { /* ignore */ }
  }, [role])

  const bootstrap = useCallback(async () => {
    const ac = new AbortController()
    const tid = window.setTimeout(() => ac.abort(), 25_000)
    try {
      await load(ac.signal)
    } finally {
      clearTimeout(tid)
    }
  }, [load])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  useDashboardPoll({
    pollKey: 'GET:/admin/support-requests',
    intervalMs: 45_000,
    leading: false,
    run: signal => load(signal),
  })

  const goToMerchant = () => navigate('/admin/merchants')
  return { helpReqs, goToMerchant, reload: bootstrap }
}

// ── Merchant extra notifications (system notifs) ──────────────────────────────
interface SysNotif {
  id: string; title: string; body: string; read: boolean
  type: string; action_url?: string; created_at: string
}

function useMerchantNotifs(role: string) {
  const [notifs, setNotifs] = useState<SysNotif[]>([])

  const load = useCallback(async (signal: AbortSignal) => {
    if (role === 'admin' || role === 'super_admin') return
    try {
      const res = await fetch(`${API_BASE}/merchant/notifications?unread_only=true`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
        signal,
      })
      if (res.ok) {
        const d = await res.json()
        setNotifs(d.notifications ?? [])
      }
    } catch { /* ignore */ }
  }, [role])

  const bootstrap = useCallback(async () => {
    const ac = new AbortController()
    const tid = window.setTimeout(() => ac.abort(), 25_000)
    try {
      await load(ac.signal)
    } finally {
      clearTimeout(tid)
    }
  }, [load])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  useDashboardPoll({
    pollKey: 'GET:/merchant/notifications?unread_only=true',
    intervalMs: 90_000,
    leading: false,
    run: signal => load(signal),
  })

  const markRead = async (notifId: string) => {
    try {
      await fetch(`${API_BASE}/merchant/notifications/${notifId}/read`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      setNotifs(prev => prev.filter(n => n.id !== notifId))
    } catch { /* ignore */ }
  }

  return { notifs, markRead }
}

export default function Header({ title, subtitle, onMenuClick }: HeaderProps) {
  const { lang, setLang, t } = useLanguage()
  const { mode: themeMode, setMode: setThemeMode } = useTheme()
  const navigate = useNavigate()
  const [profileOpen, setProfileOpen] = useState(false)
  const [bellOpen, setBellOpen]       = useState(false)
  const [ttlChoice, setTtlChoice]     = useState<Record<string, number>>({})
  const profileRef = useRef<HTMLDivElement>(null)
  const bellRef    = useRef<HTMLDivElement>(null)

  const email      = getEmail()
  const role       = getRole()
  const storeName  = getStoreName()
  const tenantId   = getTenantId()
  const impersonating = isImpersonating()
  const impersonInfo  = getImpersonation()

  // ── Display-name resolution ────────────────────────────────────────────────
  // Platform-admin sessions (admin / owner / super_admin) MUST NOT show a
  // merchant's store name as the owner's identity. The token issued by the
  // env-fallback admin login carries `tenant_id=1`, which makes /settings
  // return that tenant's store_name; if we render it here, the owner header
  // ends up advertising tenant 1's brand ("آل عايد للعسل البلدي") as the
  // platform owner's name. Always show a generic Nahla-admin label instead.
  // For impersonation sessions, the existing impersonation banner already
  // surfaces the merchant being acted on, so the header stays neutral.
  const isPlatformAdmin = role === 'admin' || role === 'owner' || role === 'super_admin'
  const rawName      = isPlatformAdmin
    ? ''  // force the fallback below — never use storeName for owners
    : _displayName(email, storeName)
  const displayName  = rawName || (
    isPlatformAdmin
      ? t(tr => tr.roles.nahlaAdmin)
      : t(tr => tr.roles.defaultMerchant)
  )
  const avatarLetter = _avatarLetter(displayName)
  const avatarColor  = _avatarColor(role)

  const roleLabel = (() => {
    if (impersonating)                                        return t(tr => tr.roles.support)
    if (role === 'admin' || role === 'super_admin')           return t(tr => tr.roles.platformOwner)
    if (role === 'owner')                                     return t(tr => tr.roles.owner)
    if (role === 'staff')                                     return t(tr => tr.roles.staff)
    return t(tr => tr.roles.merchant)
  })()

  const { requests, responding, respond, approved } = useAccessRequests(role)
  const { helpReqs, goToMerchant } = useAdminBell(role)
  const { notifs, markRead } = useMerchantNotifs(role)
  const notifCount = requests.length + helpReqs.length + notifs.length

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setProfileOpen(false)
      }
      if (bellRef.current && !bellRef.current.contains(e.target as Node)) {
        setBellOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  const handleStopImpersonation = async () => {
    // Resolve the support session: marks request as "resolved", disables access,
    // stores a thank-you notification for the merchant, and writes audit log.
    try {
      await fetch(`${API_BASE}/merchant/support-access/resolve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body: JSON.stringify({}),
      })
    } catch { /* ignore — we still exit regardless */ }
    stopImpersonation()
    navigate('/admin')
    setProfileOpen(false)
  }

  const getTtl = (reqId: string) => ttlChoice[reqId] ?? 4
  const setTtl = (reqId: string, v: number) =>
    setTtlChoice(prev => ({ ...prev, [reqId]: v }))

  // Cycle: light → dark → system → light
  const nextThemeMode: Record<ThemeMode, ThemeMode> = {
    light:  'dark',
    dark:   'system',
    system: 'light',
  }
  const themeIcon = themeMode === 'light' ? <Sun className="w-4 h-4" />
                  : themeMode === 'dark'  ? <Moon className="w-4 h-4" />
                  :                          <Monitor className="w-4 h-4" />
  const themeAriaLabel =
    lang === 'ar'
      ? `الوضع الحالي: ${themeMode === 'light' ? 'فاتح' : themeMode === 'dark' ? 'داكن' : 'تلقائي حسب النظام'} — اضغط للتبديل`
      : `Current theme: ${themeMode === 'light' ? 'light' : themeMode === 'dark' ? 'dark' : 'system'} — click to toggle`

  return (
    <header className="h-14 md:h-16 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between px-3 md:px-6 sticky top-0 z-20 pt-safe-top">

      {/* Left side */}
      <div className="flex items-center gap-3">
        <button
          className="lg:hidden w-9 h-9 flex items-center justify-center rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-300 transition-colors"
          onClick={onMenuClick}
          aria-label="فتح القائمة"
        >
          <Menu className="w-5 h-5" />
        </button>
        <div>
          <h1 className="text-base font-semibold text-slate-900 dark:text-slate-100 leading-none">{title}</h1>
          {subtitle && <p className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">{subtitle}</p>}
        </div>
      </div>

      {/* Right-side actions */}
      <div className="flex items-center gap-2">

        {impersonating && (
          <div className="hidden md:flex items-center gap-1.5 px-2.5 py-1 bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/30 rounded-lg text-xs font-medium text-amber-700 dark:text-amber-300">
            <Shield className="w-3.5 h-3.5" />
            دعم فني: {impersonInfo?.storeName || 'متجر'}
          </div>
        )}

        {/* Theme toggle */}
        <button
          type="button"
          onClick={() => setThemeMode(nextThemeMode[themeMode])}
          aria-label={themeAriaLabel}
          title={themeAriaLabel}
          className="w-9 h-9 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
        >
          {themeIcon}
        </button>

        {/* Language toggle */}
        <div className="flex items-center rounded-lg border border-slate-200 dark:border-slate-700 overflow-hidden">
          {(['ar', 'en'] as Lang[]).map((code) => (
            <button
              key={code}
              onClick={() => setLang(code)}
              aria-label={code === 'ar' ? 'التبديل إلى العربية' : 'التبديل إلى الإنجليزية'}
              className={`px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                lang === code
                  ? 'bg-brand-500 text-white'
                  : 'text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800'
              }`}
            >
              {code === 'ar' ? 'AR' : 'EN'}
            </button>
          ))}
        </div>

        {/* ── Notifications Bell ── */}
        <div ref={bellRef} className="relative">
          <button
            onClick={() => setBellOpen(o => !o)}
            className="relative w-9 h-9 flex items-center justify-center rounded-lg hover:bg-slate-50 text-slate-500 transition-colors"
            aria-label="الإشعارات"
          >
            <Bell className="w-4 h-4" />
            {notifCount > 0 && (
              <span className="absolute top-1.5 end-1.5 w-4 h-4 bg-red-500 rounded-full ring-2 ring-white flex items-center justify-center">
                <span className="text-[9px] font-bold text-white leading-none">{notifCount}</span>
              </span>
            )}
          </button>

          {bellOpen && (
            <div className="absolute end-0 top-full mt-1 w-80 max-w-[calc(100vw-1rem)] bg-white rounded-xl shadow-xl border border-slate-100 z-50 overflow-hidden" dir="rtl">
              <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
                <span className="text-sm font-semibold text-slate-800">الإشعارات</span>
                {notifCount > 0 && (
                  <span className="text-xs px-2 py-0.5 bg-red-100 text-red-600 rounded-full font-medium">
                    {notifCount} جديد
                  </span>
                )}
              </div>

              <div className="max-h-96 overflow-y-auto">
                {notifCount === 0 ? (
                  <div className="flex flex-col items-center justify-center py-10 text-slate-400">
                    <Bell className="w-8 h-8 mb-2 text-slate-200" />
                    <p className="text-sm">لا توجد إشعارات</p>
                  </div>
                ) : (
                  <div className="divide-y divide-slate-50">

                    {/* ── Admin: merchant help requests ── */}
                    {helpReqs.map(hr => (
                      <div key={hr.req_id} className="p-4 bg-blue-50/50 space-y-2">
                        <div className="flex items-start gap-3">
                          <div className="w-8 h-8 bg-blue-100 rounded-lg flex items-center justify-center shrink-0">
                            <Headphones className="w-4 h-4 text-blue-600" />
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-semibold text-slate-800">
                              طلب مساعدة من تاجر
                            </p>
                            <p className="text-xs text-slate-600 font-medium truncate">
                              {hr.store_name || hr.email}
                            </p>
                            <p className="text-xs text-slate-500 mt-0.5 line-clamp-2">{hr.reason}</p>
                            <div className="flex items-center gap-1 mt-0.5 text-slate-400">
                              <Clock className="w-3 h-3" />
                              <span className="text-xs">
                                {new Date(hr.requested_at).toLocaleString('ar-SA', {
                                  dateStyle: 'short', timeStyle: 'short'
                                })}
                              </span>
                            </div>
                          </div>
                        </div>
                        <button
                          onClick={() => { setBellOpen(false); goToMerchant() }}
                          className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium rounded-lg transition-colors"
                        >
                          <ShieldCheck className="w-3.5 h-3.5" />
                          الانتقال لإدارة التجار
                        </button>
                      </div>
                    ))}

                    {/* ── Merchant: admin-initiated access requests (approval needed) ── */}
                    {requests.map(r => (
                      <div key={r.id} className={`p-4 space-y-3 transition-all ${approved === r.id ? 'bg-green-50' : 'bg-amber-50/50'}`}>
                        {approved === r.id ? (
                          <div className="flex items-center gap-3">
                            <div className="w-8 h-8 bg-green-100 rounded-lg flex items-center justify-center shrink-0">
                              <CheckCircle className="w-4 h-4 text-green-600" />
                            </div>
                            <div>
                              <p className="text-sm font-semibold text-green-800">تمت الموافقة ✓</p>
                              <p className="text-xs text-green-600">سيختفي هذا الإشعار تلقائياً</p>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div className="flex items-start gap-3">
                              <div className="w-8 h-8 bg-amber-100 rounded-lg flex items-center justify-center shrink-0">
                                <ShieldCheck className="w-4 h-4 text-amber-600" />
                              </div>
                              <div className="flex-1 min-w-0">
                                <p className="text-sm font-semibold text-slate-800">طلب وصول من فريق الدعم</p>
                                <p className="text-xs text-slate-500 mt-0.5 truncate">{r.requested_by}</p>
                                <div className="flex items-center gap-1 mt-0.5 text-slate-400">
                                  <Clock className="w-3 h-3" />
                                  <span className="text-xs">
                                    {new Date(r.requested_at).toLocaleString('ar-SA', {
                                      dateStyle: 'short', timeStyle: 'short'
                                    })}
                                  </span>
                                </div>
                              </div>
                            </div>
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className="text-xs text-slate-500 shrink-0">مدة الوصول:</span>
                              {[1, 2, 4].map(h => (
                                <button
                                  key={h}
                                  onClick={() => setTtl(r.id, h)}
                                  className={`px-2 py-0.5 text-xs rounded-md border transition-colors ${
                                    getTtl(r.id) === h
                                      ? 'bg-amber-500 text-white border-amber-500'
                                      : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                                  }`}
                                >
                                  {h === 1 ? 'ساعة' : h === 2 ? 'ساعتان' : '4 ساعات'}
                                </button>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <button
                                onClick={() => respond(r.id, true, getTtl(r.id))}
                                disabled={responding === r.id}
                                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-green-500 hover:bg-green-600 text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                              >
                                {responding === r.id
                                  ? <span className="w-3.5 h-3.5 border border-white border-t-transparent rounded-full animate-spin" />
                                  : <CheckCircle className="w-3.5 h-3.5" />}
                                موافقة
                              </button>
                              <button
                                onClick={() => respond(r.id, false)}
                                disabled={responding === r.id}
                                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-white hover:bg-red-50 text-red-600 border border-red-200 text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                              >
                                <XCircle className="w-3.5 h-3.5" />
                                رفض
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    ))}

                    {/* ── Merchant: system notifications (help request status, etc.) ── */}
                    {notifs.map(n => (
                      <div key={n.id} className="p-4 bg-slate-50/50 space-y-2">
                        <div className="flex items-start gap-3">
                          <div className="w-8 h-8 bg-brand-100 rounded-lg flex items-center justify-center shrink-0">
                            <AlertCircle className="w-4 h-4 text-brand-600" />
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-semibold text-slate-800">{n.title}</p>
                            <p className="text-xs text-slate-500 mt-0.5">{n.body}</p>
                            <div className="flex items-center gap-1 mt-0.5 text-slate-400">
                              <Clock className="w-3 h-3" />
                              <span className="text-xs">
                                {new Date(n.created_at).toLocaleString('ar-SA', {
                                  dateStyle: 'short', timeStyle: 'short'
                                })}
                              </span>
                            </div>
                          </div>
                          <button
                            onClick={() => markRead(n.id)}
                            className="shrink-0 text-slate-400 hover:text-slate-600"
                            title="تمييز كمقروء"
                          >
                            <XCircle className="w-4 h-4" />
                          </button>
                        </div>
                        {n.action_url && (
                          <button
                            onClick={() => { setBellOpen(false); navigate(n.action_url!) }}
                            className="w-full text-xs text-brand-600 hover:underline text-start"
                          >
                            عرض التفاصيل ←
                          </button>
                        )}
                      </div>
                    ))}

                  </div>
                )}
              </div>
            </div>
          )}
        </div>

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
              <div className="px-3 py-2.5 border-b border-slate-100">
                <p className="text-xs font-semibold text-slate-900 truncate">{displayName}</p>
                <p className="text-xs text-slate-400 truncate">{email || '—'}</p>
                <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                  <span className={`inline-block text-[10px] font-medium px-1.5 py-0.5 rounded-full ${
                    impersonating
                      ? 'bg-amber-100 text-amber-700'
                      : isPlatformOwner()
                        ? 'bg-rose-100 text-rose-700'
                        : 'bg-emerald-100 text-emerald-700'
                  }`}>
                    {roleLabel}
                  </span>
                  {tenantId !== null && (
                    <span className="inline-block text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-500 select-all">
                      tenant:{tenantId}
                    </span>
                  )}
                </div>
              </div>

              <button
                onClick={() => { setProfileOpen(false); navigate('/settings') }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 transition-colors"
              >
                <User className="w-4 h-4 text-slate-400" />
                الإعدادات
              </button>

              {impersonating && (
                <button
                  onClick={handleStopImpersonation}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-amber-600 hover:bg-amber-50 transition-colors border-t border-slate-100"
                >
                  <ShieldOff className="w-4 h-4" />
                  إنهاء وصول الدعم
                </button>
              )}

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
