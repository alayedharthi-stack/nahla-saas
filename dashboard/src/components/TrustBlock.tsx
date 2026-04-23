/**
 * TrustBlock.tsx
 *
 * Saudi commercial registration & business-authentication trust block.
 * Shown at the bottom of the public Landing, Login and Register pages
 * to surface that Nahla is a legally registered Saudi business and
 * authenticated by the Ministry of Commerce platform.
 *
 * Design notes:
 *   - All icons come from lucide-react so nothing can render as a
 *     broken image. Once the official Maroof / Business-Centre logo
 *     files land in `dashboard/public/`, swap them in by updating the
 *     `BUSINESS_AUTH_LOGO_SRC` constant — no other change is needed.
 *   - Two variants: dark (Landing) and light (Login / Register). The
 *     visual treatment is identical; only the underlying base surface
 *     darkens slightly so the card stays legible over white forms.
 */

import {
  ShieldCheck,
  Lock,
  ExternalLink,
} from 'lucide-react'

// ─── Verifiable destinations ──────────────────────────────────────────────
const COMMERCIAL_REGISTRY_NUMBER = '7050202485'

// "الاستعلام عن متجر إلكتروني موثق" on the Saudi Business Centre — visitors
// who tap the CR number land on the official lookup so trust is verifiable,
// not just claimed.
const COMMERCIAL_REGISTRY_URL =
  'https://business.sa/eservices/details/3fd371e5-11de-4078-08cf-08dbf015747a'

// "خدمة توثيق التجارة الإلكترونية" on the same Saudi Business Centre. The
// Business-Authentication card on the left links here.
const BUSINESS_AUTH_URL =
  'https://business.sa/ar/eservices/details/4d6e9d30-e989-4940-08ce-08dbf015747a'

// Official logos — both PNGs are served from /public so they are part
// of the build bundle and can never result in a broken-image error.
// Ministry of Commerce logo (on white background).
const MOC_LOGO_SRC = '/logo-moc.png'
// Saudi Business Centre logo (shown in the business-authentication card).
const BUSINESS_AUTH_LOGO_SRC = '/logo-sbc.png'

// ─── Visual tokens ────────────────────────────────────────────────────────
const WRAPPER_BG_GRADIENT =
  'linear-gradient(135deg, rgba(255,255,255,0.045), rgba(16,185,129,0.035))'
const WRAPPER_BORDER = '1px solid rgba(255,255,255,0.14)'
const WRAPPER_SHADOW =
  '0 12px 40px rgba(0,0,0,0.25), inset 0 0 0 1px rgba(255,255,255,0.04)'

const CARD_SURFACE_STYLE: React.CSSProperties = {
  background: 'rgba(255,255,255,0.045)',
  border: '1px solid rgba(255,255,255,0.10)',
  backdropFilter: 'blur(8px)',
  WebkitBackdropFilter: 'blur(8px)',
  borderRadius: 14,
}

const CARD_HOVER_SHADOW = '0 20px 60px rgba(0,0,0,0.35)'

interface TrustBlockProps {
  /** "dark" for landing-style backgrounds, "light" for white panels (login/register). */
  variant?: 'dark' | 'light'
  className?: string
}

