import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Bot, User, Send, Phone, Search, MoreVertical,
  UserCheck, ArrowLeft, ArrowRight, Check, CheckCheck, Clock, AlertCircle,
  Megaphone, Zap, ShoppingCart, PackageCheck, MessageSquare, AlertTriangle, BellOff,
  Pause, Play, Ban, FileText, RotateCcw, CheckCircle2, X, Loader2, Pencil,
} from 'lucide-react'

import { featureRealityApi, type DashboardConversation, type DashboardMessage, type MessageEventType, type AIPauseReason } from '../api/featureReality'
import { customersApi } from '../api/customers'
import { getTenantId } from '../auth'
import InboundMediaPreview from '../components/inbound/InboundMediaPreview'
import EditCustomerNameModal from '../components/conversations/EditCustomerNameModal'
import CampaignExcludeControl from '../components/customers/CampaignExcludeControl'
import ConversationFiltersMobileMenu from '../components/conversations/ConversationFiltersMobileMenu'
import {
  CONVERSATION_FILTER_KEYS,
  conversationFilterActiveClass,
  conversationFilterCount,
  conversationFilterCountClass,
  conversationFilterIcon,
  conversationFilterInactiveClass,
  type ConversationFilter,
} from '../components/conversations/conversationFilterConfig'

import { formatRiyadh, formatRiyadhDate, formatRiyadhTime } from '../lib/datetime'
import { useDashboardPoll } from '../lib/dashboardPolling'
import {
  loadConversationListCache,
  loadConversationMessagesCache,
  mergeMessagesPreserveOrder,
  saveConversationListCache,
  saveConversationMessagesCache,
} from '../lib/conversationsCache'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD, resolveOutboundSendError } from '../i18n/uiOnly'

const LIST_PAGE_LIMIT = 60
const LIST_POLL_MS = 5_000
const MESSAGE_PAGE_LIMIT = 30
const MESSAGE_POLL_MS = 4_000

function logConversationsUiFetch(meta: {
  endpoint: string
  duration_ms: number
  error_message: string
  tenant_id: string | number | null
  conversation_id?: string | null
}) {
  // Structured line for dashboards / copy-paste from Safari devtools console
  console.warn('[CONV_LIST_FETCH]', JSON.stringify(meta))
}

