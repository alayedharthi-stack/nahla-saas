/**
 * FeatureGate — Show + Lock + Explain + Upgrade
 * ─────────────────────────────────────────────
 * Wraps any feature/section. If the tenant's plan doesn't include the feature:
 *  - Renders children with a lock overlay (not hidden)
 *  - Shows plan badge + "ترقية الباقة" button
 *  - Opens UpgradeModal on click
 *
 * Usage:
 *   <FeatureGate feature="meta_catalog_sync">
 *     <MetaCatalogPanel />
 *   </FeatureGate>
 *
 *   <FeatureGate feature="advanced_ai_analytics" inline>
 *     <AnalyticsButton />
 *   </FeatureGate>
 */
import { useState, type ReactNode } from 'react'
import {
  useEntitlements,
  FEATURE_LABELS_AR,
  FEATURE_REQUIRED_PLAN,
  PLAN_LABELS_AR,
  type PlanFeatures,
  type PlanSlug,
} from '../hooks/useEntitlements'
import { UpgradeModal } from './UpgradeModal'

// ── Status types for explicit override ───────────────────────────────────────

export type FeatureStatus =
  | 'available'        // plan has access
  | 'locked'           // plan doesn't have access
  | 'usage_limited'    // approaching / at limit
  | 'billing_blocked'  // billing failed / cancelled

// ── Props ─────────────────────────────────────────────────────────────────────

interface FeatureGateProps {
  /** Feature key matching PlanFeatures and FEATURE_REQUIRED_PLAN */
  feature: keyof PlanFeatures
  /** Optional: short benefit description shown in the locked overlay */
  description?: string
  /** If true, renders inline (no full-width overlay card) */
  inline?: boolean
  /** Children are always rendered — locked state adds overlay */
  children: ReactNode
  /** Optional callback when user clicks upgrade */
  onUpgradeClick?: (requiredPlan: PlanSlug) => void
}

export function FeatureGate({
  feature,
  description,
  inline = false,
  children,
  onUpgradeClick,
}: FeatureGateProps) {
  const { hasFeature, requiredPlan, ent } = useEntitlements()
  const [modalOpen, setModalOpen] = useState(false)

  const isAvailable   = hasFeature(feature)
  const isBlocked     = ent.is_blocked
  const required      = requiredPlan(feature)
  const featureLabel  = FEATURE_LABELS_AR[feature] ?? String(feature)
  const requiredLabel = PLAN_LABELS_AR[required] ?? required

  const handleUpgrade = () => {
    onUpgradeClick?.(required)
    setModalOpen(true)
  }

  // ── Available: render children as-is ──────────────────────────────────────
  if (isAvailable && !isBlocked) {
    return <>{children}</>
  }

  // ── Billing blocked banner ─────────────────────────────────────────────────
  if (isBlocked) {
    return (
      <>
        <div className="relative">
          <div className="pointer-events-none select-none opacity-40 blur-[1px]">
            {children}
          </div>
          <BillingBlockedOverlay inline={inline} />
        </div>
      </>
    )
  }

  // ── Plan locked overlay ────────────────────────────────────────────────────
  return (
    <>
      <div className="relative" dir="rtl">
        {/* Children rendered but visually locked */}
        <div className="pointer-events-none select-none opacity-30 blur-[1.5px]">
          {children}
        </div>

        {/* Lock overlay */}
        {inline ? (
          <InlineLock
            featureLabel={featureLabel}
            requiredLabel={requiredLabel}
            required={required}
            onUpgrade={handleUpgrade}
          />
        ) : (
          <CardLock
            featureLabel={featureLabel}
            description={description}
            requiredLabel={requiredLabel}
            required={required}
            onUpgrade={handleUpgrade}
          />
        )}
      </div>

      <UpgradeModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        feature={feature}
        requiredPlan={required}
      />
    </>
  )
}

// ── Lock overlays ─────────────────────────────────────────────────────────────

