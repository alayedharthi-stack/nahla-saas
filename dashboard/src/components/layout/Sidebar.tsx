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
  MessageCircle,
  Crown,
  TrendingUp,
  Layers,
  X,
  Puzzle,
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
  isAI?: boolean
}

interface NavGroup {
  groupLabel: string
  items: NavItem[]
}

// ── Admin-only navigation (platform owner) ────────────────────────────────────
const ADMIN_NAV_GROUPS: NavGroup[] = [
  {
    groupLabel: 'المنصة',
    items: [
      { to: '/admin',          icon: Crown,         label: _tr => 'Dashboard'        },
      { to: '/admin/merchants',icon: Users,         label: _tr => 'Merchants'        },
      { to: '/admin/revenue',  icon: TrendingUp,    label: _tr => 'Revenue'          },
      { to: '/admin/team',     icon: Layers,        label: _tr => 'Team'             },
      { to: '/system-status',  icon: Activity,      label: _tr => 'System Status'    },
    ],
  },
  {
    groupLabel: 'نحلة AI',
    items: [
      { to: '/whatsapp-connect', icon: MessageCircle, label: _tr => 'بوت نحلة' },
      { to: '/templates',        icon: FileText,       label: _tr => 'قوالب الرسائل' },
    ],
  },
  {
    groupLabel: 'الإعدادات',
    items: [
      { to: '/settings', icon: Settings, label: _tr => 'Settings' },
    ],
  },
]

