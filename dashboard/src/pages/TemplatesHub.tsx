import { FileText, Store } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

/**
 * Templates Hub — two template types only.
 * «Nahla Template Library» is a source/filter inside each type, not a third hub card.
 */
export default function TemplatesHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.templatesHub)

  const items: HubCardItem[] = [
    {
      to: '/templates',
      icon: FileText,
      title: page.cards.whatsappTemplates.title,
      description: page.cards.whatsappTemplates.description,
    },
    {
      to: '/marketing/templates',
      icon: Store,
      title: page.cards.ecommerceTemplates.title,
      description: page.cards.ecommerceTemplates.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
