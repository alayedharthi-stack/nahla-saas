import { useState } from 'react'
import {
  Settings,
  ShieldCheck,
  CreditCard,
  Brain,
  BookOpen,
  Activity,
  Gauge,
  BrainCircuit,
  BarChart2,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, HubSectionHeading, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function SettingsHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.settingsHub)
  const [advancedExpanded, setAdvancedExpanded] = useState(false)

  const coreItems: HubCardItem[] = [
    {
      to: '/settings',
      icon: Settings,
      title: page.cards.general.title,
      description: page.cards.general.description,
    },
    {
      to: '/settings/security',
      icon: ShieldCheck,
      title: page.cards.security.title,
      description: page.cards.security.description,
    },
    {
      to: '/billing',
      icon: CreditCard,
      title: page.cards.billing.title,
      description: page.cards.billing.description,
    },
  ]

  const nahlaSmartItems: HubCardItem[] = [
    {
      to: '/intelligence',
      icon: Brain,
      title: page.cards.intelligence.title,
      description: page.cards.intelligence.description,
      isAI: true,
    },
    {
      to: '/knowledge-base',
      icon: BookOpen,
      title: page.cards.knowledgeBase.title,
      description: page.cards.knowledgeBase.description,
      isAI: true,
    },
  ]

  const advancedItems: HubCardItem[] = [
    {
      to: '/system-status',
      icon: Activity,
      title: page.cards.systemStatus.title,
      description: page.cards.systemStatus.description,
    },
    {
      to: '/delivery-quality',
      icon: Gauge,
      title: page.cards.deliveryQuality.title,
      description: page.cards.deliveryQuality.description,
    },
    {
      to: '/ai-sales-logs',
      icon: BrainCircuit,
      title: page.cards.salesAgent.title,
      description: page.cards.salesAgent.description,
      isAI: true,
    },
    {
      to: '/analytics',
      icon: BarChart2,
      title: page.cards.analytics.title,
      description: page.cards.analytics.description,
      isAI: true,
    },
  ]

  return (
    <div className="space-y-8">
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <section>
        <HubSectionHeading title={page.sections.core.title} description={page.sections.core.description} />
        <HubCardGrid items={coreItems} />
      </section>

      <section>
        <HubSectionHeading
          title={page.sections.nahlaSmart.title}
          description={page.sections.nahlaSmart.description}
        />
        <HubCardGrid items={nahlaSmartItems} />
      </section>

      <section>
        <button
          type="button"
          onClick={() => setAdvancedExpanded(prev => !prev)}
          className="flex w-full items-center gap-2 text-left mb-3 group"
          aria-expanded={advancedExpanded}
        >
          {advancedExpanded
            ? <ChevronDown className="w-4 h-4 text-slate-400 shrink-0" />
            : <ChevronRight className="w-4 h-4 text-slate-400 shrink-0" />}
          <div>
            <h2 className="text-sm font-semibold text-slate-900 group-hover:text-brand-600 transition-colors">
              {page.sections.advanced.title}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">{page.sections.advanced.description}</p>
          </div>
        </button>
        {advancedExpanded && <HubCardGrid items={advancedItems} />}
      </section>
    </div>
  )
}
