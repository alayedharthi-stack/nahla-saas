/**
 * InboxDemo.tsx
 *
 * Interactive landing-page simulation of Nahla's unified WhatsApp inbox.
 * Shows the same building blocks as the real product:
 *   - tab filters with live counts (active, human, agent-request,
 *     campaigns, closed)
 *   - colour-coded conversation badges (AI / human reply / agent request /
 *     campaign / autopilot / cart recovery)
 *   - dynamic per-contact status pill that changes when the visitor
 *     selects another conversation
 *   - message bubbles for AI replies, templates with buttons, manual
 *     human replies, autopilot system messages and abandoned-cart
 *     reminders
 *
 * The data is mocked — no API calls — but the UX mirrors the production
 * inbox closely enough that visitors feel they are operating Nahla.
 */

import { useMemo, useState } from 'react'
import {
  Bot,
  Hand,
  AlertTriangle,
  Megaphone,
  Sparkles,
  ShoppingCart,
  CheckCheck,
  Search,
  Phone,
  Video,
  MoreVertical,
} from 'lucide-react'

// ─── Types ────────────────────────────────────────────────────────────────
type FilterId = 'all' | 'active' | 'human' | 'agent_req' | 'campaigns' | 'closed'

type MessageKind =
  | 'ai'
  | 'human'
  | 'autopilot'
  | 'campaign'
  | 'cart'
  | 'customer'

interface DemoMessage {
  kind: MessageKind
  text: string
  /** time label like "10:42" */
  time: string
  /** optional template buttons (campaign / cart) */
  buttons?: string[]
  /** show double-blue read ticks on outgoing messages */
  read?: boolean
}

interface DemoConversation {
  id: string
  name: string
  avatarColor: string
  initials: string
  preview: string
  time: string
  unread?: number
  /** which filter this conversation belongs to (drives counts + filtering) */
  bucket: Exclude<FilterId, 'all'>
  /** badge shown next to the contact name and in the list */
  badge: BadgeKind
  messages: DemoMessage[]
}

type BadgeKind =
  | 'ai'
  | 'human'
  | 'agent_req'
  | 'campaign'
  | 'autopilot'
  | 'cart'
  | 'closed'

// ─── Visual tokens ────────────────────────────────────────────────────────
const BADGE_META: Record<
  BadgeKind,
  { label: string; icon: typeof Bot; classes: string }
