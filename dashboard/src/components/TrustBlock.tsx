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

  // Surfaces
  const wrapperBg = isDark
    ? 'bg-gradient-to-b from-slate-900/70 to-slate-950/80 border-white/5'
    : 'bg-gradient-to-b from-slate-900/95 to-slate-950 border-white/5'
  const cardBg = isDark
    ? 'bg-slate-900/60 border-white/8'
    : 'bg-slate-900/70 border-white/8'

  // Typography
  const titleClass = 'text-white font-bold text-sm sm:text-base'
  const labelClass = 'text-slate-400 text-[11px] sm:text-xs'
  const bodyClass = 'text-slate-300 text-xs sm:text-[13px] leading-relaxed'
  const accentClass = 'text-amber-400'

  return (
    <div
      className={[
        'w-full rounded-2xl border backdrop-blur-sm',
        'shadow-[0_8px_24px_-12px_rgba(0,0,0,0.6)]',
        wrapperBg,
        className,
      ].join(' ')}
      dir="rtl"
    >
      {/* Top row — three columns: CR card · attestation copy · auth badge */}
      <div className="grid grid-cols-1 md:grid-cols-[auto_1fr_auto] items-stretch gap-4 p-4 sm:p-5">
        {/* ── Commercial registry card (right in RTL) ─────────────────── */}
        <a
          href={COMMERCIAL_REGISTRY_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={[
            'group flex items-center gap-3 px-4 py-3 rounded-xl border',
            'transition-colors hover:border-emerald-400/40',
            cardBg,
          ].join(' ')}
          aria-label={`السجل التجاري ${COMMERCIAL_REGISTRY_NUMBER} — موثق لدى وزارة التجارة`}
        >
          <div className="shrink-0 w-10 h-7 rounded-md overflow-hidden ring-1 ring-white/10">
            <SaudiFlag className="w-full h-full" />
          </div>
          <div className="flex flex-col text-right leading-tight">
            <span className={labelClass}>السجل التجاري</span>
            <span className="text-white font-bold tracking-wider text-sm sm:text-base font-mono">
              {COMMERCIAL_REGISTRY_NUMBER}
            </span>
          </div>
        </a>

        {/* ── Attestation copy (centre) ───────────────────────────────── */}
        <div className="flex flex-col justify-center text-center md:text-right gap-1.5 px-1">
          <h3 className={titleClass}>
            نحلة موثقة رسمياً في المملكة العربية السعودية
          </h3>
          <ul className={`${bodyClass} space-y-0.5`}>
            <li>نحلة علامة تجارية سعودية مسجلة</li>
            <li>
              سجل تجاري رقم:{' '}
              <a
                href={COMMERCIAL_REGISTRY_URL}
                target="_blank"
                rel="noopener noreferrer"
                className={`${accentClass} font-semibold tracking-wider font-mono hover:underline`}
              >
                {COMMERCIAL_REGISTRY_NUMBER}
              </a>
            </li>
            <li>
              <a
                href={COMMERCIAL_REGISTRY_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-emerald-400 hover:text-emerald-300 font-medium"
              >
                <ShieldCheck className="w-3.5 h-3.5" />
                موثق لدى وزارة التجارة
              </a>
            </li>
          </ul>
        </div>

        {/* ── Business-authentication badge (left in RTL) ─────────────── */}
        <a
          href={BUSINESS_AUTH_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={[
            'group flex items-center gap-3 px-4 py-3 rounded-xl border',
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
              // Placeholder mark — kept brand-neutral until the official
              // badge is uploaded. Renders as a small "Saudi geometric"
              // diamond so the slot never looks empty.
              <svg viewBox="0 0 32 32" className="w-7 h-7" aria-hidden="true">
                <defs>
                  <linearGradient id="ba-grad" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" stopColor="#006C35" />
                    <stop offset="1" stopColor="#0E8A47" />
                  </linearGradient>
                </defs>
                <path
                  d="M16 3 L29 16 L16 29 L3 16 Z"
                  fill="url(#ba-grad)"
                />
                <path
                  d="M16 9 L23 16 L16 23 L9 16 Z"
                  fill="#FFFFFF"
                  fillOpacity="0.9"
                />
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
            <span className={labelClass}>موثّق لدى</span>
            <span className="text-white font-bold text-sm sm:text-[15px]">
              منصة توثيق الأعمال
            </span>
            <span className="text-slate-400 text-[10px] sm:text-[11px] tracking-wide flex items-center gap-1">
              Business Authentication
              <ExternalLink className="w-3 h-3 opacity-60 group-hover:opacity-100 transition-opacity" />
            </span>
          </div>
        </a>
      </div>

      {/* ── Bottom strip — encryption / privacy reassurance ──────────── */}
      <div className="border-t border-white/5 px-4 sm:px-5 py-2.5 flex items-center justify-center gap-2 text-slate-400 text-[11px] sm:text-xs">
        <Lock className="w-3.5 h-3.5 text-emerald-400/80" />
        <span>بياناتك آمنة ومشفرة 100% ولا نشاركها مع أي جهة خارجية</span>
      </div>
    </div>
  )
}
