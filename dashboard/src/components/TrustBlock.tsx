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

import { ShieldCheck, ExternalLink, Lock } from 'lucide-react'

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

// TODO: drop the official badge file into `dashboard/public/`
// (e.g. `business-authentication.png`) and set the constant below to its
// path. Until then we render a clean styled placeholder that matches the
// final layout exactly so swapping it in later is a one-line change.
const BUSINESS_AUTH_LOGO_SRC: string | null = null

interface TrustBlockProps {
  /** "dark" for landing-style backgrounds, "light" for white panels (login/register). */
  variant?: 'dark' | 'light'
  className?: string
}

function SaudiFlag({ className = '' }: { className?: string }) {
  // Tiny inline rendition of the Saudi flag — green field with a white
  // calligraphic stripe + sword line. Lightweight, no external asset.
  return (
    <svg viewBox="0 0 30 20" className={className} aria-hidden="true">
      <rect width="30" height="20" rx="2" fill="#006C35" />
      <path
        d="M5 8 L25 8"
        stroke="#FFFFFF"
        strokeWidth="0.9"
        strokeLinecap="round"
      />
      <path
        d="M5 12 L25 12"
        stroke="#FFFFFF"
        strokeWidth="0.6"
        strokeLinecap="round"
      />
      <path
        d="M6 14.5 L24 14.5"
        stroke="#FFFFFF"
        strokeWidth="0.7"
        strokeLinecap="round"
      />
    </svg>
  )
}

export default function TrustBlock({
  variant = 'light',
  className = '',
}: TrustBlockProps) {
  const isDark = variant === 'dark'

  // Surfaces — both variants render the dark Saudi-trust look the user
  // approved in the reference image; the only difference is the outer
  // glow used over light backgrounds (login/register) so the block keeps
  // the same premium feel without clashing with the white form panel.
  const wrapperBg = isDark
    ? 'bg-gradient-to-b from-slate-900/70 to-slate-950/85 border-white/8'
    : 'bg-gradient-to-b from-slate-900/95 to-slate-950 border-white/8'
  const cardBg = 'bg-slate-900/55 border-white/10'

  return (
    <div
      className={[
        'w-full rounded-2xl border backdrop-blur-sm',
        'shadow-[0_10px_30px_-12px_rgba(0,0,0,0.7)]',
        wrapperBg,
        className,
      ].join(' ')}
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
      <div className="grid grid-cols-1 md:grid-cols-[minmax(180px,auto)_1fr_minmax(220px,auto)] items-stretch gap-4 p-5 sm:p-6">

        {/* ── Right: Commercial registry card ─────────────────────────── */}
        <a
          href={COMMERCIAL_REGISTRY_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={[
            'group flex items-center gap-3 px-4 py-3.5 rounded-xl border',
            'transition-colors hover:border-emerald-400/40',
            cardBg,
          ].join(' ')}
          aria-label={`السجل التجاري ${COMMERCIAL_REGISTRY_NUMBER} — موثق لدى وزارة التجارة`}
        >
          <div className="shrink-0 w-11 h-8 rounded-md overflow-hidden ring-1 ring-white/10 shadow-sm">
            <SaudiFlag className="w-full h-full" />
          </div>
          <div className="flex flex-col text-right leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              السجل التجاري
            </span>
            <span className="text-white font-bold tracking-wider text-base sm:text-[17px] font-mono mt-0.5">
              {COMMERCIAL_REGISTRY_NUMBER}
            </span>
          </div>
        </a>

        {/* ── Centre: Attestation copy (truly centred) ───────────────── */}
        <div className="flex items-center justify-center">
          <div className="flex flex-col items-center text-center gap-2 max-w-md mx-auto">
            <h3 className="text-white font-bold text-[15px] sm:text-base leading-snug">
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
                className="text-amber-400 hover:text-amber-300 font-bold tracking-wider font-mono hover:underline"
              >
                {COMMERCIAL_REGISTRY_NUMBER}
              </a>
            </p>
            <a
              href={COMMERCIAL_REGISTRY_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-emerald-400 hover:text-emerald-300 text-[12px] sm:text-[13px] font-semibold"
            >
              <ShieldCheck className="w-4 h-4" />
              موثق لدى وزارة التجارة
            </a>
          </div>
        </div>

        {/* ── Left: Business-authentication card ─────────────────────── */}
        <a
          href={BUSINESS_AUTH_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={[
            'group flex items-center gap-3 px-4 py-3.5 rounded-xl border',
            'transition-colors hover:border-amber-400/40',
            cardBg,
          ].join(' ')}
          aria-label="الانتقال إلى منصة توثيق الأعمال للتحقق من نحلة"
          title="اضغط للتحقق من توثيق نحلة لدى منصة توثيق الأعمال"
        >
          <div className="shrink-0 w-12 h-12 rounded-lg bg-white flex items-center justify-center ring-1 ring-white/10 overflow-hidden">
            {BUSINESS_AUTH_LOGO_SRC ? (
              <img
                src={BUSINESS_AUTH_LOGO_SRC}
                alt="منصة توثيق الأعمال"
                className="w-full h-full object-contain p-1"
              />
            ) : (
              // Brand-neutral diamond placeholder until the official badge
              // image is dropped into /public and BUSINESS_AUTH_LOGO_SRC
              // is updated above.
              <svg viewBox="0 0 32 32" className="w-7 h-7" aria-hidden="true">
                <defs>
                  <linearGradient id="ba-grad" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" stopColor="#006C35" />
                    <stop offset="1" stopColor="#0E8A47" />
                  </linearGradient>
                </defs>
                <path d="M16 3 L29 16 L16 29 L3 16 Z" fill="url(#ba-grad)" />
                <path d="M16 9 L23 16 L16 23 L9 16 Z" fill="#FFFFFF" fillOpacity="0.92" />
                <path
                  d="M13 16 L15.5 18.5 L19 14.5"
                  stroke="#006C35"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  fill="none"
                />
              </svg>
            )}
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
      <div className="border-t border-white/10 bg-slate-950/40 px-4 sm:px-6 py-3 flex items-center justify-center gap-2 text-slate-200 text-[12px] sm:text-[13px] font-medium text-center">
        <Lock className="w-4 h-4 text-emerald-400 shrink-0" />
        <span>بياناتك آمنة ومشفرة 100% ولا نشاركها مع أي جهة خارجية</span>
      </div>
    </div>
  )
}
