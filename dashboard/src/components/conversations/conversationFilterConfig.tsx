import {
  Ban, BellOff, Megaphone, PackageCheck, Pause,
} from 'lucide-react'
import type { ReactNode } from 'react'

import type { DashboardConversation } from '../../api/featureReality'

export type ConversationFilter =
  | 'all' | 'active' | 'human' | 'agent_req' | 'paused' | 'blocked'
  | 'paid' | 'unsubscribed' | 'campaign_excluded' | 'closed'

export const CONVERSATION_FILTER_KEYS: ConversationFilter[] = [
  'all', 'active', 'human', 'agent_req', 'paused', 'blocked',
  'paid', 'unsubscribed', 'campaign_excluded', 'closed',
]

export interface ConversationFilterHelpers {
  isHumanResponding: (c: DashboardConversation) => boolean
  isAwaitingAgent: (c: DashboardConversation) => boolean
  isAIPausedOnly: (c: DashboardConversation) => boolean
  isBlocked: (c: DashboardConversation) => boolean
  isPaid: (c: DashboardConversation) => boolean
  isUnsubscribed: (c: DashboardConversation) => boolean
  isCampaignExcluded: (c: DashboardConversation) => boolean
  isClosed: (c: DashboardConversation) => boolean
}

export function resolveConversationFilterCount(
  f: ConversationFilter,
  serverCounts: Partial<Record<ConversationFilter, number>> | null | undefined,
  conversations: DashboardConversation[],
  h: ConversationFilterHelpers,
): number | null {
  if (f === 'all') return null
  const fromServer = serverCounts?.[f]
  if (typeof fromServer === 'number' && Number.isFinite(fromServer)) {
    return fromServer
  }
  return null
}

/** @deprecated Prefer ``resolveConversationFilterCount`` with backend totals. */
export function conversationFilterCount(
  f: ConversationFilter,
  conversations: DashboardConversation[],
  h: ConversationFilterHelpers,
): number {
  if (f === 'all') return 0
  if (f === 'active') {
    return conversations.filter(c => c.windowOpen === true && !h.isUnsubscribed(c)).length
  }
  if (f === 'human') return conversations.filter(c => h.isHumanResponding(c)).length
  if (f === 'agent_req') return conversations.filter(c => h.isAwaitingAgent(c)).length
  if (f === 'paused') return conversations.filter(c => h.isAIPausedOnly(c)).length
  if (f === 'blocked') return conversations.filter(c => h.isBlocked(c)).length
  if (f === 'paid') return conversations.filter(c => h.isPaid(c)).length
  if (f === 'unsubscribed') return conversations.filter(c => h.isUnsubscribed(c)).length
  if (f === 'campaign_excluded') return conversations.filter(c => h.isCampaignExcluded(c)).length
  return conversations.filter(c => h.isClosed(c)).length
}

export function conversationFilterActiveClass(f: ConversationFilter): string {
  if (f === 'agent_req') return 'bg-red-500 text-white shadow-sm'
  if (f === 'paused') return 'bg-amber-500 text-white shadow-sm'
  if (f === 'blocked') return 'bg-rose-600 text-white shadow-sm'
  if (f === 'paid') return 'bg-sky-500 text-white shadow-sm'
  if (f === 'unsubscribed') return 'bg-slate-600 text-white shadow-sm'
  if (f === 'campaign_excluded') return 'bg-violet-600 text-white shadow-sm'
  return 'bg-brand-500 text-white shadow-sm'
}

export function conversationFilterInactiveClass(f: ConversationFilter, count: number): string {
  if (f === 'agent_req' && count > 0) return 'text-red-600 bg-red-50 hover:bg-red-100'
  if (f === 'paused' && count > 0) return 'text-amber-700 bg-amber-50 hover:bg-amber-100'
  if (f === 'blocked' && count > 0) return 'text-rose-700 bg-rose-50 hover:bg-rose-100'
  if (f === 'paid' && count > 0) return 'text-sky-700 bg-sky-50 hover:bg-sky-100'
  if (f === 'unsubscribed' && count > 0) return 'text-slate-600 bg-slate-100 hover:bg-slate-200'
  if (f === 'campaign_excluded' && count > 0) return 'text-violet-700 bg-violet-50 hover:bg-violet-100'
  return 'text-slate-500 hover:bg-slate-100'
}

export function conversationFilterCountClass(
  f: ConversationFilter,
  isActive: boolean,
): string {
  if (isActive) return 'text-white/70'
  if (f === 'agent_req') return 'text-red-400'
  if (f === 'paused') return 'text-amber-500'
  if (f === 'blocked') return 'text-rose-500'
  if (f === 'paid') return 'text-sky-500'
  if (f === 'unsubscribed') return 'text-slate-500'
  if (f === 'campaign_excluded') return 'text-violet-500'
  return 'text-slate-400'
}

export function conversationFilterIcon(f: ConversationFilter): ReactNode {
  if (f === 'unsubscribed') return <BellOff className="inline w-3 h-3 me-1 opacity-70" />
  if (f === 'paused') return <Pause className="inline w-3 h-3 me-1 opacity-70" />
  if (f === 'blocked') return <Ban className="inline w-3 h-3 me-1 opacity-70" />
  if (f === 'paid') return <PackageCheck className="inline w-3 h-3 me-1 opacity-70" />
  if (f === 'campaign_excluded') return <Megaphone className="inline w-3 h-3 me-1 opacity-70" />
  return null
}
