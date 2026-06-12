/**
 * Client-side "as AI sees it" preview (PR-2).
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
    messages.push('هذه المعلومة غير نشطة، ولن يستخدمها الذكاء.')
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
      'هذه المعلومة نشطة وستُحقَن في طبقة سلوك/سياسة عالية الأولوية — وليس في facts block.',
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
      'هذه المعلومة نشطة وتُحقَن كقواعد سلوك في الطبقة عالية الأولوية — ليست حقائق تجارية.',
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
      'هذه المعلومة محفوظة كبيانات منظمة (metadata). قد لا تظهر نصياً في facts block إلا عبر consumer مخصص (مثل توصيات حسب الهدف).',
    )
    if (active && !behavioral) {
      messages.push('العنوان والنص قد يظهران أيضاً في facts block إذا كان النص غير فارغ.')
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
  messages.push(`هذه المعلومة نشطة وستظهر للذكاء ضمن قسم «${groupLabel}».`)

  if (productScoped) {
    messages.push(
      'قسم مرتبط بمنتج: قد يُقيد ظهوره عندما لا يكون المحادثة عن أحد المنتجات المربوطة.',
    )
  }

  if ((section.media_links?.length ?? 0) > 0) {
    const withKey = section.media_links.filter(l => l.media?.media_key)
    if (withKey.length > 0) {
      messages.push(
        `وسائط مرتبطة: سيُعرَض ${withKey.length} marker [MEDIA_KEY:…] في facts block عند وجود media_key.`,
      )
    } else {
      messages.push('وسائط مرتبطة بدون media_key — قد لا تظهر كـ marker في facts block.')
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
