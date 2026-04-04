import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import WhatsAppDemo from '../components/landing/WhatsAppDemo'
import {
  MessageCircle,
  ShoppingBag,
  BarChart3,
  Zap,
  Gift,
  ShoppingCart,
  Clock,
  ChevronDown,
  ChevronUp,
  ArrowLeft,
  Star,
  Check,
  Bot,
  Users,
  TrendingUp,
  Send,
  CreditCard,
  RefreshCw,
  Menu,
  X,
  Shield,
  Quote,
  AlertCircle,
  BadgeCheck,
} from 'lucide-react'

// ── Bee SVG ────────────────────────────────────────────────────────────────────
function BeeIcon({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <ellipse cx="32" cy="34" rx="16" ry="12" fill="#F59E0B" />
      <ellipse cx="32" cy="34" rx="16" ry="12" fill="url(#beeStripe)" opacity="0.8" />
      <rect x="20" y="28" width="24" height="4" rx="2" fill="#1e293b" opacity="0.3" />
      <rect x="20" y="34" width="24" height="4" rx="2" fill="#1e293b" opacity="0.3" />
      <ellipse cx="32" cy="24" rx="8" ry="7" fill="#F59E0B" />
      <circle cx="28" cy="22" r="2" fill="#1e293b" />
      <circle cx="36" cy="22" r="2" fill="#1e293b" />
      <path d="M24 18 Q28 14 32 18" stroke="#F59E0B" strokeWidth="1.5" fill="none" />
      <path d="M32 18 Q36 14 40 18" stroke="#F59E0B" strokeWidth="1.5" fill="none" />
      <ellipse cx="20" cy="28" rx="9" ry="5" fill="white" opacity="0.7" transform="rotate(-20 20 28)" />
      <ellipse cx="44" cy="28" rx="9" ry="5" fill="white" opacity="0.7" transform="rotate(20 44 28)" />
      <path d="M30 44 Q32 50 30 54" stroke="#1e293b" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M34 44 Q36 50 38 54" stroke="#1e293b" strokeWidth="1.5" strokeLinecap="round" />
      <defs>
        <linearGradient id="beeStripe" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#F59E0B" />
          <stop offset="0.4" stopColor="#1e293b" stopOpacity="0.2" />
          <stop offset="0.6" stopColor="#1e293b" stopOpacity="0.2" />
          <stop offset="1" stopColor="#F59E0B" />
        </linearGradient>
      </defs>
    </svg>
  )
}

// ── Honeycomb background pattern ──────────────────────────────────────────────
function HoneycombBg({ opacity = 'opacity-[0.04]' }: { opacity?: string }) {
  return (
    <svg
      className={`absolute inset-0 w-full h-full ${opacity} pointer-events-none select-none`}
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <pattern id="hex" x="0" y="0" width="60" height="52" patternUnits="userSpaceOnUse">
          <polygon points="30,2 58,17 58,47 30,62 2,47 2,17" fill="none" stroke="#F59E0B" strokeWidth="1" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill="url(#hex)" />
    </svg>
  )
}

// ── FAQ accordion item ────────────────────────────────────────────────────────
function FaqItem({ q, a }: { q: string; a: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div
      className={`rounded-2xl overflow-hidden transition-all duration-200 ${
        open ? 'bg-amber-500/8 border border-amber-500/25' : 'bg-white/4 border border-white/8 hover:border-amber-500/20'
      }`}
    >
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-5 sm:p-6 text-right gap-4"
      >
        <span className="text-white font-bold text-base sm:text-lg leading-snug">{q}</span>
        <span className={`shrink-0 transition-colors ${open ? 'text-amber-400' : 'text-slate-500'}`}>
          {open ? <ChevronUp size={20} /> : <ChevronDown size={20} />}
        </span>
      </button>
      {open && (
        <div className="px-5 sm:px-6 pb-5 sm:pb-6 text-slate-300 leading-loose text-sm sm:text-base border-t border-white/6 pt-4">
          {a}
        </div>
      )}
    </div>
  )
}

// ── Pricing plan card ─────────────────────────────────────────────────────────
interface PlanProps {
  name: string
  nameAr: string
  price: number
  launchPrice: number
  tagline: string
  idealFor: string
  features: string[]
  highlighted?: boolean
  badge?: string
  ctaLabel?: string
}

