import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { CheckCircle, Clock, Package, RefreshCw, Send, Store } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import OrderUpdatesSettingsTab from '../components/settings/OrderUpdatesSettingsTab'
import {
  STATUS_COLORS,
  getBody,
  templatesApi,
  type TemplateStatus,
  type WhatsAppTemplateRecord,
} from '../api/templates'
import {
  ORDER_UPDATE_SERVICE_KEYS,
  isOrderUpdateServiceKey,
  orderUpdatesApi,
  serviceBodyText,
  type OrderUpdateServiceDetail,
  type OrderUpdateServiceKey,
} from '../api/orderUpdates'

type StoreFilter = 'all' | 'order_updates' | 'marketing'

type OrderUpdateCard = {
  serviceKey: OrderUpdateServiceKey
  body: string
  detail: OrderUpdateServiceDetail | null
}

/**
 * E-commerce store templates surface (organizational).
 *
 * Honest data contract (no invented store-template library):
 * - Order updates: only services in ORDER_UPDATE_SERVICE_KEYS via /order-updates/*
 * - Marketing / store-UI / custom store templates: not backed by a distinct
 *   store-template API today — show an explicit empty state, never nahlaLibrary
 *   WhatsApp MARKETING rows re-labeled as store templates.
 *
 * Order-update enablement, message text, revisions, and preview are rendered
 * inline on this page so merchants do not leave the store-template context.
 *
 * Open-window / Meta / Lifecycle send orchestration are out of scope here.
 */
