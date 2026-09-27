import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import {
  campaignsApi, extractVariables, getTemplateBody, getTemplateFooter, getTemplateHeader, renderTemplate,
  type CampaignContentRevision, type CampaignRecord, type UpdateCampaignContentPayload, type WaTemplate,
} from '../../api/campaigns'
import { campaignHeroLabels, fill } from '../../i18n/campaignHeroLabels'
import type { Lang } from '../../i18n/types'
import { fmtCount, formatUtcTime, isoToLocalInput, localInputToIso } from './campaignFormat'

interface Props {
  campaign: CampaignRecord
  lang: Lang
  dir: 'rtl' | 'ltr'
  onClose: () => void
  /** Called with the updated campaign after a version was saved. */
  onSaved: (c: CampaignRecord) => void
  /** Save then resume, in that order. */
  onSaveAndResume: (c: CampaignRecord) => Promise<void>
}

function previewDir(language: string | null | undefined): 'rtl' | 'ltr' {
  const lc = (language || '').toLowerCase()
  return lc === 'en' || lc.startsWith('en_') ? 'ltr' : 'rtl'
}

export default function EditCampaignContentModal({ campaign, lang, dir, onClose, onSaved, onSaveAndResume }: Props) {
  const L = campaignHeroLabels(lang).edit
  const [templates, setTemplates] = useState<WaTemplate[]>([])
  const [revisions, setRevisions] = useState<CampaignContentRevision[]>([])
  const [currentRevision, setCurrentRevision] = useState<number>(campaign.content_revision ?? 1)
  const [loading, setLoading] = useState(true)
  const [templateId, setTemplateId] = useState<string>(campaign.template_id)
  const [vars, setVars] = useState<Record<string, string>>({ ...(campaign.template_variables || {}) })
  const [coupon, setCoupon] = useState<string>(campaign.coupon_code || '')
  const [expiry, setExpiry] = useState<string>(isoToLocalInput(campaign.offer_expires_at))
  const [noExpiry, setNoExpiry] = useState<boolean>(!campaign.offer_expires_at)
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [t, r] = await Promise.all([campaignsApi.getTemplates(), campaignsApi.revisions(campaign.id)])
        if (cancelled) return
        setTemplates(t.templates.filter(x => x.status === 'APPROVED'))
        setRevisions(r.revisions)
        setCurrentRevision(r.current_revision)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [campaign.id])

  const selected = useMemo(() => templates.find(t => t.id === templateId) ?? null, [templates, templateId])
  const body = selected ? getTemplateBody(selected) : (campaign.template_body || '')
  const header = selected ? getTemplateHeader(selected) : ''
  const footer = selected ? getTemplateFooter(selected) : ''
  const placeholders = useMemo(() => extractVariables(body), [body])
  const preview = renderTemplate(body, vars)
  const language = selected?.language || campaign.template_language

  const buildPayload = (): UpdateCampaignContentPayload => {
    const payload: UpdateCampaignContentPayload = {}
    if (templateId !== campaign.template_id) payload.template_id = templateId
    const publicVars: Record<string, string> = {}
    for (const k of placeholders) publicVars[k] = vars[k] ?? ''
    payload.template_variables = publicVars
    payload.coupon_code = coupon.trim()
    if (noExpiry) payload.clear_offer_expiry = true
    else {
      const iso = localInputToIso(expiry)
      if (iso) payload.offer_expires_at = iso
    }
    if (note.trim()) payload.note = note.trim()
    return payload
  }

  const mapError = (e: unknown): string => {
    const msg = e instanceof Error ? e.message : String(e)
    if (/campaign_active/.test(msg)) return L.errorActive
    if (/worker_running/.test(msg)) return L.errorWorker
    if (/لا يوجد تغيير|422/.test(msg) && !/template_invalid/.test(msg)) return L.errorNoChange
    return fill(L.errorGeneric, { msg })
  }

  const save = async (thenResume: boolean) => {
    setSaving(true)
    setError(null)
    setSavedMsg(null)
    try {
      const updated = await campaignsApi.updateContent(campaign.id, buildPayload())
      onSaved(updated)
      setCurrentRevision(updated.content_revision ?? currentRevision + 1)
      try {
        const r = await campaignsApi.revisions(campaign.id)
        setRevisions(r.revisions)
      } catch { /* history is informational */ }
      if (thenResume) {
        await onSaveAndResume(updated)
        onClose()
        return
      }
      setSavedMsg(`${L.saved} ${L.savedResumeHint}`)
      setNote('')
    } catch (e) {
      setError(mapError(e))
    } finally {
      setSaving(false)
    }
  }

  const inputCls = 'w-full text-sm rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-3 py-2 text-slate-800 dark:text-slate-100'

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="edit-campaign-title">
      <div dir={dir} className="bg-white dark:bg-slate-900 w-full sm:max-w-3xl sm:rounded-2xl rounded-t-2xl shadow-xl max-h-[92vh] flex flex-col">
        <div className="px-5 py-4 border-b border-slate-100 dark:border-slate-700 flex items-start justify-between gap-3">
          <div>
            <h3 id="edit-campaign-title" className="font-semibold text-slate-900 dark:text-slate-100">{L.title}</h3>
            <p className="text-xs text-slate-500 mt-0.5">{L.subtitle}</p>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-700 p-1" aria-label={L.cancel}><X className="w-4 h-4" /></button>
        </div>

        <div className="p-5 overflow-y-auto space-y-5">
          {loading ? (
            <p className="text-sm text-slate-500">{L.loading}</p>
          ) : (
            <div className="grid gap-5 md:grid-cols-2">
              <div className="space-y-4">
                <label className="block text-xs text-slate-600 dark:text-slate-300 space-y-1">
                  <span className="font-medium">{L.template}</span>
                  <select className={inputCls} value={templateId} onChange={e => setTemplateId(e.target.value)}>
                    {!selected && <option value={campaign.template_id}>{campaign.template_name}</option>}
                    {templates.map(t => <option key={t.id} value={t.id}>{t.name} · {t.language}</option>)}
                  </select>
                  <span className="text-[11px] text-slate-400">{L.templateHint}</span>
                </label>
                {placeholders.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-xs font-medium text-slate-600 dark:text-slate-300">{L.variables}</p>
                    {placeholders.map(ph => (
                      <label key={ph} className="block text-xs text-slate-500 space-y-1">
                        {fill(L.variableLabel, { n: ph.replace(/\D/g, '') })}
                        <input className={inputCls} value={vars[ph] ?? ''} onChange={e => setVars(v => ({ ...v, [ph]: e.target.value }))} />
                      </label>
                    ))}
                  </div>
                )}
                <label className="block text-xs text-slate-600 dark:text-slate-300 space-y-1">
                  <span className="font-medium">{L.coupon}</span>
                  <input className={inputCls} value={coupon} onChange={e => setCoupon(e.target.value)} />
                  <span className="text-[11px] text-slate-400">{L.couponHint}</span>
                </label>
                <div className="space-y-1 text-xs text-slate-600 dark:text-slate-300">
                  <span className="font-medium">{L.expiry}</span>
                  <input type="datetime-local" className={inputCls} value={expiry} disabled={noExpiry}
                    onChange={e => setExpiry(e.target.value)} />
                  <label className="inline-flex items-center gap-2 text-[11px] text-slate-500">
                    <input type="checkbox" checked={noExpiry} onChange={e => setNoExpiry(e.target.checked)} /> {L.clearExpiry}
                  </label>
                  <p className="text-[11px] text-slate-400">{L.expiryHint}</p>
                </div>
                <label className="block text-xs text-slate-600 dark:text-slate-300 space-y-1">
                  <span className="font-medium">{L.note}</span>
                  <input className={inputCls} value={note} placeholder={L.notePlaceholder} onChange={e => setNote(e.target.value)} />
                </label>
              </div>

              <div className="space-y-4">
                <div>
                  <p className="text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">{L.preview}</p>
                  <div className="bg-[#e5ddd5] rounded-xl p-4 min-h-32 flex items-end">
                    <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs p-3 text-sm space-y-1" dir={previewDir(language)}>
                      {header && <p className="font-semibold text-slate-900 text-xs">{header}</p>}
                      <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">{preview}</p>
                      {coupon.trim() && <p className="text-[11px] text-slate-600 font-mono">{coupon.trim()}</p>}
                      {footer && <p className="text-slate-400 text-[10px] mt-1">{footer}</p>}
                    </div>
                  </div>
                  <p className="text-[11px] text-slate-400 mt-1">{L.previewNote}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">{L.history}</p>
                  <ul className="space-y-1.5">
                    {revisions.slice().reverse().map(r => (
                      <li key={r.revision_no} className="rounded-lg border border-slate-100 dark:border-slate-700 p-2 text-[11px] text-slate-600 dark:text-slate-300">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-medium">
                            {fill(L.revision, { n: r.revision_no })}
                            {r.revision_no === currentRevision && <span className="ms-1 text-emerald-600">· {L.current}</span>}
                          </span>
                          <span className="text-slate-400">{formatUtcTime(r.created_at, lang)}</span>
                        </div>
                        <p className="text-slate-500 mt-0.5 truncate">{r.template_name}{r.coupon_code ? ` · ${r.coupon_code}` : ''}{r.offer_expires_at ? ` · ${formatUtcTime(r.offer_expires_at, lang)}` : ''}</p>
                        <p className="text-slate-400 mt-0.5">
                          {r.sends && r.sends.attempted > 0
                            ? fill(L.sends, { accepted: fmtCount(r.sends.accepted, lang), delivered: fmtCount(r.sends.delivered, lang), read: fmtCount(r.sends.read, lang) })
                            : L.noSends}
                        </p>
                        {r.note && <p className="text-slate-500 mt-0.5 italic">{r.note}</p>}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )}
          {error && <p className="text-xs text-red-600" role="alert">{error}</p>}
          {savedMsg && <p className="text-xs text-emerald-700">{savedMsg}</p>}
        </div>

        <div className="px-5 py-4 border-t border-slate-100 dark:border-slate-700 flex flex-wrap justify-end gap-2">
          <button type="button" onClick={onClose} className="text-sm px-4 py-2 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50">{L.cancel}</button>
          <button type="button" disabled={saving || loading} onClick={() => save(false)}
            className="text-sm px-4 py-2 rounded-lg border border-brand-500 text-brand-600 hover:bg-brand-50 disabled:opacity-50">
            {saving ? L.saving : L.save}
          </button>
          <button type="button" disabled={saving || loading} onClick={() => save(true)} className="btn-primary text-sm disabled:opacity-50">
            {saving ? L.saving : L.saveAndResume}
          </button>
        </div>
      </div>
    </div>
  )
}