function PlanCard({
  name, nameAr, price, launchPrice, tagline, idealFor,
  features, highlighted, badge, ctaLabel,
}: PlanProps) {
  const discount = Math.round(((price - launchPrice) / price) * 100)
  return (
    <div
      className={`relative rounded-3xl p-7 flex flex-col gap-5 transition-all duration-300 hover:-translate-y-1 ${
        highlighted
          ? 'bg-gradient-to-br from-amber-400 to-amber-600 shadow-2xl shadow-amber-500/40 ring-2 ring-amber-400/50'
          : 'bg-slate-800/70 border border-white/10 hover:border-amber-400/30 backdrop-blur-sm'
      }`}
    >
      {/* Top badge */}
      {badge && (
        <div className="absolute -top-4 inset-x-0 flex justify-center">
          <span className="bg-slate-900 text-amber-400 border border-amber-500/40 text-xs font-black px-4 py-1.5 rounded-full shadow-lg tracking-wide">
            {badge}
          </span>
        </div>
      )}

      {/* Plan label */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <p className={`text-xs font-bold tracking-widest uppercase ${highlighted ? 'text-amber-900' : 'text-amber-500'}`}>
            {name}
          </p>
          {highlighted && (
            <span className="text-xs bg-amber-900/30 text-amber-900 font-bold px-2 py-0.5 rounded-full">
              وفّر {discount}٪
            </span>
          )}
        </div>
        <h3 className={`text-2xl font-black mb-1 ${highlighted ? 'text-slate-900' : 'text-white'}`}>
          {nameAr}
        </h3>
        <p className={`text-xs leading-relaxed ${highlighted ? 'text-amber-900/70' : 'text-slate-500'}`}>
          {idealFor}
        </p>
      </div>

      {/* Price block */}
      <div className={`rounded-2xl p-4 ${highlighted ? 'bg-amber-900/20' : 'bg-white/5'}`}>
        <div className="flex items-end gap-2 mb-0.5">
          <span className={`text-4xl font-black leading-none ${highlighted ? 'text-slate-900' : 'text-white'}`}>
            {launchPrice.toLocaleString('ar-SA')}
          </span>
          <div className="pb-1">
            <div className={`text-xs line-through ${highlighted ? 'text-amber-900/50' : 'text-slate-600'}`}>
              {price.toLocaleString('ar-SA')} ريال
            </div>
            <div className={`text-xs font-medium ${highlighted ? 'text-amber-900/60' : 'text-slate-400'}`}>
              ريال / شهرياً
            </div>
          </div>
        </div>
        <p className={`text-xs ${highlighted ? 'text-amber-900/70' : 'text-slate-500'}`}>
          {tagline}
        </p>
      </div>

      {/* Features */}
      <ul className="flex flex-col gap-2.5 flex-1">
        {features.map((f, i) => (
          <li key={i} className="flex items-start gap-2.5">
            <Check size={15} className={`mt-0.5 shrink-0 ${highlighted ? 'text-amber-900' : 'text-amber-400'}`} />
            <span className={`text-sm leading-snug ${highlighted ? 'text-amber-900' : 'text-slate-300'}`}>
              {f}
            </span>
          </li>
        ))}
      </ul>

      {/* CTA */}
      <Link
        to="/register"
        className={`mt-2 text-center py-3.5 rounded-2xl font-black text-sm transition-all duration-200 hover:scale-[1.02] ${
          highlighted
            ? 'bg-slate-900 text-amber-400 hover:bg-slate-800 shadow-lg'
            : 'bg-amber-500/12 text-amber-400 border border-amber-500/25 hover:bg-amber-500/20 hover:border-amber-400/50'
        }`}
      >
        {ctaLabel ?? 'ابدأ مجاناً'}
      </Link>
    </div>
  )
}

// ── Feature card ──────────────────────────────────────────────────────────────
function FeatureCard({
  icon: Icon, title, desc, outcome, highlight = false,
}: { icon: React.ElementType; title: string; desc: string; outcome?: string; highlight?: boolean }) {
  if (highlight) {
    return (
      <div className="group relative p-6 rounded-2xl bg-gradient-to-b from-amber-500/12 to-amber-500/4 border border-amber-400/30 hover:border-amber-400/60 hover:from-amber-500/18 transition-all duration-300 hover:-translate-y-1 backdrop-blur-sm shadow-lg shadow-amber-500/8">
        {/* "مميز" badge */}
        <div className="absolute -top-2.5 start-4">
          <span className="bg-amber-500 text-slate-900 text-[10px] font-black px-2.5 py-0.5 rounded-full shadow-md shadow-amber-500/30">
            ⭐ مميزة
          </span>
        </div>
        <div className="w-11 h-11 rounded-xl bg-amber-500/25 flex items-center justify-center mb-4 group-hover:bg-amber-500/35 transition-colors ring-1 ring-amber-400/30">
          <Icon size={20} className="text-amber-300" />
        </div>
        <h3 className="text-white font-black text-base mb-1.5">{title}</h3>
        <p className="text-slate-300 text-sm leading-relaxed mb-3">{desc}</p>
        {outcome && (
          <div className="flex items-center gap-1.5 text-amber-400 text-xs font-bold">
            <TrendingUp size={12} />
            {outcome}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="group p-6 rounded-2xl bg-slate-800/60 border border-white/8 hover:border-amber-400/35 hover:bg-amber-500/4 transition-all duration-300 hover:-translate-y-0.5 backdrop-blur-sm">
      <div className="w-11 h-11 rounded-xl bg-amber-500/12 flex items-center justify-center mb-4 group-hover:bg-amber-500/22 transition-colors">
        <Icon size={20} className="text-amber-400" />
      </div>
      <h3 className="text-white font-bold text-base mb-1.5">{title}</h3>
      <p className="text-slate-400 text-sm leading-relaxed mb-3">{desc}</p>
      {outcome && (
        <div className="flex items-center gap-1.5 text-amber-500/80 text-xs font-bold">
          <TrendingUp size={12} />
          {outcome}
        </div>
      )}
    </div>
  )
}

// ── Step in "how it works" ─────────────────────────────────────────────────────
function StepCard({ num, title, desc, time, last }: {
  num: string; title: string; desc: string; time?: string; last?: boolean
}) {
  return (
    <div className="flex gap-5 items-start">
      <div className="flex flex-col items-center shrink-0">
        <div className="w-12 h-12 rounded-full bg-gradient-to-br from-amber-400 to-amber-600 flex items-center justify-center shadow-lg shadow-amber-500/25">
          <span className="text-slate-900 font-black text-lg">{num}</span>
        </div>
        {!last && <div className="w-px h-16 bg-gradient-to-b from-amber-500/40 to-transparent mt-2" />}
      </div>
      <div className="pt-1.5 pb-8">
        <div className="flex items-center gap-2 mb-1">
          <h3 className="text-white font-bold text-xl">{title}</h3>
          {time && (
            <span className="text-xs text-amber-500/70 bg-amber-500/10 px-2 py-0.5 rounded-full border border-amber-500/15 font-medium">
              {time}
            </span>
          )}
        </div>
        <p className="text-slate-400 leading-relaxed text-base">{desc}</p>
      </div>
    </div>
  )
}

// ── Testimonial card ──────────────────────────────────────────────────────────
function TestimonialCard({ quote, name, store, result }: {
  quote: string; name: string; store: string; result: string
}) {
  return (
    <div className="p-6 rounded-2xl bg-slate-800/60 border border-white/8 hover:border-amber-400/25 transition-all duration-300 backdrop-blur-sm flex flex-col gap-4">
      <Quote size={20} className="text-amber-500/50 shrink-0" />
      <p className="text-slate-300 leading-loose text-base flex-1">"{quote}"</p>
      <div className="flex items-center justify-between pt-2 border-t border-white/6">
        <div>
          <div className="text-white font-bold text-sm">{name}</div>
          <div className="text-slate-500 text-xs">{store}</div>
        </div>
        <div className="text-right">
          <div className="text-amber-400 font-black text-sm">{result}</div>
          <div className="flex gap-0.5 justify-end mt-0.5">
            {[...Array(5)].map((_, i) => (
              <Star key={i} size={10} className="text-amber-400 fill-amber-400" />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Main landing page ─────────────────────────────────────────────────────────
export default function Landing() {
  const [scrolled, setScrolled]         = useState(false)
  const [mobileMenuOpen, setMobile]     = useState(false)
  const heroRef                          = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 50)
    window.addEventListener('scroll', fn, { passive: true })
    return () => window.removeEventListener('scroll', fn)
  }, [])

  const scrollTo = (id: string) => {
    setMobile(false)
    setTimeout(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 50)
  }

  const navLinks = [
    { label: 'كيف تعمل', id: 'how' },
    { label: 'شاهد نحلة', id: 'demo' },
    { label: 'المميزات', id: 'features' },
    { label: 'آراء التجار', id: 'testimonials' },
    { label: 'الأسعار', id: 'pricing' },
    { label: 'الأسئلة الشائعة', id: 'faq' },
  ]

  return (
    <div dir="rtl" className="min-h-screen bg-slate-900 overflow-x-hidden" style={{ fontFamily: "'Cairo', sans-serif" }}>

      {/* ══════════════════════════════════════════════════════════
          NAVBAR
      ══════════════════════════════════════════════════════════ */}
      <nav className={`fixed top-0 inset-x-0 z-50 transition-all duration-300 ${
        scrolled ? 'bg-slate-900/96 backdrop-blur-xl shadow-lg shadow-black/30 border-b border-white/5' : 'bg-transparent'
      }`}>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            {/* Logo */}
            <Link to="/landing" className="flex items-center gap-2 group">
              <img src="/logo.png" alt="نحلة" className="w-10 h-10 object-contain drop-shadow-md" />
              <span className="text-white font-black text-xl tracking-tight">نحلة</span>
              <span className="text-amber-400 text-[10px] font-black bg-amber-500/15 px-1.5 py-0.5 rounded-full border border-amber-500/25 leading-none">
                AI
              </span>
            </Link>

            {/* Desktop nav links */}
            <div className="hidden md:flex items-center gap-0.5">
              {navLinks.map((l) => (
                <button
                  key={l.id}
                  onClick={() => scrollTo(l.id)}
                  className="text-slate-400 hover:text-white transition-colors px-3.5 py-2 rounded-lg text-sm font-medium"
                >
                  {l.label}
                </button>
              ))}
            </div>

            {/* Desktop CTAs */}
            <div className="hidden md:flex items-center gap-3">
              <Link to="/login" className="text-slate-400 hover:text-white text-sm font-medium transition-colors px-3 py-2">
                دخول
              </Link>
              <Link
                to="/register"
                className="bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-sm px-5 py-2.5 rounded-xl transition-all duration-200 shadow-lg shadow-amber-500/25"
              >
                جرّب مجاناً 14 يوم
              </Link>
            </div>

            {/* Mobile toggle */}
            <button className="md:hidden text-slate-400 p-1" onClick={() => setMobile(!mobileMenuOpen)}>
              {mobileMenuOpen ? <X size={22} /> : <Menu size={22} />}
            </button>
          </div>
        </div>

        {/* Mobile dropdown */}
        {mobileMenuOpen && (
          <div className="md:hidden border-t border-white/8 bg-slate-900/98 backdrop-blur-xl">
            <div className="px-4 py-3 flex flex-col divide-y divide-white/5">
              <div className="flex flex-col pb-2">
                {navLinks.map((l) => (
                  <button key={l.id} onClick={() => scrollTo(l.id)}
                    className="text-slate-300 text-sm font-medium py-3 text-right hover:text-amber-400 transition-colors">
                    {l.label}
                  </button>
                ))}
              </div>
              <div className="flex flex-col gap-2 pt-3">
                <Link to="/login" className="text-center text-slate-400 py-2.5 text-sm" onClick={() => setMobile(false)}>
                  تسجيل الدخول
                </Link>
                <Link to="/register" onClick={() => setMobile(false)}
                  className="text-center bg-amber-500 text-slate-900 font-black text-sm py-3.5 rounded-2xl shadow-lg shadow-amber-500/20">
                  جرّب مجاناً 14 يوم — بلا بطاقة
                </Link>
              </div>
            </div>
          </div>
        )}
      </nav>

      {/* ══════════════════════════════════════════════════════════
          HERO
      ══════════════════════════════════════════════════════════ */}
      <section
        ref={heroRef}
        className="relative min-h-[100svh] flex items-center justify-center overflow-hidden bg-gradient-to-br from-slate-900 via-[#0f1d2e] to-slate-900 pt-16"
      >
        <HoneycombBg />
        {/* Glow */}
        <div className="absolute top-1/3 right-1/4 w-[500px] h-[500px] bg-amber-500/8 rounded-full blur-[100px] pointer-events-none" />
        <div className="absolute bottom-1/4 left-1/4 w-96 h-96 bg-blue-600/5 rounded-full blur-[80px] pointer-events-none" />

        <div className="relative z-10 max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 text-center py-20">

          {/* Urgency badge */}
          <div className="inline-flex items-center gap-2 bg-amber-500/10 border border-amber-500/25 rounded-full px-4 py-2 mb-8">
            <span className="w-2 h-2 bg-amber-400 rounded-full animate-pulse" />
            <span className="text-amber-300 text-sm font-bold">
              عرض الإطلاق — خصم 50٪ لأول شهرين
            </span>
          </div>

          {/* Headline — pain hook first */}
          <h1 className="text-[2.6rem] sm:text-5xl lg:text-[5.5rem] font-black text-white leading-[1.1] mb-6 tracking-tight">
            عملاؤك يرسلون
            <br />
            <span className="text-transparent bg-clip-text bg-gradient-to-l from-amber-300 via-amber-400 to-yellow-500">
              ومتجرك لا يرد؟
            </span>
          </h1>

          {/* Value proposition — specific & punchy */}
          <p className="text-lg sm:text-xl text-slate-300 max-w-2xl mx-auto leading-relaxed mb-3 font-medium">
            نحلة تحوّل كل رسالة واتساب إلى فرصة بيع حقيقية — ترد، تقترح، وتُتمّ الطلب بدلاً عنك.
          </p>
          <p className="text-base text-slate-500 max-w-xl mx-auto leading-loose mb-10">
            أدخل منتجاتك وعروضك مرة واحدة… ونحلة تعمل على مدار الساعة.
          </p>

          {/* Primary CTA — single clear action */}
          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <Link
              to="/register"
              className="group flex items-center gap-2.5 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-base sm:text-lg px-8 sm:px-10 py-4 rounded-2xl transition-all duration-200 shadow-2xl shadow-amber-500/30 hover:shadow-amber-400/45 hover:scale-[1.03] w-full sm:w-auto justify-center"
            >
              ابدأ تجربتك المجانية الآن
              <ArrowLeft size={18} className="group-hover:-translate-x-1 transition-transform" />
            </Link>
            <button
              onClick={() => scrollTo('how')}
              className="flex items-center gap-2 text-slate-400 hover:text-white border border-white/15 hover:border-white/30 text-sm sm:text-base px-6 py-4 rounded-2xl transition-all duration-200 w-full sm:w-auto justify-center"
            >
              شاهد كيف تعمل
            </button>
          </div>

          {/* Risk-reversal micro-copy */}
          <p className="text-slate-600 text-xs mt-4">
            بلا بطاقة ائتمانية · بلا عقود · تلغي في أي وقت
          </p>

          {/* Social proof bar */}
          <div className="mt-14 inline-flex flex-wrap items-center justify-center gap-4 sm:gap-6 bg-white/3 border border-white/8 rounded-2xl px-6 py-4 backdrop-blur-sm">
            <div className="flex items-center gap-2.5">
              <div className="flex -space-x-2 space-x-reverse">
                {['🧑‍💼', '👩‍💼', '👨‍💻', '👩‍🍳'].map((e, i) => (
                  <div key={i} className="w-7 h-7 rounded-full bg-slate-700 border-2 border-slate-800 flex items-center justify-center text-xs">
                    {e}
                  </div>
                ))}
              </div>
              <span className="text-slate-400 text-sm font-medium">+500 متجر نشط</span>
            </div>
            <div className="w-px h-5 bg-white/10 hidden sm:block" />
            <div className="flex items-center gap-1.5">
              {[...Array(5)].map((_, i) => <Star key={i} size={13} className="text-amber-400 fill-amber-400" />)}
              <span className="text-slate-400 text-sm font-medium">4.9/5 تقييم</span>
            </div>
            <div className="w-px h-5 bg-white/10 hidden sm:block" />
            <span className="text-slate-400 text-sm font-medium">14 يوم مجاناً</span>
          </div>
        </div>

        <button
          onClick={() => scrollTo('how')}
          className="absolute bottom-8 left-1/2 -translate-x-1/2 text-slate-600 hover:text-amber-400 transition-colors animate-bounce"
        >
          <ChevronDown size={26} />
        </button>
      </section>

      {/* ══════════════════════════════════════════════════════════
          PROBLEM STRIP — Pain acknowledgment before solution
      ══════════════════════════════════════════════════════════ */}
      <section className="bg-slate-800/40 border-y border-white/5 py-10">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8">
          <p className="text-center text-slate-500 text-xs font-bold uppercase tracking-widest mb-7">
            هل تعاني من هذه المشاكل؟
          </p>
          <div className="grid sm:grid-cols-3 gap-4">
            {[
              { icon: AlertCircle, text: 'رسائل واتساب بلا رد تعني عملاء يشترون من منافسك' },
              { icon: Clock,       text: 'فريق الدعم غارق في نفس الأسئلة المتكررة يومياً' },
              { icon: ShoppingCart, text: 'عملاء يتركون السلة لأن لا أحد يتابع معهم' },
            ].map(({ icon: Icon, text }, i) => (
              <div key={i} className="flex items-start gap-3 p-4 rounded-xl bg-red-500/5 border border-red-500/10">
                <Icon size={18} className="text-red-400/70 shrink-0 mt-0.5" />
                <p className="text-slate-400 text-sm leading-relaxed">{text}</p>
              </div>
            ))}
          </div>
          <p className="text-center text-amber-400 font-bold text-sm mt-7">
            نحلة تحل هذه المشاكل الثلاث تلقائياً، من اليوم الأول. 🐝
          </p>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          HOW IT WORKS
      ══════════════════════════════════════════════════════════ */}
      <section id="how" className="py-24 relative overflow-hidden bg-slate-900">
        <HoneycombBg />
        <div className="max-w-2xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-14">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">في 4 خطوات فقط</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              كيف تعمل نحلة؟
            </h2>
            <p className="text-slate-400 text-base">إعداد كامل في أقل من ساعة واحدة</p>
          </div>
          <div>
            <StepCard
              num="١"
              title="اربط متجرك"
              desc="أضف متجرك على سلة أو منصتك التجارية — التكامل فوري وبلا تعقيدات تقنية."
              time="5 دقائق"
            />
            <StepCard
              num="٢"
              title="اربط واتساب"
              desc="ربط رقم واتساب Business الخاص بمتجرك مع نحلة بخطوات موضّحة داخل اللوحة."
              time="10 دقائق"
            />
            <StepCard
              num="٣"
              title="درّب نحلة على متجرك"
              desc="أدخل منتجاتك وعروضك وسياسات الشحن والإرجاع — كل ما تعرفه عن متجرك، علّمه لنحلة."
              time="20 دقيقة"
            />
            <StepCard
              num="٤"
              title="شغّل نحلة واسترح"
              desc="نحلة تبدأ الرد على عملائك فور تفعيلها — ترد، تبيع، وتُتمّ الطلبات بدون أي تدخل منك."
              time="الآن وإلى الأبد"
              last
            />
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          WHATSAPP DEMO
      ══════════════════════════════════════════════════════════ */}
      <section id="demo" className="py-24 bg-slate-800/50 relative overflow-hidden">
        <HoneycombBg opacity="opacity-[0.03]" />

        {/* Ambient glow */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[400px] bg-[#25D366]/6 rounded-full blur-[80px] pointer-events-none" />

        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="grid lg:grid-cols-2 gap-12 lg:gap-16 items-center">

            {/* Text side */}
            <div>
              <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-4">
                تجربة حقيقية
              </p>
              <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-5">
                كيف تتحدث نحلة
                <br />
                <span className="text-transparent bg-clip-text bg-gradient-to-l from-[#25D366] to-[#128C7E]">
                  مع عملائك؟
                </span>
              </h2>
              <p className="text-slate-400 leading-loose text-base mb-8 max-w-lg">
                شاهد مثالاً حقيقياً لكيفية رد نحلة على العملاء ومساعدتهم على إتمام الطلب مباشرة عبر واتساب.
              </p>

              {/* What the demo shows */}
              <ul className="space-y-4 mb-10">
                {[
                  { emoji: '💬', title: 'رد فوري',         desc: 'نحلة ترد في ثوانٍ — لا انتظار، لا تفويت.' },
                  { emoji: '📦', title: 'تحقق من المخزون', desc: 'تؤكد التوفر والسعر من بيانات متجرك الفعلية.' },
                  { emoji: '🛒', title: 'رابط الشراء',     desc: 'ترسل رابط الدفع مباشرة داخل المحادثة.' },
                  { emoji: '💳', title: 'خيارات الدفع',    desc: 'بطاقة، Apple Pay، أو تحويل بنكي — كل شيء داخل الشات.' },
                ].map(({ emoji, title, desc }) => (
                  <li key={title} className="flex items-start gap-3.5">
                    <span className="text-xl shrink-0 mt-0.5">{emoji}</span>
                    <div>
                      <span className="text-white font-bold text-sm">{title} — </span>
                      <span className="text-slate-400 text-sm">{desc}</span>
                    </div>
                  </li>
                ))}
              </ul>

              <Link
                to="/register"
                className="inline-flex items-center gap-2.5 bg-[#25D366] hover:bg-[#1ebe5d] text-white font-black text-sm px-7 py-3.5 rounded-2xl transition-all duration-200 shadow-lg shadow-[#25D366]/25"
              >
                <span>ابدأ مجاناً — مثل ما شاهدت</span>
                <ArrowLeft size={16} />
              </Link>
            </div>

            {/* Demo side */}
            <div className="flex justify-center lg:justify-end">
              <WhatsAppDemo />
            </div>

          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          FEATURES
      ══════════════════════════════════════════════════════════ */}
      <section id="features" className="py-24 bg-slate-800/30">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-14">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">قدرات نحلة</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4">
              نحلة تعمل كفريق مبيعات متكامل
            </h2>
            <p className="text-slate-400 max-w-xl mx-auto leading-relaxed text-base">
              لا توظيف، لا رواتب، لا إجازات — فقط مبيعات متواصلة على مدار الساعة.
            </p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <FeatureCard
              icon={Bot}
              title="ردود ذكية طبيعية"
              desc="تفهم أسئلة العملاء بالعامية والفصحى وترد بأسلوب متجرك تماماً."
              outcome="يقلل متوسط وقت الرد من ساعات إلى ثوانٍ"
            />
            <FeatureCard
              icon={Zap}
              title="الطيار الآلي"
              desc="شغّله مرة واحدة ثم ارتاح — نحلة تتولى الرد والمتابعة وإتمام الطلب من أوله لآخره بدون أي تدخل منك."
              outcome="متجرك يبيع وأنت نائم، 24/7 بلا انقطاع"
              highlight
            />
            <FeatureCard
              icon={ShoppingCart}
              title="استرجاع السلات المتروكة"
              desc="تراقب من يترك الطلب وترسل تذكيرات ذكية في الوقت المناسب."
              outcome="تسترجع ما يصل إلى 30٪ من الطلبات المفقودة"
            />
            <FeatureCard
              icon={RefreshCw}
              title="إعادة الطلب التنبؤي"
              desc='نحلة تتذكر كل عميل وتُرسل له رسالة في اللحظة المناسبة — مثال: "سلمى، مرت 3 أسابيع على طلبك الأخير من كريم الترطيب، هل تريدين إعادة الطلب؟ 🍯"'
              outcome="يزيد معدل تكرار الشراء حتى 40٪ — بدون إعلانات"
              highlight
            />
            <FeatureCard
              icon={Star}
              title="توصيات المنتجات"
              desc="تقترح منتجات مكملة بناءً على ما يريده العميل لزيادة قيمة الطلب."
              outcome="ترفع متوسط قيمة الطلب بنسبة تصل لـ 35٪"
            />
            <FeatureCard
              icon={CreditCard}
              title="روابط الدفع الفورية"
              desc="ترسل رابط الدفع للعميل مباشرة داخل المحادثة فيتم الشراء في ثوانٍ."
              outcome="تحوّل الاستفسار إلى شراء في نفس المحادثة"
            />
            <FeatureCard
              icon={Gift}
              title="كوبونات ذكية"
              desc="تُنشئ كوبونات شخصية للعملاء المترددين لدفعهم للإتمام."
              outcome="تزيد معدل التحويل لدى العملاء المترددين"
            />
            <FeatureCard
              icon={ShoppingBag}
              title="طلبات داخل الواتساب"
              desc="العميل يطلب ويدفع ويتأكد كل شيء داخل واتساب دون مغادرته."
              outcome="تجربة شراء سلسة = عميل راضٍ يعود"
            />
            <FeatureCard
              icon={Send}
              title="حملات مجدولة"
              desc="أرسل عروضك وإشعارات الطلبات والمناسبات لآلاف العملاء بضغطة واحدة."
              outcome="وصول مضمون أعلى من البريد الإلكتروني"
            />
            <FeatureCard
              icon={BarChart3}
              title="لوحة تحكم كاملة"
              desc="تابع المحادثات والمبيعات والتحويلات والمشكلات من مكان واحد."
              outcome="قرارات مبنية على بيانات حقيقية"
            />
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          TESTIMONIALS
      ══════════════════════════════════════════════════════════ */}
      <section id="testimonials" className="py-24 bg-slate-900 relative overflow-hidden">
        <HoneycombBg />
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-14">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">قصص نجاح حقيقية</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              التجار يتكلمون
            </h2>
            <p className="text-slate-400 text-base">ليست أرقام، هذه نتائج متاجر حقيقية</p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
            <TestimonialCard
              quote="كنت أرد يدوياً على 200 رسالة يومياً. الآن نحلة تتولى 90٪ منها وأنا أتابع فقط الحالات الاستثنائية."
              name="محمد العتيبي"
              store="متجر ملابس — الرياض"
              result="+3 ساعات يومياً"
            />
            <TestimonialCard
              quote="في أول أسبوع استرجعت 7 طلبات كانت ستضيع. نحلة ترسل للعميل في الوقت الصح وبالكلام الصح."
              name="نورة الشمري"
              store="متجر عطور — جدة"
              result="+23٪ في المبيعات"
            />
            <TestimonialCard
              quote="أفضل استثمار عملته لمتجري. الإعداد أخذ أقل من ساعة والنتائج ظهرت من اليوم الأول."
              name="خالد المنصور"
              store="متجر إلكترونيات — الدمام"
              result="ROI في أسبوع"
            />
          </div>

          {/* Aggregate trust bar */}
          <div className="mt-12 grid grid-cols-2 sm:grid-cols-4 gap-4">
            {[
              { value: '+500',  label: 'متجر نشط',           sub: 'في 3 دول خليجية' },
              { value: '98٪',   label: 'رضا التجار',          sub: 'بعد أول شهر' },
              { value: '+2.4M', label: 'محادثة معالجة',        sub: 'هذا الشهر' },
              { value: '14 يوم', label: 'تجربة مجانية كاملة', sub: 'بلا قيود' },
            ].map(({ value, label, sub }, i) => (
              <div key={i} className="text-center p-5 rounded-2xl bg-white/4 border border-white/8">
                <div className="text-2xl sm:text-3xl font-black text-amber-400 mb-1">{value}</div>
                <div className="text-white font-bold text-sm">{label}</div>
                <div className="text-slate-500 text-xs mt-0.5">{sub}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          PRICING
      ══════════════════════════════════════════════════════════ */}
      <section id="pricing" className="py-24 bg-slate-800/40 relative overflow-hidden">
        <HoneycombBg />
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-4">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">الأسعار والباقات</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              استثمار يعود عليك من أول أسبوع
            </h2>
            <p className="text-slate-400 text-base max-w-lg mx-auto">
              كل خطة تشمل تجربة مجانية 14 يوم. لا يلزم بطاقة ائتمانية.
            </p>
          </div>

          {/* Launch promo alert */}
          <div className="flex justify-center mb-10">
            <div className="inline-flex items-center gap-2.5 bg-amber-500/10 border border-amber-500/30 rounded-2xl px-6 py-3">
              <Zap size={15} className="text-amber-400 shrink-0" />
              <span className="text-amber-200 font-bold text-sm">
                عرض الإطلاق: الأسعار المعروضة بخصم 50٪ لأول شهرين — ينتهي قريباً
              </span>
            </div>
          </div>

          {/* Cards — no scale transform (breaks mobile) */}
          <div className="grid md:grid-cols-3 gap-5 lg:gap-6 items-stretch">
            <PlanCard
              name="Starter"
              nameAr="المبتدئ"
              price={899}
              launchPrice={449}
              tagline="سعر الإطلاق — وفّر 450 ريال شهرياً"
              idealFor="مثالي للمتاجر الناشئة التي تريد البدء بشكل احترافي"
              features={[
                'حتى 1,000 محادثة في الشهر',
                'ردود ذكاء اصطناعي باللغة العربية',
                '3 أتمتات مفعّلة في آنٍ واحد',
                'حملتان تسويقيتان شهرياً',
                'تقارير أساسية للمبيعات',
                'دعم عبر البريد الإلكتروني',
              ]}
              ctaLabel="ابدأ مجاناً 14 يوم"
            />
            <PlanCard
              name="Growth"
              nameAr="النمو"
              price={1699}
              launchPrice={849}
              tagline="سعر الإطلاق — وفّر 850 ريال شهرياً"
              idealFor="للمتاجر النشطة التي تريد تحقيق أقصى مبيعات عبر واتساب"
              highlighted
              badge="الأكثر اختياراً"
              features={[
                'حتى 5,000 محادثة في الشهر',
                'ردود ذكاء اصطناعي متقدمة',
                'أتمتات غير محدودة',
                '10 حملات تسويقية شهرياً',
                'تقارير مبيعات متقدمة',
                'أولوية في الدعم الفني',
              ]}
              ctaLabel="جرّب الخطة الأكثر شيوعاً"
            />
            <PlanCard
              name="Scale"
              nameAr="التوسع"
              price={2999}
              launchPrice={1499}
              tagline="سعر الإطلاق — وفّر 1,500 ريال شهرياً"
              idealFor="للعلامات التجارية والمتاجر الكبيرة بحجم مبيعات عالٍ"
              features={[
                'محادثات غير محدودة',
                'أتمتات وحملات غير محدودة',
                'تقارير مخصصة ولوحات تحكم',
                'مدير حساب مخصص',
                'دعم فني 24/7 على الواتساب',
                'وصول كامل لـ API',
              ]}
              ctaLabel="تحدث مع فريق المبيعات"
            />
          </div>

          {/* Guarantees row */}
          <div className="mt-10 grid sm:grid-cols-3 gap-4">
            {[
              { icon: Shield,     text: '14 يوم مجاناً بلا شروط' },
              { icon: BadgeCheck, text: 'بلا عقود طويلة، ألغِ متى شئت' },
              { icon: RefreshCw,  text: 'استرداد كامل خلال 7 أيام إن لم تقتنع' },
            ].map(({ icon: Icon, text }, i) => (
              <div key={i} className="flex items-center justify-center gap-2.5 p-4 rounded-2xl bg-green-500/5 border border-green-500/15">
                <Icon size={16} className="text-green-400 shrink-0" />
                <span className="text-green-300/80 text-sm font-medium">{text}</span>
              </div>
            ))}
          </div>

          {/* Main CTA under pricing */}
          <div className="text-center mt-10">
            <Link
              to="/register"
              className="inline-flex items-center gap-3 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-base px-10 py-4 rounded-2xl transition-all duration-200 shadow-xl shadow-amber-500/25 hover:shadow-amber-400/40 hover:scale-[1.02]"
            >
              ابدأ تجربتك المجانية الآن
              <ArrowLeft size={18} />
            </Link>
            <p className="text-slate-600 text-xs mt-3">
              بلا بطاقة ائتمانية · تلغي في أي وقت · الإعداد في أقل من ساعة
            </p>
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          TRUST / WHY NAHLA
      ══════════════════════════════════════════════════════════ */}
      <section className="py-24 bg-slate-900">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid lg:grid-cols-2 gap-12 lg:gap-20 items-center">
            <div>
              <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-4">لماذا نحلة؟</p>
              <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-6">
                ليست أداة — بل شريك نمو لمتجرك
              </h2>
              <p className="text-slate-400 leading-loose mb-8 text-base">
                نحلة لم تُبنَ لتكون بوتاً للردود. بُنيت لتفهم متجرك كما تفهمه أنت، وتتحدث مع عملائك
                بأسلوبك، وتحوّل كل محادثة إلى إيراد حقيقي.
              </p>
              <ul className="space-y-4">
                {[
                  { icon: TrendingUp,  text: 'تزيد إيرادات واتساب بمتوسط 35٪ في أول 3 أشهر',     highlight: true },
                  { icon: Clock,       text: 'توفّر على فريقك 3–5 ساعات يومياً من الردود المتكررة' },
                  { icon: Users,       text: 'تحوّل 30٪ من السلات المتروكة إلى طلبات مكتملة' },
                  { icon: Zap,         text: 'إعداد كامل في أقل من ساعة — دون خبرة تقنية' },
                  { icon: BadgeCheck,  text: 'مبنية خصيصاً للمتاجر السعودية والخليجية' },
                ].map(({ icon: Icon, text, highlight }, i) => (
                  <li key={i} className={`flex items-start gap-3.5 ${highlight ? 'opacity-100' : 'opacity-85'}`}>
                    <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 mt-0.5 ${
                      highlight ? 'bg-amber-500/20' : 'bg-white/5'
                    }`}>
                      <Icon size={16} className={highlight ? 'text-amber-400' : 'text-slate-400'} />
                    </div>
                    <span className={`leading-relaxed text-sm sm:text-base ${
                      highlight ? 'text-white font-bold' : 'text-slate-400'
                    }`}>{text}</span>
                  </li>
                ))}
              </ul>
            </div>

            {/* Outcome stats — specific, credible */}
            <div className="grid grid-cols-2 gap-4">
              {[
                { value: '35٪',    label: 'زيادة متوسطة في إيرادات واتساب',    color: 'text-amber-400',  bg: 'bg-amber-500/8 border-amber-500/15' },
                { value: '3 ساعات', label: 'توفّر يومياً من وقت فريق الدعم',    color: 'text-blue-400',   bg: 'bg-blue-500/8 border-blue-500/15' },
                { value: '30٪',    label: 'من السلات المتروكة تُسترجع',          color: 'text-green-400',  bg: 'bg-green-500/8 border-green-500/15' },
                { value: '< ساعة',  label: 'الإعداد الكامل من الصفر',             color: 'text-purple-400', bg: 'bg-purple-500/8 border-purple-500/15' },
              ].map(({ value, label, color, bg }, i) => (
                <div key={i} className={`p-5 sm:p-6 rounded-2xl border text-center ${bg}`}>
                  <div className={`text-3xl sm:text-4xl font-black mb-2 ${color}`}>{value}</div>
                  <div className="text-slate-400 text-xs sm:text-sm leading-snug">{label}</div>
                </div>
              ))}
              <div className="col-span-2 p-4 rounded-2xl bg-amber-500/5 border border-amber-500/15 flex items-center justify-center gap-3">
                <Shield size={18} className="text-amber-400" />
                <span className="text-amber-300/80 font-bold text-sm">ضمان استرداد كامل خلال 7 أيام</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          FINAL CTA
      ══════════════════════════════════════════════════════════ */}
      <section className="py-24 relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-amber-600/15 via-slate-800 to-slate-900" />
        <HoneycombBg />
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-amber-500/30 to-transparent" />
        <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[700px] h-[300px] bg-amber-500/8 rounded-full blur-3xl pointer-events-none" />

        <div className="relative z-10 max-w-2xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <img src="/logo.png" alt="نحلة" className="w-20 h-20 mx-auto mb-6 object-contain drop-shadow-lg" />
          <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4">
            متجرك يستحق مساعداً لا يتعب
          </h2>
          <p className="text-slate-300 text-lg leading-loose mb-3">
            ابدأ اليوم، وخلال أسبوع ستسأل نفسك: لماذا لم أفعل هذا قبل كذا؟
          </p>
          <p className="text-slate-500 text-sm mb-10">
            تجربة 14 يوم مجانية · الإعداد في ساعة واحدة · إلغاء بضغطة واحدة
          </p>

          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <Link
              to="/register"
              className="group flex items-center gap-2.5 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-base px-10 py-4 rounded-2xl transition-all duration-200 shadow-2xl shadow-amber-500/35 hover:shadow-amber-400/50 hover:scale-[1.03] w-full sm:w-auto justify-center"
            >
              أنشئ حسابك الآن مجاناً
              <ArrowLeft size={18} className="group-hover:-translate-x-1 transition-transform" />
            </Link>
            <a
              href="https://wa.me/966500000000?text=أريد معرفة المزيد عن نحلة"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 border border-white/15 hover:border-amber-400/30 text-slate-300 hover:text-white font-bold text-base px-8 py-4 rounded-2xl transition-all duration-200 hover:bg-amber-500/5 w-full sm:w-auto justify-center"
            >
              <MessageCircle size={18} />
              تحدث معنا على واتساب
            </a>
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          FAQ
      ══════════════════════════════════════════════════════════ */}
      <section id="faq" className="py-24 bg-slate-900">
        <div className="max-w-2xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-12">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">لديك تساؤلات؟</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight">
              أسئلة شائعة
            </h2>
          </div>
          <div className="space-y-2.5">
            <FaqItem
              q="كم تستغرق عملية الإعداد الكاملة؟"
              a="في المتوسط أقل من ساعة. ربط سلة يأخذ 5 دقائق، ربط واتساب 10 دقائق، وإدخال المنتجات والعروض يعتمد على حجم كتالوجك. فريقنا يساعدك في كل خطوة."
            />
            <FaqItem
              q="هل تحتاج نحلة إلى WhatsApp Business API؟"
              a="نعم، نحلة تعمل مع WhatsApp Cloud API من Meta لتقديم تجربة موثوقة ومتوافقة. نساعدك في الحصول على الوصول وإعداد الحساب ضمن باقة الاشتراك."
            />
            <FaqItem
              q="هل تدعم نحلة منصة سلة؟"
              a="نعم، نحلة مبنية أصلاً للتكامل مع سلة. يمكنها جلب منتجاتك وأسعارك وحالة المخزون تلقائياً، وإنشاء الطلبات مباشرة داخل متجرك."
            />
            <FaqItem
              q="هل يمكنني التحكم الكامل في ما تقوله نحلة؟"
              a="بالكامل. أنت تحدد المنتجات، العروض، أسلوب التواصل، والقيود. نحلة لا تتجاوز ما أذنت له — أي معلومة خارج ما أدخلته، تحوّل المحادثة لك."
            />
            <FaqItem
              q="ماذا يحدث بعد انتهاء التجربة المجانية؟"
              a="ستتلقى إشعاراً قبل 3 أيام من انتهاء التجربة. لا يُخصم أي مبلغ تلقائياً — اختر الخطة التي تناسبك، أو ألغِ بلا أي رسوم."
            />
            <FaqItem
              q="هل يمكن لنحلة إنشاء الطلبات ومعالجة الدفع؟"
              a="نعم. نحلة تستطيع إنشاء الطلب داخل متجرك وإرسال رابط الدفع للعميل مباشرة عبر واتساب، سواء عبر مدى أو فيزا أو الدفع عند الاستلام."
            />
            <FaqItem
              q="ما الفرق بين نحلة وبوتات واتساب الأخرى؟"
              a="البوتات التقليدية تعمل بقوائم وكلمات مفتاحية ثابتة. نحلة تفهم السياق والنية وتُجري محادثة طبيعية، وترتبط بمتجرك لتعرف منتجاتك وطلباتك وعروضك في الوقت الفعلي."
            />
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          MOBILE APP SECTION
      ══════════════════════════════════════════════════════════ */}
      <section className="bg-gradient-to-b from-slate-900 to-slate-950 py-20 px-4">
        <div className="max-w-3xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 bg-amber-500/10 border border-amber-500/20 rounded-full px-4 py-1.5 mb-6">
            <span className="text-amber-400 text-xs font-semibold">قريباً</span>
            <span className="text-slate-400 text-xs">·</span>
            <span className="text-slate-400 text-xs">تطبيق الجوال</span>
          </div>
          <h2 className="text-3xl md:text-4xl font-black text-white mb-4">
            نحلة في جيبك{' '}
            <span className="text-amber-400">دائماً</span>
          </h2>
          <p className="text-slate-400 text-base leading-relaxed mb-10 max-w-xl mx-auto">
            تابع محادثات متجرك، راجع الطلبات، وأدِر مساعدك الذكي من هاتفك في أي وقت ومن أي مكان.
            التطبيق قادم قريباً على App Store وGoogle Play.
          </p>

          {/* Mock phone + badges */}
          <div className="flex flex-col items-center gap-8">
            {/* Feature chips */}
            <div className="flex flex-wrap justify-center gap-3">
              {[
                { icon: '💬', text: 'ردود واتساب فورية' },
                { icon: '📦', text: 'إدارة الطلبات' },
                { icon: '📊', text: 'إحصائيات حية' },
                { icon: '🔔', text: 'إشعارات لحظية' },
              ].map(chip => (
                <div key={chip.text} className="flex items-center gap-2 bg-slate-800/60 border border-slate-700/50 rounded-xl px-4 py-2">
                  <span className="text-base">{chip.icon}</span>
                  <span className="text-slate-300 text-sm font-medium">{chip.text}</span>
                </div>
              ))}
            </div>

            {/* Store badges */}
            <div className="flex flex-col items-center gap-3">
              <div className="flex flex-wrap justify-center gap-3" dir="ltr">
                {/* App Store */}
                <div className="flex items-center gap-2 bg-slate-800 text-white rounded-2xl px-5 py-3 border border-slate-600/50 opacity-70 cursor-not-allowed select-none">
                  <svg viewBox="0 0 24 24" className="w-6 h-6 fill-white flex-shrink-0">
                    <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z" />
                  </svg>
                  <div className="flex flex-col leading-none text-right">
                    <span className="text-[10px] text-slate-400">قريباً على</span>
                    <span className="text-base font-bold">App Store</span>
                  </div>
                </div>

                {/* Google Play */}
                <div className="flex items-center gap-2 bg-slate-800 text-white rounded-2xl px-5 py-3 border border-slate-600/50 opacity-70 cursor-not-allowed select-none">
                  <svg viewBox="0 0 24 24" className="w-6 h-6 fill-white flex-shrink-0">
                    <path d="M3.18 23.76c.31.17.66.22 1.02.14l12.2-7.03-2.66-2.66-10.56 9.55zM.54 1.3C.2 1.67 0 2.2 0 2.9v18.2c0 .7.2 1.23.54 1.6l.09.08 10.2-10.2v-.24L.63 1.22l-.09.08zM20.3 10.27l-2.9-1.67-2.98 2.98 2.98 2.98 2.92-1.68c.83-.48.83-1.26-.02-1.61zM4.2.1L16.4 7.13l-2.66 2.66L3.18.24C3.5.08 3.89.05 4.2.1z" />
                  </svg>
                  <div className="flex flex-col leading-none text-right">
                    <span className="text-[10px] text-slate-400">قريباً على</span>
                    <span className="text-base font-bold">Google Play</span>
                  </div>
                </div>
              </div>

              <p className="text-slate-600 text-xs">
                في انتظار المراجعة · سيُعلن عند الإطلاق
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          FOOTER
      ══════════════════════════════════════════════════════════ */}
      <footer className="border-t border-white/6 bg-slate-900 pt-14 pb-8">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-10 mb-12">
            {/* Brand */}
            <div className="lg:col-span-2">
              <div className="flex items-center gap-2.5 mb-5">
                <img src="/logo.png" alt="نحلة" className="w-10 h-10 object-contain" />
                <span className="text-white font-black text-xl">نحلة</span>
                <span className="text-amber-400 text-[10px] font-black bg-amber-500/15 px-2 py-0.5 rounded-full border border-amber-500/20">
                  AI
                </span>
              </div>
              <p className="text-slate-500 text-sm leading-loose max-w-xs mb-6">
                منصة ذكية تحوّل واتساب إلى قناة مبيعات كاملة لمتجرك — ردود، طلبات، ودفع تلقائي.
              </p>
              <Link
                to="/register"
                className="inline-flex items-center gap-2 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-sm px-5 py-2.5 rounded-xl transition-all duration-200"
              >
                ابدأ مجاناً
                <ArrowLeft size={14} />
              </Link>
            </div>

            {/* Platform links */}
            <div>
              <h4 className="text-white font-bold text-sm mb-5">المنصة</h4>
              <ul className="space-y-3">
                {[
                  { label: 'كيف تعمل', id: 'how' },
                  { label: 'شاهد نحلة', id: 'demo' },
                  { label: 'المميزات', id: 'features' },
                  { label: 'الأسعار', id: 'pricing' },
                  { label: 'الأسئلة الشائعة', id: 'faq' },
                ].map(({ label, id }) => (
                  <li key={id}>
                    <button
                      onClick={() => scrollTo(id)}
                      className="text-slate-500 hover:text-amber-400 transition-colors text-sm"
                    >
                      {label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>

            {/* Account links */}
            <div>
              <h4 className="text-white font-bold text-sm mb-5">الحساب</h4>
              <ul className="space-y-3">
                <li>
                  <Link to="/register" className="text-slate-500 hover:text-amber-400 transition-colors text-sm">
                    إنشاء حساب جديد
                  </Link>
                </li>
                <li>
                  <Link to="/login" className="text-slate-500 hover:text-amber-400 transition-colors text-sm">
                    تسجيل الدخول
                  </Link>
                </li>
                <li>
                  <a
                    href="https://wa.me/966500000000"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-slate-500 hover:text-amber-400 transition-colors text-sm"
                  >
                    تواصل معنا
                  </a>
                </li>
              </ul>
            </div>
          </div>

          <div className="pt-6 border-t border-white/6 flex flex-col items-center gap-3 text-slate-600 text-xs">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-4 w-full">
              <p>© 2025 نحلة AI — جميع الحقوق محفوظة</p>
              <div className="flex items-center gap-2">
                <img src="/logo.png" alt="نحلة" className="w-5 h-5 object-contain" />
                <span>صُنع بعناية في المملكة العربية السعودية 🇸🇦</span>
              </div>
            </div>
            <p className="text-slate-500 text-center">
              تطوير وإدارة:{' '}
              <span className="text-slate-400 font-medium">تركي بن عايد الحارثي</span>
              {' '}·{' '}
              <span className="text-slate-500">المدير التنفيذي والمؤسس · nahlah.ai</span>
            </p>
          </div>
        </div>
      </footer>
    </div>
  )
}
