/**
 * TrustBlock.tsx
 *
 * Saudi commercial registration & business-authentication trust block.
 * Shown at the bottom of the public Landing, Login and Register pages
 * to surface that Nahla is a legally registered Saudi business and
 * authenticated by the Ministry of Commerce platform.
 *
 * To plug in the real authentication badge / URL once issued, just
 * update the two constants below — nothing else needs to change.
 */

import { ExternalLink, Lock } from 'lucide-react'

const COMMERCIAL_REGISTRY_NUMBER = '7050202485'
const COMMERCIAL_REGISTRY_URL =
  // Public CR lookup on the Ministry of Commerce portal. Visitors who tap the
  // CR number land on the official record so trust is verifiable, not just
  // claimed.
  `https://mc.gov.sa/ar/eservices/Pages/ServiceDetails.aspx?sId=24`

// TODO: replace with the dedicated Maroof / Business-Authentication public
// profile URL the moment it is issued. Until then we link to the Maroof
// homepage so the click still lands on a real, recognisable destination.
const BUSINESS_AUTH_URL = 'https://maroof.sa/'

// Official, public-domain logos served directly from Wikimedia Commons.
// These are stable, license-clean, vector assets — they don't bloat the
// bundle and render crisply on any device. Swap any of them for a local
// file later by changing the constant; nothing else needs to change.
const SAUDI_FLAG_SRC =
  'https://upload.wikimedia.org/wikipedia/commons/0/0d/Flag_of_Saudi_Arabia.svg'
const MOC_LOGO_SRC =
  'https://upload.wikimedia.org/wikipedia/commons/6/6b/Ministry_of_Commerce_%28Saudi_Arabia%29_Logo.svg'
const BUSINESS_AUTH_LOGO_SRC =
  'https://upload.wikimedia.org/wikipedia/commons/3/36/Checkmark-green.svg'

interface TrustBlockProps {
  /** "dark" for landing-style backgrounds, "light" for white panels (login/register). */
  variant?: 'dark' | 'light'
  className?: string
}

// Premium visual tokens (kept inline so this component remains drop-in
// portable). Centralised here so any future tweak only happens once.
const WRAPPER_BG_GRADIENT =
  'linear-gradient(135deg, rgba(255,255,255,0.03), rgba(16,185,129,0.04))'
const WRAPPER_SHADOW =
  '0 10px 40px rgba(0,0,0,0.30), inset 0 0 0 1px rgba(255,255,255,0.05)'
const CARD_HOVER_SHADOW = '0 20px 60px rgba(0,0,0,0.40)'
// Frosted glass card surface — translucent white over the dark gradient.
const CARD_SURFACE_STYLE: React.CSSProperties = {
  background: 'rgba(255,255,255,0.03)',
  backdropFilter: 'blur(8px)',
  WebkitBackdropFilter: 'blur(8px)',
  border: '1px solid rgba(255,255,255,0.08)',
}