// ── Merchant navigation ────────────────────────────────────────────────────────
const MERCHANT_NAV_GROUPS: NavGroup[] = [
  {
    groupLabel: 'الرئيسية',
    items: [
      { to: '/overview',          icon: LayoutDashboard, label: tr  => tr.nav.items.overview      },
      { to: '/conversations',     icon: MessageSquare,   label: tr  => tr.nav.items.conversations },
      { to: '/orders',            icon: ShoppingCart,    label: tr  => tr.nav.items.orders        },
      { to: '/smart-automations', icon: Bot,             label: _tr => 'الطيار الآلي',  isAI: true },
      { to: '/coupons',           icon: Tag,             label: tr  => tr.nav.items.coupons       },
      { to: '/campaigns',         icon: Megaphone,       label: tr  => tr.nav.items.campaigns     },
      { to: '/templates',         icon: FileText,        label: tr  => tr.nav.items.templates     },
    ],
  },
  {
    groupLabel: 'الذكاء الاصطناعي',
    items: [
      { to: '/intelligence',  icon: Brain,        label: tr  => tr.nav.items.intelligence, isAI: true },
      { to: '/analytics',     icon: BarChart2,    label: tr  => tr.nav.items.analyticsAI,  isAI: true },
      { to: '/ai-sales-logs', icon: BrainCircuit, label: _tr => 'وكيل المبيعات',           isAI: true },
      { to: '/handoff-queue', icon: UserCheck,    label: _tr => 'طابور التحويل'            },
    ],
  },
  {
    groupLabel: 'المتجر',
    items: [
      { to: '/integrations',      icon: Plug,          label: tr  => tr.nav.items.integrations },
      { to: '/store-integration', icon: Store,          label: _tr => 'ربط المتجر'             },
      { to: '/whatsapp-connect',  icon: MessageCircle,  label: _tr => 'ربط واتساب'             },
      { to: '/addons',            icon: Puzzle,         label: _tr => 'إضافات المتجر'          },
      { to: '/widgets',           icon: TrendingUp,     label: _tr => 'أدوات زيادة المبيعات'   },
      { to: '/system-status',     icon: Activity,       label: _tr => 'حالة النظام'             },
      { to: '/billing',           icon: CreditCard,     label: _tr => 'الاشتراك والفوترة'       },
      { to: '/settings',          icon: Settings,       label: tr  => tr.nav.items.settings     },
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
  const [logoUrl,   setLogoUrl]   = useState('/logo.png')
  const adminMode = isAdmin()

  useEffect(() => {
    if (adminMode) return  // Admin doesn't need merchant store info
    apiCall<{ store?: { store_name?: string; store_logo_url?: string } }>('/settings')
      .then(data => {
        if (data?.store?.store_name)     setStoreName(data.store.store_name)
        if (data?.store?.store_logo_url) setLogoUrl(data.store.store_logo_url)
      })
      .catch(() => {/* keep defaults */})
  }, [adminMode])

  const navGroups = adminMode ? ADMIN_NAV_GROUPS : MERCHANT_NAV_GROUPS
  const accentColor = adminMode ? 'bg-amber-400' : 'bg-brand-400'

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
          'lg:!translate-x-0',
          isOpen ? 'translate-x-0' : 'ltr:-translate-x-full rtl:translate-x-full',
        ].join(' ')}
      >
        {/* Logo */}
        <div className="flex items-center gap-2.5 px-5 py-5 border-b border-slate-800">
          <div className={`w-8 h-8 rounded-lg shrink-0 overflow-hidden flex items-center justify-center ${adminMode ? 'bg-amber-500/20' : 'bg-slate-800'}`}>
            {adminMode
              ? <Crown className="w-4 h-4 text-amber-400" />
              : <img src={logoUrl} alt={storeName} className="w-full h-full object-contain"
                  onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }} />
            }
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5">
              <p className="text-white font-semibold text-sm leading-none truncate">
                {adminMode ? 'نحلة' : storeName}
              </p>
              {/* AI brand badge — always visible */}
              <span className="inline-flex items-center px-1.5 py-0.5 rounded-md bg-amber-500/15 border border-amber-500/50 shadow-[0_0_8px_rgba(245,158,11,0.3)]">
                <span className="text-[9px] font-black text-amber-400 leading-none tracking-wide">AI</span>
              </span>
            </div>
            <p className={`text-xs mt-0.5 ${adminMode ? 'text-amber-500/70' : 'text-slate-500'}`}>
              {adminMode ? 'لوحة المالك' : t(tr => tr.nav.logoTagline)}
            </p>
          </div>
          <button className="lg:hidden text-slate-400 hover:text-white transition-colors ms-auto" onClick={onClose}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 overflow-y-auto space-y-5">
          {navGroups.map((group, gi) => (
            <div key={gi}>
              <p className={`px-3 mb-1.5 text-[10px] font-semibold uppercase tracking-widest ${adminMode ? 'text-amber-500/60' : 'text-slate-500'}`}>
                {group.groupLabel}
              </p>
              <div className="space-y-0.5">
                {group.items.map(({ to, icon: Icon, label, isAI }) => (
                  <NavLink
                    key={to}
                    to={to}
                    onClick={onClose}
                    className={({ isActive }) =>
                      `relative flex items-center gap-3 px-3 py-3 lg:py-2.5 rounded-lg text-sm font-medium transition-all touch-manipulation ${
                        isActive
                          ? 'bg-white/10 text-white'
                          : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
                      }`
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {isActive && (
                          <span className={`absolute start-0 inset-y-1.5 w-0.5 ${accentColor} rounded-e-full`} />
                        )}
                        {/* Icon with optional AI badge */}
                        <span className="relative shrink-0">
                          <Icon className="w-4 h-4" />
                          {isAI && (
                            <span className="absolute -bottom-1.5 -end-1.5 flex items-center justify-center w-3.5 h-3.5 rounded-full bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.7)]">
                              <span className="text-[6px] font-black text-slate-900 leading-none tracking-tight">AI</span>
                            </span>
                          )}
                        </span>
                        {t(label)}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {/* Bottom badge — with safe area for iOS home bar */}
        <div className="px-3 py-4 pb-[max(1rem,env(safe-area-inset-bottom,1rem))] border-t border-slate-800">
          <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-white/5">
            <div className={`w-7 h-7 rounded-full flex items-center justify-center shrink-0 ${adminMode ? 'bg-amber-500/20' : 'bg-brand-500/20'}`}>
              {adminMode
                ? <Crown className="w-3.5 h-3.5 text-amber-400" />
                : <img src={logoUrl} alt={storeName} className="w-full h-full object-cover rounded-full" />
              }
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-white text-xs font-medium truncate">
                {adminMode ? 'تركي الحارثي' : storeName}
              </p>
              <div className="flex items-center gap-1.5 mt-0.5">
                <p className={`text-xs truncate ${adminMode ? 'text-amber-500/60' : 'text-slate-500'}`}>
                  {adminMode ? 'مالك المنصة' : t(tr => tr.nav.storeBadge.plan)}
                </p>
                {/* AI badge — always visible */}
                <span className="inline-flex items-center px-1 py-px rounded bg-amber-500/15 border border-amber-500/40 shadow-[0_0_6px_rgba(245,158,11,0.25)]">
                  <span className="text-[7px] font-black text-amber-400 leading-none tracking-wide">AI</span>
                </span>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </>
  )
}
