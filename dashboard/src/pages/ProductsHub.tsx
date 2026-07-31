import { Package, FolderTree } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function ProductsHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.productsHub)

  const items: HubCardItem[] = [
    {
      to: '/catalog',
      icon: Package,
      title: page.cards.catalog.title,
      description: page.cards.catalog.description,
    },
    {
      to: '/catalog-intelligence',
      icon: FolderTree,
      title: page.cards.catalogIntelligence.title,
      description: page.cards.catalogIntelligence.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