export default function NahlaTemplateLibrary() {
  const { t, dir, lang } = useLanguage()
  const { hash, search } = useLocation()
  const page = t(tr => tr.pages.ecommerceTemplates)
  const isAr = lang === 'ar'
  const importedTemplateId = Number(new URLSearchParams(search).get('imported')) || null

  const [filter, setFilter] = useState<StoreFilter>('all')
  const [loading, setLoading] = useState(true)
  const [orderCards, setOrderCards] = useState<OrderUpdateCard[]>([])
  const [importedTemplates, setImportedTemplates] = useState<WhatsAppTemplateRecord[]>([])
  const [templateError, setTemplateError] = useState<string | null>(null)
  const [submittingId, setSubmittingId] = useState<number | null>(null)

  // Only expose filters with a real contract today:
  // - all / order_updates → ORDER_UPDATE_SERVICE_KEYS
  // - marketing → unsupported store category (empty only; no WhatsApp MARKETING reuse)
  const filters = useMemo(
    () =>
      [
        { key: 'all' as const, label: page.filters.all },
        { key: 'order_updates' as const, label: page.filters.orderUpdates },
        { key: 'marketing' as const, label: page.filters.marketing },
      ] satisfies { key: StoreFilter; label: string }[],
    [page.filters],
  )

  const serviceLabel = (key: OrderUpdateServiceKey): string => {
    return page.orderUpdates.services[key]
  }

  const serviceDescription = (key: OrderUpdateServiceKey): string => {
    return page.orderUpdates.serviceDescriptions[key]
  }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [details, records] = await Promise.all([
        Promise.all(
        ORDER_UPDATE_SERVICE_KEYS.map(async key => {
          try {
            const detail = await orderUpdatesApi.getService(key)
            return { serviceKey: key, body: serviceBodyText(detail), detail } satisfies OrderUpdateCard
          } catch {
            return { serviceKey: key, body: '', detail: null } satisfies OrderUpdateCard
          }
        }),
        ),
        templatesApi.list(),
      ])
      setOrderCards(details)
      setImportedTemplates(
        records.templates.filter(template => isOrderUpdateServiceKey(template.service_key ?? '')),
      )
      setTemplateError(null)
    } catch {
      setOrderCards(
        ORDER_UPDATE_SERVICE_KEYS.map(key => ({
          serviceKey: key,
          body: '',
          detail: null,
        })),
      )
      setImportedTemplates([])
      setTemplateError(isAr
        ? 'تعذر تحميل القوالب المستوردة الآن. حدّث الصفحة للمحاولة مرة أخرى.'
        : 'Imported templates could not be loaded. Refresh to try again.')
    } finally {
      setLoading(false)
    }
  }, [isAr])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (hash === '#ecommerce' || hash === '#order-updates' || hash === '#imported-store-templates') {
      setFilter('order_updates')
      requestAnimationFrame(() => {
        document.getElementById(hash.slice(1))?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        })
      })
    }
  }, [hash])

  useEffect(() => {
    if (!importedTemplateId || loading) return
    requestAnimationFrame(() => {
      document.getElementById(`imported-template-${importedTemplateId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      })
    })
  }, [importedTemplateId, loading])

  const templateStatusLabel = (status: TemplateStatus) => {
    if (status === 'DRAFT') return isAr ? 'مسودة — جاهز للإرسال إلى Meta' : 'Draft — ready to submit to Meta'
    if (status === 'PENDING') return isAr ? 'قيد مراجعة Meta' : 'Under Meta review'
    if (status === 'APPROVED') return isAr ? 'معتمد من Meta' : 'Meta approved'
    if (status === 'REJECTED') return isAr ? 'مرفوض من Meta' : 'Rejected by Meta'
    return status
  }

  const handleSubmit = async (template: WhatsAppTemplateRecord) => {
    setSubmittingId(template.id)
    setTemplateError(null)
    try {
      const result = await templatesApi.submit(template.id)
      setImportedTemplates(current => current.map(item => item.id === template.id ? result.template : item))
    } catch (error) {
      setTemplateError(error instanceof Error ? error.message : (isAr
        ? 'تعذر إرسال القالب إلى Meta.'
        : 'The template could not be submitted to Meta.'))
    } finally {
      setSubmittingId(null)
    }
  }

  const showOrderUpdates =
    filter === 'all' || filter === 'order_updates'
  const showMarketingEmpty = filter === 'marketing'
  const showAllUnsupportedNote = filter === 'all'

  return (
    <div className="space-y-6" dir={dir}>
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <div className="flex flex-wrap gap-2">
        {filters.map(item => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              filter === item.key
                ? 'bg-brand-500 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {showOrderUpdates && (
        <section id="order-updates" className="card p-5 scroll-mt-24">
          <div className="flex items-start gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
              <Package className="w-5 h-5 text-slate-600" />
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-sm font-semibold text-slate-900">{page.orderUpdates.title}</h2>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                {page.orderUpdates.description}
              </p>
              <p className="text-[11px] text-slate-400 mt-2">{page.orderUpdates.scopeNote}</p>
            </div>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <RefreshCw className="w-6 h-6 text-amber-500 animate-spin" />
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {orderCards.map(card => (
                <div
                  key={card.serviceKey}
                  className="border border-slate-200 rounded-xl p-4 bg-white"
                >
                  <p className="text-[10px] font-semibold text-slate-500 mb-1">
                    {card.serviceKey}
                  </p>
                  <p className="text-sm font-semibold text-slate-900">
                    {serviceLabel(card.serviceKey)}
                  </p>
                  <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                    {serviceDescription(card.serviceKey)}
                  </p>
                  {card.body ? (
                    <p className="text-[11px] text-slate-400 mt-2 leading-relaxed line-clamp-3 whitespace-pre-line">
                      {card.body}
                    </p>
                  ) : (
                    <p className="text-[11px] text-slate-400 mt-2">
                      {page.orderUpdates.noPreview}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {showOrderUpdates && (
        <section id="imported-store-templates" className="card p-5 scroll-mt-24">
          <div className="flex items-start gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center shrink-0">
              <Store className="w-5 h-5 text-amber-700" />
            </div>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-slate-900">
                {isAr ? 'قوالب المتجر المستوردة' : 'Imported store templates'}
              </h2>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                {isAr
                  ? 'هذه هي القوالب التي استوردتها من مكتبة قوالب نحلة. تظهر هنا حالتها الحقيقية لدى Meta، وليست مجرد إعدادات الخدمة.'
                  : 'These are the templates imported from Nahla’s library. Their real Meta status appears here, not only the service settings.'}
              </p>
            </div>
          </div>

          {templateError && (
            <p className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{templateError}</p>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-8"><RefreshCw className="w-5 h-5 text-amber-500 animate-spin" /></div>
          ) : importedTemplates.length === 0 ? (
            <p className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
              {isAr ? 'لم تستورد أي قالب متجر بعد. افتح مكتبة قوالب نحلة واختر «استيراد وتخصيص».' : 'No store template has been imported yet. Open Nahla’s template library and choose “Import & customize”.'}
            </p>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {importedTemplates.map(template => {
                const header = template.components.find(component => component.type === 'HEADER')
                const imageUrl = header?.format === 'IMAGE' ? header.example?.header_url?.trim() : null
                const highlighted = template.id === importedTemplateId
                const statusColor = STATUS_COLORS[template.status] ?? 'slate'
                const statusClasses = statusColor === 'green'
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : statusColor === 'amber'
                  ? 'bg-amber-50 text-amber-800 border-amber-200'
                  : statusColor === 'red'
                  ? 'bg-red-50 text-red-700 border-red-200'
                  : 'bg-slate-50 text-slate-600 border-slate-200'
                return (
                  <article
                    id={`imported-template-${template.id}`}
                    key={template.id}
                    className={`overflow-hidden rounded-xl border bg-white ${highlighted ? 'border-amber-400 ring-2 ring-amber-100' : 'border-slate-200'}`}
                  >
                    {imageUrl && <img src={imageUrl} alt="" className="h-28 w-full object-cover" />}
                    <div className="p-4">
                      {highlighted && (
                        <p className="mb-2 text-[11px] font-semibold text-amber-700">{isAr ? 'القالب الذي استوردته الآن' : 'The template you just imported'}</p>
                      )}
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-semibold text-slate-900">{isAr ? (template.display_name_ar || template.name) : template.name}</p>
                          <p className="mt-1 text-[11px] font-mono text-slate-400">{template.service_key}</p>
                        </div>
                        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-medium ${statusClasses}`}>
                          {template.status === 'APPROVED' ? <CheckCircle className="w-3 h-3" /> : <Clock className="w-3 h-3" />}
                          {templateStatusLabel(template.status)}
                        </span>
                      </div>
                      <p className="mt-3 line-clamp-3 whitespace-pre-line text-xs leading-relaxed text-slate-500">{getBody(template) || (isAr ? 'لا يوجد نص معاينة بعد.' : 'No preview text yet.')}</p>
                      {template.submittable && (
                        <button
                          type="button"
                          onClick={() => void handleSubmit(template)}
                          disabled={submittingId === template.id}
                          className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-brand-500 px-3 py-2 text-xs font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {submittingId === template.id ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                          {submittingId === template.id ? (isAr ? 'جارٍ الإرسال...' : 'Submitting…') : (isAr ? 'إرسال إلى Meta' : 'Submit to Meta')}
                        </button>
                      )}
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      )}

      {showOrderUpdates && (
        <section id="order-update-settings" className="space-y-4 scroll-mt-24">
          <div className="card px-5 py-4">
            <h2 className="text-sm font-semibold text-slate-900">{page.orderUpdates.opsLink}</h2>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">{page.orderUpdates.scopeNote}</p>
          </div>
          <OrderUpdatesSettingsTab />
        </section>
      )}

      <section id="ecommerce" className="card p-5 scroll-mt-24">
        <div className="flex items-start gap-3 mb-3">
          <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center shrink-0">
            <Store className="w-5 h-5 text-amber-600" />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">{page.libraryTitle}</h2>
            <p className="text-xs text-slate-500 mt-1">{page.librarySubtitle}</p>
          </div>
        </div>

        {showMarketingEmpty && (
          <p className="text-center text-slate-400 text-sm py-10">{page.empty.marketing}</p>
        )}
        {showAllUnsupportedNote && !showMarketingEmpty && (
          <p className="text-xs text-slate-500 leading-relaxed bg-slate-50 border border-slate-100 rounded-lg px-3 py-2">
            {page.empty.unsupportedStoreLibrary}
          </p>
        )}
        {!showMarketingEmpty && !showAllUnsupportedNote && filter === 'order_updates' && (
          <p className="text-xs text-slate-400 leading-relaxed">
            {page.empty.orderUpdatesOnlyHint}
          </p>
        )}
      </section>
    </div>
  )
}
