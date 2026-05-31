/**
 * Map stable campaign debug / send error keys → static UI labels.
 * Never pass API ``label_ar`` / ``error_label_ar`` through t() in EN.
 */
import type { Lang } from './types'
import type { CampaignsListLabels } from './campaignsListPageLabels'
import type { CampaignDebugSnapshot, CampaignRecord } from '../api/campaigns'

type Runtime = CampaignsListLabels['runtime']

export function campaignErrorLabel(
  code: string | null | undefined,
  labelAr: string | null | undefined,
  runtime: Runtime,
  lang: Lang,
): string {
  if (code && runtime.errorCodes[code]?.label) return runtime.errorCodes[code].label
  if (lang === 'ar' && labelAr) return labelAr
  return code || labelAr || runtime.errorCodes.unknown?.label || 'unknown'
}

export function campaignErrorAdvice(
  code: string | null | undefined,
  adviceAr: string | null | undefined,
  runtime: Runtime,
  lang: Lang,
): string | null {
  if (code && runtime.errorCodes[code]?.advice) return runtime.errorCodes[code].advice ?? null
  if (lang === 'ar' && adviceAr) return adviceAr
  return null
}

export function excludeReasonLabel(
  reasonKey: string,
  labelAr: string,
  runtime: Runtime,
  lang: Lang,
): string {
  if (runtime.excludeReasons[reasonKey]) return runtime.excludeReasons[reasonKey]
  if (lang === 'ar' && labelAr) return labelAr
  return reasonKey
}

export function campaignLastErrorDisplay(
  campaign: Pick<CampaignRecord, 'last_error_key' | 'last_error_ar' | 'last_error'>,
  runtime: Runtime,
  lang: Lang,
): string {
  const key = campaign.last_error_key
  if (key && runtime.errorCodes[key]?.label) return runtime.errorCodes[key].label
  if (lang === 'ar') return campaign.last_error_ar || campaign.last_error || ''
  return key || campaign.last_error || ''
}

export function lifecycleLabelFromList(
  lifecycleKey: string,
  statusKey: string,
  list: CampaignsListLabels,
): string {
  const lc = list.lifecycle[lifecycleKey as keyof typeof list.lifecycle]
  if (lc) return lc
  const st = list.status[statusKey as keyof typeof list.status]
  return st ?? list.status.draft
}

function severityIcon(severity: string): string {
  return severity === 'minor' ? 'ℹ️' : severity === 'major' ? '⚠️' : '⛔'
}

export function appendFrequencyCapDiagnostic(
  lines: string[],
  snap: CampaignDebugSnapshot,
  list: CampaignsListLabels,
  lang: Lang,
): void {
  const fc = snap.frequency_cap
  const dr = list.diagnostics.report
  if (!fc || fc.capped_count <= 0) return
  lines.push(
    dr.freqCapHeader
      .replace('{days}', String(fc.cap_days))
      .replace('{count}', String(fc.capped_count)),
  )
  if (fc.last_successful_sent_at) {
    let agg = dr.freqCapLatest.replace('{at}', fc.last_successful_sent_at)
    if (fc.last_successful_campaign_id != null) {
      agg += dr.freqCapCampaignSuffix.replace('{id}', String(fc.last_successful_campaign_id))
    }
    lines.push(agg)
  }
  const rows = (fc.frequency_cap_source_rows?.length ?? 0) > 0
    ? fc.frequency_cap_source_rows
    : fc.source_rows
  for (const row of rows || []) {
    const cid =
      row.last_successful_campaign_id != null
        ? `#${row.last_successful_campaign_id}`
        : '—'
    const ts = row.last_successful_sent_at ?? '—'
    lines.push(
      dr.freqCapRow
        .replace('{phone}', row.phone_masked)
        .replace('{at}', ts)
        .replace('{campaign}', cid),
    )
  }
  void lang
}