> = {
  ai:        { label: 'الذكاء الاصطناعي', icon: Bot,           classes: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  human:     { label: 'رد بشري',          icon: Hand,          classes: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  agent_req: { label: 'يطلب موظف',        icon: AlertTriangle, classes: 'bg-rose-500/15 text-rose-300 border-rose-500/35' },
  campaign:  { label: 'حملة تسويقية',     icon: Megaphone,     classes: 'bg-violet-500/15 text-violet-300 border-violet-500/30' },
  autopilot: { label: 'الطيار الآلي',     icon: Sparkles,      classes: 'bg-sky-500/15 text-sky-300 border-sky-500/30' },
  cart:      { label: 'استرجاع سلة',      icon: ShoppingCart,  classes: 'bg-orange-500/15 text-orange-300 border-orange-500/30' },
  closed:    { label: 'مغلقة',            icon: CheckCheck,    classes: 'bg-slate-500/15 text-slate-400 border-slate-500/30' },
}

const FILTER_LABELS: Record<FilterId, string> = {
  all:       'الكل',
  active:    'نشطة',
  human:     'بشري',
  agent_req: 'يطلب موظف',
  campaigns: 'حملات',
  closed:    'مغلقة',
}

// ─── Mock data ────────────────────────────────────────────────────────────
const CONVERSATIONS: DemoConversation[] = [
  {
    id: 'reem',
    name: 'ريم الحربي',
    initials: 'ر',
    avatarColor: 'from-rose-500 to-pink-600',
    preview: 'أبغى أتكلم مع موظف لو سمحت 🙏',
    time: 'الآن',
    unread: 2,
    bucket: 'agent_req',
    badge: 'agent_req',
    messages: [
      { kind: 'customer',  time: '10:38', text: 'السلام عليكم، عندي استفسار خاص بطلب سابق' },
      { kind: 'ai',        time: '10:38', read: true, text: 'وعليكم السلام أهلاً ريم 🌷 أكيد، اعطيني رقم الطلب وأخدمك مباشرة.' },
      { kind: 'customer',  time: '10:41', text: 'أبغى أتكلم مع موظف لو سمحت 🙏' },
    ],
  },
  {
    id: 'sara',
    name: 'سارة الأحمدي',
    initials: 'س',
    avatarColor: 'from-amber-500 to-orange-500',
    preview: 'تمام، أرسلي لي الرابط 🌷',
    time: 'دقيقتين',
    unread: 1,
    bucket: 'active',
    badge: 'ai',
    messages: [
      { kind: 'customer', time: '10:30', text: 'أبغى أعرف عن كريم الترطيب اليومي، هل متوفر؟' },
      { kind: 'ai',       time: '10:30', read: true, text: 'هلا سارة 🌷 نعم متوفر — العبوة 50مل بسعر 89 ريال، والشحن مجاني للطلبات فوق 150.' },
      { kind: 'ai',       time: '10:31', read: true, text: 'أرسل لكِ رابط الشراء المباشر؟' },
      { kind: 'customer', time: '10:32', text: 'تمام، أرسلي لي الرابط 🌷' },
    ],
  },
  {
    id: 'noof',
    name: 'نوف العتيبي',
    initials: 'ن',
    avatarColor: 'from-emerald-500 to-teal-600',
    preview: 'تم تأكيد طلبك، شكراً لتواصلك 💚',
    time: '15 د',
    bucket: 'human',
    badge: 'human',
    messages: [
      { kind: 'customer', time: '09:55', text: 'فيه خصم على المجموعة الكاملة؟' },
      { kind: 'ai',       time: '09:55', read: true, text: 'هلا نوف 🌷 خليني أتحقق وأرد عليكِ بالتفاصيل.' },
      { kind: 'human',    time: '10:02', read: true, text: 'مرحباً نوف، أنا تركي من فريق المتجر. أكيد، نقدر نعطيكِ خصم 12٪ على المجموعة الكاملة هذي المرة.' },
      { kind: 'human',    time: '10:25', read: true, text: 'تم تأكيد طلبك، شكراً لتواصلك 💚' },
    ],
  },
  {
    id: 'khalid',
    name: 'خالد المطيري',
    initials: 'خ',
    avatarColor: 'from-violet-500 to-purple-600',
    preview: 'عرض الجمعة البيضاء — خصم 25٪',
    time: 'ساعة',
    bucket: 'campaigns',
    badge: 'campaign',
    messages: [
      {
        kind: 'campaign',
        time: '09:40',
        read: true,
        text:
          'مرحباً خالد 🌷 الجمعة البيضاء بدأت في متجر نحلة!\n\n— خصم 25٪ على كل المنتجات\n— شحن مجاني للطلبات +150 ريال\n— العرض ينتهي بعد 48 ساعة',
        buttons: ['تسوّق الآن', 'إلغاء الاشتراك'],
      },
      { kind: 'customer', time: '09:48', text: 'ممتاز، أنا متابع' },
    ],
  },
  {
    id: 'mohammed',
    name: 'محمد القحطاني',
    initials: 'م',
    avatarColor: 'from-sky-500 to-blue-600',
    preview: 'تم شحن طلبك #4127 — رقم البوليصة 8842…',
    time: '3 س',
    bucket: 'active',
    badge: 'autopilot',
    messages: [
      { kind: 'customer',  time: 'أمس', text: 'بكم الشحن للرياض؟' },
      { kind: 'ai',        time: 'أمس', read: true, text: 'أهلاً محمد 🌷 الشحن للرياض 15 ريال ويصلك خلال 2-4 أيام عمل.' },
      { kind: 'customer',  time: 'أمس', text: 'تمام، خذ هذا طلبي' },
      { kind: 'autopilot', time: 'أمس', read: true, text: '✅ تم استلام طلبك #4127 وجاري تجهيزه.' },
      { kind: 'autopilot', time: '07:15', read: true, text: '🚚 تم شحن طلبك #4127 — رقم البوليصة 8842309561 (سمسا).' },
    ],
  },
  {
    id: 'fahd',
    name: 'فهد السويدي',
    initials: 'ف',
    avatarColor: 'from-orange-500 to-rose-600',
    preview: 'تذكير: سلتك في متجر نحلة لا تزال محفوظة…',
    time: '5 س',
    bucket: 'active',
    badge: 'cart',
    messages: [
      {
        kind: 'cart',
        time: '06:00',
        read: true,
        text:
          'فهد 🌷 سلتك في متجر نحلة لا تزال محفوظة لك.\n\nأكمل طلبك خلال الساعتين القادمتين واحصل على خصم 10٪.',
        buttons: ['أكمل طلبي', 'استخدم الكوبون'],
      },
    ],
  },
  {
    id: 'abdullah',
    name: 'عبدالله الزهراني',
    initials: 'ع',
    avatarColor: 'from-slate-500 to-slate-700',
    preview: 'شكراً، تم استلام الطلب 👍',
    time: 'أمس',
    bucket: 'closed',
    badge: 'closed',
    messages: [
      { kind: 'autopilot', time: 'أمس', read: true, text: '📦 تم تسليم طلبك #4081. شكراً لاختيارك متجر نحلة 💛' },
      { kind: 'customer',  time: 'أمس', text: 'شكراً، تم استلام الطلب 👍' },
    ],
  },
]

// ─── Filtering helper ─────────────────────────────────────────────────────
function matchesFilter(c: DemoConversation, f: FilterId): boolean {
  if (f === 'all') return true
  if (f === 'active') {
    // "active" mirrors the product: anything that is not closed and not a
    // pure agent request — open service-window conversations.
    return c.bucket !== 'closed' && c.bucket !== 'agent_req'
  }
  return c.bucket === f
}

// ─── Component ────────────────────────────────────────────────────────────
export default function InboxDemo() {
  const [filter, setFilter] = useState<FilterId>('all')
  const [selectedId, setSelectedId] = useState<string>('reem')

  const counts = useMemo(() => {
    const out: Record<FilterId, number> = {
      all: CONVERSATIONS.length,
      active: 0,
      human: 0,
      agent_req: 0,
      campaigns: 0,
      closed: 0,
    }
    for (const c of CONVERSATIONS) {
      ;(['active', 'human', 'agent_req', 'campaigns', 'closed'] as FilterId[]).forEach(
        f => {
          if (matchesFilter(c, f)) out[f] += 1
        },
      )
    }
    return out
  }, [])

  const visible = CONVERSATIONS.filter(c => matchesFilter(c, filter))
  const selected =
    visible.find(c => c.id === selectedId) ??
    visible[0] ??
    CONVERSATIONS[0]

  return (
    <div
      dir="rtl"
      className="relative w-full max-w-5xl mx-auto rounded-3xl border border-white/10 bg-slate-950/70 backdrop-blur-sm shadow-[0_20px_60px_-20px_rgba(0,0,0,0.7)] overflow-hidden"
    >
      {/* macOS-style window chrome */}
      <div className="flex items-center gap-1.5 px-4 py-2.5 border-b border-white/5 bg-slate-900/80">
        <span className="w-2.5 h-2.5 rounded-full bg-rose-400/70" />
        <span className="w-2.5 h-2.5 rounded-full bg-amber-400/70" />
        <span className="w-2.5 h-2.5 rounded-full bg-emerald-400/70" />
        <span className="ms-3 text-[11px] text-slate-500 font-medium tracking-wide">
          app.nahlah.ai / inbox
        </span>
        <span className="ms-auto inline-flex items-center gap-1.5 text-[10px] font-bold text-emerald-300 bg-emerald-500/10 border border-emerald-500/25 px-2 py-0.5 rounded-full">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          Live
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[320px_1fr] min-h-[520px]">
        {/* ── Conversation list ──────────────────────────────────────── */}
        <aside className="border-l border-white/5 bg-slate-900/50 flex flex-col">
          {/* Search bar */}
          <div className="px-3 pt-3 pb-2">
            <div className="flex items-center gap-2 bg-slate-800/70 border border-white/5 rounded-xl px-3 py-2 text-slate-400 text-xs">
              <Search className="w-3.5 h-3.5" />
              <span className="opacity-60">ابحث في المحادثات…</span>
            </div>
          </div>

          {/* Filter chips */}
          <div className="px-2 pb-2 flex gap-1.5 overflow-x-auto scrollbar-thin">
            {(Object.keys(FILTER_LABELS) as FilterId[]).map(f => {
              const active = filter === f
              const count = counts[f]
              return (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={[
                    'shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold border transition-colors',
                    active
                      ? 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                      : 'bg-slate-800/40 text-slate-400 border-white/5 hover:text-white hover:bg-slate-800',
                  ].join(' ')}
                >
                  {FILTER_LABELS[f]}
                  <span className={`text-[10px] tabular-nums ${
                    active ? 'text-amber-200/90' : 'text-slate-500'
                  }`}>
                    {count}
                  </span>
                </button>
              )
            })}
          </div>

          {/* Conversation rows */}
          <div className="flex-1 overflow-y-auto divide-y divide-white/5">
            {visible.length === 0 && (
              <p className="text-center text-slate-500 text-xs py-8">
                لا توجد محادثات في هذا الفلتر
              </p>
            )}
            {visible.map(c => {
              const Badge = BADGE_META[c.badge]
              const Icon = Badge.icon
              const isSel = selected?.id === c.id
              return (
                <button
                  key={c.id}
                  onClick={() => setSelectedId(c.id)}
                  className={[
                    'w-full text-right flex items-start gap-3 px-3 py-3 transition-colors',
                    isSel
                      ? 'bg-amber-500/[0.08] border-r-2 border-amber-500'
                      : 'hover:bg-slate-800/40',
                  ].join(' ')}
                >
                  <div className={`shrink-0 w-10 h-10 rounded-full bg-gradient-to-br ${c.avatarColor} flex items-center justify-center text-white font-bold text-sm shadow-md`}>
                    {c.initials}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <span className="text-white text-sm font-bold truncate">{c.name}</span>
                      <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[9px] font-bold ${Badge.classes}`}>
                        <Icon className="w-2.5 h-2.5" />
                        {Badge.label}
                      </span>
                    </div>
                    <div className="flex items-end justify-between gap-2">
                      <p className="text-slate-400 text-xs truncate flex-1 leading-relaxed">
                        {c.preview}
                      </p>
                      <div className="flex flex-col items-end gap-0.5 shrink-0">
                        <span className="text-[10px] text-slate-500 tabular-nums">{c.time}</span>
                        {c.unread ? (
                          <span className="bg-emerald-500 text-white text-[10px] font-bold w-4 h-4 rounded-full flex items-center justify-center">
                            {c.unread}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                </button>
              )
            })}
          </div>
        </aside>

        {/* ── Active conversation ────────────────────────────────────── */}
        <main className="flex flex-col bg-[url('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22120%22 height=%22120%22%3E%3Cg fill=%22%23f59e0b%22 fill-opacity=%220.025%22%3E%3Cpolygon points=%2260 0 75 8 75 26 60 34 45 26 45 8%22/%3E%3C/g%3E%3C/svg%3E')] bg-slate-950/70">
          {selected && (
            <>
              {/* Header */}
              <header className="flex items-center gap-3 px-4 py-3 border-b border-white/5 bg-slate-900/80">
                <div className={`w-10 h-10 rounded-full bg-gradient-to-br ${selected.avatarColor} flex items-center justify-center text-white font-bold shadow-md`}>
                  {selected.initials}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-white font-bold text-sm">{selected.name}</span>
                    <DynamicBadge kind={selected.badge} />
                  </div>
                  <span className="text-[10px] text-slate-500">+966 5• ••• ••••</span>
                </div>
                <div className="flex items-center gap-1 text-slate-500">
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="phone"><Phone className="w-4 h-4" /></button>
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="video"><Video className="w-4 h-4" /></button>
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="more"><MoreVertical className="w-4 h-4" /></button>
                </div>
              </header>

              {/* Messages */}
              <div className="flex-1 overflow-y-auto px-4 py-5 space-y-2.5">
                {selected.messages.map((m, i) => (
                  <MessageBubble key={i} msg={m} />
                ))}
              </div>

              {/* Composer */}
              <footer className="px-4 py-3 border-t border-white/5 bg-slate-900/80 flex items-center gap-2">
                <div className="flex-1 bg-slate-800/70 border border-white/5 rounded-2xl px-4 py-2.5 text-slate-500 text-xs">
                  اكتب رسالة… أو اترك نحلة ترد تلقائياً ✨
                </div>
                <button className="bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-xs px-4 py-2.5 rounded-2xl transition-colors">
                  إرسال
                </button>
              </footer>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

// ─── Sub-components ───────────────────────────────────────────────────────

function DynamicBadge({ kind }: { kind: BadgeKind }) {
  const meta = BADGE_META[kind]
  const Icon = meta.icon
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[10px] font-bold ${meta.classes}`}>
      <Icon className="w-3 h-3" />
      {meta.label}
    </span>
  )
}

function MessageBubble({ msg }: { msg: DemoMessage }) {
  const incoming = msg.kind === 'customer'

  // Outgoing message colours per kind
  const outgoingClasses: Record<Exclude<MessageKind, 'customer'>, string> = {
    ai:        'bg-amber-500/12 border-amber-500/25 text-amber-50',
    human:     'bg-emerald-500/12 border-emerald-500/25 text-emerald-50',
    autopilot: 'bg-sky-500/12 border-sky-500/25 text-sky-50',
    campaign:  'bg-violet-500/12 border-violet-500/25 text-violet-50',
    cart:      'bg-orange-500/12 border-orange-500/25 text-orange-50',
  }

  const tagClasses: Record<Exclude<MessageKind, 'customer'>, string> = {
    ai:        'text-amber-300',
    human:     'text-emerald-300',
    autopilot: 'text-sky-300',
    campaign:  'text-violet-300',
    cart:      'text-orange-300',
  }

  const tagLabel: Record<Exclude<MessageKind, 'customer'>, string> = {
    ai:        '🤖 الذكاء',
    human:     '✋ ردّ بشري',
    autopilot: '✨ الطيار الآلي',
    campaign:  '📣 حملة',
    cart:      '🛒 استرجاع سلة',
  }

  if (incoming) {
    return (
      <div className="flex justify-start">
        <div className="max-w-[78%] bg-slate-800/80 border border-white/5 text-slate-100 text-sm rounded-2xl rounded-bl-sm px-3.5 py-2 shadow-sm">
          <p className="whitespace-pre-line leading-relaxed">{msg.text}</p>
          <span className="block text-[10px] text-slate-500 mt-1 text-left">
            {msg.time}
          </span>
        </div>
      </div>
    )
  }

  const k = msg.kind as Exclude<MessageKind, 'customer'>
  return (
    <div className="flex justify-end">
      <div className={`max-w-[80%] border text-sm rounded-2xl rounded-br-sm px-3.5 py-2 shadow-sm ${outgoingClasses[k]}`}>
        <span className={`block text-[10px] font-bold mb-1 ${tagClasses[k]}`}>
          {tagLabel[k]}
        </span>
        <p className="whitespace-pre-line leading-relaxed">{msg.text}</p>
        {msg.buttons && msg.buttons.length > 0 && (
          <div className="mt-2 grid gap-1">
            {msg.buttons.map(b => (
              <span
                key={b}
                className="block text-center text-[11px] font-bold py-1.5 rounded-lg bg-white/5 border border-white/10 text-white"
              >
                {b}
              </span>
            ))}
          </div>
        )}
        <span className="flex items-center justify-end gap-1 text-[10px] text-slate-400 mt-1">
          {msg.time}
          {msg.read && <CheckCheck className="w-3 h-3 text-sky-300" />}
        </span>
      </div>
    </div>
  )
}
