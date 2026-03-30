import { useState } from 'react'
import { Bot, User, Send, Phone, Search, MoreVertical, UserCheck, RefreshCw } from 'lucide-react'
import Badge from '../components/ui/Badge'

interface Message {
  id: string
  direction: 'in' | 'out'
  body: string
  time: string
  isAI?: boolean
}

interface Conversation {
  id: string
  customer: string
  phone: string
  lastMsg: string
  time: string
  isAI: boolean
  status: 'active' | 'human' | 'closed'
  unread: number
  messages: Message[]
}

const conversations: Conversation[] = [
  {
    id: 'c1', customer: 'Ahmed Al-Rashid', phone: '+966 50 123 4567',
    lastMsg: 'هل الهودي الأحمر متوفر بـ XL؟', time: 'منذ 2د', isAI: true, status: 'active', unread: 2,
    messages: [
      { id: 'm1', direction: 'in',  body: 'مرحباً، هل عندكم الهودي الأحمر بمقاس XL؟', time: '10:04 ص' },
      { id: 'm2', direction: 'out', body: 'أهلاً بك أحمد! نعم، هودي Classic الأحمر متوفر بمقاس XL. السعر 199 ر.س.', time: '10:04 ص', isAI: true },
      { id: 'm3', direction: 'in',  body: 'ممتاز! هل أقدر أطلب وأدفع عند الاستلام؟', time: '10:05 ص' },
      { id: 'm4', direction: 'out', body: 'بالتأكيد! أنشأت لك رابط دفع: pay.nahla.co/a7f2x\nإجمالي الطلب: 199 ر.س + الشحن.', time: '10:05 ص', isAI: true },
      { id: 'm5', direction: 'in',  body: 'هل الهودي الأحمر متوفر بـ XL؟', time: '10:06 ص' },
    ],
  },
  {
    id: 'c2', customer: 'Sara Al-Zahrani', phone: '+966 55 987 6543',
    lastMsg: 'متى يصل طلبي؟', time: 'منذ 11د', isAI: false, status: 'human', unread: 1,
    messages: [
      { id: 'm1', direction: 'in',  body: 'مرحبا، طلبت من أمس، متى يوصل؟',              time: '9:50 ص' },
      { id: 'm2', direction: 'out', body: 'أهلاً سارة! طلبك رقم #3805 قيد التجهيز الآن.', time: '9:51 ص', isAI: true },
      { id: 'm3', direction: 'in',  body: 'لكن التتبع ما يشتغل!',                         time: '9:55 ص' },
      { id: 'm4', direction: 'out', body: 'تم تصعيد الأمر لفريقنا، سيردون عليك قريباً.', time: '9:55 ص', isAI: false },
    ],
  },
  {
    id: 'c3', customer: 'Mohammed Khalid', phone: '+966 56 222 3344',
    lastMsg: 'هل عندكم خصم على الكميات؟', time: 'منذ 34د', isAI: true, status: 'active', unread: 0,
    messages: [
      { id: 'm1', direction: 'in',  body: 'هل تعطون خصم على الطلبات الكبيرة؟',                                                      time: '9:30 ص' },
      { id: 'm2', direction: 'out', body: 'نعم! للطلبات فوق 1,000 ر.س نقدم خصم 10%. هل تريد أرسل لك كوبون؟', time: '9:30 ص', isAI: true },
    ],
  },
  {
    id: 'c4', customer: 'Fatima Al-Hassan', phone: '+966 54 551 2200',
    lastMsg: 'شكراً ✅', time: 'منذ 1س', isAI: true, status: 'closed', unread: 0,
    messages: [
      { id: 'm1', direction: 'in',  body: 'أبي القميص الأزرق',                   time: '8:00 ص' },
      { id: 'm2', direction: 'out', body: 'تفضلي رابط الطلب: pay.nahla.co/b3x9k', time: '8:01 ص', isAI: true },
      { id: 'm3', direction: 'in',  body: 'شكراً، قدّمت الطلب ✅',               time: '8:03 ص' },
    ],
  },
]

const filterLabels: Record<string, string> = {
  all:    'الكل',
  active: 'نشطة',
  human:  'بشري',
  closed: 'مغلقة',
}

export default function Conversations() {
  const [selected, setSelected] = useState<Conversation>(conversations[0])
  const [filter, setFilter] = useState<'all' | 'active' | 'human' | 'closed'>('all')
  const [reply, setReply] = useState('')

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
          {filtered.map((c) => (
            <li
              key={c.id}
              onClick={() => setSelected(c)}
              className={`flex items-start gap-2.5 px-3 py-3 cursor-pointer transition-colors ${
                selected.id === c.id ? 'bg-brand-50' : 'hover:bg-slate-50'
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
              <button className="btn-secondary text-xs py-1.5">
                <UserCheck className="w-3.5 h-3.5" /> تولّ المحادثة
              </button>
            )}
            {selected.status === 'human' && (
              <button className="btn-secondary text-xs py-1.5">
                <RefreshCw className="w-3.5 h-3.5" /> أعد لنهلة
              </button>
            )}
            <button className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-400">
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
                    <Bot className="w-3 h-3" /> رد نهلة
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
            <button className="btn-primary py-2 px-3 shrink-0">
              <Send className="w-4 h-4" />
            </button>
          </div>
          <p className="text-xs text-slate-400 mt-1.5 flex items-center gap-1">
            <Bot className="w-3 h-3 text-brand-400" />
            نهلة تتولى هذه المحادثة — انقر «تولّ» للرد يدوياً
          </p>
        </div>
      </div>
    </div>
  )
}
