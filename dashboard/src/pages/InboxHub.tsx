import { MessageSquare, UserCheck } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function InboxHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.inboxHub)

  const items: HubCardItem[] = [
    {
      to: '/conversations',
      icon: MessageSquare,
      title: page.cards.conversations.title,
      description: page.cards.conversations.description,
    },
    {
      to: '/handoff-queue',
      icon: UserCheck,
      title: page.cards.handoffQueue.title,
      description: page.cards.handoffQueue.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
