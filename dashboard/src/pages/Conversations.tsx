import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Bot, User, Send, Phone, Search, MoreVertical,
  UserCheck, ArrowRight, Check, CheckCheck,
  Megaphone, Zap, ShoppingCart, PackageCheck, MessageSquare, AlertTriangle, BellOff,
  Pause, Play, Ban,
} from 'lucide-react'

import { featureRealityApi, type DashboardConversation, type DashboardMessage, type MessageEventType, type AIPauseReason } from '../api/featureReality'
import { getTenantId } from '../auth'
import InboundMediaPreview from '../components/inbound/InboundMediaPreview'

import { formatRiyadh, formatRiyadhDate, formatRiyadhTime } from '../lib/datetime'
import { useDashboardPoll } from '../lib/dashboardPolling'

const LIST_PAGE_LIMIT = 60
const LIST_POLL_MS = 32_000

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

const EVENT_BADGE: Record<MessageEventType, { label: string; icon: React.ReactNode; cls: string }> = {
  ai:         { label: 'ذكاء اصطناعي', icon: <Bot className="w-3 h-3" />, cls: 'bg-brand-50 text-brand-600 border-brand-200' },
  campaign:   { label: 'حملة تسويقية', icon: <Megaphone className="w-3 h-3" />, cls: 'bg-blue-50 text-blue-600 border-blue-200' },
  automation: { label: 'طيار آلي', icon: <Zap className="w-3 h-3" />, cls: 'bg-amber-50 text-amber-600 border-amber-200' },
  cod:        { label: 'تأكيد طلب COD', icon: <PackageCheck className="w-3 h-3" />, cls: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
  manual:     { label: 'رد يدوي', icon: <User className="w-3 h-3" />, cls: 'bg-slate-50 text-slate-600 border-slate-200' },
  system:     { label: 'نظام', icon: <MessageSquare className="w-3 h-3" />, cls: 'bg-purple-50 text-purple-600 border-purple-200' },
  customer:   { label: '', icon: null, cls: '' },
}

interface Conversation extends DashboardConversation {
  messages: DashboardMessage[]
}

const filterLabels: Record<string, string> = {
  all:          'الكل',
  active:       'نشطة',
  human:        'بشري',
  agent_req:    'طلب موظف',
  paused:       'متوقف الذكاء',
  blocked:      'محظور',
  unsubscribed: 'ألغى الاشتراك',
  closed:       'مغلقة',
}

export default function Conversations() {
  const [searchParams] = useSearchParams()
  const requestedPhone = searchParams.get('phone')?.trim() || null

  const [selected, setSelected]     = useState<Conversation | null>(null)
  const [filter, setFilter]         = useState<'all' | 'active' | 'human' | 'agent_req' | 'paused' | 'blocked' | 'unsubscribed' | 'closed'>('all')
  const [reply, setReply]           = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [searchQuery, setSearchQuery] = useState('')

  // mobile: 'list' = show list panel, 'chat' = show chat panel
  const [mobileView, setMobileView] = useState<'list' | 'chat'>('list')

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef    = useRef<HTMLTextAreaElement>(null)

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
  const filterRef = useRef<'all' | 'active' | 'human' | 'agent_req' | 'paused' | 'blocked' | 'unsubscribed' | 'closed'>('all')

  const [listStaleBanner, setListStaleBanner] = useState<string | null>(null)
  const [hasMoreServer, setHasMoreServer]      = useState(false)
  const [loadingMore, setLoadingMore]           = useState(false)

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
      setConversations((prev) => mergeHeadPreserveTailServerOrder(rows, prev))
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
        setListStaleBanner('تعذّر تحديث المحادثات مؤقتًا، سنعيد المحاولة تلقائيًا.')
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
      setConversations((prev) => mergeRowsKeepMessages(rowsSnap, prev))

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
      setListStaleBanner('تعذّر تحديث المحادثات مؤقتًا، سنعيد المحاولة تلقائيًا.')
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
        setListStaleBanner('تعذّر تحميل المزيد مؤقتًا، حاول خلال ثوانٍ.')
      }
    } finally {
      setLoadingMore(false)
      if (gen === listReqGen.current) listBusyRef.current = false
    }
  }

  const loadMessagesForOpenChat = async (phone: string) => {
    msgsCtrlRef.current?.abort()
    const ac = new AbortController()
    msgsCtrlRef.current = ac
    const t0 = performance.now()
    try {
      const { messages } = await featureRealityApi.conversationMessages(phone, {
        signal: ac.signal,
        limit: 150,
      })
      setConversations((prev) =>
        prev.map((c) => (c.phone === phone ? { ...c, messages } : c)),
      )
      setSelected((prevSel) =>
        prevSel && prevSel.phone === phone ? { ...prevSel, messages } : prevSel,
      )
    } catch (err: unknown) {
      if (ac.signal.aborted) return
      logFetchFail(`/conversations/messages/${encodeURIComponent(phone)}`, t0, err, phone)
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

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [selected?.messages])

  // Auto-resize textarea
  const handleTextareaInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setReply(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
  }

  const selectConversation = (c: Conversation) => {
    setSelected(c)
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
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر إرسال الرد')
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleReply()
    }
  }

  const _optimisticUpdate = (phone: string, patch: Partial<DashboardConversation>) => {
    setConversations(prev => prev.map(c => c.phone === phone ? { ...c, ...patch } : c))
    setSelected(prev => prev && prev.phone === phone ? { ...prev, ...patch } : prev)
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
      alert(e instanceof Error ? e.message : 'تعذّر تحويل المحادثة')
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
      alert(e instanceof Error ? e.message : 'تعذّر إيقاف الذكاء')
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
      alert(e instanceof Error ? e.message : 'تعذّر تشغيل الذكاء')
    }
  }

  const handleBlockNumber = async () => {
    if (!selected) return
    const ok = window.confirm(
      `سيتم إضافة الرقم ${selected.phone} لقائمة الأرقام الممنوعة، ` +
      'ولن يتلقى الذكاء أي رسالة من هذا الرقم. متابعة؟',
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
      alert(e instanceof Error ? e.message : 'تعذّر حظر الرقم')
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

  // "مغلقة": server-stamped ``status='closed'`` is the canonical
  // signal (set by /conversations/close or by automations). The
  // 24h WhatsApp window expiry is included as a secondary signal so
  // dormant conversations the merchant has stopped engaging with
  // still surface here even before they're explicitly closed. This
  // mirrors the merchant mental model: "any conversation that's no
  // longer a live thread".
  const _isClosed = (c: DashboardConversation) =>
    c.status === 'closed' || c.windowOpen === false

  const filtered = conversations.filter(c => {
    let matchFilter = false
    if (filter === 'all') matchFilter = true
    else if (filter === 'active') matchFilter = c.windowOpen === true && !_isUnsubscribed(c)
    else if (filter === 'human') matchFilter = _isHumanResponding(c)
    else if (filter === 'agent_req') matchFilter = _isAwaitingAgent(c)
    else if (filter === 'paused') matchFilter = _isAIPausedOnly(c)
    else if (filter === 'blocked') matchFilter = _isBlocked(c)
    else if (filter === 'unsubscribed') matchFilter = _isUnsubscribed(c)
    else if (filter === 'closed') matchFilter = _isClosed(c)
    const matchSearch = !searchQuery || c.customer.includes(searchQuery) || c.phone.includes(searchQuery)
    return matchFilter && matchSearch
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
  return (
    <div className="
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
          <h2 className="text-base font-bold text-white md:text-slate-900">المحادثات</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-white/70 md:hidden">
              {conversations.filter(c => c.unread > 0).length > 0
                ? `${conversations.filter(c => c.unread > 0).length} غير مقروءة`
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
              placeholder="ابحث في المحادثات…"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        {/* Filter tabs */}
        <div className="flex gap-1.5 px-3 py-2 bg-white border-b border-slate-100 overflow-x-auto" style={{ scrollbarWidth: 'thin' }}>
          {(['all', 'active', 'human', 'agent_req', 'paused', 'blocked', 'unsubscribed', 'closed'] as const).map((f) => {
            const count = f === 'all' ? 0
              : f === 'active' ? conversations.filter(c => c.windowOpen === true && !_isUnsubscribed(c)).length
              : f === 'human' ? conversations.filter(c => _isHumanResponding(c)).length
              : f === 'agent_req' ? conversations.filter(c => _isAwaitingAgent(c)).length
              : f === 'paused' ? conversations.filter(c => _isAIPausedOnly(c)).length
              : f === 'blocked' ? conversations.filter(c => _isBlocked(c)).length
              : f === 'unsubscribed' ? conversations.filter(c => _isUnsubscribed(c)).length
              : conversations.filter(c => _isClosed(c)).length

            const activeClass =
              f === 'agent_req'    ? 'bg-red-500 text-white shadow-sm' :
              f === 'paused'       ? 'bg-amber-500 text-white shadow-sm' :
              f === 'blocked'      ? 'bg-rose-600 text-white shadow-sm' :
              f === 'unsubscribed' ? 'bg-slate-600 text-white shadow-sm' :
              'bg-brand-500 text-white shadow-sm'

            const inactiveClass =
              f === 'agent_req' && count > 0    ? 'text-red-600 bg-red-50 hover:bg-red-100' :
              f === 'paused' && count > 0       ? 'text-amber-700 bg-amber-50 hover:bg-amber-100' :
              f === 'blocked' && count > 0      ? 'text-rose-700 bg-rose-50 hover:bg-rose-100' :
              f === 'unsubscribed' && count > 0 ? 'text-slate-600 bg-slate-100 hover:bg-slate-200' :
              'text-slate-500 hover:bg-slate-100'

            const countClass =
              filter === f ? 'text-white/70' :
              f === 'agent_req' ? 'text-red-400' :
              f === 'paused' ? 'text-amber-500' :
              f === 'blocked' ? 'text-rose-500' :
              f === 'unsubscribed' ? 'text-slate-500' :
              'text-slate-400'

            return (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors ${
                  filter === f ? activeClass : inactiveClass
                }`}
              >
                {f === 'unsubscribed' && <BellOff className="inline w-3 h-3 me-1 opacity-70" />}
                {f === 'paused' && <Pause className="inline w-3 h-3 me-1 opacity-70" />}
                {f === 'blocked' && <Ban className="inline w-3 h-3 me-1 opacity-70" />}
                {filterLabels[f]}
                {f !== 'all' && count > 0 && (
                  <span className={`ms-1 ${countClass}`}>{count}</span>
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
              إذا كنت تريد استعادة هؤلاء العملاء، ننصحك بالتواصل معهم شخصياً لمعرفة أسباب الإلغاء ومحاولة استعادتهم.
              سيعودون تلقائياً فور إرسالهم أي رسالة.
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
                تحديث الآن
              </button>
            </div>
          </div>
        )}

        {/* Conversation list */}
        <ul className="flex-1 overflow-y-auto divide-y divide-slate-100">
          {filtered.length === 0 && (
            <li className="py-20 text-center">
              <Bot className="w-10 h-10 text-slate-200 mx-auto mb-3" />
              <p className="text-sm text-slate-400">لا توجد محادثات</p>
            </li>
          )}
          {filtered.map((c) => (
            <li
              key={c.id}
              onClick={() => selectConversation(c)}
              className={`flex items-start gap-3 px-4 py-3.5 cursor-pointer active:bg-slate-100 transition-colors ${
                selected?.id === c.id ? 'bg-brand-50 border-e-2 border-brand-400' : 'hover:bg-slate-50'
              }`}
            >
              {/* Avatar */}
              <div className={`w-11 h-11 rounded-full flex items-center justify-center shrink-0 font-semibold text-sm ${
                c.isAI ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-600'
              }`}>
                {initials(c.customer)}
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
                    <span className="ms-2 min-w-[18px] h-[18px] px-1 bg-brand-500 text-white text-xs rounded-full flex items-center justify-center shrink-0">
                      {c.unread}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                  {/* Unsubscribe badges — shown first, highest priority */}
                  {c.isUnsubscribed ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-300">
                      <BellOff className="w-2.5 h-2.5" /> ألغى الاشتراك
                    </span>
                  ) : c.pendingUnsubscribe ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200">
                      <BellOff className="w-2.5 h-2.5" /> بانتظار تأكيد الإلغاء
                    </span>
                  ) : null}
                  {/* Status badges */}
                  {c.status === 'human' && c.handoffReason === 'customer_request' && c.lastMsgType !== 'manual' ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-200 animate-pulse">
                      <AlertTriangle className="w-2.5 h-2.5" /> يطلب موظف
                    </span>
                  ) : c.status === 'human' ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-orange-50 text-orange-600 border border-orange-200">
                      <User className="w-3 h-3" /> رد بشري
                    </span>
                  ) : !c.isUnsubscribed && !c.pendingUnsubscribe && c.lastMsgType && c.lastMsgType !== 'customer' && EVENT_BADGE[c.lastMsgType] ? (
                    <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full border ${EVENT_BADGE[c.lastMsgType].cls}`}>
                      {EVENT_BADGE[c.lastMsgType].icon}
                      {EVENT_BADGE[c.lastMsgType].label}
                    </span>
                  ) : !c.isUnsubscribed && !c.pendingUnsubscribe && c.unread > 0 ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-green-50 text-green-600 border border-green-200">
                      <MessageSquare className="w-2.5 h-2.5" /> رسالة عميل
                    </span>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ul>
        {hasMoreServer && (
          <div className="border-t border-slate-100 p-2 bg-white shrink-0">
            <button
              type="button"
              disabled={loadingMore}
              onClick={() => void appendNextPage()}
              className="w-full py-2 text-xs font-medium text-brand-700 bg-brand-50 rounded-lg hover:bg-brand-100 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loadingMore ? 'جاري التحميل…' : 'تحميل المزيد من المحادثات'}
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
              <p className="text-sm font-medium text-slate-500">اختر محادثة للعرض</p>
              <p className="text-xs text-slate-400 mt-1">ستظهر المحادثات هنا عند وصول رسائل من العملاء</p>
            </div>
          </div>
        ) : (
          <>
            {/* Chat header */}
            <div className="flex items-center gap-2 px-3 md:px-5 py-3 border-b border-slate-100 bg-white shadow-sm">
              {/* Back button — mobile only */}
              <button
                onClick={goBackToList}
                className="md:hidden -ms-1 p-2 rounded-full hover:bg-slate-100 text-slate-600 active:bg-slate-200 transition-colors"
                aria-label="رجوع"
              >
                <ArrowRight className="w-5 h-5" />
              </button>

              {/* Avatar */}
              <div className={`w-9 h-9 rounded-full flex items-center justify-center shrink-0 font-semibold text-sm ${
                selected.isAI ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-600'
              }`}>
                {initials(selected.customer)}
              </div>

              {/* Name + phone */}
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-slate-900 truncate">{selected.customer}</p>
                <p className="text-xs text-slate-400 flex items-center gap-1 truncate">
                  <Phone className="w-3 h-3 shrink-0" />
                  {selected.phone}
                </p>
              </div>

              {/* تحكم بالذكاء: زر واحد (تشغيل / إيقاف مؤقت) + تولّي اختياري */}
              <div className="flex items-center gap-1 flex-wrap justify-end">
                {!_isBlocked(selected) && (() => {
                  const humanTakeover =
                    !!selected.needsHuman ||
                    !!selected.handoffActive ||
                    selected.status === 'human'
                  const intelligenceOff = humanTakeover || !!selected.aiPaused
                  return (
                    <>
                      {!humanTakeover && (
                        <button
                          className="flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3"
                          onClick={handleHandoff}
                          title="تولّي المحادثة بدلاً من الذكاء (تنتقل لتبويب «بشري»)"
                        >
                          <UserCheck className="w-3.5 h-3.5" />
                          تولّي
                        </button>
                      )}
                      {intelligenceOff ? (
                        <button
                          className="flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3 text-emerald-600 border-emerald-200 bg-emerald-50 hover:bg-emerald-100"
                          onClick={resumeIntelligenceForSelected}
                          title="استئناف ردود الذكاء الآلية (يشمل إنهاء التولّي البشري إن وُجد)"
                        >
                          <Play className="w-3.5 h-3.5" />
                          تشغيل الذكاء
                        </button>
                      ) : (
                        <button
                          className="flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3 text-amber-600 border-amber-200 bg-amber-50 hover:bg-amber-100"
                          onClick={pauseIntelligenceForSelected}
                          title="إيقاف الردود الآلية مؤقتاً بدون تحويل للموظف"
                        >
                          <Pause className="w-3.5 h-3.5" />
                          إيقاف الذكاء مؤقتاً
                        </button>
                      )}
                    </>
                  )
                })()}
                <button
                  className="hidden sm:flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3 text-rose-600 border-rose-200 bg-rose-50 hover:bg-rose-100"
                  onClick={handleBlockNumber}
                  title="إضافة الرقم لقائمة الأرقام الممنوعة (الذكاء لن يرد عليه أبداً)"
                >
                  <Ban className="w-3.5 h-3.5" />
                  حظر الرقم
                </button>
                <button
                  className="w-9 h-9 flex items-center justify-center rounded-full hover:bg-slate-100 text-slate-400 active:bg-slate-200"
                  onClick={() => {
                    // Show context menu — future feature
                  }}
                >
                  <MoreVertical className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Human-takeover banner — shown above the AI-paused banner so
                the merchant always sees the takeover state explicitly. */}
            {(selected.needsHuman || selected.handoffActive || selected.status === 'human') && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-blue-50 border-b border-blue-200 text-sm text-blue-700">
                <UserCheck className="w-4 h-4 shrink-0 text-blue-500" />
                <span>
                  هذه المحادثة <strong>تحت إشراف موظف بشري</strong>
                  {selected.takenOverBy && <> — بواسطة <strong>{selected.takenOverBy}</strong></>}
                  . لن يرد الذكاء حتى تضغط «تشغيل الذكاء» أعلاه.
                </span>
              </div>
            )}

            {/* AI paused banner (manual pause path only — takeover has its
                own banner above). */}
            {selected.aiPaused && !(selected.needsHuman || selected.handoffActive || selected.status === 'human') && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-sm text-amber-700">
                <Pause className="w-4 h-4 shrink-0 text-amber-500" />
                <span>
                  الذكاء <strong>متوقف مؤقتاً لهذه المحادثة</strong>
                  {selected.aiPausedReason && (
                    <>
                      {' '}— السبب:{' '}
                      <strong>
                        {(selected.aiPausedReason === 'manual' || selected.aiPausedReason === 'manual_pause') && 'إيقاف يدوي'}
                        {selected.aiPausedReason === 'human_handoff' && 'تحويل لموظف'}
                        {selected.aiPausedReason === 'manual_takeover' && 'تولّي بشري'}
                        {selected.aiPausedReason === 'support_escalation' && 'تصعيد للدعم'}
                        {selected.aiPausedReason === 'bot_loop_detected' && 'تم اكتشاف دوامة ردود آلية'}
                        {selected.aiPausedReason === 'rate_limit' && 'تجاوز الحد الأقصى للردود'}
                        {selected.aiPausedReason === 'internal_number' && 'رقم داخلي / محظور'}
                      </strong>
                    </>
                  )}
                  . الرسائل تُحفظ بدون إرسال أي رد آلي. اضغط «تشغيل الذكاء» للاستئناف.
                </span>
              </div>
            )}

            {/* Unsubscribe banner */}
            {selected.isUnsubscribed && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-slate-100 border-b border-slate-200 text-sm text-slate-600">
                <BellOff className="w-4 h-4 shrink-0 text-slate-500" />
                <span>
                  هذا العميل <strong>ألغى اشتراكه</strong> — لن يتلقى أي رسائل آلية أو حملات.
                  إذا أرسل رسالة جديدة، سيعود تلقائياً للقوائم العادية.
                </span>
              </div>
            )}
            {!selected.isUnsubscribed && selected.pendingUnsubscribe && (
              <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-sm text-amber-700">
                <BellOff className="w-4 h-4 shrink-0 text-amber-500" />
                <span>
                  هذا العميل <strong>بانتظار تأكيد إلغاء الاشتراك</strong> — النظام في انتظار رده خلال 24 ساعة.
                </span>
              </div>
            )}

            {/* Messages area */}
            <div
              className="flex-1 overflow-y-auto py-4 px-3 md:px-5 space-y-1"
              style={{ background: 'linear-gradient(180deg, #f8f9fb 0%, #f1f3f6 100%)' }}
            >
              {selected.messages.length === 0 && (
                <div className="text-center py-10 text-xs text-slate-400">لا توجد رسائل بعد</div>
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
                        {isOut && m.eventType && m.eventType !== 'customer' && EVENT_BADGE[m.eventType] && (
                          <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border mb-0.5 ${EVENT_BADGE[m.eventType].cls}`}>
                            {EVENT_BADGE[m.eventType].icon}
                            {EVENT_BADGE[m.eventType].label}
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

                          // Inbound media: render the audio player / image
                          // preview INSTEAD of the textual bubble. The
                          // backend already concatenated transcript /
                          // description into ``body`` for AI context, but
                          // here we want the merchant to see the actual
                          // recording / image with the extracted text shown
                          // discreetly below it (per spec point #9).
                          const inboundMedia = !isOut && m.media ? m.media : null

                          return (
                            <>
                              {inboundMedia ? (
                                <div className={`
                                  relative px-3 py-2 text-sm leading-relaxed shadow-sm
                                  bg-white text-slate-800 rounded-2xl rounded-es-sm border border-slate-100
                                `}>
                                  <InboundMediaPreview media={inboundMedia} />
                                </div>
                              ) : (
                                <div className={`
                                  relative px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words
                                  shadow-sm
                                  ${isOut
                                    ? `bg-brand-500 text-white ${btnLines.length ? 'rounded-t-2xl rounded-ee-sm' : 'rounded-2xl rounded-ee-sm'}`
                                    : `bg-white text-slate-800 ${btnLines.length ? 'rounded-t-2xl rounded-es-sm' : 'rounded-2xl rounded-es-sm border border-slate-100'}`
                                  }
                                `}>
                                  {textPart}
                                </div>
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

                        {/* Time + read status */}
                        <div className={`flex items-center gap-1 mt-0.5 px-1 ${isOut ? 'flex-row-reverse' : ''}`}>
                          <span className="text-xs text-slate-400">
                            {formatRiyadhTime(m.time)}
                          </span>
                          {isOut && (
                            <CheckCheck className="w-3.5 h-3.5 text-brand-400" />
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                )
              })}
              <div ref={messagesEndRef} />
            </div>

            {/* Reply bar — mobile quick actions */}
            <div className="sm:hidden flex items-center gap-2 px-3 py-2 bg-white border-t border-slate-100">
              {!_isBlocked(selected) &&
                (() => {
                  const humanTakeover =
                    !!selected.needsHuman ||
                    !!selected.handoffActive ||
                    selected.status === 'human'
                  const intelligenceOff = humanTakeover || !!selected.aiPaused
                  return (
                    <>
                      {!humanTakeover && (
                        <button
                          className="flex-1 flex items-center justify-center gap-1.5 text-xs py-2 px-3 rounded-lg bg-amber-50 text-amber-600 font-medium active:bg-amber-100"
                          type="button"
                          onClick={handleHandoff}
                        >
                          <UserCheck className="w-3.5 h-3.5" /> تولّي
                        </button>
                      )}
                      <button
                        type="button"
                        className={`flex-1 flex items-center justify-center gap-1.5 text-xs py-2 px-3 rounded-lg font-medium active:opacity-90 ${
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
                            <Play className="w-3.5 h-3.5" /> تشغيل الذكاء
                          </>
                        ) : (
                          <>
                            <Pause className="w-3.5 h-3.5" /> إيقاف الذكاء مؤقتاً
                          </>
                        )}
                      </button>
                    </>
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
                  placeholder="اكتب رسالة…"
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
                  نحلة تتولى هذه المحادثة — اضغط «تولّ» للرد يدوياً
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
