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
  ArrowRight,
  ArrowLeft,
} from 'lucide-react'
import type { Lang } from '../../i18n/types'
import {
  INBOX_DEMO_COPY,
  type InboxBadgeKind,
  type InboxDemoConversation,
  type InboxDemoMessage,
  type InboxFilterId,
  type InboxMessageKind,
} from './inboxDemoCopy'
// ─── Visual tokens ────────────────────────────────────────────────────────
const BADGE_ICONS: Record<InboxBadgeKind, typeof Bot> = {
  ai: Bot,
  human: Hand,
  agent_req: AlertTriangle,
  campaign: Megaphone,
  autopilot: Sparkles,
  cart: ShoppingCart,
  closed: CheckCheck,
}

const BADGE_CLASSES: Record<InboxBadgeKind, string> = {
  ai: 'bg-amber-100 text-amber-700 border-amber-200/80',
  human: 'bg-emerald-100 text-emerald-700 border-emerald-200/80',
  agent_req: 'bg-rose-100 text-rose-700 border-rose-200/80',
  campaign: 'bg-violet-100 text-violet-700 border-violet-200/80',
  autopilot: 'bg-sky-100 text-sky-700 border-sky-200/80',
  cart: 'bg-orange-100 text-orange-700 border-orange-200/80',
  closed: 'bg-slate-100 text-slate-600 border-slate-200/80',
}
function matchesFilter(c: InboxDemoConversation, f: InboxFilterId): boolean {
  if (f === 'all') return true
  if (f === 'active') {
    // "active" mirrors the product: anything that is not closed and not a
    // pure agent request — open service-window conversations.
    return c.bucket !== 'closed' && c.bucket !== 'agent_req'
  }
  return c.bucket === f
}

