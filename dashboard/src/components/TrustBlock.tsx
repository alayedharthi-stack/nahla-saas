/**
 * TrustBlock.tsx
 *
 * Saudi National Unified Number & business-lookup trust block.
 * Shown at the bottom of the public Landing, Login and Register pages
 * to surface official business lookup links for Nahlah Ai Establishment.
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
import type { Lang } from '../i18n/types'
import { COMPANY_INFO } from '../config/companyInfo'

const TRUST_COPY: Record<Lang, {
  dir: 'rtl' | 'ltr'
  nationalNumberAria: (n: string) => string
  mocAlt: string
  nationalNumberLabel: string
  title: string
  subtitle: string
  numberPrefix: string
  mocLookup: string
  authAria: string
  authTitle: string
  lookupBy: string
  sbcName: string
  privacy: string
}> = {
  ar: {
    dir: 'rtl',
    nationalNumberAria: (n) => `الرقم الوطني الموحد ${n} — الاستعلام عبر المركز السعودي للأعمال`,
    mocAlt: 'شعار وزارة التجارة',
    nationalNumberLabel: 'الرقم الوطني الموحد',
    title: 'نحلة AI — مؤسسة نحلة أي آي',
    subtitle: 'منصة تقنية سعودية — يمكنك الاطلاع على بيانات المنشأة عبر الروابط الرسمية للاستعلام',
    numberPrefix: 'الرقم الوطني الموحد:',
    mocLookup: 'الموقع الرسمي لوزارة التجارة',
    authAria: 'الانتقال إلى منصة المركز السعودي للأعمال للاطلاع على خدمات الاستعلام',
    authTitle: 'الاطلاع على خدمات الاستعلام في المركز السعودي للأعمال',
    lookupBy: 'الاستعلام عبر',
    sbcName: 'المركز السعودي للأعمال',
    privacy: 'تُعالج البيانات وفق سياسة الخصوصية المنشورة',
  },
  en: {
    dir: 'ltr',
    nationalNumberAria: (n) => `National Unified Number ${n} — official business lookup via Saudi Business Centre`,
    mocAlt: 'Ministry of Commerce logo',
    nationalNumberLabel: 'National Unified Number',
    title: 'Nahlah AI — Nahlah Ai Establishment',
    subtitle: 'Saudi technology platform — view business details via official lookup links',
    numberPrefix: 'National Unified Number:',
    mocLookup: 'Ministry of Commerce official website',
    authAria: 'Open the Saudi Business Centre for official business lookup services',
    authTitle: 'View business lookup services on the Saudi Business Centre',
    lookupBy: 'Business lookup via',
    sbcName: 'Saudi Business Centre',
    privacy: 'Data is handled according to the published Privacy Policy.',
  },
}

// ─── Verifiable destinations ──────────────────────────────────────────────
const NATIONAL_UNIFIED_NUMBER = COMPANY_INFO.nationalUnifiedNumber

// "الاستعلام عن متجر إلكتروني موثق" on the Saudi Business Centre — visitors
// who tap the national unified number land on the official lookup so trust
// is verifiable, not just claimed.
const BUSINESS_LOOKUP_URL =
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
  /**
   * compact=true → always single-column (for Login / Register where the
   * block sits below a narrow form and should match its width).
   * compact=false (default) → responsive 3-column on desktop (Landing page).
   */
  compact?: boolean
  className?: string
  /** UI language — defaults to Arabic for Login/Register compatibility */
  lang?: Lang
}