function InlineLock({
  featureLabel,
  requiredLabel,
  required,
  onUpgrade,
}: {
  featureLabel:  string
  requiredLabel: string
  required:      PlanSlug
  onUpgrade:     () => void
}) {
  const planColor = required === 'scale' ? '#a78bfa' : '#f59e0b'

  return (
    <div
      className="absolute inset-0 flex items-center justify-center rounded-lg gap-2 px-3"
      style={{ background: 'rgba(15,23,42,0.75)', backdropFilter: 'blur(4px)' }}
    >
      <span style={{ fontSize: 14 }}>🔒</span>
      <span className="text-xs font-semibold text-slate-300 truncate">{featureLabel}</span>
      <span
        className="text-[10px] font-black px-2 py-0.5 rounded-full shrink-0"
        style={{ background: `${planColor}22`, color: planColor }}
      >
        {requiredLabel}
      </span>
      <button
        type="button"
        onClick={onUpgrade}
        className="text-[10px] font-black px-2 py-0.5 rounded-full shrink-0"
        style={{ background: planColor, color: '#0f172a' }}
      >
        ترقية
      </button>
    </div>
  )
}

function CardLock({
  featureLabel,
  description,
  requiredLabel,
  required,
  onUpgrade,
}: {
  featureLabel:  string
  description?:  string
  requiredLabel: string
  required:      PlanSlug
  onUpgrade:     () => void
}) {
  const planColor    = required === 'scale' ? '#a78bfa' : '#f59e0b'
  const planColorBg  = required === 'scale' ? 'rgba(167,139,250,0.08)' : 'rgba(245,158,11,0.08)'
  const planColorBorder = required === 'scale' ? 'rgba(167,139,250,0.2)' : 'rgba(245,158,11,0.2)'

  return (
    <div
      className="absolute inset-0 flex flex-col items-center justify-center rounded-xl p-4 gap-2"
      style={{
        background:    `rgba(15,23,42,0.82)`,
        backdropFilter: 'blur(6px)',
        border:        `1px solid ${planColorBorder}`,
      }}
    >
      <div
        className="w-10 h-10 rounded-xl flex items-center justify-center text-xl"
        style={{ background: planColorBg }}
      >
        🔒
      </div>

      <div className="text-center">
        <p className="text-sm font-bold text-white">{featureLabel}</p>
        {description && (
          <p className="text-xs text-slate-400 mt-0.5 leading-relaxed">{description}</p>
        )}
      </div>

      <div
        className="text-[11px] font-bold px-3 py-1 rounded-full"
        style={{ background: planColorBg, color: planColor, border: `1px solid ${planColorBorder}` }}
      >
        متاحة في باقة {requiredLabel}
      </div>

      <button
        type="button"
        onClick={onUpgrade}
        className="mt-1 px-5 py-2 rounded-xl text-xs font-black"
        style={{ background: planColor, color: '#0f172a' }}
      >
        ترقية الباقة →
      </button>
    </div>
  )
}

function BillingBlockedOverlay({ inline }: { inline: boolean }) {
  return (
    <div
      className={`absolute inset-0 flex ${inline ? 'items-center' : 'flex-col items-center justify-center'} gap-2 rounded-xl p-3`}
      style={{
        background:    'rgba(239,68,68,0.08)',
        backdropFilter: 'blur(4px)',
        border:        '1px solid rgba(239,68,68,0.2)',
      }}
    >
      <span className="text-lg">⚠️</span>
      <p className="text-xs font-semibold text-red-300 text-center">
        الاشتراك معلّق — يرجى تحديث بيانات الدفع
      </p>
    </div>
  )
}

// ── Convenience component: just the locked badge ─────────────────────────────

/**
 * LockedBadge — renders inline badge only, no children wrapping.
 * Use when you want to show a feature label + lock badge in a list.
 */
export function LockedBadge({
  feature,
  onUpgrade,
}: {
  feature: keyof PlanFeatures
  onUpgrade?: () => void
}) {
  const { hasFeature, requiredPlan, ent } = useEntitlements()
  const [modalOpen, setModalOpen] = useState(false)

  if (hasFeature(feature) && !ent.is_blocked) return null

  const required     = requiredPlan(feature)
  const planColor    = required === 'scale' ? '#a78bfa' : '#f59e0b'
  const requiredLabel = PLAN_LABELS_AR[required] ?? required

  return (
    <>
      <button
        type="button"
        dir="rtl"
        onClick={() => { onUpgrade?.(); setModalOpen(true) }}
        className="inline-flex items-center gap-1 text-[10px] font-black px-2 py-0.5 rounded-full"
        style={{ background: `${planColor}18`, color: planColor, border: `1px solid ${planColor}33` }}
      >
        🔒 {requiredLabel}
      </button>

      <UpgradeModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        feature={feature}
        requiredPlan={required}
      />
    </>
  )
}
