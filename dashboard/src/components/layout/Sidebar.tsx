import { NavLink } from 'react-router-dom'
import { useState, useEffect } from 'react'
import {
  LayoutDashboard,
  MessageSquare,
  ShoppingCart,
  Bot,
  Tag,
  Megaphone,
  FileText,
  Brain,
  Plug,
  BarChart2,
  Settings,
  BrainCircuit,
  Store,
  UserCheck,
  Users,
  Activity,
  CreditCard,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'
import { isAdmin } from '../../auth'
import { apiCall } from '../../api/client'

interface NavItem {
  to:    string
  icon:  LucideIcon
  label: (tr: Translations) => string
}

interface NavGroup {
  groupLabel: (tr: Translations) => string
  items: NavItem[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    groupLabel: tr => tr.nav.groups.main,
    items: [
      { to: '/overview',         icon: LayoutDashboard, label: tr  => tr.nav.items.overview      },
      { to: '/conversations',    icon: MessageSquare,   label: tr  => tr.nav.items.conversations },
      { to: '/orders',           icon: ShoppingCart,    label: tr  => tr.nav.items.orders        },
      { to: '/smart-automations',icon: Bot,             label: _tr => 'الطيار الآلي'              },
      { to: '/coupons',          icon: Tag,             label: tr  => tr.nav.items.coupons       },
      { to: '/campaigns',        icon: Megaphone,       label: tr  => tr.nav.items.campaigns     },
      { to: '/templates',        icon: FileText,        label: tr  => tr.nav.items.templates     },
    ],
  },
  {
    groupLabel: tr => tr.nav.groups.ai,
    items: [
      { to: '/intelligence',  icon: Brain,        label: tr  => tr.nav.items.intelligence },
      { to: '/analytics',     icon: BarChart2,    label: tr  => tr.nav.items.analyticsAI  },
      { to: '/ai-sales-logs', icon: BrainCircuit, label: _tr => 'وكيل المبيعات'           },
      { to: '/handoff-queue', icon: UserCheck,    label: _tr => 'طابور التحويل'            },
    ],
  },
  {
    groupLabel: tr => tr.nav.groups.store,
    items: [
      { to: '/integrations',      icon: Plug,       label: tr  => tr.nav.items.integrations },
      { to: '/store-integration', icon: Store,      label: _tr => 'ربط المتجر'              },
      { to: '/system-status',     icon: Activity,   label: _tr => 'حالة النظام'             },
      { to: '/billing',           icon: CreditCard, label: _tr => 'الاشتراك والفوترة'       },
      { to: '/settings',          icon: Settings,   label: tr  => tr.nav.items.settings     },
    ],
  },
]

interface SidebarProps {
  isOpen:  boolean
  onClose: () => void
}

export default function Sidebar({ isOpen, onClose }: SidebarProps) {
  const { t } = useLanguage()
  const [storeName, setStoreName] = useState('نحلة')
  const [logoUrl,   setLogoUrl]   = useState('/logo-v2.png')

  useEffect(() => {
    apiCall<{ store?: { store_name?: string; store_logo_url?: string } }>('/settings')
      .then(data => {
        if (data?.store?.store_name)     setStoreName(data.store.store_name)
        if (data?.store?.store_logo_url) setLogoUrl(data.store.store_logo_url)
      })
      .catch(() => {/* keep defaults */})
  }, [])

  const initial = storeName.trim().charAt(0) || 'ن'

  return (
    <>
      {/* ── Mobile overlay ───────────────────────────────────────────── */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-30 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      {/* ── Sidebar panel ────────────────────────────────────────────── */}
      <aside
        className={[
          'fixed inset-y-0 start-0 w-60 bg-slate-900 flex flex-col z-40',
          'transition-transform duration-300 ease-in-out',
          /* Desktop: always visible — override any mobile transform */
          'lg:!translate-x-0',
          /* Mobile: slide in/out from the inline-start edge */
          isOpen
            ? 'translate-x-0'
            : 'ltr:-translate-x-full rtl:translate-x-full',
        ].join(' ')}
      >
        {/* Logo + close button (mobile only) */}
        <div className="flex items-center gap-2.5 px-5 py-5 border-b border-slate-800">
          <div className="w-8 h-8 rounded-lg shrink-0 overflow-hidden bg-slate-800">
            <img
              src={logoUrl}
              alt={storeName}
              className="w-full h-full object-contain"
              onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
            />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-white font-semibold text-sm leading-none truncate">{storeName}</p>
            <p className="text-slate-500 text-xs mt-0.5">{t(tr => tr.nav.logoTagline)}</p>
          </div>
          {/* Close button — visible only on mobile */}
          <button
            className="lg:hidden text-slate-400 hover:text-white transition-colors ms-auto"
            onClick={onClose}
            aria-label="إغلاق القائمة"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 overflow-y-auto space-y-5">
          {NAV_GROUPS.map((group, gi) => (
            <div key={gi}>
              <p className="px-3 mb-1.5 text-[10px] font-semibold text-slate-500 uppercase tracking-widest">
                {t(group.groupLabel)}
              </p>
              <div className="space-y-0.5">
                {group.items.map(({ to, icon: Icon, label }) => (
                  <NavLink
                    key={to}
                    to={to}
                    onClick={onClose}
                    className={({ isActive }) =>
                      `relative flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
                        isActive
                          ? 'bg-white/10 text-white'
                          : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
                      }`
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {isActive && (
                          <span className="absolute start-0 inset-y-1.5 w-0.5 bg-brand-400 rounded-e-full" />
                        )}
                        <Icon className="w-4 h-4 shrink-0" />
                        {t(label)}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}

          {/* Admin-only section */}
          {isAdmin() && (
            <div>
              <p className="px-3 mb-1.5 text-[10px] font-semibold text-amber-500/80 uppercase tracking-widest">
                المالك
              </p>
              <div className="space-y-0.5">
                <NavLink
                  to="/merchants"
                  onClick={onClose}
                  className={({ isActive }) =>
                    `relative flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
                      isActive
                        ? 'bg-white/10 text-white'
                        : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && (
                        <span className="absolute start-0 inset-y-1.5 w-0.5 bg-amber-400 rounded-e-full" />
                      )}
                      <Users className="w-4 h-4 shrink-0" />
                      إدارة التجار
                    </>
                  )}
                </NavLink>
              </div>
            </div>
          )}
        </nav>

        {/* Store badge */}
        <div className="px-3 py-4 border-t border-slate-800">
          <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-white/5 hover:bg-white/10 transition-colors cursor-pointer">
            <div className="w-7 h-7 bg-brand-500/20 rounded-full flex items-center justify-center shrink-0 overflow-hidden">
              {logoUrl
                ? <img src={logoUrl} alt={storeName} className="w-full h-full object-cover rounded-full" />
                : <span className="text-brand-400 text-xs font-bold">{initial}</span>}
            </div>
            <div className="min-w-0">
              <p className="text-white text-xs font-medium truncate">{storeName}</p>
              <p className="text-slate-500 text-xs truncate">{t(tr => tr.nav.storeBadge.plan)}</p>
            </div>
          </div>
        </div>
      </aside>
    </>
  )
}
