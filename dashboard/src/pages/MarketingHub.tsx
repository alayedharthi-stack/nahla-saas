import {
  Megaphone,
  Gift,
  Tag,
  TrendingUp,
  Bot,
  BookOpen,
  FileText,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, HubSectionHeading, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function MarketingHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.marketingHub)

  const operationalItems: HubCardItem[] = [
    {
      to: '/campaigns',
      icon: Megaphone,
      title: page.cards.campaigns.title,
      description: page.cards.campaigns.description,
    },
    {
      to: '/promotions',
      icon: Gift,
      title: page.cards.promotions.title,
      description: page.cards.promotions.description,
      isAI: true,
    },
    {
      to: '/coupons',
      icon: Tag,
      title: page.cards.coupons.title,
      description: page.cards.coupons.description,
      isAI: true,
    },
    {
      to: '/widgets',
      icon: TrendingUp,
      title: page.cards.widgets.title,
      description: page.cards.widgets.description,
    },
    {
      to: '/smart-automations',
      icon: Bot,
      title: page.cards.smartAutomations.title,
      description: page.cards.smartAutomations.description,
      isAI: true,
    },
  ]

  const templateItems: HubCardItem[] = [
    {
      to: '/marketing/templates',
      icon: BookOpen,
      title: page.cards.templateLibrary.title,
      description: page.cards.templateLibrary.description,
    },
    {
      to: '/templates',
      icon: FileText,
      title: page.cards.whatsappTemplates.title,
      description: page.cards.whatsappTemplates.description,
    },
  ]

  return (
    <div className="space-y-8">
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <section>
        <HubCardGrid items={operationalItems} />
      </section>

      <section>
        <HubSectionHeading
          title={page.sections.templates.title}
          description={page.sections.templates.description}
        />
        <HubCardGrid items={templateItems} />
      </section>
    </div>
  )
}
