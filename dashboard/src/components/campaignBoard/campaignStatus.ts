import type { CampaignRecord } from '../../api/campaigns'
import type { CampaignsListLabels } from '../../i18n/campaignsListPageLabels'
import type { CampaignHeroLabels } from '../../i18n/campaignHeroLabels'
import { fill } from '../../i18n/campaignHeroLabels'
import type { Lang } from '../../i18n/types'
import { formatUtcTime } from './campaignFormat'

export type StatusTone = 'calm' | 'action' | 'neutral' | 'done'

export interface StatusDescription {
  /** One calm sentence about what is happening. */
  text: string
  /** Optional second line: the next known check time, or the action to take. */
  detail: string | null
  tone: StatusTone
  /** True when the platform continues this campaign on its own. */
  autoResume: boolean
}

/**
 * Turn the platform's structured state (lifecycle + status_explanation +
 * lease facts) into the sentence the merchant reads. Normal waiting is
 * never phrased as an error; a red tone is reserved for states that need
 * the merchant, with the reason and the action.
 */
export function describeCampaignStatus(
  c: CampaignRecord,
  labels: CampaignHeroLabels,
  list: CampaignsListLabels,
  lang: Lang,
): StatusDescription {
  const st = labels.hero.status
  const lc = c.lifecycle || c.status
  const ex = c.status_explanation
  const pauseReason = c.pause_reason || c.execution?.pause_reason || null
  const nextCheck = ex?.next_check_at ? formatUtcTime(ex.next_check_at, lang) : null

  switch (lc) {
    case 'sending':
      return { text: st.sendingNow, detail: null, tone: 'calm', autoResume: true }
    case 'pending_dispatch':
      return { text: st.pendingStart, detail: null, tone: 'calm', autoResume: true }
    case 'waiting_scheduler':
      return { text: st.waitingScheduler, detail: null, tone: 'calm', autoResume: true }
    case 'stalled':
      return {
        text: st.stalledRecovering,
        detail: nextCheck ? fill(st.nextCheck, { time: nextCheck }) : st.nextCheckUnknown,
        tone: 'calm', autoResume: true,
      }
    case 'waiting_for_capacity': {
      let detail = st.capacityWaitUnknown
      if (nextCheck) detail = fill(ex?.next_check_exact ? st.capacityWaitExactAt : st.capacityWaitAt, { time: nextCheck })
      return { text: st.capacityWait, detail, tone: 'calm', autoResume: true }
    }
    case 'rate_limit_backoff':
      return {
        text: nextCheck ? fill(st.rateLimitAt, { time: nextCheck }) : st.rateLimitUnknown,
        detail: null, tone: 'calm', autoResume: true,
      }
    case 'offer_expired':
      return { text: st.offerExpired, detail: st.offerExpiredAction, tone: 'action', autoResume: false }
    case 'needs_review': {
      const reasonKey = pauseReason === 'provider_throttling' ? 'marketing_blocked' : (pauseReason || '')
      const reason = list.sendHealth.pauseReasons[reasonKey] || c.pause_reason_ar || null
      return { text: st.needsReview, detail: reason, tone: 'action', autoResume: false }
    }
    case 'provider_throttled': {
      const until = c.throttle?.clears_at ? fill(st.providerThrottledUntil, { time: formatUtcTime(c.throttle.clears_at, lang) }) : null
      return { text: st.providerThrottled, detail: until, tone: 'action', autoResume: false }
    }
    case 'paused': {
      if (pauseReason === 'merchant_stop' || !pauseReason) {
        return { text: st.merchantPaused, detail: null, tone: 'neutral', autoResume: false }
      }
      if (pauseReason === 'content_revised') {
        return { text: st.contentRevised, detail: null, tone: 'neutral', autoResume: false }
      }
      const reason = list.sendHealth.pauseReasons[pauseReason] || c.pause_reason_ar || pauseReason
      return { text: st.reviewGeneric, detail: reason, tone: 'action', autoResume: false }
    }
    case 'sent':
    case 'partial':
    case 'partial_minor':
    case 'completed_empty':
      return { text: st.completed, detail: null, tone: 'done', autoResume: false }
    default: {
      const label = list.lifecycle[lc as keyof typeof list.lifecycle]
      const tone: StatusTone = ex?.tone === 'action' || c.status === 'failed' ? 'action' : 'neutral'
      return { text: label || lc, detail: null, tone, autoResume: !!ex?.auto_resume }
    }
  }
}