export function buildCampaignDiagnosticLines(
  snap: CampaignDebugSnapshot,
  audienceFallback: number,
  list: CampaignsListLabels,
  lang: Lang,
): string[] {
  const dr = list.diagnostics.report
  const rt = list.runtime
  const r = snap.recipients
  const total = r.total || audienceFallback || 0
  const skipped = r.skipped_duplicate + r.skipped_invalid +
                  r.skipped_unsubscribed + r.skipped_unreachable +
                  r.skipped_manual_exclusion
  const wa = snap.wa_connection
    ? `${snap.wa_connection.status} / ${snap.wa_connection.phone_number_id ?? '—'}`
    : rt.noConnection
  const tpl = snap.template
    ? `${snap.template.name} (${snap.template.status})`
    : rt.templateMissing
  const lines = [
    dr.sentSummary
      .replace('{sent}', String(r.sent))
      .replace('{total}', String(total))
      .replace('{failed}', r.failed > 0 ? dr.sentFailedSuffix.replace('{n}', String(r.failed)) : '')
      .replace('{skipped}', skipped > 0 ? dr.sentSkippedSuffix.replace('{n}', String(skipped)) : ''),
    dr.templateLine.replace('{tpl}', tpl),
    dr.whatsappLine.replace('{wa}', wa),
    dr.schedulerLine.replace(
      '{state}',
      snap.scheduler.campaign_dispatcher_enabled ? rt.schedulerOn : rt.schedulerOff,
    ),
  ]

  const f = snap.audience_funnel
  if (f && (f.raw_audience > 0 || f.materialized_rows > 0)) {
    lines.push(dr.funnelHeader)
    lines.push(dr.funnelRaw.replace('{n}', String(f.raw_audience)))
    lines.push(dr.funnelReachable.replace('{n}', String(f.after_reachable_filter)))
    lines.push(dr.funnelMaterialized.replace('{n}', String(f.materialized_rows)))
    if (f.queued_for_send > 0) {
      lines.push(dr.funnelQueued.replace('{n}', String(f.queued_for_send)))
    }
    if (f.frequency_cap_skipped > 0) {
      lines.push(dr.funnelFreqCap.replace('{n}', String(f.frequency_cap_skipped)))
    }
  }

  if ((snap.excluded_reasons_summary || []).length > 0) {
    lines.push(
      dr.excludedHeader.replace('{count}', String(snap.excluded_before_send_count)),
    )
    for (const ex of snap.excluded_reasons_summary) {
      const key = ex.skip_reason || ex.status || 'unknown'
      const label = excludeReasonLabel(key, ex.label_ar, rt, lang)
      lines.push(dr.excludedItem.replace('{label}', label).replace('{count}', String(ex.count)))
    }
  }

  const ds = snap.delivery_summary
  if (ds && ds.accepted_by_provider > 0) {
    lines.push(dr.deliveryHeader)
    lines.push(dr.deliveryAccepted.replace('{n}', String(ds.accepted_by_provider)))
    lines.push(dr.deliveryDelivered.replace('{n}', String(ds.delivered)))
    lines.push(dr.deliveryRead.replace('{n}', String(ds.read)))
    if (ds.failed_after_accept > 0) {
      lines.push(dr.deliveryFailedAfter.replace('{n}', String(ds.failed_after_accept)))
    }
    if (ds.unknown_delivery > 0) {
      lines.push(dr.deliveryUnknown.replace('{n}', String(ds.unknown_delivery)))
    }
    if (ds.missing_provider_message_id > 0) {
      lines.push(
        dr.deliveryMissingWamid.replace('{n}', String(ds.missing_provider_message_id)),
      )
    }
  }

  if ((snap.failure_summary || []).length > 0) {
    lines.push(dr.failureHeader)
    for (const fs of snap.failure_summary) {
      const label = campaignErrorLabel(fs.error_code, fs.error_label_ar, rt, lang)
      lines.push(`  ${severityIcon(fs.severity)} ${label} (${fs.count})`)
      const advice = campaignErrorAdvice(fs.error_code, fs.advice_ar, rt, lang)
      if (advice) lines.push(`     ↳ ${advice}`)
    }
  }

  appendFrequencyCapDiagnostic(lines, snap, list, lang)
  const hints = (snap.hints || []).join(' • ')
  if (hints) lines.push(`${dr.hintsPrefix} ${hints}`)
  return lines
}

export function buildDispatchPollLines(
  snap: CampaignDebugSnapshot,
  audienceFallback: number,
  list: CampaignsListLabels,
  lang: Lang,
): string[] {
  const dr = list.diagnostics.report
  const rt = list.runtime
  const r = snap.recipients
  const total = r.total || audienceFallback || 0
  const lifecycleLabel = lifecycleLabelFromList(
    snap.campaign.lifecycle,
    snap.campaign.status ?? 'draft',
    list,
  )
  const lines: string[] = [
    dr.pollSent.replace('{sent}', String(r.sent)).replace('{total}', String(total)),
  ]
  if (r.queued > 0) lines.push(dr.pollQueued.replace('{n}', String(r.queued)))
  if (r.failed > 0) lines.push(dr.pollFailed.replace('{n}', String(r.failed)))
  if ((snap.failure_summary || []).length > 0) {
    lines.push(dr.failureHeader)
    for (const fs of snap.failure_summary) {
      const label = campaignErrorLabel(fs.error_code, fs.error_label_ar, rt, lang)
      lines.push(`  ${severityIcon(fs.severity)} ${label} (${fs.count})`)
    }
  }
  if ((snap.excluded_reasons_summary || []).length > 0) {
    lines.push(dr.pollExcluded.replace('{n}', String(snap.excluded_before_send_count)))
    for (const ex of snap.excluded_reasons_summary) {
      const key = ex.skip_reason || ex.status || 'unknown'
      const label = excludeReasonLabel(key, ex.label_ar, rt, lang)
      lines.push(`  • ${label} (${ex.count})`)
    }
  }
  appendFrequencyCapDiagnostic(lines, snap, list, lang)
  lines.push(dr.pollLifecycle.replace('{label}', lifecycleLabel))
  return lines
}