const SENDER_TYPE_STYLES: Record<MessageEventType, { icon: React.ReactNode; cls: string }> = {
  ai:         { icon: <Bot className="w-3 h-3" />, cls: 'bg-brand-50 text-brand-600 border-brand-200' },
  campaign:   { icon: <Megaphone className="w-3 h-3" />, cls: 'bg-blue-50 text-blue-600 border-blue-200' },
  automation: { icon: <Zap className="w-3 h-3" />, cls: 'bg-amber-50 text-amber-600 border-amber-200' },
  cod:        { icon: <PackageCheck className="w-3 h-3" />, cls: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
  manual:     { icon: <User className="w-3 h-3" />, cls: 'bg-slate-50 text-slate-600 border-slate-200' },
  system:     { icon: <MessageSquare className="w-3 h-3" />, cls: 'bg-purple-50 text-purple-600 border-purple-200' },
  customer:   { icon: null, cls: '' },
}

interface Conversation extends DashboardConversation {
  messages: DashboardMessage[]
}

const SCROLL_NEAR_BOTTOM_PX = 80

function _normalizePhoneDigits(phone: string): string {
  return (phone || '').replace(/\D/g, '')
}

async function _resolveCustomerIdByPhone(phone: string): Promise<number | null> {
  const digits = _normalizePhoneDigits(phone)
  if (!digits) return null
  const res = await customersApi.list({ search: phone, perPage: 20, page: 1 })
  const match = res.customers.find((c) => {
    const cd = _normalizePhoneDigits(c.phone)
    return cd === digits || cd.endsWith(digits.slice(-9)) || digits.endsWith(cd.slice(-9))
  })
  return match?.id ?? null
}

function conversationHasDisplayName(c: { customer: string; phone: string }, phonesMatch: (a?: string | null, b?: string | null) => boolean): boolean {
  if (!c.customer?.trim()) return false
  return !phonesMatch(c.customer, c.phone)
}

function conversationEditInitialName(c: { customer: string; phone: string }, phonesMatch: (a?: string | null, b?: string | null) => boolean): string {
  if (!conversationHasDisplayName(c, phonesMatch)) return ''
  return c.customer.trim()
}

export default function Conversations() {
  // UI_ONLY_GUARD: only static labels use t(); customer names, phones, message bodies stay as API data.
  void UI_ONLY_GUARD

  const { t, dir, isRTL, lang } = useLanguage()
  const cp = t(tr => tr.conversationsPage)

  const filterLabels = useMemo(() => ({
    all:          cp.filters.all,
    active:       cp.filters.active,
    human:        cp.filters.human,
    agent_req:    cp.filters.agentReq,
    paused:       cp.filters.paused,
    blocked:      cp.filters.blocked,
    paid:         cp.filters.paid,
    unsubscribed: cp.filters.unsubscribed,
    campaign_excluded: cp.filters.campaignExcluded,
    closed:       cp.filters.closed,
  }), [cp])

  const eventBadge = useMemo(() => ({
    ai:         { ...SENDER_TYPE_STYLES.ai,         label: cp.senderTypes.ai },
    campaign:   { ...SENDER_TYPE_STYLES.campaign,   label: cp.senderTypes.campaign },
    automation: { ...SENDER_TYPE_STYLES.automation, label: cp.senderTypes.automation },
    cod:        { ...SENDER_TYPE_STYLES.cod,        label: cp.senderTypes.cod },
    manual:     { ...SENDER_TYPE_STYLES.manual,     label: cp.senderTypes.manual },
    system:     { ...SENDER_TYPE_STYLES.system,     label: cp.senderTypes.system },
    customer:   { ...SENDER_TYPE_STYLES.customer,   label: '' },
  }), [cp])

  const pauseReasonLabel = (reason: AIPauseReason) => {
    if (reason === 'manual' || reason === 'manual_pause') return cp.pauseReasons.manual
    if (reason === 'human_handoff') return cp.pauseReasons.humanHandoff
    if (reason === 'manual_takeover') return cp.pauseReasons.manualTakeover
    if (reason === 'support_escalation') return cp.pauseReasons.supportEscalation
    if (reason === 'bot_loop_detected') return cp.pauseReasons.botLoop
    if (reason === 'rate_limit') return cp.pauseReasons.rateLimit
    if (reason === 'internal_number') return cp.pauseReasons.internalNumber
    return reason
  }

  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const requestedPhone = searchParams.get('phone')?.trim() || null

  const [selected, setSelected]     = useState<Conversation | null>(null)
  const [filter, setFilter]         = useState<ConversationFilter>('all')
  const [reply, setReply]           = useState('')
  const [conversations, setConversations] = useState<Conversation[]>(() => {
    const cached = loadConversationListCache(getTenantId())
    return cached.map((row) => ({ ...row, messages: [] as DashboardMessage[] }))
  })
  const [listBootstrapping, setListBootstrapping] = useState(
    () => loadConversationListCache(getTenantId()).length === 0,
  )
  const [searchQuery, setSearchQuery] = useState('')
  const [actionToast, setActionToast] = useState<string | null>(null)
  const [actionErrorToast, setActionErrorToast] = useState<string | null>(null)
  const [editNameOpen, setEditNameOpen] = useState(false)
  const [editNameSaving, setEditNameSaving] = useState(false)
  const [endingSupervision, setEndingSupervision] = useState(false)
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false)
  const headerMenuRef = useRef<HTMLDivElement | null>(null)
  const [mobileFilterMenuOpen, setMobileFilterMenuOpen] = useState(false)

  // mobile: 'list' = show list panel, 'chat' = show chat panel
  const [mobileView, setMobileView] = useState<'list' | 'chat'>('list')

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef    = useRef<HTMLTextAreaElement>(null)
  const isNearBottomRef = useRef(true)
  const pauseAutoScrollRef = useRef(false)
  const prevMessageCountRef = useRef(0)
  const prevLastMessageIdRef = useRef<number | string | null>(null)
  const selectedPhoneForScrollRef = useRef<string | null>(null)

  const listCtrlRef         = useRef<AbortController | null>(null)
  const msgsCtrlRef         = useRef<AbortController | null>(null)
  const listReqGen          = useRef(0)
  const nextSliceOffsetRef  = useRef(0)
  const bannerRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const listBusyRef         = useRef(false)
  // The filter the merchant has selected, mirrored in a ref so the
  // async fetch helpers always read the LATEST value rather than the
  // value captured at definition time. Without this, switching tabs
  // mid-fetch would still hit the API with the previous filter and
  // the new tab would render the wrong slice. Server-side SQL
  // narrowing depends on this — that's the post-pagination fix for
  // "بعض الفلاتر تأثرت بعد إصلاحات SQL".
  const filterRef = useRef<ConversationFilter>('all')

  const [listStaleBanner, setListStaleBanner] = useState<string | null>(null)
  const [hasMoreServer, setHasMoreServer]      = useState(false)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore]           = useState(false)
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false)
  const [hasMoreMessages, setHasMoreMessages] = useState(false)
  const messagesScrollRef = useRef<HTMLDivElement>(null)

  const phonesMatch = (a?: string | null, b?: string | null) => {
    const norm = (p?: string | null) =>
      (p || '').trim().replace(/^\+/, '').replace(/[\s-]/g, '')
    return !!a && !!b && norm(a) === norm(b)
  }

  const mergeRowsKeepMessages = (
    incoming: DashboardConversation[],
    prev: Conversation[],
  ): Conversation[] => {
    const map = new Map(prev.map((c) => [c.phone, c.messages]))
    return incoming.map((row) => ({ ...row, messages: map.get(row.phone) ?? [] }))
  }

  const mergeHeadPreserveTailServerOrder = (
    headPage: DashboardConversation[],
    prev: Conversation[],
  ): Conversation[] => {
    const head = mergeRowsKeepMessages(headPage, prev)
    const headPhones = new Set(head.map((c) => c.phone))
    const tail = prev.filter((c) => !headPhones.has(c.phone))
    return [...head, ...tail]
  }

  const logFetchFail = (endpoint: string, t0: number, err: unknown, conversationPhone?: string | null) => {
    logConversationsUiFetch({
      endpoint,
      duration_ms: Math.round(performance.now() - t0),
      error_message: err instanceof Error ? err.message : String(err ?? ''),
      tenant_id: getTenantId(),
      conversation_id: conversationPhone ?? null,
    })
  }

  const clearStaleRetryTimer = () => {
    if (bannerRetryTimerRef.current) {
      clearTimeout(bannerRetryTimerRef.current)
      bannerRetryTimerRef.current = null
    }
  }

  const scheduleListReconnect = () => {
    clearStaleRetryTimer()
    bannerRetryTimerRef.current = setTimeout(() => {
      bannerRetryTimerRef.current = null
      if (document.visibilityState !== 'hidden' && !listBusyRef.current) {
        void reloadFirstPagePreserveTail({ silent: true })
      }
    }, 8_500)
  }

  const reloadFirstPagePreserveTail = async (opts?: { silent?: boolean; signal?: AbortSignal }) => {
    const gen = ++listReqGen.current
    let signal: AbortSignal
    if (opts?.signal) {
      // Background poll tick — do not cancel user-driven list refreshes wired to ``listCtrlRef``.
      signal = opts.signal
    } else {
      listCtrlRef.current?.abort()
      const ac = new AbortController()
      listCtrlRef.current = ac
      signal = ac.signal
    }
    const t0 = performance.now()
    listBusyRef.current = true
    try {
      const page = await featureRealityApi.conversations({
        signal,
        limit: LIST_PAGE_LIMIT,
        offset: 0,
        filter: filterRef.current,
      })
      if (gen !== listReqGen.current) return
      const rows = Array.isArray(page.conversations) ? page.conversations : []
      nextSliceOffsetRef.current = rows.length
      setHasMoreServer(Boolean(page.has_more))
      setConversations((prev) => {
        const merged = mergeHeadPreserveTailServerOrder(rows, prev)
        saveConversationListCache(getTenantId(), merged)
        return merged
      })
      setListBootstrapping(false)
      setSelected((prevSel) => {
        if (!prevSel) return prevSel
        const hit = rows.find((c: DashboardConversation) => prevSel.phone === c.phone)
        return hit ? { ...hit, messages: prevSel.messages } : prevSel
      })
      clearStaleRetryTimer()
      setListStaleBanner(null)
    } catch (err: unknown) {
      if (
        (err instanceof DOMException && err.name === 'AbortError') ||
        signal.aborted ||
        gen !== listReqGen.current
      ) {
        return
      }
      logFetchFail(`/conversations?limit=${LIST_PAGE_LIMIT}&offset=0`, t0, err)
      if (!opts?.silent) {
        setListStaleBanner(cp.errors.refreshFailed)
      }
      scheduleListReconnect()
    } finally {
      if (gen === listReqGen.current) listBusyRef.current = false
    }
  }

  const replaceFirstPageFromServer = async (opts?: { signal?: AbortSignal }) => {
    const gen = ++listReqGen.current
    listCtrlRef.current?.abort()
    const ac = opts?.signal ? undefined : new AbortController()
    if (!opts?.signal && ac) listCtrlRef.current = ac
    const signal = opts?.signal ?? ac!.signal
    const t0 = performance.now()
    listBusyRef.current = true
    try {
      const page = await featureRealityApi.conversations({
        signal,
        limit: LIST_PAGE_LIMIT,
        offset: 0,
        filter: filterRef.current,
      })
      if (gen !== listReqGen.current) return
      let rows = Array.isArray(page.conversations)
        ? (page.conversations as DashboardConversation[])
        : []
      nextSliceOffsetRef.current = rows.length
      setHasMoreServer(Boolean(page.has_more))

      if (
        requestedPhone &&
        !rows.some((c) => phonesMatch(c.phone, requestedPhone)) &&
        !signal.aborted
      ) {
        const supplemental = await fetchOutlineForPhoneMaybe(requestedPhone, signal).catch(() => null)
        if (
          supplemental &&
          gen === listReqGen.current &&
          !rows.some((r) => r.phone === supplemental.phone)
        ) {
          rows = [...rows, supplemental]
        }
      }

      const rowsSnap = rows
      setConversations((prev) => {
        const merged = mergeRowsKeepMessages(rowsSnap, prev)
        saveConversationListCache(getTenantId(), merged)
        return merged
      })
      setListBootstrapping(false)

      setSelected((prevSel) => {
        if (!requestedPhone) {
          if (!prevSel) return prevSel
          const hitFresh = rowsSnap.find((c) => c.phone === prevSel.phone)
          return hitFresh ? { ...hitFresh, messages: prevSel.messages } : prevSel
        }
        const hit = rowsSnap.find((c) => phonesMatch(c.phone, requestedPhone))
        if (!hit) return prevSel
        const msgs =
          prevSel && phonesMatch(prevSel.phone, requestedPhone)
            ? prevSel.messages
            : []
        return { ...hit, messages: msgs }
      })

      clearStaleRetryTimer()
      setListStaleBanner(null)
    } catch (err: unknown) {
      if (
        opts?.signal?.aborted ||
        signal.aborted ||
        (err instanceof DOMException && err.name === 'AbortError') ||
        gen !== listReqGen.current
      ) {
        return
      }
      logFetchFail(`/conversations?limit=${LIST_PAGE_LIMIT}&offset=0`, t0, err)
      setListStaleBanner(cp.errors.refreshFailed)
      scheduleListReconnect()
    } finally {
      if (gen === listReqGen.current) listBusyRef.current = false
    }
  }

  /** Broader inbox fetch — only when linked phone misses the newest page snapshot. */
  async function fetchOutlineForPhoneMaybe(phoneGuess: string, signal: AbortSignal) {
    // Intentionally NOT filtered — when a deep-link arrives for a
    // specific phone and it isn't in the current filter's slice, we
    // want to surface the row anyway. The router returns the full
    // (unfiltered) inbox so the search lands across categories.
    const res = await featureRealityApi.conversations({
      signal,
      limit: 200,
      offset: 0,
    })
    return res.conversations.find((c) => phonesMatch(c.phone, phoneGuess)) ?? null
  }

  const appendNextPage = async () => {
    if (!hasMoreServer || loadingMore || listBusyRef.current) return
    const gen = ++listReqGen.current
    setLoadingMore(true)
    listBusyRef.current = true
    const ac = new AbortController()
    listCtrlRef.current = ac
    const t0 = performance.now()
    try {
      const page = await featureRealityApi.conversations({
        signal: ac.signal,
        limit: LIST_PAGE_LIMIT,
        offset: nextSliceOffsetRef.current,
        filter: filterRef.current,
      })
      if (gen !== listReqGen.current) return
      const rows = Array.isArray(page.conversations) ? page.conversations : []
      nextSliceOffsetRef.current += rows.length
      setHasMoreServer(Boolean(page.has_more))
      const newRows = rows as DashboardConversation[]
      setConversations((prev) => {
        const have = new Set(prev.map((c) => c.phone))
        const extra = newRows.filter((row) => !have.has(row.phone)).map((c) => ({
          ...c,
          messages: [] as DashboardMessage[],
        }))
        return [...prev, ...extra]
      })
    } catch (err: unknown) {
      if (!ac.signal.aborted && gen === listReqGen.current) {
        logFetchFail(
          `/conversations?limit=${LIST_PAGE_LIMIT}&offset=${nextSliceOffsetRef.current}`,
          t0,
          err,
        )
        setListStaleBanner(cp.errors.loadMoreFailed)
      }
    } finally {
      setLoadingMore(false)
      if (gen === listReqGen.current) listBusyRef.current = false
    }
  }

  const loadMessagesForOpenChat = async (
    phone: string,
    opts?: { silent?: boolean; signal?: AbortSignal },
  ) => {
    if (!opts?.silent) {
      const cached = loadConversationMessagesCache(getTenantId(), phone)
      if (cached?.messages?.length) {
        setConversations((prev) =>
          prev.map((c) =>
            c.phone === phone ? { ...c, messages: cached.messages } : c,
          ),
        )
        setSelected((prevSel) =>
          prevSel && prevSel.phone === phone
            ? { ...prevSel, messages: cached.messages }
            : prevSel,
        )
        setHasMoreMessages(Boolean(cached.hasMore))
      }
    }

    let signal: AbortSignal
    if (opts?.signal) {
      signal = opts.signal
    } else {
      msgsCtrlRef.current?.abort()
      const ac = new AbortController()
      msgsCtrlRef.current = ac
      signal = ac.signal
    }
    const t0 = performance.now()
    if (!opts?.silent) setLoadingMessages(true)
    try {
      const { messages, has_more } = await featureRealityApi.conversationMessages(phone, {
        signal,
        limit: MESSAGE_PAGE_LIMIT,
      })
      setHasMoreMessages(Boolean(has_more))
      let mergedForCache = messages
      setConversations((prev) =>
        prev.map((c) => {
          if (c.phone !== phone) return c
          const merged = opts?.silent && c.messages.length
            ? mergeMessagesPreserveOrder(c.messages, messages)
            : messages
          mergedForCache = merged
          return { ...c, messages: merged }
        }),
      )
      setSelected((prevSel) => {
        if (!prevSel || prevSel.phone !== phone) return prevSel
        const merged = opts?.silent && prevSel.messages.length
          ? mergeMessagesPreserveOrder(prevSel.messages, messages)
          : messages
        mergedForCache = merged
        return { ...prevSel, messages: merged }
      })
      saveConversationMessagesCache(getTenantId(), phone, mergedForCache, has_more)
    } catch (err: unknown) {
      if (signal.aborted) return
      logFetchFail(`/conversations/messages/${encodeURIComponent(phone)}`, t0, err, phone)
    } finally {
      if (!signal.aborted && !opts?.silent) setLoadingMessages(false)
    }
  }

  const loadOlderMessages = async (phone: string) => {
    const current = selected?.phone === phone ? selected.messages : conversations.find((c) => c.phone === phone)?.messages
    if (!current?.length || loadingOlderMessages || !hasMoreMessages) return
    const oldestId = Number(current[0]?.id)
    if (!Number.isFinite(oldestId)) return

    setLoadingOlderMessages(true)
    const scrollEl = messagesScrollRef.current
    const prevHeight = scrollEl?.scrollHeight ?? 0
    try {
      const { messages: older, has_more } = await featureRealityApi.conversationMessages(phone, {
        limit: MESSAGE_PAGE_LIMIT,
        beforeId: oldestId,
      })
      if (!older.length) {
        setHasMoreMessages(false)
        return
      }
      const merged = mergeMessagesPreserveOrder(older, current)
      setHasMoreMessages(Boolean(has_more))
      setConversations((prev) =>
        prev.map((c) => (c.phone === phone ? { ...c, messages: merged } : c)),
      )
      setSelected((prevSel) =>
        prevSel && prevSel.phone === phone ? { ...prevSel, messages: merged } : prevSel,
      )
      saveConversationMessagesCache(getTenantId(), phone, merged, has_more)
      requestAnimationFrame(() => {
        if (scrollEl) {
          scrollEl.scrollTop = scrollEl.scrollHeight - prevHeight
        }
      })
    } catch (err: unknown) {
      logFetchFail(
        `/conversations/messages/${encodeURIComponent(phone)}?before_id=${oldestId}`,
        performance.now(),
        err,
        phone,
      )
    } finally {
      setLoadingOlderMessages(false)
    }
  }

  useDashboardPoll({
    pollKey: `GET:/conversations?limit=${LIST_PAGE_LIMIT}&offset=0`,
    intervalMs: LIST_POLL_MS,
    leading: false,
    run: async (signal) => {
      if (listBusyRef.current) return
      await reloadFirstPagePreserveTail({ silent: true, signal })
    },
  })

  useDashboardPoll({
    pollKey: selected ? `GET:/conversations/messages/${selected.phone}` : 'GET:/conversations/messages/_idle',
    intervalMs: MESSAGE_POLL_MS,
    enabled: Boolean(selected?.phone),
    leading: false,
    run: async (signal) => {
      if (!selected?.phone) return
      await loadMessagesForOpenChat(selected.phone, { silent: true, signal })
    },
  })

  useEffect(() => {
    const run = async () => {
      await replaceFirstPageFromServer()
      if (requestedPhone) {
        setMobileView('chat')
        await loadMessagesForOpenChat(requestedPhone)
      }
    }
    void run()

    return () => {
      clearStaleRetryTimer()
      listCtrlRef.current?.abort()
      msgsCtrlRef.current?.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedPhone])

  // ── Filter change → refetch first page narrowed server-side ──────────────
  // When the merchant switches tabs (e.g. "كل" → "طلب موظف") we
  // refetch with the new ?filter= param so the SQL window narrows
  // BEFORE pagination. Without this, large inboxes would show empty
  // tabs (the human/closed tail can live beyond the first 200-1500
  // rows). The very first render is handled by the mount effect
  // above so we skip this on the initial pass when filter is still
  // its default ``all`` — otherwise we'd fire two parallel fetches.
  const filterChangedOnceRef = useRef(false)
  useEffect(() => {
    filterRef.current = filter
    if (!filterChangedOnceRef.current) {
      filterChangedOnceRef.current = true
      return
    }
    void replaceFirstPageFromServer()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  const isScrollNearBottom = (el: HTMLElement) =>
    el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_NEAR_BOTTOM_PX

  const syncScrollAnchors = (el: HTMLElement) => {
    const nearBottom = isScrollNearBottom(el)
    isNearBottomRef.current = nearBottom
    if (nearBottom) pauseAutoScrollRef.current = false
    return nearBottom
  }

  const markUserScrolling = () => {
    pauseAutoScrollRef.current = true
  }

  const mayAutoScrollToBottom = () => {
    const el = messagesScrollRef.current
    if (el) syncScrollAnchors(el)
    return !pauseAutoScrollRef.current && isNearBottomRef.current
  }

  const scrollMessagesToBottom = (behavior: ScrollBehavior = 'smooth') => {
    const el = messagesScrollRef.current
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior })
    } else {
      messagesEndRef.current?.scrollIntoView({ behavior })
    }
    isNearBottomRef.current = true
    pauseAutoScrollRef.current = false
  }

  // Close filter sheet when entering chat view (mobile).
  useEffect(() => {
    if (mobileView === 'chat') setMobileFilterMenuOpen(false)
  }, [mobileView])

  // Scroll to bottom once when opening a conversation.
  useEffect(() => {
    if (!selected) return
    const phoneChanged = selectedPhoneForScrollRef.current !== selected.phone
    if (!phoneChanged) return
    selectedPhoneForScrollRef.current = selected.phone
    isNearBottomRef.current = true
    pauseAutoScrollRef.current = false
    prevMessageCountRef.current = 0
    prevLastMessageIdRef.current = null
    requestAnimationFrame(() => scrollMessagesToBottom('auto'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.phone])

  // Auto-scroll only when a new message arrives at the bottom and the user allows it.
  useEffect(() => {
    if (!selected?.messages) return
    const msgs = selected.messages
    const count = msgs.length
    const lastId = msgs[count - 1]?.id ?? null
    const prevCount = prevMessageCountRef.current
    const prevLastId = prevLastMessageIdRef.current

    prevMessageCountRef.current = count
    prevLastMessageIdRef.current = lastId

    if (count === 0) return

    if (prevCount === 0 && count > 0) {
      if (mayAutoScrollToBottom()) {
        requestAnimationFrame(() => scrollMessagesToBottom('auto'))
      }
      return
    }

    const appendedAtEnd = lastId != null && lastId !== prevLastId && count >= prevCount
    if (!appendedAtEnd) return

    if (mayAutoScrollToBottom()) {
      requestAnimationFrame(() => scrollMessagesToBottom('smooth'))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.messages])

  // Auto-resize textarea
  const handleTextareaInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setReply(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
  }

  const selectConversation = (c: Conversation) => {
    const cached = loadConversationMessagesCache(getTenantId(), c.phone)
    const withMessages = cached?.messages?.length
      ? { ...c, messages: cached.messages }
      : c
    setSelected(withMessages)
    setHasMoreMessages(Boolean(cached?.hasMore))
    setMobileFilterMenuOpen(false)
    setMobileView('chat')
    loadMessagesForOpenChat(c.phone)
    // Zero the unread badge locally the moment we open the
    // conversation, then ask the backend to stamp last_read_at so the
    // count stays at 0 across refetches. Failures are non-fatal — the
    // legacy "newer than last outbound" rule still applies.
    if (c.unread > 0) {
      setConversations(prev => prev.map(x => x.phone === c.phone ? { ...x, unread: 0 } : x))
      setSelected(prev => prev && prev.phone === c.phone ? { ...prev, unread: 0 } : prev)
    }
    void featureRealityApi.markConversationRead({ customer_phone: c.phone }).catch((err) => {
      console.warn('[MARK_READ_UI] failed phone=%s err=%o', c.phone, err)
    })
  }

  const goBackToList = () => {
    setMobileView('list')
  }

  const handleReply = async () => {
    if (!selected || !reply.trim()) return
    try {
      await featureRealityApi.replyToConversation({
        customer_phone: selected.phone,
        message: reply.trim(),
      })
      setReply('')
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
      await loadMessagesForOpenChat(selected.phone)
      await reloadFirstPagePreserveTail({ silent: true })
      pauseAutoScrollRef.current = false
      isNearBottomRef.current = true
      scrollMessagesToBottom('smooth')
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.sendReplyFailed)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleReply()
    }
  }

  const _optimisticUpdate = (phone: string, patch: Partial<DashboardConversation>) => {
    setConversations(prev =>
      prev.map(c => (phonesMatch(c.phone, phone) ? { ...c, ...patch } : c)),
    )
    setSelected(prev =>
      prev && phonesMatch(prev.phone, phone) ? { ...prev, ...patch } : prev,
    )
  }

  const _applyCustomerNameByPhone = (phone: string, newName: string) => {
    setConversations(prev =>
      prev.map(c => (phonesMatch(c.phone, phone) ? { ...c, customer: newName } : c)),
    )
    setSelected(prev =>
      prev && phonesMatch(prev.phone, phone) ? { ...prev, customer: newName } : prev,
    )
  }

  const openEditCustomerName = () => {
    if (!selected) return
    setEditNameOpen(true)
  }

  const handleSaveCustomerName = async (newName: string) => {
    if (!selected || editNameSaving) return
    const phone = selected.phone
    const trimmed = newName.trim()
    const snapshot = conversations
      .filter(c => phonesMatch(c.phone, phone))
      .map(c => ({ id: c.id, customer: c.customer }))

    setEditNameSaving(true)
    _applyCustomerNameByPhone(phone, trimmed)

    try {
      const customerId = await _resolveCustomerIdByPhone(phone)
      if (!customerId) {
        throw new Error(cp.errors.customerNotFound)
      }
      await customersApi.update(customerId, { name: trimmed })
      setEditNameOpen(false)
      setActionErrorToast(null)
      setActionToast(cp.editCustomerName.toastSuccess)
    } catch {
      setConversations(prev =>
        prev.map(c => {
          const snap = snapshot.find(s => s.id === c.id)
          return snap ? { ...c, customer: snap.customer } : c
        }),
      )
      setSelected(prev => {
        if (!prev) return prev
        const snap = snapshot.find(s => s.id === prev.id)
        return snap ? { ...prev, customer: snap.customer } : prev
      })
      setEditNameOpen(false)
      setActionToast(null)
      setActionErrorToast(cp.editCustomerName.toastError)
    } finally {
      setEditNameSaving(false)
    }
  }

  const handleHandoff = async () => {
    if (!selected) return
    try {
      await featureRealityApi.handoffConversation({
        customer_phone: selected.phone,
        customer_name: selected.customer,
        last_message: selected.lastMsg,
      })
      _optimisticUpdate(selected.phone, {
        status: 'human',
        isAI: false,
        handoffReason: 'customer_request',
        needsHuman: true,
        handoffActive: true,
        takenOverAt: new Date().toISOString(),
        aiPaused: true,
        aiPausedReason: 'manual_takeover',
      })
      await reloadFirstPagePreserveTail({ silent: true })
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.handoffFailed)
    }
  }



  const _applyAIState = (phone: string, state: {
    aiPaused: boolean
    aiPausedReason: AIPauseReason | null
    aiPausedAt: string | null
  }) => {
    const patch: Partial<DashboardConversation> = {
      aiPaused: state.aiPaused,
      aiPausedReason: state.aiPausedReason,
      aiPausedAt: state.aiPausedAt,
      // ``isAI`` reflects the merged state: AI is "on" only when not
      // paused AND there's no human takeover in progress.
      isAI: !state.aiPaused,
    }
    _optimisticUpdate(phone, patch)
  }

  // Apply takeover-cleared dashboard state (`/handoff/return-to-ai`).
  const _applyReturnToAIState = (phone: string) => {
    _optimisticUpdate(phone, {
      aiPaused: false,
      aiPausedReason: null,
      aiPausedAt: null,
      isAI: true,
      status: 'active',
      handoffReason: null,
      needsHuman: false,
      handoffActive: false,
      takenOverAt: null,
      takenOverBy: null,
    })
  }

  const pauseIntelligenceForSelected = async () => {
    if (!selected) return
    console.log('[AI_PAUSE_UI] request pause phone=', selected.phone, 'reason=manual_pause')
    try {
      const res = await featureRealityApi.pauseConversationAI({
        customer_phone: selected.phone,
        reason: 'manual_pause',
      })
      console.log('[AI_PAUSE_UI] response', res)
      _applyAIState(selected.phone, {
        aiPaused: !!res.aiPaused,
        aiPausedReason: (res.aiPausedReason ?? null) as AIPauseReason | null,
        aiPausedAt: res.aiPausedAt ?? null,
      })
      await reloadFirstPagePreserveTail({ silent: true })
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.pauseFailed)
    }
  }

  useEffect(() => {
    if (!actionToast) return
    const t = window.setTimeout(() => setActionToast(null), 4000)
    return () => window.clearTimeout(t)
  }, [actionToast])

  useEffect(() => {
    if (!actionErrorToast) return
    const t = window.setTimeout(() => setActionErrorToast(null), 5000)
    return () => window.clearTimeout(t)
  }, [actionErrorToast])

  useEffect(() => {
    setHeaderMenuOpen(false)
  }, [selected?.phone])

  useEffect(() => {
    if (!headerMenuOpen) return
    const onDocClick = (event: MouseEvent) => {
      if (headerMenuRef.current && !headerMenuRef.current.contains(event.target as Node)) {
        setHeaderMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [headerMenuOpen])


  const endHumanSupervisionForSelected = async () => {
    if (!selected) return
    const inTakeover =
      !!selected.needsHuman ||
      !!selected.handoffActive ||
      selected.status === 'human'
    if (!inTakeover) return

    setEndingSupervision(true)
    try {
      await featureRealityApi.returnHandoffToAI({
        customer_phone: selected.phone,
      })
      _applyReturnToAIState(selected.phone)
      setActionToast(cp.toasts.resumedToAI)
      setHeaderMenuOpen(false)
      await reloadFirstPagePreserveTail({ silent: true })
      await loadMessagesForOpenChat(selected.phone)
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.resumeFailed)
    } finally {
      setEndingSupervision(false)
    }
  }

  const resumeIntelligenceForSelected = async () => {
    if (!selected) return
    const inTakeover =
      !!selected.needsHuman ||
      !!selected.handoffActive ||
      selected.status === 'human'
    console.log(
      `[AI_RESUME_UI] phone=${selected.phone} takeover=${inTakeover}`,
    )
    try {
      if (inTakeover) {
        await featureRealityApi.returnHandoffToAI({
          customer_phone: selected.phone,
        })
        _applyReturnToAIState(selected.phone)
      } else {
        const res = await featureRealityApi.resumeConversationAI({
          customer_phone: selected.phone,
        })
        _applyAIState(selected.phone, {
          aiPaused: !!res.aiPaused,
          aiPausedReason: (res.aiPausedReason ?? null) as AIPauseReason | null,
          aiPausedAt: res.aiPausedAt ?? null,
        })
      }
      await reloadFirstPagePreserveTail({ silent: true })
      await loadMessagesForOpenChat(selected.phone)
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.unpauseFailed)
    }
  }

  const handleBlockNumber = async () => {
    if (!selected) return
    const ok = window.confirm(
      cp.confirm.blockNumber.replace('{phone}', selected.phone),
    )
    if (!ok) return
    console.log('[AI_PAUSE_UI] request blocklist add phone=', selected.phone)
    try {
      await featureRealityApi.addToBlocklist({
        phone: selected.phone,
        customer_phone: selected.phone,
      })
      _applyAIState(selected.phone, {
        aiPaused: true,
        aiPausedReason: 'internal_number',
        aiPausedAt: new Date().toISOString(),
      })
      _optimisticUpdate(selected.phone, { isBlocked: true })
      await reloadFirstPagePreserveTail({ silent: true })
    } catch (e) {
      alert(e instanceof Error ? e.message : cp.errors.blockFailed)
    }
  }

  // ── Three mutually-exclusive operational filters ──────────────
  // Priority order: blocked > human > paused. A blocked phone never
  // appears under "human" or "paused"; a human takeover never
  // appears under "paused". This matches the merchant's mental model
  // ("each conversation is in exactly ONE operational state").

  // "محظور": phone is on the tenant blocklist (server-resolved) or
  // paused with the legacy ``internal_number`` reason.
  const _isBlocked = (c: DashboardConversation) =>
    !!c.isBlocked || c.aiPausedReason === 'internal_number'

  // "بشري": unified — the conversation is owned by a human right now.
  // ONLY driven by the explicit human-state columns (needs_human,
  // handoff_active, status='human'). NOT influenced by ``aiPaused``.
  // Excludes blocked rows so the filters stay disjoint.
  const _isHumanResponding = (c: DashboardConversation) => {
    if (_isBlocked(c)) return false
    if (c.needsHuman) return true
    if (c.handoffActive) return true
    if (c.status === 'human') return true
    return false
  }

  // "متوقف الذكاء": AI is paused but it's NOT a human takeover and
  // NOT a blocked number. Catches every soft-pause reason
  // (manual_pause, bot_loop_detected, rate_limit, system pauses…).
  const _isAIPausedOnly = (c: DashboardConversation) => {
    if (!c.aiPaused) return false
    if (_isBlocked(c)) return false
    if (_isHumanResponding(c)) return false
    return true
  }

  // "يطلب موظف": a takeover that hasn't received a manual reply yet.
  // Kept for backwards-compat of the existing pill in the screenshot.
  const _isAwaitingAgent = (c: DashboardConversation) =>
    _isHumanResponding(c) && c.lastMsgType !== 'manual'

  const _isUnsubscribed = (c: DashboardConversation) =>
    !!(c.isUnsubscribed || c.pendingUnsubscribe)

  const _isPaid = (c: DashboardConversation) =>
    !!c.lastPaymentConfirmedAt

  // "مغلقة": server-stamped ``status='closed'`` is the canonical
  // signal (set by /conversations/close or by automations). The
  // 24h WhatsApp window expiry is included as a secondary signal so
  // dormant conversations the merchant has stopped engaging with
  // still surface here even before they're explicitly closed. This
  // mirrors the merchant mental model: "any conversation that's no
  // longer a live thread".
  const _isClosed = (c: DashboardConversation) =>
    c.status === 'closed' || c.windowOpen === false

  const _isCampaignExcluded = (c: DashboardConversation) =>
    !!c.marketingOptOutManual

  const filterHelpers = useMemo(() => ({
    isHumanResponding: _isHumanResponding,
    isAwaitingAgent: _isAwaitingAgent,
    isAIPausedOnly: _isAIPausedOnly,
    isBlocked: _isBlocked,
    isPaid: _isPaid,
    isUnsubscribed: _isUnsubscribed,
    isCampaignExcluded: _isCampaignExcluded,
    isClosed: _isClosed,
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [conversations])

  const filtered = conversations.filter(c => {
    let matchFilter = false
    if (filter === 'all') matchFilter = true
    else if (filter === 'active') matchFilter = c.windowOpen === true && !_isUnsubscribed(c)
    else if (filter === 'human') matchFilter = _isHumanResponding(c)
    else if (filter === 'agent_req') matchFilter = _isAwaitingAgent(c)
    else if (filter === 'paused') matchFilter = _isAIPausedOnly(c)
    else if (filter === 'blocked') matchFilter = _isBlocked(c)
    else if (filter === 'paid') matchFilter = _isPaid(c)
    else if (filter === 'unsubscribed') matchFilter = _isUnsubscribed(c)
    else if (filter === 'campaign_excluded') matchFilter = _isCampaignExcluded(c)
    else if (filter === 'closed') matchFilter = _isClosed(c)
    const matchSearch = !searchQuery || c.customer.includes(searchQuery) || c.phone.includes(searchQuery)
    return matchFilter && matchSearch
  })

  // Priority ordering: rows where the customer is currently waiting on a
  // human reply float to the top of EVERY filter view (including "الكل")
  // so the merchant never has to switch tabs to spot a pending takeover.
  // Stable sort — same-priority rows keep their server order (most-recent
  // first) so this only re-positions the awaiting bucket without
  // scrambling the rest of the list.
  const sortedFiltered = [...filtered].sort((a, b) => {
    const pa = _isAwaitingAgent(a) ? 1 : 0
    const pb = _isAwaitingAgent(b) ? 1 : 0
    return pb - pa
  })

  const initials = (name: string) =>
    name.split(' ').map(n => n[0]).join('').slice(0, 2)

  // ─────────────────────────────────────────────────────────────────────────────
  // Layout:
  //   Mobile  → show list XOR chat (WhatsApp style)
  //   Desktop → side-by-side (two-column)
  //
  // We break out of Layout's p-3 padding via -m-3 / md:-m-6 so the component
  // is edge-to-edge, then reclaim full viewport height.
  // ─────────────────────────────────────────────────────────────────────────────
  const BackIcon = isRTL ? ArrowRight : ArrowLeft

  return (
    <div
      dir={dir}
      className="
      -m-3 md:-m-6
      flex overflow-hidden
      h-[calc(100dvh-3.5rem)] md:h-[calc(100dvh-4rem)]
      bg-white md:rounded-xl md:shadow-sm md:border md:border-slate-200
    ">

      {/* ── PANEL 1: Conversation list ─────────────────────────────────────── */}
      <div className={`
        flex flex-col shrink-0 bg-white
        w-full md:w-80 md:border-e md:border-slate-100
        ${mobileView === 'chat' ? 'hidden md:flex' : 'flex'}
      `}>

        {/* List header (mobile) */}
        <div className="flex items-center justify-between px-4 py-3 bg-brand-600 md:bg-white md:border-b md:border-slate-100">
          <h2 className="text-base font-bold text-white md:text-slate-900">{cp.title}</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-white/70 md:hidden">
              {conversations.filter(c => c.unread > 0).length > 0
                ? cp.unreadCount.replace('{count}', String(conversations.filter(c => c.unread > 0).length))
                : ''}
            </span>
          </div>
        </div>

        {/* Search */}
        <div className="px-3 py-2 bg-slate-50 border-b border-slate-100">
          <div className="relative">
            <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              className="w-full ps-9 pe-3 py-2 bg-white rounded-full text-sm border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-200 placeholder:text-slate-400"
              placeholder={cp.searchPlaceholder}
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        {/* Filter tabs — mobile list only; desktop: horizontal chips */}
        {mobileView === 'list' && (
          <div className="px-3 py-2 bg-white border-b border-slate-100 md:hidden">
            <ConversationFiltersMobileMenu
              dir={dir}
              open={mobileFilterMenuOpen}
              onOpenChange={setMobileFilterMenuOpen}
              activeFilter={filter}
              filterLabels={filterLabels}
              conversations={conversations}
              helpers={filterHelpers}
              menuButtonLabel={cp.mobileFilters.menuButtonLabel}
              sheetTitle={cp.mobileFilters.sheetTitle}
              onSelect={setFilter}
            />
          </div>
        )}

        <div
          className="hidden md:flex gap-1.5 px-3 py-2 bg-white border-b border-slate-100 overflow-x-auto"
          style={{ scrollbarWidth: 'thin' }}
        >
          {CONVERSATION_FILTER_KEYS.map((f) => {
            const count = conversationFilterCount(f, conversations, filterHelpers)
            const isActive = filter === f

            return (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors ${
                  isActive
                    ? conversationFilterActiveClass(f)
                    : conversationFilterInactiveClass(f, count)
                }`}
              >
                {conversationFilterIcon(f)}
                {filterLabels[f]}
                {f !== 'all' && count > 0 && (
                  <span className={`ms-1 ${conversationFilterCountClass(f, isActive)}`}>
                    {count}
                  </span>
                )}
              </button>
            )
          })}
        </div>

        {/* Unsubscribed filter hint */}
        {filter === 'unsubscribed' && (
          <div className="mx-3 my-2 flex items-start gap-2 bg-slate-50 border border-slate-200 rounded-xl px-3 py-2.5 text-xs text-slate-500">
            <BellOff className="w-3.5 h-3.5 shrink-0 mt-0.5 text-slate-400" />
            <span>
              {cp.unsubscribedFilterHint}
            </span>
          </div>
        )}

        {listStaleBanner && (
          <div className="mx-3 my-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50/95 px-3 py-2 text-xs text-amber-950">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-600" />
            <div className="flex-1 leading-relaxed">
              {listStaleBanner}{' '}
              <button
                type="button"
                className="font-semibold text-brand-700 underline underline-offset-2 decoration-brand-400"
                onClick={() => {
                  clearStaleRetryTimer()
                  void replaceFirstPageFromServer()
                }}
              >
                {cp.refreshNow}
              </button>
            </div>
          </div>
        )}

        {/* Conversation list */}
        <ul className="flex-1 overflow-y-auto divide-y divide-slate-100">
          {listBootstrapping && sortedFiltered.length === 0 && (
            <>
              {Array.from({ length: 8 }).map((_, i) => (
                <li key={`sk-${i}`} className="flex items-start gap-3 px-4 py-3.5 animate-pulse">
                  <div className="w-11 h-11 rounded-full bg-slate-200 shrink-0" />
                  <div className="flex-1 space-y-2 pt-1">
                    <div className="h-3.5 bg-slate-200 rounded w-2/5" />
                    <div className="h-3 bg-slate-100 rounded w-4/5" />
                  </div>
                </li>
              ))}
            </>
          )}
          {!listBootstrapping && sortedFiltered.length === 0 && (
            <li className="py-20 text-center">
              <Bot className="w-10 h-10 text-slate-200 mx-auto mb-3" />
              <p className="text-sm text-slate-400">{cp.emptyList}</p>
            </li>
          )}
          {sortedFiltered.length > 0 && sortedFiltered.map((c) => {
            // ``awaitingAgent`` is the single source of truth for the
            // "row needs human attention RIGHT NOW" visual treatment
            // (red unread badge, subtle red row tint, red end-border,
            // and the pulsing "يطلب موظف" pill below). Using the same
            // helper as the filter tab + the priority sort above keeps
            // the three signals in sync — toggling a server-side
            // ``needs_human`` / ``handoff_active`` row will update the
            // badge AND its position AND its row chrome together.
            const awaitingAgent = _isAwaitingAgent(c)
            const isSelected    = selected?.id === c.id
            return (
              <li
                key={c.id}
                onClick={() => selectConversation(c)}
                className={`flex items-start gap-3 px-4 py-3.5 cursor-pointer active:bg-slate-100 transition-colors ${
                  isSelected
                    ? 'bg-brand-50 border-e-2 border-brand-400'
                    : awaitingAgent
                      ? 'bg-red-50/40 border-e-2 border-red-300 hover:bg-red-50/60'
                      : 'hover:bg-slate-50'
                }`}
              >
                {/* Avatar — red dot indicator when this row is waiting
                    on a human reply, layered on top of the existing
                    initials circle so it's visible at glance even when
                    the badge below is scrolled off the row. */}
                <div className="relative shrink-0">
                  <div className={`w-11 h-11 rounded-full flex items-center justify-center font-semibold text-sm ${
                    c.isAI ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-600'
                  }`}>
                    {initials(c.customer)}
                  </div>
                  {awaitingAgent && (
                    <span
                      aria-hidden="true"
                      className="absolute -top-0.5 -end-0.5 w-3 h-3 rounded-full bg-red-500 ring-2 ring-white animate-pulse"
                    />
                  )}
                </div>

                {/* Content */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-0.5">
                    <p className="text-sm font-semibold text-slate-900 truncate">{c.customer}</p>
                    <span className="text-xs text-slate-400 shrink-0 ms-2">{formatRiyadh(c.time)}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <p className="text-xs text-slate-500 truncate flex-1">{c.lastMsg}</p>
                    {c.unread > 0 && (
                      <span className={`ms-2 min-w-[18px] h-[18px] px-1 text-white text-xs rounded-full flex items-center justify-center shrink-0 ${
                        awaitingAgent ? 'bg-red-500' : 'bg-brand-500'
                      }`}>
                        {c.unread}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    {/* "يطلب موظف" — highest-priority visual signal.
                        Driven by the same _isAwaitingAgent helper as
                        the filter + sort + row chrome above so the
                        three never disagree. Rendered FIRST so it
                        wins the limited horizontal space on narrow
                        widths. The pulse stays on the pill only — the
                        row itself doesn't animate so scanning the list
                        isn't fatiguing. */}
                    {awaitingAgent && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-red-100 text-red-700 border border-red-300 shadow-sm animate-pulse">
                        <AlertTriangle className="w-2.5 h-2.5" /> {cp.badges.requestsStaff}
                      </span>
                    )}
                    {c.lastPaymentConfirmedAt && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200">
                        <PackageCheck className="w-2.5 h-2.5" /> {cp.badges.paymentConfirmed}
                      </span>
                    )}
                    {/* Unsubscribe badges */}
                    {c.isUnsubscribed ? (
                      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-300">
                        <BellOff className="w-2.5 h-2.5" /> {cp.badges.unsubscribed}
                      </span>
                    ) : c.pendingUnsubscribe ? (
                      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200">
                        <BellOff className="w-2.5 h-2.5" /> {cp.badges.pendingUnsub}
                      </span>
                    ) : null}
                    {/* Secondary status — only shown if the row is NOT
                        already flagged as awaiting (the red pill above
                        already conveys the strongest signal we have). */}
                    {!awaitingAgent && c.status === 'human' ? (
                      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-orange-50 text-orange-600 border border-orange-200">
                        <User className="w-3 h-3" /> {cp.badges.humanReply}
                      </span>
                    ) : !awaitingAgent && !c.isUnsubscribed && !c.pendingUnsubscribe && c.lastMsgType && c.lastMsgType !== 'customer' && eventBadge[c.lastMsgType] ? (
                      <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full border ${eventBadge[c.lastMsgType].cls}`}>
                        {eventBadge[c.lastMsgType].icon}
                        {eventBadge[c.lastMsgType].label}
                      </span>
                    ) : !awaitingAgent && !c.isUnsubscribed && !c.pendingUnsubscribe && c.unread > 0 ? (
                      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-green-50 text-green-600 border border-green-200">
                        <MessageSquare className="w-2.5 h-2.5" /> {cp.badges.customerMessage}
                      </span>
                    ) : null}
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
        {hasMoreServer && (
          <div className="border-t border-slate-100 p-2 bg-white shrink-0">
            <button
              type="button"
              disabled={loadingMore}
              onClick={() => void appendNextPage()}
              className="w-full py-2 text-xs font-medium text-brand-700 bg-brand-50 rounded-lg hover:bg-brand-100 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loadingMore ? cp.loadingMore : cp.loadMore}
            </button>
          </div>
        )}
      </div>

      {/* ── PANEL 2: Chat view ────────────────────────────────────────────────── */}
      <div className={`
        flex-1 flex flex-col
        ${mobileView === 'list' ? 'hidden md:flex' : 'flex'}
      `}>
        {!selected ? (
          /* Empty state — desktop only */
          <div className="flex-1 flex items-center justify-center bg-slate-50">
            <div className="text-center px-6">
              <div className="w-20 h-20 bg-slate-100 rounded-full flex items-center justify-center mx-auto mb-4">
                <Bot className="w-10 h-10 text-slate-300" />
              </div>
              <p className="text-sm font-medium text-slate-500">{cp.emptyDetailTitle}</p>
              <p className="text-xs text-slate-400 mt-1">{cp.emptyDetailSubtitle}</p>
            </div>
          </div>
        ) : (
          <>
            {/* Chat header — sticky on mobile like WhatsApp */}
            <div className="sticky top-0 z-20 flex items-center gap-2 px-3 md:px-5 py-2.5 md:py-3 border-b border-slate-100 bg-white shadow-sm shrink-0 min-w-0">
              {/* Back → conversation list (mobile only) */}
              <button
                onClick={goBackToList}
                className="md:hidden -ms-1 p-2 rounded-full hover:bg-slate-100 text-slate-600 active:bg-slate-200 transition-colors shrink-0"
                aria-label={cp.actions.back}
              >
                <BackIcon className="w-5 h-5" />
              </button>

              {/* Avatar */}
              <div className={`w-9 h-9 rounded-full flex items-center justify-center shrink-0 font-semibold text-sm ${
                selected.isAI ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-600'
              }`}>
                {initials(selected.customer)}
              </div>

              {/* Name + phone */}
              <div className="flex-1 min-w-0 overflow-hidden">
                <button
                  type="button"
                  onClick={openEditCustomerName}
                  className="group flex min-w-0 w-full flex-col items-start text-start rounded-md -mx-1 px-1 py-0.5 hover:bg-slate-50 transition-colors"
                  title={cp.editCustomerName.title}
                >
                  <span className="flex items-center gap-1.5 min-w-0 w-full">
                    <span className="text-sm font-semibold text-slate-900 truncate flex-1 min-w-0">
                      {conversationHasDisplayName(selected, phonesMatch)
                        ? selected.customer
                        : selected.phone}
                    </span>
                    <Pencil
                      aria-hidden="true"
                      className="w-3.5 h-3.5 shrink-0 text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity hidden md:block"
                    />
                  </span>
                  <span className={`text-xs text-slate-400 flex items-center gap-1 truncate w-full mt-0.5 ${
                    conversationHasDisplayName(selected, phonesMatch) ? 'md:mt-0' : 'md:hidden'
                  }`}>
                    <Phone className="w-3 h-3 shrink-0 md:hidden" />
                    <span className="truncate">{selected.phone}</span>
                  </span>
                </button>
              </div>

              {/* Desktop: pause/resume + menu. Mobile: menu only (AI toggle in reply bar). */}
              <div className="flex items-center gap-1 shrink-0">
                {!_isBlocked(selected) && (() => {
                  const humanTakeover =
                    !!selected.needsHuman ||
                    !!selected.handoffActive ||
                    selected.status === 'human'
                  const intelligenceOff = humanTakeover || !!selected.aiPaused
                  return intelligenceOff ? (
                    <button
                      className="hidden md:flex items-center justify-center gap-1.5 btn-secondary text-xs py-1.5 px-3 text-emerald-600 border-emerald-200 bg-emerald-50 hover:bg-emerald-100"
                      onClick={resumeIntelligenceForSelected}
                      title={cp.actions.resumeAI}
                      aria-label={cp.actions.resumeAI}
                    >
                      <Play className="w-3.5 h-3.5" />
                      {cp.actions.resumeAI}
                    </button>
                  ) : (
                    <button
                      className="hidden md:flex items-center justify-center gap-1.5 btn-secondary text-xs py-1.5 px-3 text-amber-600 border-amber-200 bg-amber-50 hover:bg-amber-100"
                      onClick={pauseIntelligenceForSelected}
                      title={cp.actions.pauseAI}
                      aria-label={cp.actions.pauseAI}
                    >
                      <Pause className="w-3.5 h-3.5" />
                      {cp.actions.pauseAI}
                    </button>
                  )
                })()}

                <div className="relative" ref={headerMenuRef}>
                  <button
                    type="button"
                    className="w-9 h-9 flex items-center justify-center rounded-full hover:bg-slate-100 text-slate-500 active:bg-slate-200"
                    onClick={() => setHeaderMenuOpen((open) => !open)}
                    aria-label={cp.actions.moreActions}
                    aria-expanded={headerMenuOpen}
                  >
                    <MoreVertical className="w-4 h-4" />
                  </button>

                  {headerMenuOpen && selected && (() => {
                    const humanTakeover =
                      !!selected.needsHuman ||
                      !!selected.handoffActive ||
                      selected.status === 'human'
                    return (
                      <div
                        className="absolute end-0 top-full mt-1 w-56 bg-white rounded-xl shadow-lg border border-slate-200 py-1 z-50"
                        dir={dir}
                      >
                        {!_isBlocked(selected) && !humanTakeover && (
                          <button
                            type="button"
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
                            onClick={() => {
                              setHeaderMenuOpen(false)
                              void handleHandoff()
                            }}
                          >
                            <UserCheck className="w-4 h-4 text-slate-500" />
                            {cp.actions.takeOver}
                          </button>
                        )}
                        {humanTakeover && (
                          <button
                            type="button"
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                            onClick={() => {
                              setHeaderMenuOpen(false)
                              void endHumanSupervisionForSelected()
                            }}
                            disabled={endingSupervision}
                          >
                            {endingSupervision ? (
                              <Clock className="w-4 h-4 animate-spin" />
                            ) : (
                              <RotateCcw className="w-4 h-4" />
                            )}
                            {cp.actions.endSupervision}
                          </button>
                        )}
                        <CampaignExcludeControl
                          customerId={selected.customerId ?? undefined}
                          phone={selected.phone}
                          optedOut={!!selected.marketingOptOutManual}
                          customerLabel={selected.customer || selected.phone}
                          variant="menu"
                          onMenuClose={() => setHeaderMenuOpen(false)}
                          onSuccess={(nextOptedOut) => {
                            _optimisticUpdate(selected.phone, {
                              marketingOptOutManual: nextOptedOut,
                            })
                            setActionToast(
                              nextOptedOut
                                ? cp.toasts.excludedFromCampaigns
                                : cp.toasts.reEnabledFromCampaigns,
                            )
                            void reloadFirstPagePreserveTail({ silent: true })
                          }}
                        />
                        {!_isBlocked(selected) && (
                          <>
                            <div className="my-1 border-t border-slate-100" />
                            <button
                              type="button"
                              className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-rose-600 hover:bg-rose-50"
                              onClick={() => {
                                setHeaderMenuOpen(false)
                                void handleBlockNumber()
                              }}
                            >
                              <Ban className="w-4 h-4" />
                              {cp.actions.blockNumber}
                            </button>
                          </>
                        )}
                      </div>
                    )
                  })()}
                </div>
              </div>
            </div>

            {/* Human-takeover banner — shown above the AI-paused banner so
                the merchant always sees the takeover state explicitly. */}
            {(selected.needsHuman || selected.handoffActive || selected.status === 'human') && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-blue-50 border-b border-blue-200 text-sm text-blue-700">
                <UserCheck className="w-4 h-4 shrink-0 text-blue-500" />
                <span>
                  <strong>{cp.banners.humanSupervision}</strong>
                  {selected.takenOverBy && <> — <strong>{selected.takenOverBy}</strong></>}
                </span>
              </div>
            )}

            {actionToast && (
              <div className="flex items-center gap-2 px-4 py-2.5 bg-emerald-50 border-b border-emerald-200 text-sm text-emerald-800">
                <CheckCircle2 className="w-4 h-4 shrink-0 text-emerald-600" />
                <span className="flex-1">{actionToast}</span>
                <button
                  type="button"
                  className="p-1 rounded hover:bg-emerald-100 text-emerald-600"
                  onClick={() => setActionToast(null)}
                  aria-label={cp.actions.close}
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            )}

            {actionErrorToast && (
              <div className="flex items-center gap-2 px-4 py-2.5 bg-rose-50 border-b border-rose-200 text-sm text-rose-800">
                <AlertCircle className="w-4 h-4 shrink-0 text-rose-600" />
                <span className="flex-1">{actionErrorToast}</span>
                <button
                  type="button"
                  className="p-1 rounded hover:bg-rose-100 text-rose-600"
                  onClick={() => setActionErrorToast(null)}
                  aria-label={cp.actions.close}
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            )}

            <EditCustomerNameModal
              open={editNameOpen}
              onClose={() => { if (!editNameSaving) setEditNameOpen(false) }}
              initialName={selected ? conversationEditInitialName(selected, phonesMatch) : ''}
              saving={editNameSaving}
              onSave={(name) => { void handleSaveCustomerName(name) }}
              labels={cp.editCustomerName}
              dir={dir}
            />

            {/* AI paused banner (manual pause path only — takeover has its
                own banner above). */}
            {selected.aiPaused && !(selected.needsHuman || selected.handoffActive || selected.status === 'human') && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-sm text-amber-700">
                <Pause className="w-4 h-4 shrink-0 text-amber-500" />
                <span>
                  <strong>{cp.banners.aiPaused}</strong>
                  {selected.aiPausedReason && (
                    <>
                      {' '}—{' '}
                      <strong>{pauseReasonLabel(selected.aiPausedReason)}</strong>
                    </>
                  )}
                </span>
              </div>
            )}

            {/* Unsubscribe banner */}
            {selected.isUnsubscribed && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-slate-100 border-b border-slate-200 text-sm text-slate-600">
                <BellOff className="w-4 h-4 shrink-0 text-slate-500" />
                <span>{cp.banners.unsubscribed}</span>
              </div>
            )}
            {!selected.isUnsubscribed && selected.pendingUnsubscribe && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-sm text-amber-700">
                <BellOff className="w-4 h-4 shrink-0 text-amber-500" />
                <span>{cp.banners.pendingUnsub}</span>
              </div>
            )}

            {/* Messages area */}
            <div className="relative flex-1 min-h-0 flex flex-col">
              <div
                ref={messagesScrollRef}
                className="flex-1 overflow-y-auto py-4 px-3 md:px-5 space-y-1"
                style={{ background: 'linear-gradient(180deg, #f8f9fb 0%, #f1f3f6 100%)' }}
                onTouchStart={markUserScrolling}
                onTouchMove={markUserScrolling}
                onWheel={markUserScrolling}
                onScroll={(e) => {
                  const el = e.currentTarget
                  syncScrollAnchors(el)
                  if (el.scrollTop <= 48 && selected && hasMoreMessages && !loadingOlderMessages) {
                    void loadOlderMessages(selected.phone)
                  }
                }}
              >
              {loadingOlderMessages && (
                <div className="flex items-center justify-center py-2 gap-2 text-xs text-slate-400">
                  <Loader2 className="w-3.5 h-3.5 animate-spin text-brand-500" />
                  {cp.loadingMore}
                </div>
              )}
              {loadingMessages && selected.messages.length === 0 && (
                <div className="space-y-3 px-2 py-4 animate-pulse">
                  {[false, true, false, true].map((out, i) => (
                    <div key={`msg-sk-${i}`} className={`flex ${out ? 'justify-end' : 'justify-start'}`}>
                      <div className={`h-10 rounded-2xl bg-slate-200/80 ${out ? 'w-2/5' : 'w-1/2'}`} />
                    </div>
                  ))}
                </div>
              )}
              {!loadingMessages && selected.messages.length === 0 && (
                <div className="text-center py-10 text-xs text-slate-400">{cp.noMessages}</div>
              )}

              {selected.messages.map((m, idx) => {
                const isOut   = m.direction === 'out'
                const prevMsg = selected.messages[idx - 1]
                // Compare day boundaries in Riyadh time, not on a brittle
                // ``split(' ')[0]`` of an opaque backend ISO string.
                const dayKey  = formatRiyadhDate(m.time)
                const prevDay = prevMsg ? formatRiyadhDate(prevMsg.time) : ''
                const showDate = !prevMsg || prevDay !== dayKey

                return (
                  <div key={m.id}>
                    {/* Date separator */}
                    {showDate && (
                      <div className="flex justify-center my-3">
                        <span className="text-xs text-slate-500 bg-white px-3 py-1 rounded-full shadow-sm border border-slate-100">
                          {dayKey}
                        </span>
                      </div>
                    )}

                    <div className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1`}>
                      <div className={`flex flex-col ${isOut ? 'items-end' : 'items-start'} max-w-[78%] md:max-w-md`}>
                        {/* Smart event badge */}
                        {isOut && m.eventType && m.eventType !== 'customer' && eventBadge[m.eventType] && (
                          <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border mb-0.5 ${eventBadge[m.eventType].cls}`}>
                            {eventBadge[m.eventType].icon}
                            {eventBadge[m.eventType].label}
                          </span>
                        )}

                        {/* Bubble + WhatsApp-style Buttons */}
                        {(() => {
                          const sep = '━━━━━'
                          const hasButtons = m.body.includes(sep)
                          const textPart = hasButtons ? m.body.split(sep)[0].trimEnd() : m.body
                          const btnLines = hasButtons
                            ? m.body.split(sep).slice(1).join('').trim().split('\n').filter(Boolean)
                            : []

                          const parseBtn = (raw: string) => {
                            const t = raw.trim()
                            if (t.startsWith('📋')) return { icon: '📋', label: t.replace(/^📋\s*/, ''), type: 'copy' as const }
                            if (t.startsWith('🔗')) return { icon: '🔗', label: t.replace(/^🔗\s*/, ''), type: 'url' as const }
                            if (t.startsWith('↩️')) return { icon: '↩️', label: t.replace(/^↩️\s*/, ''), type: 'reply' as const }
                            return { icon: '', label: t, type: 'reply' as const }
                          }

                          // Media preview: render the audio player / image
                          // preview INSTEAD of the textual bubble.
                          //
                          // Originally this only fired for inbound customer
                          // media (``!isOut && m.media``). May 2026 P1 fix:
                          // also fire for OUTBOUND merchant-mobile echoes
                          // (Coexistence ``smb_message_echo``) so an image
                          // the merchant sent from his mobile WhatsApp app
                          // appears as a real image instead of the literal
                          // ``[merchant_image]`` placeholder.
                          //
                          // The backend's ``_build_media_block`` only
                          // returns a non-null ``media`` when a real
                          // storage URL exists (it reads
                          // ``extra_metadata.normalized_inbound``), so
                          // relaxing the guard here cannot accidentally
                          // render a media bubble for unrelated outbound
                          // text.
                          const mediaPreview = m.media || null

                          return (
                            <>
                              {mediaPreview ? (
                                <div className={`
                                  relative px-3 py-2 text-sm leading-relaxed shadow-sm
                                  ${isOut
                                    ? 'bg-amber-50 text-slate-800 rounded-2xl rounded-ee-sm border border-amber-100'
                                    : 'bg-white text-slate-800 rounded-2xl rounded-es-sm border border-slate-100'}
                                `}>
                                  <InboundMediaPreview media={mediaPreview} />
                                </div>
                              ) : (
                                (() => {
                                  // ── Bubble theming by send status ──────────────
                                  // Outbound bubbles MUST look different from the
                                  // "delivered" state when the message never made
                                  // it (or hasn't been confirmed yet). The merchant
                                  // should be able to glance at the chat and see
                                  // failures at a distance.
                                  //
                                  //   • sent / null (historical) → full brand
                                  //   • queued                   → 70% opacity +
                                  //                                  dashed border
                                  //                                  + clock cue
                                  //   • failed                   → red-tinted
                                  //                                  background +
                                  //                                  red border +
                                  //                                  serif italic
                                  //                                  underline-style
                                  //                                  to read as
                                  //                                  "draft, not
                                  //                                  delivered"
                                  let outboundTheme = 'bg-brand-500 text-white'
                                  if (isOut) {
                                    if (m.sendStatus === 'queued') {
                                      outboundTheme =
                                        'bg-brand-400/70 text-white/95 border border-dashed border-white/40'
                                    } else if (m.sendStatus === 'failed') {
                                      outboundTheme =
                                        'bg-red-50 text-red-900 border border-red-300 ring-1 ring-red-100'
                                    }
                                  }
                                  const radiusOut = btnLines.length
                                    ? 'rounded-t-2xl rounded-ee-sm'
                                    : 'rounded-2xl rounded-ee-sm'
                                  const radiusIn = btnLines.length
                                    ? 'rounded-t-2xl rounded-es-sm'
                                    : 'rounded-2xl rounded-es-sm border border-slate-100'
                                  return (
                                    <div className={`
                                      relative px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words
                                      shadow-sm
                                      ${isOut
                                        ? `${outboundTheme} ${radiusOut}`
                                        : `bg-white text-slate-800 ${radiusIn}`
                                      }
                                    `}>
                                      {textPart}
                                      {/* "DRAFT" overlay watermark for failed
                                          messages — makes the bubble feel like
                                          something that did NOT leave the
                                          merchant's outbox. */}
                                      {isOut && m.sendStatus === 'failed' && (
                                        <span className="absolute -top-2 start-2 px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider bg-red-500 text-white shadow-sm">
                                          {cp.delivery.notSent}
                                        </span>
                                      )}
                                    </div>
                                  )
                                })()
                              )}
                              {btnLines.length > 0 && (
                                <div className="flex flex-col gap-[3px] mt-[3px] w-full">
                                  {btnLines.map((line, bi) => {
                                    const btn = parseBtn(line)
                                    return (
                                      <div
                                        key={bi}
                                        className="
                                          flex items-center justify-center gap-2 py-2 px-3
                                          bg-white rounded-lg shadow-sm border border-slate-100
                                          text-[13px] font-medium text-[#00a884]
                                          cursor-default select-none
                                        "
                                      >
                                        {btn.type === 'url' && (
                                          <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                          </svg>
                                        )}
                                        {btn.type === 'copy' && (
                                          <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                                          </svg>
                                        )}
                                        {btn.type === 'reply' && (
                                          <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" />
                                          </svg>
                                        )}
                                        <span>{btn.label}</span>
                                      </div>
                                    )
                                  })}
                                </div>
                              )}
                            </>
                          )
                        })()}

                        {/* ── Send-status badge (failed only) ──────────────────
                            Rendered INSIDE the bubble column so the merchant
                            can immediately read WHY the customer didn't get
                            the message. Backend stamps this via
                            `core.outbound_send_status` from the WhatsApp
                            wire-layer outcome. */}
                        {isOut && m.sendStatus === 'failed' && m.sendError && (() => {
                          const errCopy = resolveOutboundSendError(m.sendError, cp, lang)
                          return (
                          <div
                            className="mt-1 px-2 py-1 rounded-md text-[11px] bg-red-50 text-red-700 border border-red-200 flex items-start gap-1.5 max-w-full"
                            title={
                              [
                                errCopy.label,
                                errCopy.advice,
                                m.sendError.code != null ? `Meta code=${m.sendError.code}` : null,
                                m.sendError.subcode != null ? `subcode=${m.sendError.subcode}` : null,
                              ].filter(Boolean).join(' • ')
                            }
                          >
                            <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-[1px]" />
                            <div className="flex flex-col leading-tight gap-1 min-w-0">
                              <span className="font-medium">{cp.delivery.notSent}: {errCopy.label}</span>
                              {errCopy.advice && (
                                <span className="text-red-600/80">{errCopy.advice}</span>
                              )}
                              {m.sendError.key === 'out_of_24h_window' && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    const target = selected?.phone
                                      ? `/templates?phone=${encodeURIComponent(selected.phone)}`
                                      : '/templates'
                                    navigate(target)
                                  }}
                                  className="self-start mt-0.5 inline-flex items-center gap-1 px-2 py-1 rounded-md bg-red-600 hover:bg-red-700 active:bg-red-800 text-white text-[11px] font-semibold transition-colors"
                                >
                                  <FileText className="w-3 h-3" />
                                  {cp.actions.sendTemplate}
                                </button>
                              )}
                            </div>
                          </div>
                          )
                        })()}

                        {/* Time + send-status icon */}
                        <div className={`flex items-center gap-1 mt-0.5 px-1 ${isOut ? 'flex-row-reverse' : ''}`}>
                          <span className="text-xs text-slate-400">
                            {formatRiyadhTime(m.time)}
                          </span>
                          {isOut && (() => {
                            // ── Per-status icon (replaces the unconditional ✔✔) ──
                            //
                            // STRICT safeguard: a ✔✔ ("delivered to WhatsApp
                            // edge") is shown ONLY when the wire layer
                            // confirmed BOTH:
                            //   1. ``sendStatus === 'sent'`` (200 from
                            //      Meta / 360dialog) AND
                            //   2. ``wamid`` is a non-empty string
                            //
                            // Some providers occasionally return 2xx with a
                            // missing/null ``messages[0].id`` (transient
                            // upstream weirdness, or a queue-only ack). We
                            // refuse to claim "delivered" without the
                            // wamid — those rows render as a single grey
                            // check ("سُلّمت للنظام لكن لم يتأكد wamid")
                            // until the wamid arrives or a webhook
                            // re-stamps the row.
                            //
                            // Historical rows (sendStatus === undefined)
                            // keep the legacy ✔✔ so old conversations
                            // don't suddenly look failed after deploy.
                            switch (m.sendStatus) {
                              case 'queued':
                                return (
                                  <Clock
                                    className="w-3.5 h-3.5 text-slate-300 animate-pulse"
                                    aria-label={cp.delivery.pending}
                                  />
                                )
                              case 'failed':
                                return (
                                  <AlertCircle
                                    className="w-3.5 h-3.5 text-red-500"
                                    aria-label={cp.delivery.failed}
                                  />
                                )
                              case 'sent': {
                                const hasWamid = typeof m.wamid === 'string'
                                  && m.wamid.trim().length > 0
                                if (!hasWamid) {
                                  return (
                                    <Check
                                      className="w-3.5 h-3.5 text-slate-400"
                                      aria-label={cp.delivery.awaitingWamid}
                                    />
                                  )
                                }
                                return (
                                  <CheckCheck
                                    className="w-3.5 h-3.5 text-brand-400"
                                    aria-label={cp.delivery.sent}
                                  />
                                )
                              }
                              default:
                                // Historical / pre-fix rows: keep the legacy
                                // appearance so old chats don't visibly
                                // regress. The merchant only sees the new
                                // strict signal on rows actually stamped
                                // by the wire-layer bridge.
                                return (
                                  <CheckCheck
                                    className="w-3.5 h-3.5 text-brand-400"
                                    aria-label={cp.delivery.sent}
                                  />
                                )
                            }
                          })()}
                        </div>
                      </div>
                    </div>
                  </div>
                )
              })}
              <div ref={messagesEndRef} />
              </div>
            </div>

            {/* Reply bar — mobile: زر الذكاء الأساسي فقط (باقي الإجراءات في ⋮) */}
            <div className="sm:hidden flex items-center gap-2 px-3 py-2 bg-white border-t border-slate-100">
              {!_isBlocked(selected) &&
                (() => {
                  const humanTakeover =
                    !!selected.needsHuman ||
                    !!selected.handoffActive ||
                    selected.status === 'human'
                  const intelligenceOff = humanTakeover || !!selected.aiPaused
                  return (
                    <button
                      type="button"
                      className={`w-full flex items-center justify-center gap-1.5 text-xs py-2 px-3 rounded-lg font-medium active:opacity-90 ${
                        intelligenceOff
                          ? 'bg-emerald-50 text-emerald-700'
                          : 'bg-amber-50 text-amber-700'
                      }`}
                      onClick={
                        intelligenceOff
                          ? resumeIntelligenceForSelected
                          : pauseIntelligenceForSelected
                      }
                    >
                      {intelligenceOff ? (
                        <>
                          <Play className="w-3.5 h-3.5" /> {cp.actions.resumeAI}
                        </>
                      ) : (
                        <>
                          <Pause className="w-3.5 h-3.5" /> {cp.actions.pauseAI}
                        </>
                      )}
                    </button>
                  )
                })()}
            </div>

            {/* Reply input */}
            <div className="px-3 md:px-5 py-2 md:py-3 bg-white border-t border-slate-100">
              <div className="flex items-end gap-2">
                <textarea
                  ref={textareaRef}
                  rows={1}
                  value={reply}
                  onChange={handleTextareaInput}
                  onKeyDown={handleKeyDown}
                  placeholder={cp.replyPlaceholder}
                  className="
                    flex-1 resize-none rounded-2xl border border-slate-200 px-4 py-2.5
                    text-sm leading-relaxed bg-slate-50
                    focus:outline-none focus:ring-2 focus:ring-brand-200 focus:bg-white
                    placeholder:text-slate-400
                    min-h-[42px] max-h-[120px]
                  "
                  style={{ overflowY: 'auto' }}
                />
                <button
                  onClick={handleReply}
                  disabled={!reply.trim()}
                  className="
                    w-10 h-10 shrink-0 rounded-full flex items-center justify-center
                    bg-brand-500 text-white shadow-sm
                    disabled:opacity-40 disabled:cursor-not-allowed
                    active:bg-brand-600 transition-colors
                  "
                >
                  <Send className="w-4 h-4" />
                </button>
              </div>

              {/* AI hint */}
              {selected.status !== 'human' && (
                <p className="text-xs text-slate-400 mt-1.5 flex items-center gap-1 px-1">
                  <Bot className="w-3 h-3 text-brand-400 shrink-0" />
                  {cp.aiHandlingHint}
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