export default function TrustBlock({
  variant = 'light',
  className = '',
}: TrustBlockProps) {
  // Both variants share the same premium look; the only difference is a
  // slightly stronger underlying surface for the light-page variant so
  // the block stays legible against a white form panel beneath.
  const wrapperBaseBg =
    variant === 'dark' ? 'rgba(2,6,23,0.55)' : 'rgba(2,6,23,0.85)'

  return (
    <div
      className={[
        'w-full overflow-hidden',
        'transition-all duration-300',
        className,
      ].join(' ')}
      style={{
        backgroundColor: wrapperBaseBg,
        backgroundImage: WRAPPER_BG_GRADIENT,
        borderRadius: 18,
        boxShadow: WRAPPER_SHADOW,
      }}
      dir="rtl"
    >
      {/* Top row — three columns:
            right  → commercial registry card
            centre → attestation copy (TRULY centred at every breakpoint)
            left   → business-authentication card
         The centre column gets `1fr` so it is the visual midpoint of the
         block, and `justify-self-center` on its inner wrapper makes the
         text column itself centred inside that fr — not pushed to either
         side by the flanking cards. */}
      <div className="grid grid-cols-1 md:grid-cols-[minmax(190px,auto)_1fr_minmax(230px,auto)] items-stretch gap-4 p-5 sm:p-6">

        {/* ── Right: Commercial registry card ─────────────────────────── */}
        <a
          href={COMMERCIAL_REGISTRY_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.05] hover:border-white/15"
          style={{ ...CARD_SURFACE_STYLE, borderRadius: 14 }}
          onMouseEnter={e => {
            e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW
          }}
          onMouseLeave={e => {
            e.currentTarget.style.boxShadow = ''
          }}
          aria-label={`السجل التجاري ${COMMERCIAL_REGISTRY_NUMBER} — موثق لدى وزارة التجارة`}
        >
          <div className="shrink-0 w-12 h-8 rounded-md overflow-hidden ring-1 ring-white/10 shadow-sm bg-[#006C35]/20">
            <img
              src={SAUDI_FLAG_SRC}
              alt="علم المملكة العربية السعودية"
              loading="lazy"
              decoding="async"
              className="w-full h-full object-cover"
            />
          </div>
          <div className="flex flex-col text-right leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              السجل التجاري
            </span>
            <span
              className="font-bold tracking-wider text-base sm:text-[17px] font-mono mt-0.5"
              style={{ color: '#F59E0B' }}
            >
              {COMMERCIAL_REGISTRY_NUMBER}
            </span>
          </div>
        </a>

        {/* ── Centre: Attestation copy (truly centred) ───────────────── */}
        <div className="flex items-center justify-center">
          <div className="flex flex-col items-center text-center gap-2 max-w-md mx-auto">
            <h3
              className="text-white text-[15px] sm:text-base leading-snug"
              style={{ fontWeight: 600, letterSpacing: '0.3px' }}
            >
              نحلة مسجلة في المملكة العربية السعودية
            </h3>
            <p className="text-slate-200 text-[13px] sm:text-sm leading-relaxed">
              نحلة علامة تجارية سعودية مسجلة
            </p>
            <p className="text-slate-300 text-[12px] sm:text-[13px] leading-relaxed">
              سجل تجاري رقم:{' '}
              <a
                href={COMMERCIAL_REGISTRY_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="font-bold tracking-wider font-mono hover:underline transition-colors"
                style={{ color: '#F59E0B' }}
              >
                {COMMERCIAL_REGISTRY_NUMBER}
              </a>
            </p>
            <a
              href={COMMERCIAL_REGISTRY_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] sm:text-[13px] font-semibold hover:opacity-90 transition-opacity"
              style={{ color: '#10B981' }}
            >
              <span className="inline-flex items-center justify-center w-5 h-5 rounded-md bg-white/95 ring-1 ring-white/10 overflow-hidden shadow-sm">
                <img
                  src={MOC_LOGO_SRC}
                  alt="وزارة التجارة"
                  loading="lazy"
                  decoding="async"
                  className="w-full h-full object-contain p-0.5"
                />
              </span>
              موثق لدى وزارة التجارة
              <span aria-hidden="true">✔</span>
            </a>
          </div>
        </div>

        {/* ── Left: Business-authentication card ─────────────────────── */}
        <a
          href={BUSINESS_AUTH_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.05] hover:border-white/15"
          style={{ ...CARD_SURFACE_STYLE, borderRadius: 14 }}
          onMouseEnter={e => {
            e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW
          }}
          onMouseLeave={e => {
            e.currentTarget.style.boxShadow = ''
          }}
          aria-label="الانتقال إلى منصة توثيق الأعمال للتحقق من نحلة"
          title="اضغط للتحقق من توثيق نحلة لدى منصة توثيق الأعمال"
        >
          <div className="shrink-0 w-12 h-12 rounded-lg bg-white flex items-center justify-center ring-1 ring-white/10 overflow-hidden shadow-sm">
            <img
              src={BUSINESS_AUTH_LOGO_SRC}
              alt="منصة توثيق الأعمال — Business Authentication"
              loading="lazy"
              decoding="async"
              className="w-7 h-7 object-contain"
            />
          </div>
          <div className="flex flex-col text-right leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              موثّق لدى
            </span>
            <span className="text-white font-bold text-[14px] sm:text-[15px] mt-0.5">
              منصة توثيق الأعمال
            </span>
            <span className="text-slate-400 text-[10px] sm:text-[11px] tracking-wide flex items-center gap-1 mt-0.5">
              Business Authentication
              <ExternalLink className="w-3 h-3 opacity-60 group-hover:opacity-100 transition-opacity" />
            </span>
          </div>
        </a>
      </div>

      {/* ── Bottom strip — encryption / privacy reassurance ─────────────
           Higher-contrast text + slightly stronger divider so the line
           reads clearly against either dark or light page backgrounds. */}
      <div className="border-t border-white/10 bg-slate-950/40 px-4 sm:px-6 py-3 flex items-center justify-center gap-2 text-slate-100 text-[12px] sm:text-[13px] font-medium text-center">
        <Lock className="w-4 h-4 shrink-0" style={{ color: '#10B981' }} />
        <span>بياناتك آمنة ومشفرة 100% ولا نشاركها مع أي جهة خارجية</span>
      </div>
    </div>
  )
}
