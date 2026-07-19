import { useState, useEffect, useRef, useId } from 'react'
import { Link } from 'react-router-dom'
import LegalFooter from '../components/LegalFooter'
import TrustBlock from '../components/TrustBlock'
import WhatsAppDemo from '../components/landing/WhatsAppDemo'
import InboxDemo from '../components/landing/InboxDemo'
import '../components/landing/landing.css'
import SalesIntelligenceSection from '../components/SalesIntelligenceSection'
import { useLanguage } from '../i18n/context'
import { LANDING_COPY } from '../i18n/landingCopy'
import { landingPricingAr, landingPricingEn } from '../i18n/landingPricingLabels'
import { COMPANY_INFO } from '../config/companyInfo'
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
  ArrowRight,
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
  Smartphone,
  Shield,
  Quote,
  AlertCircle,
  BadgeCheck,
  LayoutTemplate,
  CalendarHeart,
  Rocket,
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
  const baseId = useId()
  const buttonId = `${baseId}-button`
  const panelId = `${baseId}-panel`
  return (
    <div
      className={`rounded-2xl overflow-hidden transition-all duration-200 ${
        open ? 'bg-amber-500/8 border border-amber-500/25' : 'bg-white/4 border border-white/8 hover:border-amber-500/20'
      }`}
    >
      <button
        type="button"
        id={buttonId}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-5 sm:p-6 text-start gap-4"
      >
        <span className="text-white font-bold text-base sm:text-lg leading-snug">{q}</span>
        <span
          aria-hidden="true"
          className={`shrink-0 transition-colors ${open ? 'text-amber-400' : 'text-slate-500'}`}
        >
          {open ? <ChevronUp size={20} /> : <ChevronDown size={20} />}
        </span>
      </button>
      {open && (
        <div
          id={panelId}
          role="region"
          aria-labelledby={buttonId}
          className="px-5 sm:px-6 pb-5 sm:pb-6 text-slate-300 leading-loose text-sm sm:text-base border-t border-white/6 pt-4"
        >
          {a}
        </div>
      )}
    </div>
  )
}

// ── Pricing plan card ─────────────────────────────────────────────────────────
// Each plan gets its own colour identity — lifted from the in-app Billing page
// so visitors feel the dashboard look before they even register.
const PLAN_THEME = {
  starter: {
    gradient:   'from-sky-500 to-blue-600',
    shadow:     'shadow-blue-500/30',
    ring:       'ring-blue-400/40',
    glow:       'shadow-[0_8px_30px_-8px_rgba(14,165,233,0.45)]',
    icon:       Zap,
    saveBg:     'bg-white/20 text-white',
    checkColor: 'text-sky-300',
  },
  growth: {
    gradient:   'from-amber-400 to-orange-500',
    shadow:     'shadow-amber-500/40',
    ring:       'ring-amber-400/60',
    glow:       'shadow-[0_8px_40px_-8px_rgba(245,158,11,0.55)]',
    icon:       TrendingUp,
    saveBg:     'bg-white/20 text-white',
    checkColor: 'text-amber-200',
  },
  scale: {
    gradient:   'from-violet-500 to-purple-600',
    shadow:     'shadow-purple-500/30',
    ring:       'ring-violet-400/40',
    glow:       'shadow-[0_8px_30px_-8px_rgba(139,92,246,0.45)]',
    icon:       Rocket,
    saveBg:     'bg-white/20 text-white',
    checkColor: 'text-violet-300',
  },
} as const

type PlanSlug = keyof typeof PLAN_THEME

interface PlanProps {
  slug: PlanSlug
  name: string
  nameDisplay: string
  price: number
  launchPrice: number
  tagline: string
  idealFor: string
  features: string[]
  popular?: boolean
  popularBadge?: string
  ctaLabel?: string
  perMonth?: string
  currency?: string
  securePayment?: string
  defaultCta?: string
  savePercent?: (n: number) => string
  locale?: string
}