// ─── Component ────────────────────────────────────────────────────────────
export default function InboxDemo({ lang = 'ar' }: { lang?: Lang }) {
  const copy = INBOX_DEMO_COPY[lang]
  const conversations = copy.conversations
  const dir = lang === 'ar' ? 'rtl' : 'ltr'
  const BackIcon = dir === 'rtl' ? ArrowRight : ArrowLeft

  const [filter, setFilter] = useState<InboxFilterId>('all')
  const [selectedId, setSelectedId] = useState<string>('reem')
  /** On mobile: true = show conversation detail, false = show list */
  const [mobileShowChat, setMobileShowChat] = useState(false)

  const counts = useMemo(() => {
    const out: Record<InboxFilterId, number> = {
      all: conversations.length,
      active: 0,
      human: 0,
      agent_req: 0,
      campaigns: 0,
      closed: 0,
    }
    for (const c of conversations) {
      ;(['active', 'human', 'agent_req', 'campaigns', 'closed'] as InboxFilterId[]).forEach(
        f => {
          if (matchesFilter(c, f)) out[f] += 1
        },
      )
    }
    return out
  }, [conversations])

  const visible = conversations.filter(c => matchesFilter(c, filter))
  const selected =
    visible.find(c => c.id === selectedId) ??
    visible[0] ??
    conversations[0]

  const handleSelectConversation = (id: string) => {
    setSelectedId(id)
    setMobileShowChat(true)
  }

  return (
    <div
      dir={dir}
      className="relative w-full max-w-5xl mx-auto rounded-3xl border border-white/15 bg-slate-900/85 backdrop-blur-sm shadow-[0_20px_60px_-20px_rgba(0,0,0,0.55)] overflow-hidden"
    >
      {/* macOS-style window chrome */}
      <div className="flex items-center gap-1.5 px-4 py-2.5 border-b border-white/8 bg-slate-800/90">
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

      <div className="grid grid-cols-1 md:grid-cols-[320px_1fr] min-h-[420px] md:min-h-[520px]">

        {/* ── Conversation list (hidden on mobile when chat is open) ─── */}
        <aside className={`border-l border-slate-200/90 bg-[#f7f8fa] flex flex-col ${mobileShowChat ? 'hidden md:flex' : 'flex'}`}>
          {/* Search bar */}
          <div className="px-3 pt-3 pb-2">
            <div className="flex items-center gap-2 bg-white border border-slate-200/90 rounded-xl px-3 py-2 text-slate-500 text-xs shadow-sm">
              <Search className="w-3.5 h-3.5 text-slate-400" />
              <span className="text-slate-400">{copy.searchPlaceholder}</span>
            </div>
          </div>

          {/* Filter chips */}
          <div className="px-2 pb-2 flex gap-1.5 overflow-x-auto scrollbar-thin">
            {(Object.keys(copy.filterLabels) as InboxFilterId[]).map(f => {
              const active = filter === f
              const count = counts[f]
              return (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={[
                    'shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold border transition-colors',
                    active
                      ? 'bg-amber-50 text-amber-700 border-amber-300/80 shadow-sm'
                      : 'bg-white text-slate-600 border-slate-200/90 hover:bg-slate-50 hover:border-slate-300',
                  ].join(' ')}
                >
                  {copy.filterLabels[f]}
                  <span className={`text-[10px] tabular-nums ${
                    active ? 'text-amber-600' : 'text-slate-400'
                  }`}>
                    {count}
                  </span>
                </button>
              )
            })}
          </div>

          {/* Conversation rows */}
          <div className="flex-1 overflow-y-auto divide-y divide-slate-100">
            {visible.length === 0 && (
              <p className="text-center text-slate-400 text-xs py-8">
                {copy.emptyFilter}
              </p>
            )}
            {visible.map(c => {
              const Icon = BADGE_ICONS[c.badge]
              const badgeLabel = copy.badgeLabels[c.badge]
              const badgeClasses = BADGE_CLASSES[c.badge]
              const isSel = selected?.id === c.id
              return (
                <button
                  key={c.id}
                  onClick={() => handleSelectConversation(c.id)}
                  className={[
                    'w-full text-start flex items-start gap-3 px-3 py-3 transition-colors',
                    isSel
                      ? 'bg-amber-50 border-r-[3px] border-amber-500 shadow-[inset_0_0_0_1px_rgba(245,158,11,0.08)]'
                      : 'bg-transparent hover:bg-white/80',
                  ].join(' ')}
                >
                  <div className={`shrink-0 w-10 h-10 rounded-full bg-gradient-to-br ${c.avatarColor} flex items-center justify-center text-white font-bold text-sm shadow-md`}>
                    {c.initials}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <span className="text-slate-900 text-sm font-bold truncate">{c.name}</span>
                      <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[9px] font-bold ${badgeClasses}`}>
                        <Icon className="w-2.5 h-2.5" />
                        {badgeLabel}
                      </span>
                    </div>
                    <div className="flex items-end justify-between gap-2">
                      <p className="text-slate-500 text-xs truncate flex-1 leading-relaxed">
                        {c.preview}
                      </p>
                      <div className="flex flex-col items-end gap-0.5 shrink-0">
                        <span className="text-[10px] text-slate-400 tabular-nums">{c.time}</span>
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

        {/* ── Active conversation (hidden on mobile until selected) ─── */}
        <main className={`flex flex-col bg-[#ece5dd] bg-[url('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22120%22 height=%22120%22%3E%3Cg fill=%22%23d9d2ca%22 fill-opacity=%220.35%22%3E%3Cpolygon points=%2260 0 75 8 75 26 60 34 45 26 45 8%22/%3E%3C/g%3E%3C/svg%3E')] ${mobileShowChat ? 'flex' : 'hidden md:flex'}`}>
          {selected && (
            <>
              {/* Header */}
              <header className="flex items-center gap-3 px-4 py-3 border-b border-[#d1ccc6] bg-[#f0f2f5]">
                {/* Back button — mobile only */}
                <button
                  onClick={() => setMobileShowChat(false)}
                  className="md:hidden p-1.5 -me-1 rounded-lg hover:bg-black/5 transition-colors text-slate-500"
                  aria-label={copy.back}
                >
                  <BackIcon className="w-5 h-5" />
                </button>
                <div className={`w-10 h-10 rounded-full bg-gradient-to-br ${selected.avatarColor} flex items-center justify-center text-white font-bold shadow-md`}>
                  {selected.initials}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-slate-900 font-bold text-sm">{selected.name}</span>
                    <DynamicBadge kind={selected.badge} copy={copy} />
                  </div>
                  <span className="text-[10px] text-slate-500">+966 5• ••• ••••</span>
                </div>
                <div className="flex items-center gap-1 text-slate-400">
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="phone"><Phone className="w-4 h-4" /></button>
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="video"><Video className="w-4 h-4" /></button>
                  <button className="p-1.5 rounded-lg hover:bg-white/5 transition-colors" aria-label="more"><MoreVertical className="w-4 h-4" /></button>
                </div>
              </header>

              {/* Messages */}
              <div className="flex-1 overflow-y-auto px-4 py-5 space-y-2.5">
                {selected.messages.map((m, i) => (
                  <MessageBubble key={i} msg={m} copy={copy} />
                ))}
              </div>

              <footer className="px-4 py-3 border-t border-[#d1ccc6] bg-[#f0f2f5] flex items-center gap-2">
                <div className="flex-1 bg-white border border-[#d1ccc6] rounded-2xl px-4 py-2.5 text-slate-400 text-xs">
                  {copy.composer}
                </div>
                <button className="bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-xs px-4 py-2.5 rounded-2xl transition-colors">
                  {copy.send}
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

function DynamicBadge({ kind, copy }: { kind: InboxBadgeKind; copy: typeof INBOX_DEMO_COPY.ar }) {
  const Icon = BADGE_ICONS[kind]
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[10px] font-bold ${BADGE_CLASSES[kind]}`}>
      <Icon className="w-3 h-3" />
      {copy.badgeLabels[kind]}
    </span>
  )
}

function MessageBubble({ msg, copy }: { msg: InboxDemoMessage; copy: typeof INBOX_DEMO_COPY.ar }) {
  const incoming = msg.kind === 'customer'

  // Outgoing message colours per kind — solid tints for readability on light chat bg
  const outgoingClasses: Record<Exclude<InboxMessageKind, 'customer'>, string> = {
    ai:        'bg-amber-50 border-amber-200/90 text-slate-800 shadow-sm',
    human:     'bg-emerald-50 border-emerald-200/90 text-slate-800 shadow-sm',
    autopilot: 'bg-sky-50 border-sky-200/90 text-slate-800 shadow-sm',
    campaign:  'bg-violet-50 border-violet-200/90 text-slate-800 shadow-sm',
    cart:      'bg-orange-50 border-orange-200/90 text-slate-800 shadow-sm',
  }

  const tagClasses: Record<Exclude<InboxMessageKind, 'customer'>, string> = {
    ai:        'text-amber-700',
    human:     'text-emerald-700',
    autopilot: 'text-sky-700',
    campaign:  'text-violet-700',
    cart:      'text-orange-700',
  }

  if (incoming) {
    return (
      <div className="flex justify-start">
        <div className="max-w-[78%] bg-white border border-black/[0.06] text-slate-800 text-sm rounded-2xl rounded-bl-sm px-3.5 py-2 shadow-sm">
          <p className="whitespace-pre-line leading-relaxed">{msg.text}</p>
          <span className="block text-[10px] text-slate-500 mt-1 text-end">
            {msg.time}
          </span>
        </div>
      </div>
    )
  }

  const k = msg.kind as Exclude<InboxMessageKind, 'customer'>
  return (
    <div className="flex justify-end">
      <div className={`max-w-[80%] border text-sm rounded-2xl rounded-br-sm px-3.5 py-2 shadow-sm ${outgoingClasses[k]}`}>
        <span className={`block text-[10px] font-bold mb-1 ${tagClasses[k]}`}>
          {copy.tagLabels[k]}
        </span>
        <p className="whitespace-pre-line leading-relaxed">{msg.text}</p>
        {msg.buttons && msg.buttons.length > 0 && (
          <div className="mt-2 grid gap-1">
            {msg.buttons.map(b => (
              <span
                key={b}
                className="block text-center text-[11px] font-bold py-1.5 rounded-lg bg-white/80 border border-slate-200/90 text-slate-700"
              >
                {b}
              </span>
            ))}
          </div>
        )}
        <span className="flex items-center justify-end gap-1 text-[10px] text-slate-500 mt-1">
          {msg.time}
          {msg.read && <CheckCheck className="w-3 h-3 text-sky-500" />}
        </span>
      </div>
    </div>
  )
}