// ─── Component ────────────────────────────────────────────────────────────
export default function TrustBlock({
  variant = 'light',
  className = '',
}: TrustBlockProps) {
  // Slightly stronger underlying surface for the light-page variant so
  // the block stays legible against a white form panel beneath.
  const wrapperBaseBg =
    variant === 'dark' ? 'rgba(2,6,23,0.55)' : 'rgba(2,6,23,0.85)'

  return (
    <div
      className={['w-full overflow-hidden transition-all duration-300', className].join(' ')}
      style={{
        backgroundColor: wrapperBaseBg,
        backgroundImage: WRAPPER_BG_GRADIENT,
        border: WRAPPER_BORDER,
        borderRadius: 18,
        boxShadow: WRAPPER_SHADOW,
      }}
      dir="rtl"
    >
      {/* Top row — three columns:
            right  → commercial registry card
            centre → attestation copy (TRULY centred at every breakpoint)
            left   → business-authentication card */}
      <div className="grid grid-cols-1 md:grid-cols-[minmax(190px,auto)_1fr_minmax(230px,auto)] items-stretch gap-4 p-5 sm:p-6">

        {/* ── Right: Commercial registry card ─────────────────────────── */}
        <a
          href={COMMERCIAL_REGISTRY_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.06] hover:border-white/[0.18]"
          style={CARD_SURFACE_STYLE}
          onMouseEnter={e => { e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW }}
          onMouseLeave={e => { e.currentTarget.style.boxShadow = '' }}
          aria-label={`السجل التجاري ${COMMERCIAL_REGISTRY_NUMBER} — موثق لدى وزارة التجارة`}
        >
          {/* MoC emblem — green Saudi palm-and-swords crest, icon-only PNG */}
          <div className="shrink-0 w-14 h-14 rounded-xl bg-white flex items-center justify-center ring-1 ring-white/15 overflow-hidden shadow-sm p-1">
            <img
              src={MOC_LOGO_SRC}
              alt="شعار وزارة التجارة"
              loading="lazy"
              decoding="async"
              className="w-full h-full object-contain"
              onError={e => { e.currentTarget.style.display = 'none' }}
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
              href="https://mc.gov.sa/"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] sm:text-[13px] font-semibold hover:opacity-90 transition-opacity"
              style={{ color: '#10B981' }}
            >
              <ShieldCheck className="w-4 h-4 shrink-0" />
              موثق لدى وزارة التجارة
            </a>
          </div>
        </div>

        {/* ── Left: Business-authentication card ─────────────────────── */}
        <a
          href={BUSINESS_AUTH_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.06] hover:border-white/[0.18]"
          style={CARD_SURFACE_STYLE}
          onMouseEnter={e => { e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW }}
          onMouseLeave={e => { e.currentTarget.style.boxShadow = '' }}
          aria-label="الانتقال إلى منصة توثيق الأعمال للتحقق من نحلة"
          title="اضغط للتحقق من توثيق نحلة لدى منصة توثيق الأعمال"
        >
          {/* SBC icon-only logo — purple starburst mark, no wordmark text */}
          <div className="shrink-0 w-14 h-14 rounded-xl bg-white flex items-center justify-center ring-1 ring-white/15 overflow-hidden shadow-sm p-1.5">
            <img
              src={BUSINESS_AUTH_LOGO_SRC}
              alt="شعار المركز السعودي للأعمال"
              loading="lazy"
              decoding="async"
              className="w-full h-full object-contain"
              onError={e => { e.currentTarget.style.display = 'none' }}
            />
          </div>
          <div className="flex flex-col text-right leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              موثّق لدى
            </span>
            <span className="text-white font-bold text-[14px] sm:text-[15px] mt-0.5">
              المركز السعودي للأعمال
            </span>
            <span className="text-slate-400 text-[10px] sm:text-[11px] tracking-wide flex items-center gap-1 mt-0.5">
              Saudi Business Centre
              <ExternalLink className="w-3 h-3 opacity-60 group-hover:opacity-100 transition-opacity" />
            </span>
          </div>
        </a>
      </div>

      {/* ── Bottom strip — encryption / privacy reassurance ─────────── */}
      <div className="border-t border-white/10 bg-slate-950/40 px-4 sm:px-6 py-3 flex items-center justify-center gap-2 text-slate-100 text-[12px] sm:text-[13px] font-medium text-center">
        <Lock className="w-4 h-4 shrink-0" style={{ color: '#10B981' }} />
        <span>بياناتك آمنة ومشفرة 100% ولا نشاركها مع أي جهة خارجية</span>
      </div>
    </div>
  )
}
