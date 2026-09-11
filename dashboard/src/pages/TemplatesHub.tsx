import { Link } from 'react-router-dom'
import { ChevronLeft, FileText, Store, BookOpen } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { HubCardGrid, type HubCardItem } from '../components/ui/HubCardGrid'
import { useLanguage } from '../i18n/context'

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
      <Link
        to="/templates?library=nahla"
        className="group block rounded-2xl border border-[#e9dccd] bg-[#fdf9f4] px-6 py-5 shadow-sm transition-all hover:border-[#cfad8b] hover:shadow-md focus:outline-none focus:ring-2 focus:ring-[#c79264]/40"
      >
        <div className="flex items-center gap-5">
          <div className="w-[4.5rem] h-[4.5rem] rounded-2xl border border-[#e3cfba] bg-[#f8efe5] flex items-center justify-center shrink-0">
            <BookOpen className="w-9 h-9 text-[#8a5a32]" strokeWidth={1.65} />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-bold text-[#573921]">{page.library.title}</h2>
            <p className="text-sm text-[#836a54] mt-1 leading-relaxed">{page.library.description}</p>
          </div>
          <ChevronLeft className="w-5 h-5 text-[#9a7453] shrink-0 transition-transform group-hover:-translate-x-1" />
        </div>
      </Link>
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
