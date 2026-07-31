import { Plug, Store, MessageCircle, HelpCircle } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function ChannelsHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.channelsHub)

  const items: HubCardItem[] = [
    {
      to: '/integrations',
      icon: Plug,
      title: page.cards.integrations.title,
      description: page.cards.integrations.description,
    },
    {
      to: '/store-integration',
      icon: Store,
      title: page.cards.storeIntegration.title,
      description: page.cards.storeIntegration.description,
    },
    {
      to: '/whatsapp-connect',
      icon: MessageCircle,
      title: page.cards.whatsappConnect.title,
      description: page.cards.whatsappConnect.description,
    },
    {
      to: '/help/whatsapp-manual-setup',
      icon: HelpCircle,
      title: page.cards.manualSetup.title,
      description: page.cards.manualSetup.description,
    },
    {
      to: '/sales-channels',
      icon: Store,
      title: page.cards.salesChannels.title,
      description: page.cards.salesChannels.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
