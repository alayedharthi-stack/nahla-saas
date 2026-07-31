import {
  Megaphone,
  Gift,
  Tag,
  TrendingUp,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function MarketingHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.marketingHub)

  const items: HubCardItem[] = [
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
    },
    {
      to: '/coupons',
      icon: Tag,
      title: page.cards.coupons.title,
      description: page.cards.coupons.description,
    },
    {
      to: '/widgets',
      icon: TrendingUp,
      title: page.cards.widgets.title,
      description: page.cards.widgets.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
