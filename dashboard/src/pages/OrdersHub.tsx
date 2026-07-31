import { ShoppingCart, Users } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function OrdersHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.ordersHub)

  const items: HubCardItem[] = [
    {
      to: '/orders',
      icon: ShoppingCart,
      title: page.cards.orders.title,
      description: page.cards.orders.description,
    },
    {
      to: '/customers',
      icon: Users,
      title: page.cards.customers.title,
      description: page.cards.customers.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
