import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Bot, User, Send, Phone, Search, MoreVertical,
  UserCheck, RefreshCw, ArrowRight, Check, CheckCheck,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import { featureRealityApi, type DashboardConversation, type DashboardMessage } from '../api/featureReality'

interface Conversation extends DashboardConversation {
  messages: DashboardMessage[]
}

const filterLabels: Record<string, string> = {
  all:    'الكل',
  active: 'نشطة',
  human:  'بشري',
  closed: 'مغلقة',
}

export default function Conversations() {
  const [searchParams] = useSearchParams()
  const requestedPhone = searchParams.get('phone')?.trim() || null

  const [selected, setSelected]     = useState<Conversation | null>(null)
  const [filter, setFilter]         = useState<'all' | 'active' | 'human' | 'closed'>('all')
  const [reply, setReply]           = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [searchQuery, setSearchQuery] = useState('')

  // mobile: 'list' = show list panel, 'chat' = show chat panel
  const [mobileView, setMobileView] = useState<'list' | 'chat'>('list')

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef    = useRef<HTMLTextAreaElement>(null)

  const phonesMatch = (a?: string | null, b?: string | null) => {
    const norm = (p?: string | null) =>
      (p || '').trim().replace(/^\+/, '').replace(/[\s-]/g, '')
    return !!a && !!b && norm(a) === norm(b)
  }

  const load = () => {
    featureRealityApi.conversations()
      .then(async ({ conversations }) => {
        const withMessages = await Promise.all(
          conversations.map(async (c) => {
            const msgRes = await featureRealityApi.conversationMessages(c.phone)
            return { ...c, messages: msgRes.messages }
          }),
        )
        setConversations(withMessages)
        setSelected((prev) => {
          if (requestedPhone) {
            const hit = withMessages.find(c => phonesMatch(c.phone, requestedPhone))
            if (hit) return hit
          }
          return withMessages.find(c => c.phone === prev?.phone) ?? prev
        })
      })
      .catch(() => setConversations([]))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedPhone])

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
      await load()
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

  const handleHandoff = async () => {
    if (!selected) return
    try {
      await featureRealityApi.handoffConversation({
        customer_phone: selected.phone,
        customer_name: selected.customer,
        last_message: selected.lastMsg,
      })
      await load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر تحويل المحادثة')
    }
  }

  const handleClose = async () => {
    if (!selected) return
    if (!window.confirm('إغلاق هذه المحادثة؟')) return
    try {
      await featureRealityApi.closeConversation({
        customer_phone: selected.phone,
      })
      await load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر إغلاق المحادثة')
    }
  }

  const filtered = conversations.filter(c => {
    const matchFilter = filter === 'all' || c.status === filter
    const matchSearch = !searchQuery || c.customer.includes(searchQuery) || c.phone.includes(searchQuery)
    return matchFilter && matchSearch
  })

  const statusVariant = (s: string) =>
    s === 'active' ? 'green' : s === 'human' ? 'amber' : 'slate'

  const statusLabel = (s: string) =>
    s === 'active' ? 'ذكاء اصطناعي' : s === 'human' ? 'بشري' : 'مغلقة'

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
        <div className="flex gap-1 px-3 py-2 bg-white border-b border-slate-100 overflow-x-auto scrollbar-none">
          {(['all', 'active', 'human', 'closed'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors ${
                filter === f
                  ? 'bg-brand-500 text-white shadow-sm'
                  : 'text-slate-500 hover:bg-slate-100'
              }`}
            >
              {filterLabels[f]}
              {f !== 'all' && conversations.filter(c => c.status === f).length > 0 && (
                <span className={`ms-1 ${filter === f ? 'text-white/70' : 'text-slate-400'}`}>
                  {conversations.filter(c => c.status === f).length}
                </span>
              )}
            </button>
          ))}
        </div>

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
                  <span className="text-xs text-slate-400 shrink-0 ms-2">{c.time}</span>
                </div>
                <div className="flex items-center justify-between">
                  <p className="text-xs text-slate-500 truncate flex-1">{c.lastMsg}</p>
                  {c.unread > 0 && (
                    <span className="ms-2 min-w-[18px] h-[18px] px-1 bg-brand-500 text-white text-xs rounded-full flex items-center justify-center shrink-0">
                      {c.unread}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 mt-1">
                  {c.isAI ? <Bot className="w-3 h-3 text-brand-400" /> : <User className="w-3 h-3 text-slate-400" />}
                  <Badge
                    label={statusLabel(c.status)}
                    variant={statusVariant(c.status) as 'green' | 'amber' | 'slate'}
                  />
                </div>
              </div>
            </li>
          ))}
        </ul>
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

              {/* Actions */}
              <div className="flex items-center gap-1">
                {selected.status !== 'human' && (
                  <button
                    className="hidden sm:flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3"
                    onClick={handleHandoff}
                  >
                    <UserCheck className="w-3.5 h-3.5" />
                    تولّ
                  </button>
                )}
                {selected.status === 'human' && (
                  <button
                    className="hidden sm:flex items-center gap-1.5 btn-secondary text-xs py-1.5 px-3"
                    onClick={handleClose}
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    إغلاق
                  </button>
                )}
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
                const showDate = !prevMsg || prevMsg.time.split(' ')[0] !== m.time.split(' ')[0]

                return (
                  <div key={m.id}>
                    {/* Date separator */}
                    {showDate && (
                      <div className="flex justify-center my-3">
                        <span className="text-xs text-slate-500 bg-white px-3 py-1 rounded-full shadow-sm border border-slate-100">
                          {m.time.split(' ')[0]}
                        </span>
                      </div>
                    )}

                    <div className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1`}>
                      <div className={`flex flex-col ${isOut ? 'items-end' : 'items-start'} max-w-[78%] md:max-w-md`}>
                        {/* AI label */}
                        {isOut && m.isAI && (
                          <span className="text-xs text-brand-500 flex items-center gap-1 px-2 mb-0.5">
                            <Bot className="w-3 h-3" /> رد نحلة
                          </span>
                        )}

                        {/* Bubble */}
                        <div className={`
                          relative px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words
                          shadow-sm
                          ${isOut
                            ? 'bg-brand-500 text-white rounded-2xl rounded-ee-sm'
                            : 'bg-white text-slate-800 rounded-2xl rounded-es-sm border border-slate-100'
                          }
                        `}>
                          {m.body}
                        </div>

                        {/* Time + read status */}
                        <div className={`flex items-center gap-1 mt-0.5 px-1 ${isOut ? 'flex-row-reverse' : ''}`}>
                          <span className="text-xs text-slate-400">
                            {m.time.split(' ').slice(1).join(' ') || m.time}
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

            {/* Reply bar — mobile actions row */}
            <div className="sm:hidden flex items-center gap-2 px-3 py-2 bg-white border-t border-slate-100">
              {selected.status !== 'human' && (
                <button
                  className="flex-1 flex items-center justify-center gap-1.5 text-xs py-2 px-3 rounded-lg bg-amber-50 text-amber-600 font-medium active:bg-amber-100"
                  onClick={handleHandoff}
                >
                  <UserCheck className="w-3.5 h-3.5" /> تولّ المحادثة
                </button>
              )}
              {selected.status === 'human' && (
                <button
                  className="flex-1 flex items-center justify-center gap-1.5 text-xs py-2 px-3 rounded-lg bg-red-50 text-red-500 font-medium active:bg-red-100"
                  onClick={handleClose}
                >
                  <RefreshCw className="w-3.5 h-3.5" /> إغلاق المحادثة
                </button>
              )}
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
