/**
 * WhatsAppDemo.tsx
 * ──────────────────
 * A realistic, animated WhatsApp-style conversation preview that shows
 * merchants exactly how Nahla talks to their customers.
 *
 * Messages play through in sequence, loop automatically, and pause
 * between loops for readability.
 */
import { useEffect, useRef, useState } from 'react'
import { Check, CheckCheck, Signal, Wifi, Battery } from 'lucide-react'

// ── Bee avatar (inline SVG) ───────────────────────────────────────────────────
function BeeAvatar({ size = 28 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="shrink-0"
    >
      <circle cx="32" cy="32" r="32" fill="#075E54" />
      <ellipse cx="32" cy="36" rx="13" ry="9" fill="#F59E0B" />
      <rect x="21" y="31" width="22" height="3.5" rx="1.75" fill="#1e293b" opacity="0.25" />
      <rect x="21" y="36" width="22" height="3.5" rx="1.75" fill="#1e293b" opacity="0.25" />
      <ellipse cx="32" cy="26" rx="7" ry="6" fill="#F59E0B" />
      <circle cx="28.5" cy="24.5" r="1.5" fill="#1e293b" />
      <circle cx="35.5" cy="24.5" r="1.5" fill="#1e293b" />
      <ellipse cx="19" cy="30" rx="8" ry="4" fill="white" opacity="0.6" transform="rotate(-20 19 30)" />
      <ellipse cx="45" cy="30" rx="8" ry="4" fill="white" opacity="0.6" transform="rotate(20 45 30)" />
    </svg>
  )
}

