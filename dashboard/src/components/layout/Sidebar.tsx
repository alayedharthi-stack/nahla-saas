import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  MessageSquare,
  ShoppingCart,
  Tag,
  Megaphone,
  FileText,
  Zap,
  Brain,
  Plug,
  BarChart2,
  Settings,
  Sparkles,
  BrainCircuit,
  Store,
  UserCheck,
  Activity,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

interface NavItem {
  to:   string
  icon: LucideIcon
  // Returns the label from the translation tree
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
      { to: '/overview',      icon: LayoutDashboard, label: tr => tr.nav.items.overview      },
      { to: '/conversations', icon: MessageSquare,   label: tr => tr.nav.items.conversations },
      { to: '/orders',        icon: ShoppingCart,    label: tr => tr.nav.items.orders        },
      { to: '/coupons',       icon: Tag,             label: tr => tr.nav.items.coupons       },
      { to: '/campaigns',     icon: Megaphone,       label: tr => tr.nav.items.campaigns     },
      { to: '/templates',     icon: FileText,        label: tr => tr.nav.items.templates     },
    ],
  },
  {
    groupLabel: tr => tr.nav.groups.ai,
    items: [
      { to: '/intelligence',   icon: Brain,         label: tr => tr.nav.items.intelligence  },
      { to: '/automations',    icon: Zap,           label: tr => tr.nav.items.automations   },
      { to: '/analytics',      icon: BarChart2,     label: tr => tr.nav.items.analyticsAI   },
      { to: '/ai-sales-logs',  icon: BrainCircuit,  label: _tr => 'وكيل المبيعات'           },
      { to: '/handoff-queue',  icon: UserCheck,     label: _tr => 'طابور التحويل'            },
    ],
  },
  {
    groupLabel: tr => tr.nav.groups.store,
    items: [
      { to: '/integrations',      icon: Plug,     label: tr => tr.nav.items.integrations },
      { to: '/store-integration', icon: Store,    label: _tr => 'ربط المتجر'              },
      { to: '/system-status',     icon: Activity, label: _tr => 'حالة النظام'             },
      { to: '/settings',          icon: Settings, label: tr => tr.nav.items.settings     },
    ],
  },
]

export default function Sidebar() {
  const { t } = useLanguage()

  return (
    /*
     * start-0 = inset-inline-start: 0
     * In LTR: fixed to the left edge.
     * In RTL: fixed to the right edge.
     * No ltr:/rtl: overrides needed — logical property handles it.
     */
    <aside className="fixed inset-y-0 start-0 w-60 bg-slate-900 flex flex-col z-30">

      {/* Logo */}
      <div className="flex items-center gap-2.5 px-5 py-5 border-b border-slate-800">
        <div className="w-8 h-8 bg-brand-500 rounded-lg flex items-center justify-center shrink-0">
          <Sparkles className="w-4 h-4 text-white" />
        </div>
        <div>
          <p className="text-white font-semibold text-sm leading-none">نهلة</p>
          <p className="text-slate-500 text-xs mt-0.5">{t(tr => tr.nav.logoTagline)}</p>
        </div>
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
                      {/*
                       * start-0 = inset-inline-start: 0
                       * Accent bar appears on the right edge in RTL,
                       * left edge in LTR — matches reading direction.
                       */}
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
      </nav>

      {/* Store badge */}
      <div className="px-3 py-4 border-t border-slate-800">
        <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-white/5 hover:bg-white/10 transition-colors cursor-pointer">
          <div className="w-7 h-7 bg-brand-500/20 rounded-full flex items-center justify-center shrink-0">
            <span className="text-brand-400 text-xs font-bold">ن</span>
          </div>
          <div className="min-w-0">
            <p className="text-white text-xs font-medium truncate">متجر نهلة</p>
            <p className="text-slate-500 text-xs truncate">{t(tr => tr.nav.storeBadge.plan)}</p>
          </div>
        </div>
      </div>

    </aside>
  )
}
