import { FileText, Store, BookOpen } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'
import { NahlaLibraryModal } from './Templates'

/**
 * Templates Hub — shared library first, then the two template areas.
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
    <div className="space-y-6">
      <PageHeader title={page.title} subtitle={page.subtitle} />
      <section className="card overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-100 bg-amber-50/40 flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center shrink-0">
            <BookOpen className="w-5 h-5 text-amber-600" />
          </div>
          <div>
            <h2 className="text-sm font-semibold text-slate-900">{page.library.title}</h2>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">{page.library.description}</p>
          </div>
        </div>
        <div className="p-4">
          <NahlaLibraryModal embedded onClose={() => undefined} onImported={() => undefined} />
        </div>
      </section>
      <section>
        <div className="mb-3">
          <h2 className="text-sm font-semibold text-slate-900">{page.sections.title}</h2>
          <p className="text-xs text-slate-500 mt-1 leading-relaxed">{page.sections.description}</p>
        </div>
        <HubCardGrid items={items} />
      </section>
    </div>
  )
}