// ── Typing indicator ──────────────────────────────────────────────────────────
function TypingDots() {
  return (
    <div className="flex items-center gap-1 px-3.5 py-2.5">
      {[0, 1, 2].map(i => (
        <span
          key={i}
          className="w-2 h-2 rounded-full bg-slate-400"
          style={{
            animation: `typingBounce 1.2s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
    </div>
  )
}

// ── Read receipt ──────────────────────────────────────────────────────────────
function ReadReceipt({ read }: { read: boolean }) {
  return read
    ? <CheckCheck className="w-3.5 h-3.5 text-[#34B7F1] shrink-0" />
    : <Check className="w-3.5 h-3.5 text-slate-400 shrink-0" />
}

// ── Message definition ────────────────────────────────────────────────────────
type MessageRole = 'customer' | 'nahla'

interface Message {
  id: number
  role: MessageRole
  lines: string[]
  time: string
  hasButton?: boolean
  paymentMethods?: string
}

const MESSAGES: Message[] = [
  {
    id: 1,
    role: 'customer',
    lines: ['السلام عليكم، هل هذا المنتج متوفر؟'],
    time: '10:32',
  },
  {
    id: 2,
    role: 'nahla',
    lines: [
      'وعليكم السلام 😊',
      'نعم المنتج متوفر حالياً.',
      'السعر: *129 ريال*',
      'هل ترغب أن أرسل لك رابط الشراء الآن؟',
    ],
    time: '10:32',
  },
  {
    id: 3,
    role: 'customer',
    lines: ['نعم أرسله لي'],
    time: '10:33',
  },
  {
    id: 4,
    role: 'nahla',
    lines: [
      'رائع 👍',
      'يمكنك إتمام الطلب مباشرة من هنا:',
    ],
    time: '10:33',
    hasButton: true,
    paymentMethods: 'بطاقة ائتمانية · Apple Pay · تحويل بنكي',
  },
]

// ms to wait before showing typing → then message
const DELAYS: Record<number, [number, number]> = {
  1: [0, 0],        // customer: show immediately
  2: [700, 1600],   // nahla: typing at 700ms, message at 1600ms
  3: [2600, 2600],  // customer: show at 2600ms
  4: [3300, 4400],  // nahla: typing at 3300ms, message at 4400ms
}

const LOOP_PAUSE = 3500   // pause before restarting the loop

// ── Format bold markdown-ish text ─────────────────────────────────────────────
function FormattedLine({ text }: { text: string }) {
  const parts = text.split(/(\*[^*]+\*)/g)
  return (
    <span>
      {parts.map((part, i) =>
        part.startsWith('*') && part.endsWith('*')
          ? <strong key={i} className="font-bold">{part.slice(1, -1)}</strong>
          : <span key={i}>{part}</span>
      )}
    </span>
  )
}

// ── Single chat bubble ────────────────────────────────────────────────────────
function Bubble({ msg, visible }: { msg: Message; visible: boolean }) {
  const isNahla   = msg.role === 'nahla'
  const isCustomer = msg.role === 'customer'

  return (
    <div
      className={`flex items-end gap-2 transition-all duration-500 ${
        visible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-3'
      } ${isCustomer ? 'flex-row-reverse' : 'flex-row'}`}
    >
      {/* Avatar — Nahla only */}
      {isNahla && (
        <div className="shrink-0 mb-1">
          <BeeAvatar size={30} />
        </div>
      )}

      {/* Bubble */}
      <div
        className={`max-w-[78%] rounded-2xl px-4 py-2.5 shadow-sm relative ${
          isNahla
            ? 'bg-white text-slate-800 rounded-tl-sm'
            : 'bg-[#DCF8C6] text-slate-800 rounded-tr-sm'
        }`}
      >
        {/* Sender label */}
        {isNahla && (
          <p className="text-[11px] font-bold text-[#075E54] mb-1">نحلة 🐝</p>
        )}

        {/* Lines */}
        <div className="space-y-1 text-sm leading-relaxed">
          {msg.lines.map((line, i) => (
            <p key={i}>
              <FormattedLine text={line} />
            </p>
          ))}
        </div>

        {/* CTA button inside message */}
        {msg.hasButton && (
          <div className="mt-3 mb-1.5">
            <div className="w-full text-center py-2 rounded-xl bg-[#25D366] text-white text-sm font-bold cursor-default select-none shadow-sm shadow-[#25D366]/30">
              إتمام الطلب
            </div>
            {msg.paymentMethods && (
              <p className="text-xs text-slate-500 mt-2 text-center">{msg.paymentMethods}</p>
            )}
            <p className="text-[12px] text-slate-600 mt-2.5 leading-relaxed">
              وإذا احتجت أي مساعدة أنا هنا لخدمتك. 🤝
            </p>
          </div>
        )}

        {/* Footer: time + read receipt */}
        <div className={`flex items-center gap-1 mt-1 ${isNahla ? 'justify-end' : 'justify-end'}`}>
          <span className="text-[10px] text-slate-400">{msg.time}</span>
          {isCustomer && <ReadReceipt read={msg.id <= 2} />}
        </div>
      </div>
    </div>
  )
}

// ── Main demo component ────────────────────────────────────────────────────────
export default function WhatsAppDemo() {
  // Which messages are visible and whether typing indicator is showing
  const [visibleIds, setVisibleIds] = useState<Set<number>>(new Set())
  const [typingForId, setTypingForId] = useState<number | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([])

  const clearTimers = () => {
    timersRef.current.forEach(clearTimeout)
    timersRef.current = []
  }

  const push = (fn: () => void, delay: number) => {
    timersRef.current.push(setTimeout(fn, delay))
  }

  const runSequence = () => {
    setVisibleIds(new Set())
    setTypingForId(null)

    MESSAGES.forEach(msg => {
      const [typingAt, showAt] = DELAYS[msg.id]

      if (msg.role === 'nahla' && typingAt < showAt) {
        push(() => setTypingForId(msg.id), typingAt)
      }

      push(() => {
        setTypingForId(null)
        setVisibleIds(prev => new Set([...prev, msg.id]))
        // Auto-scroll to bottom
        setTimeout(() => {
          scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
        }, 80)
      }, showAt)
    })

    // Schedule next loop
    const lastDelay = Math.max(...Object.values(DELAYS).map(([, s]) => s))
    push(() => push(runSequence, LOOP_PAUSE), lastDelay)
  }

  useEffect(() => {
    runSequence()
    return clearTimers
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    // Phone device mockup
    <div className="relative mx-auto w-[320px] sm:w-[360px]">
      {/* Outer phone shell */}
      <div className="rounded-[2.5rem] bg-slate-900 border-[3px] border-slate-700 shadow-2xl shadow-black/60 overflow-hidden">

        {/* Status bar */}
        <div className="bg-[#075E54] px-5 pt-3 pb-1 flex items-center justify-between">
          <span className="text-white text-[11px] font-semibold">10:33</span>
          <div className="flex items-center gap-1 opacity-90">
            <Signal className="w-3 h-3 text-white" />
            <Wifi className="w-3 h-3 text-white" />
            <Battery className="w-3.5 h-3.5 text-white" />
          </div>
        </div>

        {/* WhatsApp header bar */}
        <div className="bg-[#075E54] px-4 pb-3 flex items-center gap-3">
          <div className="relative">
            <BeeAvatar size={38} />
            <span className="absolute bottom-0 right-0 w-2.5 h-2.5 bg-[#25D366] rounded-full border-2 border-[#075E54]" />
          </div>
          <div>
            <p className="text-white font-semibold text-sm leading-tight">نحلة 🐝</p>
            <p className="text-[#c5e8c5] text-[11px]">متصل الآن</p>
          </div>
        </div>

        {/* Chat area */}
        <div
          ref={scrollRef}
          className="relative h-[420px] overflow-y-auto overflow-x-hidden"
          style={{
            background: '#e5ddd5',
            backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='40'%3E%3Crect width='40' height='40' fill='%23e5ddd5'/%3E%3Cpath d='M0 20h40M20 0v40' stroke='%23d4c9be' stroke-width='0.5'/%3E%3C/svg%3E")`,
          }}
        >
          <div className="px-3 py-4 space-y-3">
            {/* Date chip */}
            <div className="flex justify-center">
              <span className="text-[11px] bg-[#d1f0f0] text-[#3d8a8a] px-3 py-0.5 rounded-full shadow-sm">
                اليوم
              </span>
            </div>

            {MESSAGES.map(msg => (
              <div key={msg.id}>
                {/* Typing indicator — shows before Nahla's message */}
                {msg.role === 'nahla' && typingForId === msg.id && (
                  <div className="flex items-end gap-2">
                    <BeeAvatar size={28} />
                    <div className="bg-white rounded-2xl rounded-tl-sm shadow-sm">
                      <TypingDots />
                    </div>
                  </div>
                )}

                {/* Message bubble */}
                <Bubble msg={msg} visible={visibleIds.has(msg.id)} />
              </div>
            ))}

            {/* Bottom padding so last message isn't flush against edge */}
            <div className="h-2" />
          </div>
        </div>

        {/* Input bar */}
        <div className="bg-[#f0f0f0] px-3 py-2.5 flex items-center gap-2 border-t border-[#d0d0d0]">
          <div className="flex-1 bg-white rounded-full px-4 py-2 text-sm text-slate-400 select-none">
            اكتب رسالة…
          </div>
          <div className="w-9 h-9 rounded-full bg-[#25D366] flex items-center justify-center shadow-sm shrink-0">
            <svg viewBox="0 0 24 24" className="w-4 h-4 fill-white">
              <path d="M2 21l21-9-21-9v7l15 2-15 2z" />
            </svg>
          </div>
        </div>
      </div>

      {/* Decorative glow behind phone */}
      <div className="absolute -inset-6 -z-10 bg-[#25D366]/10 rounded-full blur-3xl pointer-events-none" />
    </div>
  )
}