function PlanCard({
  slug, name, nameDisplay, price, launchPrice, tagline, idealFor,
  features, popular, popularBadge, ctaLabel,
  perMonth = 'ريال / شهرياً',
  currency = 'ريال',
  securePayment = 'دفع آمن — لا تُطلب بطاقة للتجربة',
  defaultCta = 'ابدأ مجاناً 14 يوم',
  savePercent = (n) => `وفّر ${n}٪`,
  locale = 'ar-SA',
}: PlanProps) {
  const theme    = PLAN_THEME[slug]
  const Icon     = theme.icon
  const discount = Math.round(((price - launchPrice) / price) * 100)

  return (
    <div
      className={[
        'relative rounded-2xl flex flex-col overflow-hidden',
        'transition-all duration-300 hover:-translate-y-1.5',
        popular
          ? `ring-2 ${theme.ring} ${theme.glow}`
          : 'ring-1 ring-white/10 hover:ring-white/20',
      ].join(' ')}
    >
      {/* Popular badge — Growth only */}
      {popular && popularBadge && (
        <div className="absolute -top-px inset-x-0 flex justify-center">
          <span className="bg-amber-500/15 border border-amber-400/30 text-amber-200 text-[10px] font-bold px-3 py-0.5 rounded-b-lg backdrop-blur-sm">
            {popularBadge}
          </span>
        </div>
      )}

      {/* Gradient header */}
      <div className={`bg-gradient-to-br ${theme.gradient} p-5 ${popular ? 'pt-8' : 'pt-5'} text-white`}>
        <div className="flex items-center gap-2 mb-3">
          <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
            <Icon size={16} className="text-white" />
          </div>
          <div>
            <p className="text-white/70 text-[10px] font-bold uppercase tracking-widest">{name}</p>
            <h3 className="text-xl font-black leading-tight">{nameDisplay}</h3>
          </div>
          {discount > 0 && (
            <span className={`ms-auto text-[11px] font-bold px-2 py-0.5 rounded-full ${theme.saveBg}`}>
              {savePercent(discount)}
            </span>
          )}
        </div>
        <p className="text-white/70 text-xs mb-4 leading-relaxed">{idealFor}</p>

        {/* Price */}
        <div className="flex items-end gap-2 mb-1">
          <span className="text-4xl font-black leading-none">
            {launchPrice.toLocaleString(locale)}
          </span>
          <div className="pb-1">
            <div className="text-white/50 text-xs line-through">
              {price.toLocaleString(locale)} {currency}
            </div>
            <div className="text-white/70 text-xs font-medium">{perMonth}</div>
          </div>
        </div>
        <p className="text-white/60 text-[11px]">{tagline}</p>
      </div>

      {/* Features body — min-height keeps cards visually balanced */}
      <div className="bg-slate-800/80 flex-1 p-5 backdrop-blur-sm">
        <ul className="flex flex-col gap-2.5 min-h-[280px]">
          {features.map((f, i) => (
            <li key={i} className="flex items-start gap-2.5">
              <Check size={14} className={`mt-0.5 shrink-0 ${theme.checkColor}`} />
              <span className="text-slate-300 text-sm leading-snug">{f}</span>
            </li>
          ))}
        </ul>
      </div>

      {/* CTA */}
      <div className="bg-slate-800/90 px-5 pb-5 pt-3 backdrop-blur-sm">
        <Link
          to="/register"
          className={`block text-center py-3 rounded-xl font-black text-sm text-white transition-all duration-200 hover:scale-[1.02] active:scale-100 bg-gradient-to-br ${theme.gradient} shadow-md hover:brightness-110`}
        >
          {ctaLabel ?? defaultCta}
        </Link>
        <p className="flex items-center justify-center gap-1 text-[10px] text-slate-500 mt-2">
          <Shield size={10} /> {securePayment}
        </p>
      </div>
    </div>
  )
}