// ─── Component ────────────────────────────────────────────────────────────
export default function TrustBlock({
  variant = 'light',
  compact = false,
  className = '',
  lang = 'ar',
}: TrustBlockProps) {
  const t = TRUST_COPY[lang]
  const wrapperBaseBg =
    variant === 'dark' ? 'rgba(2,6,23,0.55)' : 'rgba(2,6,23,0.85)'

  // In compact mode the grid is always a single column — no responsive
  // breakpoint — so the block mirrors the mobile/iPhone look on every
  // screen size when placed below a narrow form.
  const gridCols = compact
    ? 'grid-cols-1'
    : 'grid-cols-1 md:grid-cols-[minmax(190px,auto)_1fr_minmax(230px,auto)]'

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
      dir={t.dir}
    >
      <div className={`grid ${gridCols} items-stretch gap-4 p-5 sm:p-6`}>

        {/* National unified number lookup card */}
        <a
          href={BUSINESS_LOOKUP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.06] hover:border-white/[0.18]"
          style={CARD_SURFACE_STYLE}
          onMouseEnter={e => { e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW }}
          onMouseLeave={e => { e.currentTarget.style.boxShadow = '' }}
          aria-label={t.nationalNumberAria(NATIONAL_UNIFIED_NUMBER)}
        >
          <img
            src={MOC_LOGO_SRC}
            alt={t.mocAlt}
            loading="lazy"
            decoding="async"
            className="shrink-0 w-16 h-16 rounded-xl object-cover shadow-sm"
            onError={e => { e.currentTarget.style.display = 'none' }}
          />
          <div className="flex flex-col text-start leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              {t.nationalNumberLabel}
            </span>
            <span
              className="font-bold tracking-wider text-base sm:text-[17px] font-mono mt-0.5"
              style={{ color: '#F59E0B' }}
            >
              {NATIONAL_UNIFIED_NUMBER}
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
              {t.title}
            </h3>
            <p className="text-slate-200 text-[13px] sm:text-sm leading-relaxed">
              {t.subtitle}
            </p>
            <p className="text-slate-300 text-[12px] sm:text-[13px] leading-relaxed">
              {t.numberPrefix}{' '}
              <a
                href={BUSINESS_LOOKUP_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="font-bold tracking-wider font-mono hover:underline transition-colors"
                style={{ color: '#F59E0B' }}
              >
                {NATIONAL_UNIFIED_NUMBER}
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
              {t.mocLookup}
            </a>
          </div>
        </div>

        {/* Business-authentication card */}
        <a
          href={BUSINESS_AUTH_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex items-center gap-3 px-4 py-3.5 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.06] hover:border-white/[0.18]"
          style={CARD_SURFACE_STYLE}
          onMouseEnter={e => { e.currentTarget.style.boxShadow = CARD_HOVER_SHADOW }}
          onMouseLeave={e => { e.currentTarget.style.boxShadow = '' }}
          aria-label={t.authAria}
          title={t.authTitle}
        >
          {/* SBC icon-only logo — purple starburst mark, no wordmark text */}
          <div
            className="shrink-0 w-14 h-14 rounded-xl bg-white ring-1 ring-white/15 overflow-hidden shadow-sm flex items-center justify-center"
            style={{ padding: '3px' }}
          >
            <img
              src={BUSINESS_AUTH_LOGO_SRC}
              alt={t.sbcName}
              loading="lazy"
              decoding="async"
              style={{ width: '100%', height: '100%', objectFit: 'contain', transform: 'scale(1.25)' }}
              onError={e => { e.currentTarget.style.display = 'none' }}
            />
          </div>
          <div className="flex flex-col text-start leading-tight">
            <span className="text-slate-300 text-[11px] sm:text-xs font-medium">
              {t.lookupBy}
            </span>
            <span className="text-white font-bold text-[14px] sm:text-[15px] mt-0.5">
              {t.sbcName}
            </span>
            <span className="text-slate-400 text-[10px] sm:text-[11px] tracking-wide flex items-center gap-1 mt-0.5">
              Saudi Business Centre
              <ExternalLink className="w-3 h-3 opacity-60 group-hover:opacity-100 transition-opacity" />
            </span>
          </div>
        </a>
      </div>

      {/* ── Bottom strip — published privacy policy ─────────────────── */}
      <div className="border-t border-white/10 bg-slate-950/40 px-4 sm:px-6 py-3 flex items-center justify-center gap-2 text-slate-100 text-[12px] sm:text-[13px] font-medium text-center">
        <Lock className="w-4 h-4 shrink-0" style={{ color: '#10B981' }} />
        <a href="/privacy" className="hover:underline underline-offset-2">
          {t.privacy}
        </a>
      </div>
    </div>
  )
}
