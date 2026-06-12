import type { KnowledgeSection } from '../../api/knowledge'
import {
  sectionIsDraftLike,
  sectionNeedsReview,
  sectionHasSensitiveOperational,
} from './kbHeuristics'

export interface KbPageStats {
  activeCount: number
  draftCount: number
  needsReviewCount: number
  warningCount: number
  lastUpdated: string | null
}

export function computeKbPageStats(sections: KnowledgeSection[]): KbPageStats {
  let activeCount = 0
  let draftCount = 0
  let needsReviewCount = 0
  let warningCount = 0
  let lastUpdated: string | null = null

  for (const s of sections) {
    if (s.is_active && !s.deleted_at) activeCount += 1
    if (sectionIsDraftLike(s)) draftCount += 1
    if (sectionNeedsReview(s)) needsReviewCount += 1
    if (sectionHasSensitiveOperational(s) && s.is_active) warningCount += 1
    if (s.updated_at) {
      if (!lastUpdated || s.updated_at > lastUpdated) lastUpdated = s.updated_at
    }
  }

  return { activeCount, draftCount, needsReviewCount, warningCount, lastUpdated }
}

export function formatKbDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('ar-SA', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}
