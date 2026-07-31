import { BookOpen, FileText } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

export default function TemplatesHub() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.templatesHub)

  const items: HubCardItem[] = [
    {
      to: '/marketing/templates',
      icon: BookOpen,
      title: page.cards.nahlaLibrary.title,
      description: page.cards.nahlaLibrary.description,
    },
    {
      to: '/templates',
      icon: FileText,
      title: page.cards.whatsappTemplates.title,
      description: page.cards.whatsappTemplates.description,
    },
  ]

  return (
    <div>
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <HubCardGrid items={items} />
    </div>
  )
}
