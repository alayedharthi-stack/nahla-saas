/**
 * Client-side "as Nahlah uses it" preview (PR-2).
 *
 * Mirrors backend visibility rules from tenant_overlay + core/knowledge
 * for merchant education only — does NOT drive runtime.
 */

import type { KnowledgeSection } from '../../api/knowledge'
import { PROMPT_FACT_GROUP_AR } from './kbUiCategories'
import {
  registryGroupForKind,
  sectionHasMetadataOnlyBody,
  sectionIsBehavioralKind,
} from './kbHeuristics'

export interface AiPreviewVerdict {
  active: boolean
  inPrompt: boolean
  channel: 'facts' | 'behavior' | 'none' | 'metadata_consumer'
  promptGroupLabel: string | null
  messages: string[]
  sensitiveOperational: boolean
  productScoped: boolean
  metadataOnlyHint: boolean
}

export function buildAiPreviewVerdict(section: KnowledgeSection): AiPreviewVerdict {
  const messages: string[] = []
  const kind = (section.kind || '').trim().toLowerCase()
  const active = section.is_active && !section.deleted_at
  const behavioral = sectionIsBehavioralKind(kind)
  const productScoped = (section.product_links?.length ?? 0) > 0
  const metadataOnly = sectionHasMetadataOnlyBody(section)
  const groupNum = registryGroupForKind(kind)

  if (!active) {
    messages.push('هذه المعلومة غير نشطة — لن تُستخدم في الردود.')
    return {
      active: false,
      inPrompt: false,
      channel: 'none',
      promptGroupLabel: null,
      messages,
      sensitiveOperational: false,
      productScoped,
      metadataOnlyHint: metadataOnly,
    }
  }

  if (behavioral && kind === 'escalation_rules') {
    messages.push(
      'هذه المعلومة نشطة وتُستخدم لقواعد التصعيد وسلوك المساعد — وليست حقائق تجارية.',
    )
    return {
      active: true,
      inPrompt: true,
      channel: 'behavior',
      promptGroupLabel: 'سلوك المساعد / التصعيد',
      messages,
      sensitiveOperational: true,
      productScoped,
      metadataOnlyHint: metadataOnly,
    }
  }

  if (behavioral) {
    messages.push(
      'هذه المعلومة نشطة وتُستخدم كقواعد سلوك للمساعد — وليست حقائق تجارية.',
    )
    return {
      active: true,
      inPrompt: true,
      channel: 'behavior',
      promptGroupLabel: 'سلوك المساعد',
      messages,
      sensitiveOperational: false,
      productScoped,
      metadataOnlyHint: metadataOnly,
    }
  }

  if (kind === 'goal_based_recommendation' || metadataOnly) {
    messages.push(
      'هذه المعلومة محفوظة كبيانات منظمة. قد لا تظهر نصّياً في الردود إلا عند الحاجة (مثل التوصيات حسب الهدف).',
    )
    if (active && !behavioral) {
      messages.push('قد تظهر أيضاً في الردود إذا كان النص غير فارغ.')
    }
    return {
      active: true,
      inPrompt: true,
      channel: metadataOnly ? 'metadata_consumer' : 'facts',
      promptGroupLabel: PROMPT_FACT_GROUP_AR[groupNum] ?? 'معلومات المتجر',
      messages,
      sensitiveOperational: false,
      productScoped,
      metadataOnlyHint: true,
    }
  }

  const groupLabel = PROMPT_FACT_GROUP_AR[groupNum] ?? 'معلومات المتجر'
  messages.push(`هذه المعلومة نشطة — قد تستخدمها نحلة في الردود ضمن قسم «${groupLabel}».`)

  if (productScoped) {
    messages.push(
      'قسم مرتبط بمنتج: قد يُقيد ظهوره عندما لا تكون المحادثة عن أحد المنتجات المربوطة.',
    )
  }

  if ((section.media_links?.length ?? 0) > 0) {
    const withKey = section.media_links.filter(l => l.media?.media_key)
    if (withKey.length > 0) {
      messages.push(
        `وسائط مرتبطة: قد تُرفق ${withKey.length} وسيط في الردود عند الحاجة.`,
      )
    } else {
      messages.push('وسائط مرتبطة — قد لا تُرفق في الردود بدون مفتاح وسائط.')
    }
  }

  return {
    active: true,
    inPrompt: true,
    channel: 'facts',
    promptGroupLabel: groupLabel,
    messages,
    sensitiveOperational: false,
    productScoped,
    metadataOnlyHint: false,
  }
}
