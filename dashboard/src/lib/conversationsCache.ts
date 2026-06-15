import type { DashboardConversation, DashboardMessage } from '../api/featureReality'

const LIST_KEY = (tenantId: string | number | null) =>
  `nahla:conv:list:v1:${tenantId ?? '0'}`

const MSGS_KEY = (tenantId: string | number | null, phone: string) =>
  `nahla:conv:msgs:v1:${tenantId ?? '0'}:${phone}`

const MAX_CACHED_CONVERSATIONS = 120
const MAX_CACHED_MESSAGES = 80

type CachedList = {
  savedAt: number
  conversations: DashboardConversation[]
}

type CachedMessages = {
  savedAt: number
  messages: DashboardMessage[]
  hasMore?: boolean
}

function safeParse<T>(raw: string | null): T | null {
  if (!raw) return null
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export function loadConversationListCache(
  tenantId: string | number | null,
): DashboardConversation[] {
  if (typeof window === 'undefined') return []
  const hit = safeParse<CachedList>(localStorage.getItem(LIST_KEY(tenantId)))
  if (!hit?.conversations?.length) return []
  return hit.conversations
}

export function saveConversationListCache(
  tenantId: string | number | null,
  conversations: DashboardConversation[],
): void {
  if (typeof window === 'undefined') return
  try {
    const payload: CachedList = {
      savedAt: Date.now(),
      conversations: conversations.slice(0, MAX_CACHED_CONVERSATIONS),
    }
    localStorage.setItem(LIST_KEY(tenantId), JSON.stringify(payload))
  } catch {
    /* quota / private mode — ignore */
  }
}

export function loadConversationMessagesCache(
  tenantId: string | number | null,
  phone: string,
): CachedMessages | null {
  if (typeof window === 'undefined' || !phone) return null
  return safeParse<CachedMessages>(localStorage.getItem(MSGS_KEY(tenantId, phone)))
}

export function saveConversationMessagesCache(
  tenantId: string | number | null,
  phone: string,
  messages: DashboardMessage[],
  hasMore?: boolean,
): void {
  if (typeof window === 'undefined' || !phone) return
  try {
    const payload: CachedMessages = {
      savedAt: Date.now(),
      messages: messages.slice(-MAX_CACHED_MESSAGES),
      hasMore,
    }
    localStorage.setItem(MSGS_KEY(tenantId, phone), JSON.stringify(payload))
  } catch {
    /* ignore */
  }
}

export function mergeMessagesPreserveOrder(
  prev: DashboardMessage[],
  incoming: DashboardMessage[],
): DashboardMessage[] {
  const map = new Map<string, DashboardMessage>()
  for (const m of prev) map.set(m.id, m)
  for (const m of incoming) map.set(m.id, m)
  return [...map.values()].sort((a, b) => {
    const ta = Date.parse(a.time || '') || 0
    const tb = Date.parse(b.time || '') || 0
    return ta - tb
  })
}