// ── Feature card ──────────────────────────────────────────────────────────────
function FeatureCard({
  icon: Icon, title, desc, outcome, highlight = false, featuredBadge = '⭐ مميزة',
}: { icon: React.ElementType; title: string; desc: string; outcome?: string; highlight?: boolean; featuredBadge?: string }) {
  if (highlight) {
    return (
      <div className="group relative p-6 rounded-2xl bg-gradient-to-b from-amber-500/12 to-amber-500/4 border border-amber-400/30 hover:border-amber-400/60 hover:from-amber-500/18 transition-all duration-300 hover:-translate-y-1 backdrop-blur-sm shadow-lg shadow-amber-500/8">
        {/* Featured badge */}
        <div className="absolute -top-2.5 start-4">
          <span className="bg-amber-500 text-slate-900 text-[10px] font-black px-2.5 py-0.5 rounded-full shadow-md shadow-amber-500/30">
            {featuredBadge}
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

// ── Salla embedded SDK signal helper ──────────────────────────────────────────
function signalSallaReady() {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const s = (window as any).Salla
    if (s?.embedded) {
      s.embedded.init({ debug: false })
        .then(() => s.embedded.ready())
        .catch(() => s.embedded.ready())
    }
  } catch { /* ignore */ }
  try { window.parent.postMessage(JSON.stringify({ event: 'app.ready' }), '*') } catch { /* ignore */ }
  try { window.parent.postMessage({ event: 'app.ready', type: 'app.ready' }, '*') } catch { /* ignore */ }
}

const FEATURE_ICONS = [
  Bot, Zap, ShoppingCart, RefreshCw, Star, CreditCard, Gift, ShoppingBag,
  Send, LayoutTemplate, CalendarHeart, BarChart3,
] as const

const OFFICIAL_WHATSAPP_DIGITS = COMPANY_INFO.phone.href.replace(/^tel:\+?/, '')

function officialWhatsAppUrl(text?: string): string {
  const base = `https://wa.me/${OFFICIAL_WHATSAPP_DIGITS}`
  return text ? `${base}?text=${encodeURIComponent(text)}` : base
}

// ── Main landing page ─────────────────────────────────────────────────────────
export default function Landing() {
  const { lang, setLang, dir } = useLanguage()
  const c = LANDING_COPY[lang]
  const pricing = lang === 'en' ? landingPricingEn : landingPricingAr
  const priceLocale = lang === 'en' ? 'en-US' : 'ar-SA'
  const ArrowCta = dir === 'rtl' ? ArrowLeft : ArrowRight
  const fontFamily = lang === 'ar' ? "'Cairo', sans-serif" : "system-ui, -apple-system, sans-serif"

  const [scrolled, setScrolled]         = useState(false)
  const [mobileMenuOpen, setMobile]     = useState(false)
  const heroRef                          = useRef<HTMLDivElement>(null)
  const mobileMenuId                     = useId()

  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 50)
    window.addEventListener('scroll', fn, { passive: true })
    return () => window.removeEventListener('scroll', fn)
  }, [])

  // Dark background on html/body so the iOS safe-area gap above the nav
  // shows the page colour (#0f172a ≈ slate-900) instead of the default white.
  useEffect(() => {
    const html = document.documentElement
    const body = document.body
    const prevHtml = html.style.backgroundColor
    const prevBody = body.style.backgroundColor
    html.style.backgroundColor = '#0f172a'
    body.style.backgroundColor = '#0f172a'
    return () => {
      html.style.backgroundColor = prevHtml
      body.style.backgroundColor = prevBody
    }
  }, [])

  // Signal Salla iframe ready (dismisses skeleton loaders when embedded in Salla)
  useEffect(() => {
    signalSallaReady()
    const t1 = setTimeout(signalSallaReady, 800)
    const t2 = setTimeout(signalSallaReady, 2000)
    return () => { clearTimeout(t1); clearTimeout(t2) }
  }, [])

  const scrollTo = (id: string) => {
    setMobile(false)
    setTimeout(() => {
      const el = document.getElementById(id)
      if (!el) return
      const navOffset = 72
      const top = el.getBoundingClientRect().top + window.scrollY - navOffset
      window.scrollTo({ top, behavior: 'smooth' })
    }, 50)
  }

  const navLinks = c.nav

  const langButtonClass =
    'shrink-0 inline-flex items-center justify-center min-h-[36px] px-3 py-2 text-sm font-semibold text-amber-300 hover:text-white transition-colors whitespace-nowrap'

  const navAriaLabel = lang === 'en' ? 'Main navigation' : 'التنقل الرئيسي'
  const skipToMainLabel = lang === 'en' ? 'Skip to main content' : 'تخطّ إلى المحتوى الرئيسي'

  return (
    <div dir={dir} lang={lang} className="landing-page min-h-screen bg-slate-900 overflow-x-hidden" style={{ fontFamily }}>

      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-4 focus:start-4 focus:z-[100] focus:rounded-lg focus:bg-amber-500 focus:px-4 focus:py-2 focus:text-slate-900 focus:font-bold focus:outline-none focus:ring-2 focus:ring-amber-300"
      >
        {skipToMainLabel}
      </a>

      {/* ══════════════════════════════════════════════════════════
          NAVBAR
      ══════════════════════════════════════════════════════════ */}
      <nav
        aria-label={navAriaLabel}
        className={`fixed top-0 inset-x-0 z-50 transition-all duration-300 pt-safe-nav ${
        scrolled ? 'bg-slate-900/96 backdrop-blur-xl shadow-lg shadow-black/30 border-b border-white/5' : 'bg-transparent'
      }`}>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center h-16 w-full">
            {/* Logo */}
            <Link to="/landing" className="flex items-center gap-2 group shrink-0">
              <img src="/logo.png" alt={c.brandName} className="w-10 h-10 object-contain drop-shadow-md" />
              <span className="text-white font-black text-xl tracking-tight">{c.brandName}</span>
              <span className="text-amber-400 text-[10px] font-black bg-amber-500/15 px-1.5 py-0.5 rounded-full border border-amber-500/25 leading-none">
                AI
              </span>
            </Link>

            {/* Center nav — lg+ only so actions always keep space */}
            <div className="hidden lg:flex flex-1 min-w-0 items-center justify-center gap-0.5 px-2">
              {navLinks.map((l) => (
                <button
                  type="button"
                  key={l.id}
                  onClick={() => scrollTo(l.id)}
                  className="text-slate-400 hover:text-white transition-colors px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap"
                >
                  {l.label}
                </button>
              ))}
            </div>

            {/* Actions — always pinned to the end; language never shares flex with nav */}
            <div className="flex items-center gap-2 sm:gap-3 shrink-0 ms-auto">
              <Link
                to="/login"
                className="hidden md:inline text-slate-400 hover:text-white text-sm font-medium transition-colors px-2 py-2 whitespace-nowrap"
              >
                {c.navLogin}
              </Link>
              <button
                type="button"
                onClick={() => setLang(lang === 'ar' ? 'en' : 'ar')}
                className={langButtonClass}
                aria-label={lang === 'ar' ? 'Switch to English' : 'التبديل إلى العربية'}
              >
                {c.langSwitch}
              </button>
              <Link
                to="/register"
                className="hidden md:inline-flex landing-trial-btn items-center justify-center text-sm px-5 py-2.5 rounded-xl whitespace-nowrap"
              >
                {c.navTrial}
              </Link>
              <button
                type="button"
                className="md:hidden text-slate-400 flex items-center justify-center min-w-[44px] min-h-[44px] rounded-xl -me-2"
                onClick={() => setMobile(!mobileMenuOpen)}
                aria-expanded={mobileMenuOpen}
                aria-controls={mobileMenuId}
                aria-label={mobileMenuOpen ? c.menuClose : c.menuOpen}
              >
                {mobileMenuOpen ? <X size={22} /> : <Menu size={22} />}
              </button>
            </div>
          </div>
        </div>

        {/* Mobile dropdown */}
        {mobileMenuOpen && (
          <div id={mobileMenuId} className="md:hidden border-t border-white/8 bg-slate-900/98 backdrop-blur-xl">
            <div className="px-4 py-3 flex flex-col divide-y divide-white/5">
              <div className="flex flex-col pb-2">
                {navLinks.map((l) => (
                  <button
                    type="button"
                    key={l.id}
                    onClick={() => scrollTo(l.id)}
                    className="text-slate-300 text-sm font-medium py-3 text-start hover:text-amber-400 transition-colors"
                  >
                    {l.label}
                  </button>
                ))}
              </div>
              <div className="flex flex-col gap-2 pt-3">
                <button
                  type="button"
                  onClick={() => { setLang(lang === 'ar' ? 'en' : 'ar'); setMobile(false) }}
                  className={`${langButtonClass} w-full text-center`}
                >
                  {c.langSwitch}
                </button>
                <Link to="/login" className="text-center text-slate-400 py-2.5 text-sm" onClick={() => setMobile(false)}>
                  {c.navLoginMobile}
                </Link>
                <Link to="/register" onClick={() => setMobile(false)}
                  className="landing-trial-btn text-center text-sm py-3.5 rounded-2xl">
                  {c.navTrialMobile}
                </Link>
              </div>
            </div>
          </div>
        )}
      </nav>

      <main id="main-content" tabIndex={-1}>

      {/* ══════════════════════════════════════════════════════════
          HERO
      ══════════════════════════════════════════════════════════ */}
      <section
        ref={heroRef}
        className="landing-hero relative min-h-[100svh] flex sm:items-center justify-center overflow-hidden bg-gradient-to-br from-slate-900 via-[#0f1d2e] to-slate-900 sm:pt-16"
      >
        <HoneycombBg />
        {/* Glow */}
        <div className="absolute top-1/3 right-1/4 w-[500px] h-[500px] bg-amber-500/8 rounded-full blur-[100px] pointer-events-none" />
        <div className="absolute bottom-1/4 left-1/4 w-96 h-96 bg-blue-600/5 rounded-full blur-[80px] pointer-events-none" />

        <div className="landing-hero-content relative z-10 max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 text-center sm:py-20">

          {/* Urgency badge */}
          <div className="inline-flex items-center gap-2 bg-amber-500/10 border border-amber-500/25 rounded-full px-4 py-2 mb-6">
            <span className="w-2 h-2 bg-amber-400 rounded-full animate-pulse" />
            <span className="text-amber-300 text-sm font-bold">
              {c.hero.badge}
            </span>
          </div>

          {/* Core promise — WhatsApp stays on your phone */}
          <h1 className="text-[2rem] sm:text-5xl lg:text-[4.25rem] font-black text-white leading-[1.12] mb-4 sm:mb-5 tracking-tight max-w-3xl mx-auto">
            {c.hero.titleLine1}
            <br />
            <span className="text-transparent bg-clip-text bg-gradient-to-l from-amber-300 via-amber-400 to-yellow-500">
              {c.hero.titleLine2}
            </span>
          </h1>

          <p className="text-base sm:text-xl text-slate-300 max-w-2xl mx-auto leading-relaxed mb-6 sm:mb-8 font-medium">
            {c.hero.subtitle}
          </p>

          {/* Primary CTA — above visual block so trial stays reachable on mobile */}
          <div className="relative z-20 flex flex-col sm:flex-row items-center justify-center gap-3 mb-6 sm:mb-8">
            <Link
              to="/register"
              className="landing-trial-btn group inline-flex items-center gap-2.5 text-base sm:text-lg px-8 sm:px-10 py-4 rounded-2xl w-full sm:w-auto justify-center order-1"
            >
              <span className="sm:hidden pointer-events-none">{c.hero.ctaTrialMobile}</span>
              <span className="hidden sm:inline pointer-events-none">{c.hero.ctaTrialDesktop}</span>
              <ArrowCta size={18} className="pointer-events-none transition-transform motion-safe:group-hover:translate-x-0.5 rtl:motion-safe:group-hover:-translate-x-1" />
            </Link>
            <button
              type="button"
              onClick={() => scrollTo('how')}
              className="flex items-center gap-2 text-slate-300 hover:text-white border border-white/15 hover:border-amber-400/35 hover:bg-amber-500/5 text-sm sm:text-base px-6 py-4 rounded-2xl transition-all duration-200 w-full sm:w-auto justify-center order-2"
            >
              <MessageCircle size={17} className="text-emerald-400/80 shrink-0" />
              {c.hero.ctaHow}
            </button>
          </div>

          {/* Visual stack — WA → AI → campaigns (compact support block) */}
          <div className="landing-hero-value">
            <div className="landing-hero-pill">
              <Smartphone size={13} className="shrink-0 text-amber-400" />
              {c.hero.pill}
            </div>
            <div className="landing-hero-stack" aria-label={c.hero.stackAria}>
              <div className="landing-hero-stack-item">
                <div className="landing-hero-stack-icon landing-hero-stack-icon--wa">
                  <MessageCircle size={18} />
                </div>
                <span className="landing-hero-stack-label">{c.hero.stackWa}</span>
              </div>
              <ArrowCta size={14} className="landing-hero-stack-arrow shrink-0" aria-hidden="true" />
              <div className="landing-hero-stack-item">
                <div className="landing-hero-stack-icon landing-hero-stack-icon--ai">
                  <Bot size={18} />
                </div>
                <span className="landing-hero-stack-label">{c.hero.stackAi}</span>
              </div>
              <ArrowCta size={14} className="landing-hero-stack-arrow shrink-0" aria-hidden="true" />
              <div className="landing-hero-stack-item">
                <div className="landing-hero-stack-icon landing-hero-stack-icon--campaign">
                  <Send size={17} />
                </div>
                <span className="landing-hero-stack-label">{c.hero.stackCampaign}</span>
              </div>
            </div>
            <p className="landing-hero-stack-note hidden sm:block">
              {c.hero.stackNote}
            </p>
          </div>

          {/* Risk-reversal micro-copy */}
          <p className="text-slate-600 text-xs mt-4">
            {c.hero.riskReversal}
          </p>

          {/* Social proof bar */}
          <div className="mt-8 sm:mt-12 inline-flex flex-wrap items-center justify-center gap-4 sm:gap-6 bg-white/3 border border-white/8 rounded-2xl px-5 sm:px-6 py-3.5 sm:py-4 backdrop-blur-sm">
            <div className="flex items-center gap-2.5">
              <div className={`flex -space-x-2 ${dir === 'rtl' ? 'space-x-reverse' : ''}`}>
                {['🧑‍💼', '👩‍💼', '👨‍💻', '👩‍🍳'].map((e, i) => (
                  <div key={i} className="w-7 h-7 rounded-full bg-slate-700 border-2 border-slate-800 flex items-center justify-center text-xs">
                    {e}
                  </div>
                ))}
              </div>
              <span className="text-slate-400 text-sm font-medium">{c.hero.socialStores}</span>
            </div>
            <div className="w-px h-5 bg-white/10 hidden sm:block" />
            <div className="flex items-center gap-1.5">
              {[...Array(5)].map((_, i) => <Star key={i} size={13} className="text-amber-400 fill-amber-400" />)}
              <span className="text-slate-400 text-sm font-medium">{c.hero.socialRating}</span>
            </div>
            <div className="w-px h-5 bg-white/10 hidden sm:block" />
            <span className="text-slate-400 text-sm font-medium">{c.hero.socialTrial}</span>
          </div>
        </div>

        <button
          type="button"
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
            {c.problem.heading}
          </p>
          <div className="grid sm:grid-cols-3 gap-4">
            {[
              { icon: AlertCircle, text: c.problem.items[0] },
              { icon: Clock,       text: c.problem.items[1] },
              { icon: ShoppingCart, text: c.problem.items[2] },
            ].map(({ icon: Icon, text }, i) => (
              <div key={i} className="flex items-start gap-3 p-4 rounded-xl bg-red-500/5 border border-red-500/10">
                <Icon size={18} className="text-red-400/70 shrink-0 mt-0.5" />
                <p className="text-slate-400 text-sm leading-relaxed">{text}</p>
              </div>
            ))}
          </div>
          <p className="text-center text-amber-400 font-bold text-sm mt-7">
            {c.problem.closing}
          </p>
        </div>
      </section>

      <div id="why" className="scroll-mt-20">
        <SalesIntelligenceSection lang={lang} />
      </div>

      {/* ══════════════════════════════════════════════════════════
          HOW IT WORKS
      ══════════════════════════════════════════════════════════ */}
      <section id="how" className="py-24 relative overflow-hidden bg-slate-900 scroll-mt-20">
        <HoneycombBg />
        <div className="max-w-2xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-14">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">{c.how.eyebrow}</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              {c.how.title}
            </h2>
            <p className="text-slate-400 text-base">{c.how.subtitle}</p>
          </div>
          <div>
            {c.how.steps.map((step, i) => (
              <StepCard
                key={step.num}
                num={step.num}
                title={step.title}
                desc={step.desc}
                time={step.time}
                last={i === c.how.steps.length - 1}
              />
            ))}
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
                {c.demo.eyebrow}
              </p>
              <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-5">
                {c.demo.titleLine1}
                <br />
                <span className="text-transparent bg-clip-text bg-gradient-to-l from-[#25D366] to-[#128C7E]">
                  {c.demo.titleLine2}
                </span>
              </h2>
              <p className="text-slate-400 leading-loose text-base mb-8 max-w-lg">
                {c.demo.subtitle}
              </p>

              <ul className="space-y-4 mb-10">
                {c.demo.bullets.map(({ emoji, title, desc }) => (
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
                <span>{c.demo.cta}</span>
                <ArrowCta size={16} />
              </Link>
            </div>

            <div className="flex justify-center lg:justify-end">
              <WhatsAppDemo lang={lang} />
            </div>

          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          UNIFIED INBOX — interactive merchant-side simulation
      ══════════════════════════════════════════════════════════ */}
      <section id="inbox" className="py-24 bg-slate-900 relative overflow-hidden">
        <HoneycombBg opacity="opacity-[0.03]" />

        {/* Ambient glows */}
        <div className="absolute -top-20 -right-20 w-[480px] h-[480px] bg-amber-500/8 rounded-full blur-[100px] pointer-events-none" />
        <div className="absolute bottom-0 left-0 w-[420px] h-[420px] bg-violet-500/8 rounded-full blur-[100px] pointer-events-none" />

        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-12">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">
              {c.inbox.eyebrow}
            </p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4">
              {c.inbox.titleLine1}
              <br />
              <span className="text-transparent bg-clip-text bg-gradient-to-l from-amber-400 to-orange-500">
                {c.inbox.titleLine2}
              </span>
            </h2>
            <p className="text-slate-400 max-w-2xl mx-auto leading-loose text-base">
              {c.inbox.subtitle}
              <span className="block mt-1 text-slate-500 text-sm">{c.inbox.subtitleHint}</span>
            </p>
          </div>

          <div className="relative">
            <span className="absolute -top-3 right-1/2 translate-x-1/2 z-20 inline-flex items-center gap-1.5 bg-amber-500 text-slate-900 text-[11px] font-black px-3 py-1 rounded-full shadow-lg shadow-amber-500/30">
              <span className="w-1.5 h-1.5 rounded-full bg-slate-900 animate-pulse" />
              {c.inbox.interactiveBadge}
            </span>
            <InboxDemo lang={lang} />
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mt-12">
            {[
              { dot: 'bg-rose-400 shadow-[0_0_8px_#fb7185]',    ...c.inbox.capabilities[0] },
              { dot: 'bg-emerald-400 shadow-[0_0_8px_#34d399]', ...c.inbox.capabilities[1] },
              { dot: 'bg-violet-400 shadow-[0_0_8px_#a78bfa]',  ...c.inbox.capabilities[2] },
              { dot: 'bg-sky-400 shadow-[0_0_8px_#38bdf8]',     ...c.inbox.capabilities[3] },
            ].map(cap => (
              <div
                key={cap.title}
                className="bg-slate-800/40 border border-white/5 rounded-xl p-4 hover:border-white/10 transition-colors"
              >
                <div className={`w-2 h-2 rounded-full mb-2.5 ${cap.dot}`} />
                <h3 className="text-white font-bold text-sm mb-1">{cap.title}</h3>
                <p className="text-slate-400 text-xs leading-relaxed">{cap.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          FEATURES
      ══════════════════════════════════════════════════════════ */}
      <section id="features" className="py-24 bg-slate-800/30">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-14">
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">{c.features.eyebrow}</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4">
              {c.features.title}
            </h2>
            <p className="text-slate-400 max-w-xl mx-auto leading-relaxed text-base">
              {c.features.subtitle}
            </p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {c.features.items.map((feat, i) => (
              <FeatureCard
                key={feat.title}
                icon={FEATURE_ICONS[i]}
                title={feat.title}
                desc={feat.desc}
                outcome={feat.outcome}
                highlight={feat.highlight}
                featuredBadge={c.features.featuredBadge}
              />
            ))}
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
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">{c.testimonials.eyebrow}</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              {c.testimonials.title}
            </h2>
            <p className="text-slate-400 text-base">{c.testimonials.subtitle}</p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
            {c.testimonials.items.map((t) => (
              <TestimonialCard
                key={t.name}
                quote={t.quote}
                name={t.name}
                store={t.store}
                result={t.result}
              />
            ))}
          </div>

          <div className="mt-12 grid grid-cols-2 sm:grid-cols-4 gap-4">
            {c.testimonials.stats.map(({ value, label, sub }, i) => (
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
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">{c.pricing.eyebrow}</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-3">
              {c.pricing.title}
            </h2>
            <p className="text-slate-400 text-base max-w-lg mx-auto">
              {c.pricing.subtitle}
            </p>
          </div>

          <div className="flex justify-center mb-10">
            <div className="inline-flex items-center gap-2.5 bg-amber-500/10 border border-amber-500/30 rounded-2xl px-6 py-3">
              <Zap size={15} className="text-amber-400 shrink-0" />
              <span className="text-amber-200 font-bold text-sm">
                {c.pricing.promo}
              </span>
            </div>
          </div>

          <div className="grid md:grid-cols-3 gap-5 lg:gap-6 items-stretch">
            {(['starter', 'growth', 'scale'] as const).map((slug) => {
              const plan = pricing.plans[slug]
              return (
                <PlanCard
                  key={slug}
                  slug={slug}
                  name={plan.name}
                  nameDisplay={plan.nameDisplay}
                  price={plan.price}
                  launchPrice={plan.launchPrice}
                  tagline={plan.tagline}
                  idealFor={plan.idealFor}
                  features={plan.features}
                  ctaLabel={plan.ctaLabel}
                  popular={slug === 'growth'}
                  popularBadge={slug === 'growth' ? pricing.popularBadge : undefined}
                  perMonth={pricing.perMonth}
                  currency={pricing.currency}
                  securePayment={pricing.securePayment}
                  defaultCta={pricing.defaultCta}
                  savePercent={c.pricing.savePercent}
                  locale={priceLocale}
                />
              )
            })}
          </div>

          <div className="mt-10 grid sm:grid-cols-3 gap-4">
            {[
              { icon: Shield,     text: c.pricing.guarantees[0] },
              { icon: BadgeCheck, text: c.pricing.guarantees[1] },
              { icon: RefreshCw,  text: c.pricing.guarantees[2] },
            ].map(({ icon: Icon, text }, i) => (
              <div key={i} className="flex items-center justify-center gap-2.5 p-4 rounded-2xl bg-green-500/5 border border-green-500/15">
                <Icon size={16} className="text-green-400 shrink-0" />
                <span className="text-green-300/80 text-sm font-medium">{text}</span>
              </div>
            ))}
          </div>

          <div className="text-center mt-10">
            <Link
              to="/register"
              className="inline-flex items-center gap-3 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-base px-10 py-4 rounded-2xl transition-all duration-200 shadow-xl shadow-amber-500/25 hover:shadow-amber-400/40 hover:scale-[1.02]"
            >
              {c.pricing.cta}
              <ArrowCta size={18} />
            </Link>
            <p className="text-slate-600 text-xs mt-3">
              {c.pricing.ctaNote}
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
              <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-4">{c.trust.eyebrow}</p>
              <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-6">
                {c.trust.title}
              </h2>
              <p className="text-slate-400 leading-loose mb-8 text-base">
                {c.trust.body}
              </p>
              <ul className="space-y-4">
                {c.trust.bullets.map(({ text, highlight }, i) => (
                  <li key={i} className={`flex items-start gap-3.5 ${highlight ? 'opacity-100' : 'opacity-85'}`}>
                    <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 mt-0.5 ${
                      highlight ? 'bg-amber-500/20' : 'bg-white/5'
                    }`}>
                      {highlight
                        ? <TrendingUp size={16} className="text-amber-400" />
                        : i === 1 ? <Clock size={16} className="text-slate-400" />
                        : i === 2 ? <Users size={16} className="text-slate-400" />
                        : i === 3 ? <Zap size={16} className="text-slate-400" />
                        : <BadgeCheck size={16} className="text-slate-400" />}
                    </div>
                    <span className={`leading-relaxed text-sm sm:text-base ${
                      highlight ? 'text-white font-bold' : 'text-slate-400'
                    }`}>{text}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="grid grid-cols-2 gap-4">
              {c.trust.stats.map(({ value, label, prefix }, i) => (
                <div key={i} className={`p-5 sm:p-6 rounded-2xl border text-center ${
                  i === 0 ? 'bg-amber-500/8 border-amber-500/15' :
                  i === 1 ? 'bg-blue-500/8 border-blue-500/15' :
                  i === 2 ? 'bg-green-500/8 border-green-500/15' :
                  'bg-purple-500/8 border-purple-500/15'
                }`}>
                  {prefix && (
                    <div className="text-slate-500 text-[10px] font-medium mb-0.5">{prefix}</div>
                  )}
                  <div className={`text-3xl sm:text-4xl font-black mb-2 ${
                    i === 0 ? 'text-amber-400' : i === 1 ? 'text-blue-400' : i === 2 ? 'text-green-400' : 'text-purple-400'
                  }`}>{value}</div>
                  <div className="text-slate-400 text-xs sm:text-sm leading-snug">{label}</div>
                </div>
              ))}
              <div className="col-span-2 p-4 rounded-2xl bg-amber-500/5 border border-amber-500/15 flex items-center justify-center gap-3">
                <Shield size={18} className="text-amber-400" />
                <span className="text-amber-300/80 font-bold text-sm">{c.trust.refund}</span>
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
          <img src="/logo.png" alt={c.brandName} className="w-20 h-20 mx-auto mb-6 object-contain drop-shadow-lg" />
          <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4">
            {c.finalCta.title}
          </h2>
          <p className="text-slate-300 text-lg leading-loose mb-3">
            {c.finalCta.body}
          </p>
          <p className="text-slate-500 text-sm mb-10">
            {c.finalCta.note}
          </p>

          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <Link
              to="/register"
              className="group flex items-center gap-2.5 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-base px-10 py-4 rounded-2xl transition-all duration-200 shadow-2xl shadow-amber-500/35 hover:shadow-amber-400/50 hover:scale-[1.03] w-full sm:w-auto justify-center"
            >
              {c.finalCta.primary}
              <ArrowCta size={18} className="transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-1" />
            </Link>
            <a
              href={officialWhatsAppUrl(c.finalCta.whatsappText)}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 border border-white/15 hover:border-amber-400/30 text-slate-300 hover:text-white font-bold text-base px-8 py-4 rounded-2xl transition-all duration-200 hover:bg-amber-500/5 w-full sm:w-auto justify-center"
            >
              <MessageCircle size={18} />
              {c.finalCta.whatsapp}
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
            <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">{c.faq.eyebrow}</p>
            <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight">
              {c.faq.title}
            </h2>
          </div>
          <div className="space-y-2.5">
            {c.faq.items.map((item) => (
              <FaqItem key={item.q} q={item.q} a={item.a} />
            ))}
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════
          MOBILE APP SECTION
      ══════════════════════════════════════════════════════════ */}
      <section className="bg-gradient-to-b from-slate-900 to-slate-950 py-20 px-4">
        <div className="max-w-3xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 bg-amber-500/10 border border-amber-500/20 rounded-full px-4 py-1.5 mb-6">
            <span className="text-amber-400 text-xs font-semibold">{c.mobileApp.soon}</span>
            <span className="text-slate-400 text-xs">·</span>
            <span className="text-slate-400 text-xs">{c.mobileApp.label}</span>
          </div>
          <h2 className="text-3xl md:text-4xl font-black text-white mb-4">
            {c.mobileApp.title}{' '}
            <span className="text-amber-400">{c.mobileApp.titleAccent}</span>
          </h2>
          <p className="text-slate-400 text-base leading-relaxed mb-10 max-w-xl mx-auto">
            {c.mobileApp.body}
          </p>

          <div className="flex flex-col items-center gap-8">
            <div className="flex flex-wrap justify-center gap-3">
              {c.mobileApp.chips.map((text, i) => (
                <div key={text} className="flex items-center gap-2 bg-slate-800/60 border border-slate-700/50 rounded-xl px-4 py-2">
                  <span className="text-base">{['💬', '📦', '📊', '🔔'][i]}</span>
                  <span className="text-slate-300 text-sm font-medium">{text}</span>
                </div>
              ))}
            </div>

            <div className="flex flex-col items-center gap-3">
              <div className="flex flex-wrap justify-center gap-3" dir="ltr">
                <div className="flex items-center gap-2 bg-slate-800 text-white rounded-2xl px-5 py-3 border border-slate-600/50 opacity-70 cursor-not-allowed select-none">
                  <svg viewBox="0 0 24 24" className="w-6 h-6 fill-white flex-shrink-0">
                    <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z" />
                  </svg>
                  <div className="flex flex-col leading-none text-start">
                    <span className="text-[10px] text-slate-400">{c.mobileApp.storeSoon}</span>
                    <span className="text-base font-bold">App Store</span>
                  </div>
                </div>

                <div className="flex items-center gap-2 bg-slate-800 text-white rounded-2xl px-5 py-3 border border-slate-600/50 opacity-70 cursor-not-allowed select-none">
                  <svg viewBox="0 0 24 24" className="w-6 h-6 fill-white flex-shrink-0">
                    <path d="M3.18 23.76c.31.17.66.22 1.02.14l12.2-7.03-2.66-2.66-10.56 9.55zM.54 1.3C.2 1.67 0 2.2 0 2.9v18.2c0 .7.2 1.23.54 1.6l.09.08 10.2-10.2v-.24L.63 1.22l-.09.08zM20.3 10.27l-2.9-1.67-2.98 2.98 2.98 2.98 2.92-1.68c.83-.48.83-1.26-.02-1.61zM4.2.1L16.4 7.13l-2.66 2.66L3.18.24C3.5.08 3.89.05 4.2.1z" />
                  </svg>
                  <div className="flex flex-col leading-none text-start">
                    <span className="text-[10px] text-slate-400">{c.mobileApp.storeSoon}</span>
                    <span className="text-base font-bold">Google Play</span>
                  </div>
                </div>
              </div>

              <p className="text-slate-600 text-xs">
                {c.mobileApp.pending}
              </p>
            </div>
          </div>
        </div>
      </section>

      </main>

      {/* ══════════════════════════════════════════════════════════
          FOOTER
      ══════════════════════════════════════════════════════════ */}
      <footer className="border-t border-white/6 bg-slate-900 pt-14 pb-8">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-10 mb-12">
            {/* Brand */}
            <div className="lg:col-span-2">
              <div className="flex items-center gap-2.5 mb-5">
                <img src="/logo.png" alt={c.brandName} className="w-10 h-10 object-contain" />
                <span className="text-white font-black text-xl">{c.brandName}</span>
                <span className="text-amber-400 text-[10px] font-black bg-amber-500/15 px-2 py-0.5 rounded-full border border-amber-500/20">
                  AI
                </span>
              </div>
              <p className="text-slate-500 text-sm leading-loose max-w-xs mb-6">
                {c.footer.tagline}
              </p>
              <Link
                to="/register"
                className="inline-flex items-center gap-2 bg-amber-500 hover:bg-amber-400 text-slate-900 font-black text-sm px-5 py-2.5 rounded-xl transition-all duration-200"
              >
                {c.footer.cta}
                <ArrowCta size={14} />
              </Link>
            </div>

            <div>
              <h2 className="text-white font-bold text-sm mb-5">{c.footer.platformHeading}</h2>
              <ul className="space-y-3">
                {c.footer.platformLinks.map(({ label, id }) => (
                  <li key={id}>
                    <button
                      type="button"
                      onClick={() => scrollTo(id)}
                      className="text-slate-500 hover:text-amber-400 transition-colors text-sm"
                    >
                      {label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <h2 className="text-white font-bold text-sm mb-5">{c.footer.accountHeading}</h2>
              <ul className="space-y-3">
                <li>
                  <Link to="/register" className="text-slate-500 hover:text-amber-400 transition-colors text-sm">
                    {c.footer.register}
                  </Link>
                </li>
                <li>
                  <Link to="/login" className="text-slate-500 hover:text-amber-400 transition-colors text-sm">
                    {c.footer.login}
                  </Link>
                </li>
                <li>
                  <a
                    href={officialWhatsAppUrl()}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-slate-500 hover:text-amber-400 transition-colors text-sm"
                  >
                    {c.footer.contact}
                  </a>
                </li>
              </ul>
            </div>
          </div>

          <div className="pt-6 border-t border-white/6 flex flex-col items-center gap-3 text-slate-600 text-xs">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-4 w-full">
              <p>{c.footer.copyright}</p>
              <div className="flex items-center gap-2">
                <img src="/logo.png" alt={c.brandName} className="w-5 h-5 object-contain" />
                <span>{c.footer.madeIn}</span>
              </div>
            </div>
            <p className="text-slate-500 text-center">
              <span className="text-slate-400 font-medium">Founder & CEO</span>
              {' '}·{' '}
              <span className="text-slate-400 font-medium">Turki Alharthi</span>
              {' '}·{' '}
              <span className="text-slate-500">nahlah.ai</span>
            </p>
            <LegalFooter variant="dark" />
          </div>

          {/* Saudi commercial registration & business-authentication trust block */}
          <div className="mt-6">
            <TrustBlock variant="dark" lang={lang} />
          </div>
        </div>
      </footer>
    </div>
  )
}
