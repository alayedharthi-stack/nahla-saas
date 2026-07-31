import { Bot } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function AutomationHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.automationHub)

  const items: HubCardItem[] = [
    {
      to: '/smart-automations',
      icon: Bot,
      title: page.cards.smartAutomations.title,
      description: page.cards.smartAutomations.description,
    },
    {
      to: '/smart-automations',
      icon: Bot,
      title: page.cards.autopilot.title,
      description: page.cards.autopilot.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
