import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bot, User, Send, Phone, Search, MoreVertical, UserCheck, RefreshCw } from 'lucide-react'
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

  const [selected, setSelected] = useState<Conversation | null>(null)
  const [filter, setFilter] = useState<'all' | 'active' | 'human' | 'closed'>('all')
  const [reply, setReply] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])

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
          // ?phone=… deep-link wins; otherwise keep previous selection.
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

  const handleReply = async () => {
    if (!selected || !reply.trim()) return
    try {
      await featureRealityApi.replyToConversation({
        customer_phone: selected.phone,
        message: reply.trim(),
      })
      setReply('')
      await load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر إرسال الرد')
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

  const filtered = conversations.filter(c => filter === 'all' || c.status === filter)

  const statusVariant = (s: string) =>
    s === 'active' ? 'green' : s === 'human' ? 'amber' : 'slate'

  const statusLabel = (s: string) =>
    s === 'active' ? 'ذكاء اصطناعي' : s === 'human' ? 'بشري' : 'مغلقة'

  return (
    <div className="flex h-[calc(100vh-8rem)] card overflow-hidden">
      {/* Conversation list */}
      <div className="w-72 border-e border-slate-100 flex flex-col shrink-0">
        {/* Search */}
        <div className="p-3 border-b border-slate-100">
          <div className="relative">
            <Search className="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input className="input ps-8 text-xs py-1.5" placeholder="ابحث في المحادثات…" />
          </div>
        </div>

        {/* Filter tabs */}
        <div className="flex gap-1 px-3 py-2 border-b border-slate-100">
          {(['all', 'active', 'human', 'closed'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                filter === f ? 'bg-brand-500 text-white' : 'text-slate-500 hover:bg-slate-100'
              }`}
            >
              {filterLabels[f]}
            </button>
          ))}
        </div>

        {/* List */}
        <ul className="flex-1 overflow-y-auto divide-y divide-slate-100">
          {filtered.length === 0 && (
            <li className="py-16 text-center text-xs text-slate-400">
              لا توجد محادثات بعد
            </li>
          )}
          {filtered.map((c) => (
            <li
              key={c.id}
              onClick={() => setSelected(c)}
              className={`flex items-start gap-2.5 px-3 py-3 cursor-pointer transition-colors ${
                selected?.id === c.id ? 'bg-brand-50' : 'hover:bg-slate-50'
              }`}
            >
              <div className="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center shrink-0 mt-0.5">
                <span className="text-slate-600 text-xs font-semibold">
                  {c.customer.split(' ').map(n => n[0]).join('').slice(0, 2)}
                </span>
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-semibold text-slate-900 truncate">{c.customer}</p>
                  <span className="text-xs text-slate-400 shrink-0 ms-1">{c.time}</span>
                </div>
                <p className="text-xs text-slate-500 truncate mt-0.5">{c.lastMsg}</p>
                <div className="flex items-center gap-1.5 mt-1">
                  {c.isAI ? <Bot className="w-3 h-3 text-brand-400" /> : <User className="w-3 h-3 text-slate-400" />}
                  <Badge label={statusLabel(c.status)} variant={statusVariant(c.status) as 'green' | 'amber' | 'slate'} />
                  {c.unread > 0 && (
                    <span className="ms-auto w-4 h-4 bg-brand-500 text-white text-xs rounded-full flex items-center justify-center">
                      {c.unread}
                    </span>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* Chat view */}
      <div className="flex-1 flex flex-col">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center bg-slate-50">
            <div className="text-center">
              <Bot className="w-10 h-10 text-slate-300 mx-auto mb-3" />
              <p className="text-sm text-slate-400">اختر محادثة للعرض</p>
              <p className="text-xs text-slate-300 mt-1">ستظهر المحادثات هنا عند وصول رسائل من العملاء</p>
            </div>
          </div>
        ) : (
          <>
            {/* Chat header */}
            <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-100 bg-white">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center">
                  <span className="text-slate-600 text-xs font-semibold">
                    {selected.customer.split(' ').map(n => n[0]).join('').slice(0, 2)}
                  </span>
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900">{selected.customer}</p>
                  <p className="text-xs text-slate-400 flex items-center gap-1">
                    <Phone className="w-3 h-3" /> {selected.phone}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {selected.status !== 'human' && (
                  <button className="btn-secondary text-xs py-1.5" onClick={handleHandoff}>
                    <UserCheck className="w-3.5 h-3.5" /> تولّ المحادثة
                  </button>
                )}
                {selected.status === 'human' && (
                  <button className="btn-secondary text-xs py-1.5" onClick={handleClose}>
                    <RefreshCw className="w-3.5 h-3.5" /> إغلاق المحادثة
                  </button>
                )}
                <button className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-400" onClick={handleClose}>
                  <MoreVertical className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 bg-slate-50">
              {selected.messages.map((m) => (
                <div key={m.id} className={`flex ${m.direction === 'out' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`max-w-xs lg:max-w-md xl:max-w-lg ${m.direction === 'out' ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
                    {m.direction === 'out' && m.isAI && (
                      <span className="text-xs text-brand-500 flex items-center gap-1 px-1">
                        <Bot className="w-3 h-3" /> رد نحلة
                      </span>
                    )}
                    <div
                      className={`px-3.5 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
                        m.direction === 'out'
                          ? 'bg-brand-500 text-white'
                          : 'bg-white text-slate-800 border border-slate-200 shadow-sm'
                      }`}
                    >
                      {m.body}
                    </div>
                    <span className="text-xs text-slate-400 px-1">{m.time}</span>
                  </div>
                </div>
              ))}
            </div>

            {/* Reply input */}
            <div className="px-5 py-3 border-t border-slate-100 bg-white">
              <div className="flex items-end gap-2">
                <textarea
                  rows={1}
                  value={reply}
                  onChange={(e) => setReply(e.target.value)}
                  placeholder="اكتب رسالة…"
                  className="input flex-1 resize-none text-sm"
                />
                <button className="btn-primary py-2 px-3 shrink-0" onClick={handleReply}>
                  <Send className="w-4 h-4" />
                </button>
              </div>
              <p className="text-xs text-slate-400 mt-1.5 flex items-center gap-1">
                <Bot className="w-3 h-3 text-brand-400" />
                نحلة تتولى هذه المحادثة — انقر «تولّ» للرد يدوياً
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
