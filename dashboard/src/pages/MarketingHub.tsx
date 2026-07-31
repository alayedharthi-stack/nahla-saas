import { Link } from 'react-router-dom'
import {
  Megaphone,
  Gift,
  Tag,
  TrendingUp,
  Bot,
  BookOpen,
  ChevronRight,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

const HUB_LINKS = [
  { key: 'campaigns' as const, to: '/campaigns', icon: Megaphone },
  { key: 'promotions' as const, to: '/promotions', icon: Gift },
  { key: 'coupons' as const, to: '/coupons', icon: Tag },
  { key: 'widgets' as const, to: '/widgets', icon: TrendingUp },
  { key: 'smartAutomations' as const, to: '/smart-automations', icon: Bot },
  { key: 'templateLibrary' as const, to: '/marketing/templates', icon: BookOpen },
] as const

export default function MarketingHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.marketingHub)

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {HUB_LINKS.map(({ key, to, icon: Icon }) => {
          const card = page.cards[key]
          return (
            <Link
              key={to}
              to={to}
              className="card p-5 hover:border-brand-200 hover:shadow-sm transition-all group"
            >
              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
                  <Icon className="w-5 h-5 text-slate-600" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <h2 className="text-sm font-semibold text-slate-900 group-hover:text-brand-600 transition-colors">
                      {card.title}
                    </h2>
                    <ChevronRight className="w-4 h-4 text-slate-300 group-hover:text-brand-400 shrink-0" />
                  </div>
                  <p className="text-xs text-slate-500 mt-1 leading-relaxed">{card.description}</p>
                </div>
              </div>
            </Link>
          )
        })}
      </div>
    </div>
  )
}
