/**
 * KnowledgeBase.tsx — /knowledge-base
 * ───────────────────────────────────
 * "مركز معرفة المتجر الذكي" — Smart Store Knowledge Hub (Phase 1).
 *
 * The page is structured around six dashboard buckets (Quick Updates,
 * Store Info, Sales Policies, Shipping, Product Extras, Linked Media)
 * with the AI media library reused from `intelligenceLibrariesApi`.
 *
 * Phase 1 covers manual section CRUD + manual media linking + a
 * one-shot migration banner that lifts the legacy free-form
 * `manual_knowledge_base` text into structured rows. The "تنسيق ودمج
 * بالذكاء" button on the Quick Update card is rendered disabled with
 * a "Phase 2" badge — it will be wired up in the follow-up sprint.
 *
 * Source-of-truth precedence (Salla / Zid / Shopify wins on price +
 * inventory + names + links + primary images) is enforced server-side
 * inside `backend/modules/ai/prompts/tenant_overlay.py`. The "أولوية
 * المنصة" banner here is a UX reminder, not a guardrail.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  BookOpen,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Edit2,
  Image as ImageIcon,
  Info,
  Link2,
  Package,
  Search,
  Loader2,
  Paperclip,
  Plus,
  Power,
  PowerOff,
  Save,
  Sparkles,
  Store,
  Trash2,
  Wand2,
  X,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { settingsApi } from '../api/settings'
import {
  knowledgeApi,
  type DraftConflict,
  type KnowledgeDraft,
  type KnowledgeSection,
  type LinkRole,
  type ProductLinkRow,
  type ProductLite,
  type ProposedOp,
  type SectionInput,
  type SectionKindMeta,
  type SectionKindsResponse,
  type LegacyKnowledgeBaseResponse,
  type MediaLinkRow,
} from '../api/knowledge'
import {
  intelligenceLibrariesApi,
  type AIMediaItem,
} from '../api/intelligenceLibraries'
import { useLanguage } from '../i18n/context'

// ───────────────────────────────────────────────────────────────────────────
// Constants + helpers
// ───────────────────────────────────────────────────────────────────────────

const LINK_ROLE_LABELS_AR: Record<LinkRole, string> = {
  primary: 'أساسي',
  evidence: 'إثبات',
  barcode: 'باركود',
  tutorial_video: 'فيديو شرح',
  recipe_video: 'فيديو وصفة',
  policy_pdf: 'PDF سياسة',
  certificate: 'شهادة',
  map: 'خريطة',
}

function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

function mediaIcon(type?: string): JSX.Element {
  // Bare minimum — full media manager lives on the Intelligence page.
  if (type === 'image') return <ImageIcon className="w-3.5 h-3.5" />
  return <Paperclip className="w-3.5 h-3.5" />
}

// ───────────────────────────────────────────────────────────────────────────
// Toast helpers
// ───────────────────────────────────────────────────────────────────────────

function SuccessToast({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  useEffect(() => {
    const t = window.setTimeout(onDismiss, 3500)
    return () => window.clearTimeout(t)
  }, [text, onDismiss])
  return (
    <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 flex items-center gap-2">
      <CheckCircle className="w-4 h-4" />
      <span className="flex-1">{text}</span>
      <button
        onClick={onDismiss}
        className="text-emerald-700/60 hover:text-emerald-900"
        aria-label="إغلاق"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

function ErrorToast({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 flex items-center gap-2">
      <AlertCircle className="w-4 h-4" />
      <span className="flex-1">{text}</span>
      <button onClick={onDismiss} className="text-red-700/60 hover:text-red-900" aria-label="إغلاق">
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Salla precedence banner — copied verbatim from the legacy page so the
// merchant lands on a familiar message; the server-side prompt overlay
// already enforces the rule.
// ───────────────────────────────────────────────────────────────────────────

function PlatformPrecedenceCard({ platformLabel }: { platformLabel: string | null }) {
  const connected = !!platformLabel
  return (
    <div
      className={classNames(
        'card p-4',
        connected ? 'border-amber-200 bg-amber-50/60' : 'border-slate-200 bg-slate-50/60',
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={classNames(
            'w-9 h-9 rounded-xl flex items-center justify-center shrink-0',
            connected ? 'bg-amber-100 text-amber-700' : 'bg-slate-200 text-slate-600',
          )}
        >
          <Store className="w-4 h-4" />
        </div>
        <div className="flex-1 text-xs leading-relaxed">
          <p
            className={classNames(
              'font-semibold text-sm mb-1',
              connected ? 'text-amber-900' : 'text-slate-800',
            )}
          >
            الأولوية لبيانات منصة التجارة في الأسعار والمخزون
          </p>
          <p className={connected ? 'text-amber-800' : 'text-slate-600'}>
            إذا كان متجرك مربوطاً بمنصة تجارية (سلة / زد / شوبيفاي)، تبقى
            أسعار المنتجات والمخزون والمتغيرات والروابط والصور الأساسية من
            المنصة هي <span className="font-bold">المصدر الرسمي</span>،
            وتُستخدم قاعدة المعرفة للسياسات وطريقة الرد والمعلومات الإضافية.
            {connected ? ` (متجرك مربوط بـ ${platformLabel} حالياً ✓)` : ' (متجرك غير مربوط بمنصة — جميع المعلومات هنا هي المصدر.)'}
          </p>
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Legacy migration banner — only renders when the merchant still has a
// non-empty `ai_settings.manual_knowledge_base` blob from the old
// single-textarea UX.
// ───────────────────────────────────────────────────────────────────────────

function LegacyMigrationBanner({
  legacy,
  onImported,
  onDismiss,
}: {
  legacy: LegacyKnowledgeBaseResponse
  onImported: () => void
  onDismiss: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const blocks = legacy.preview || []

  const handleImport = async () => {
    setBusy(true)
    setError(null)
    try {
      await knowledgeApi.migrateFromLegacy({ clearLegacy: true })
      onImported()
    } catch (err) {
      setError((err as Error).message || 'تعذّر الاستيراد')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card border-brand-200 bg-brand-50/60 p-4">
      <div className="flex items-start gap-3">
        <div className="w-9 h-9 rounded-xl bg-brand-100 text-brand-700 flex items-center justify-center shrink-0">
          <Wand2 className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-sm text-brand-900 mb-1">
            وجدنا نصاً قديماً في قاعدة المعرفة ({legacy.char_count.toLocaleString('ar-SA')} حرف)
          </p>
          <p className="text-xs text-slate-700 leading-relaxed mb-2">
            يمكننا تقسيمه تلقائياً إلى أقسام مهيكلة مرتبة. ستحصل على{' '}
            <span className="font-semibold">{blocks.length}</span> قسم/قسماً
            مقترحاً، يمكنك تعديلها بعد الاستيراد. النص القديم يُحفظ
            احتياطياً.
          </p>

          {blocks.length > 0 && (
            <div className="rounded-lg border border-brand-200 bg-white/70 p-2 mb-2 max-h-40 overflow-y-auto">
              <ul className="space-y-1 text-xs text-slate-700">
                {blocks.slice(0, 10).map((b, idx) => (
                  <li key={idx} className="flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-brand-400 shrink-0" />
                    <span className="font-semibold shrink-0">{b.kind}</span>
                    <span className="text-slate-500 truncate">— {b.title || b.body.slice(0, 60)}</span>
                  </li>
                ))}
                {blocks.length > 10 && (
                  <li className="text-slate-400">… و {blocks.length - 10} قسماً إضافياً</li>
                )}
              </ul>
            </div>
          )}

          {error && (
            <p className="text-xs text-red-600 mb-2 inline-flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" /> {error}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleImport}
              disabled={busy}
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-xs font-semibold disabled:opacity-50"
            >
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
              استيراد إلى أقسام
            </button>
            <button
              type="button"
              onClick={onDismiss}
              className="text-xs text-slate-500 hover:text-slate-700 px-2 py-1"
            >
              تجاهل الآن
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Media picker (re-uses AIMediaItem library)
// ───────────────────────────────────────────────────────────────────────────

interface MediaPickerProps {
  open: boolean
  media: AIMediaItem[]
  alreadyLinkedIds: number[]
  onPick: (mediaId: number, role: LinkRole) => Promise<void>
  onClose: () => void
}

function MediaPicker({ open, media, alreadyLinkedIds, onPick, onClose }: MediaPickerProps) {
  const [search, setSearch] = useState('')
  const [role, setRole] = useState<LinkRole>('primary')
  const [busyId, setBusyId] = useState<number | null>(null)

  useEffect(() => {
    if (!open) {
      setSearch('')
      setRole('primary')
      setBusyId(null)
    }
  }, [open])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return media.filter(m => {
      if (!q) return true
      return (
        m.title.toLowerCase().includes(q) ||
        (m.media_key || '').toLowerCase().includes(q) ||
        (m.tags || []).some(t => t.toLowerCase().includes(q))
      )
    })
  }, [media, search])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-40 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-xl max-h-[90vh] flex flex-col shadow-xl">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Link2 className="w-4 h-4 text-brand-500 shrink-0" />
            <h3 className="text-sm font-semibold text-slate-900">اربط وسائط بالقسم</h3>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-3 border-b border-slate-100 bg-slate-50/40">
          <input
            type="search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="ابحث في مكتبة الوسائط (العنوان، الكلمات المفتاحية…)"
            className="input text-sm"
          />
          <div className="flex items-center gap-2 text-xs">
            <span className="text-slate-500">دور الربط:</span>
            <select
              value={role}
              onChange={e => setRole(e.target.value as LinkRole)}
              className="input text-xs py-1 px-2 max-w-[150px]"
            >
              {(Object.keys(LINK_ROLE_LABELS_AR) as LinkRole[]).map(r => (
                <option key={r} value={r}>{LINK_ROLE_LABELS_AR[r]}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-3">
          {filtered.length === 0 ? (
            <div className="text-center text-sm text-slate-400 py-12">
              لا توجد وسائط مطابقة. ارفع وسائط من صفحة "نحلة الذكية" أولاً.
            </div>
          ) : (
            <ul className="space-y-2">
              {filtered.map(m => {
                const linked = alreadyLinkedIds.includes(m.id)
                return (
                  <li
                    key={m.id}
                    className="flex items-center gap-3 p-2.5 rounded-lg border border-slate-200 hover:border-brand-300 bg-white"
                  >
                    <div className="w-10 h-10 rounded-md bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
                      {m.thumbnail_url || (m.media_type === 'image' && m.file_url) ? (
                        <img
                          src={m.thumbnail_url || m.file_url}
                          alt={m.title}
                          className="w-full h-full object-cover"
                          onError={e => {
                            ;(e.currentTarget as HTMLImageElement).style.display = 'none'
                          }}
                        />
                      ) : (
                        mediaIcon(m.media_type)
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-slate-900 truncate">{m.title}</p>
                      <p className="text-xs text-slate-500 truncate">
                        {m.media_type}
                        {m.media_key ? ` • ${m.media_key}` : ''}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={busyId === m.id}
                      onClick={async () => {
                        setBusyId(m.id)
                        try {
                          await onPick(m.id, role)
                        } finally {
                          setBusyId(null)
                        }
                      }}
                      className={classNames(
                        'shrink-0 inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-semibold',
                        linked
                          ? 'bg-emerald-100 text-emerald-700'
                          : 'bg-brand-500 hover:bg-brand-600 text-white',
                      )}
                    >
                      {busyId === m.id ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : linked ? (
                        <>
                          <CheckCircle className="w-3 h-3" /> مربوط
                        </>
                      ) : (
                        <>
                          <Plus className="w-3 h-3" /> ربط
                        </>
                      )}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="px-4 py-2.5 border-t border-slate-100 bg-slate-50/40 text-xs text-slate-500 flex items-center justify-between">
          <span>
            <Info className="w-3 h-3 inline-block ms-1" />
            يمكن ربط نفس الوسيط بأكثر من قسم/دور.
          </span>
          <button onClick={onClose} className="text-slate-700 hover:text-slate-900 font-medium">
            تم
          </button>
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Phase 3 — Product picker
// ───────────────────────────────────────────────────────────────────────────

interface ProductPickerProps {
  open: boolean
  alreadyLinkedIds: number[]
  onPick: (productId: number) => Promise<void>
  onClose: () => void
}

function ProductPicker({ open, alreadyLinkedIds, onPick, onClose }: ProductPickerProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<ProductLite[]>([])
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setQuery('')
      setResults([])
      setError(null)
      return
    }
    // Initial load — show first 20 products so the merchant sees options
    // without needing to type.
    let alive = true
    setLoading(true)
    knowledgeApi
      .searchProducts('')
      .then(res => {
        if (alive) setResults(res.items)
      })
      .catch(err => {
        if (alive) setError((err as Error).message || 'تعذّر تحميل المنتجات')
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handle = window.setTimeout(async () => {
      setLoading(true)
      setError(null)
      try {
        const res = await knowledgeApi.searchProducts(query)
        setResults(res.items)
      } catch (err) {
        setError((err as Error).message || 'تعذّر البحث')
      } finally {
        setLoading(false)
      }
    }, 250)
    return () => window.clearTimeout(handle)
  }, [query, open])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-40 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-xl max-h-[90vh] flex flex-col shadow-xl">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Package className="w-4 h-4 text-brand-500 shrink-0" />
            <h3 className="text-sm font-semibold text-slate-900">اربط منتجات بهذا القسم</h3>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-2 border-b border-slate-100 bg-slate-50/40">
          <div className="relative">
            <Search className="w-3.5 h-3.5 absolute end-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              type="search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="ابحث في كتالوج المنتجات (الاسم، SKU، الـ external_id…)"
              className="input text-sm pe-8"
            />
          </div>
          <p className="text-[11px] text-slate-500 leading-relaxed">
            <Info className="w-3 h-3 inline-block ms-1" />
            عند ربط منتج، يستخدم الذكاء هذا القسم فقط عند الحديث عن المنتج المرتبط.
            الأقسام غير المربوطة بمنتج تبقى عامة لكل المحادثات.
          </p>
        </div>

        <div className="flex-1 overflow-y-auto p-3">
          {loading ? (
            <div className="text-center text-sm text-slate-400 py-10 inline-flex items-center gap-2 w-full justify-center">
              <Loader2 className="w-4 h-4 animate-spin" /> جاري البحث…
            </div>
          ) : error ? (
            <p className="text-xs text-red-600 inline-flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" /> {error}
            </p>
          ) : results.length === 0 ? (
            <p className="text-center text-sm text-slate-400 py-10">
              لا توجد نتائج مطابقة.
            </p>
          ) : (
            <ul className="space-y-2">
              {results.map(p => {
                const linked = alreadyLinkedIds.includes(p.id)
                return (
                  <li
                    key={p.id}
                    className="flex items-center gap-3 p-2.5 rounded-lg border border-slate-200 hover:border-brand-300 bg-white"
                  >
                    <div className="w-9 h-9 rounded bg-slate-100 flex items-center justify-center shrink-0">
                      <Package className="w-4 h-4 text-slate-500" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-slate-900 truncate">{p.title}</p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {p.sku ? `SKU: ${p.sku}` : ''}
                        {p.external_id ? ` • ${p.external_id}` : ''}
                        {!p.in_stock && <span className="ms-1 text-amber-600 font-medium">• غير متوفر</span>}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={busyId === p.id || linked}
                      onClick={async () => {
                        setBusyId(p.id)
                        try {
                          await onPick(p.id)
                        } finally {
                          setBusyId(null)
                        }
                      }}
                      className={classNames(
                        'shrink-0 inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-semibold',
                        linked
                          ? 'bg-emerald-100 text-emerald-700 cursor-default'
                          : 'bg-brand-500 hover:bg-brand-600 text-white',
                      )}
                    >
                      {busyId === p.id ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : linked ? (
                        <>
                          <CheckCircle className="w-3 h-3" /> مربوط
                        </>
                      ) : (
                        <>
                          <Plus className="w-3 h-3" /> ربط
                        </>
                      )}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="px-4 py-2.5 border-t border-slate-100 bg-slate-50/40 text-xs text-slate-500 flex items-center justify-between">
          <span>الكتالوج: تابع لمتجر التاجر فقط.</span>
          <button onClick={onClose} className="text-slate-700 hover:text-slate-900 font-medium">
            تم
          </button>
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Section editor modal (create / edit)
// ───────────────────────────────────────────────────────────────────────────

interface SectionEditorProps {
  open: boolean
  initial: KnowledgeSection | null
  defaultKind: string
  kinds: SectionKindMeta[]
  onClose: () => void
  onSave: (payload: SectionInput, sectionId: number | null) => Promise<void>
}

function SectionEditor({ open, initial, defaultKind, kinds, onClose, onSave }: SectionEditorProps) {
  const [kind, setKind] = useState(defaultKind)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [priority, setPriority] = useState(100)
  const [active, setActive] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    if (initial) {
      setKind(initial.kind)
      setTitle(initial.title || '')
      setBody(initial.body || '')
      setPriority(initial.priority)
      setActive(initial.is_active)
    } else {
      setKind(defaultKind)
      setTitle('')
      setBody('')
      setPriority(100)
      setActive(true)
    }
    setError(null)
  }, [open, initial, defaultKind])

  if (!open) return null

  const meta = kinds.find(k => k.kind === kind)

  const handleSave = async () => {
    setBusy(true)
    setError(null)
    try {
      await onSave(
        { kind, title: title.trim() || null, body: body.trim(), priority, is_active: active },
        initial ? initial.id : null,
      )
      onClose()
    } catch (err) {
      setError((err as Error).message || 'فشل الحفظ')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-xl max-h-[90vh] flex flex-col shadow-xl">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-900">
            {initial ? 'تعديل القسم' : 'إضافة قسم جديد'}
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-3">
          <label className="block">
            <span className="text-xs text-slate-600 font-medium">نوع القسم</span>
            <select
              value={kind}
              onChange={e => setKind(e.target.value)}
              className="input text-sm mt-1"
              disabled={!!initial}
            >
              {kinds.map(k => (
                <option key={k.kind} value={k.kind}>
                  {k.label_ar}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-xs text-slate-600 font-medium">عنوان مختصر (اختياري)</span>
            <input
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              maxLength={120}
              className="input text-sm mt-1"
              placeholder={meta?.label_ar ?? ''}
            />
          </label>

          <label className="block">
            <span className="text-xs text-slate-600 font-medium">المحتوى</span>
            <textarea
              value={body}
              onChange={e => setBody(e.target.value)}
              rows={8}
              className="input text-sm mt-1 leading-relaxed"
              placeholder={meta?.placeholder_ar ?? ''}
              maxLength={8000}
            />
          </label>

          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-slate-600">
              <input
                type="number"
                value={priority}
                onChange={e => setPriority(Math.max(0, Math.min(10000, Number(e.target.value) || 0)))}
                className="input text-sm py-1 px-2 w-20"
                min={0}
                max={10000}
              />
              الأولوية
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={active}
                onChange={e => setActive(e.target.checked)}
                className="rounded"
              />
              مفعّل
            </label>
          </div>

          {error && (
            <p className="text-xs text-red-600 inline-flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" /> {error}
            </p>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100 bg-slate-50/40 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-600 hover:bg-slate-100"
          >
            إلغاء
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={busy || !body.trim()}
            className="inline-flex items-center gap-2 px-4 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-xs font-semibold disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            حفظ
          </button>
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Quick Updates card — top of the page
// ───────────────────────────────────────────────────────────────────────────

interface QuickUpdateCardProps {
  mediaPool: AIMediaItem[]
  onSaveQuick: (text: string, attachedMediaIds: number[]) => Promise<void>
  onFormatWithAI: (text: string, attachedMediaIds: number[]) => Promise<void>
}

function QuickUpdateCard({ mediaPool, onSaveQuick, onFormatWithAI }: QuickUpdateCardProps) {
  const [text, setText] = useState('')
  const [busyKind, setBusyKind] = useState<'save' | 'format' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [attached, setAttached] = useState<number[]>([])

  const attachedMedia = useMemo(
    () => attached.map(id => mediaPool.find(m => m.id === id)).filter(Boolean) as AIMediaItem[],
    [attached, mediaPool],
  )

  const runWith = async (mode: 'save' | 'format') => {
    if (!text.trim()) return
    setBusyKind(mode)
    setError(null)
    try {
      if (mode === 'save') {
        await onSaveQuick(text.trim(), attached)
      } else {
        await onFormatWithAI(text.trim(), attached)
      }
      setText('')
      setAttached([])
    } catch (err) {
      setError((err as Error).message || 'تعذّر التنفيذ')
    } finally {
      setBusyKind(null)
    }
  }

  return (
    <div className="card border-brand-100 bg-gradient-to-br from-brand-50/60 to-white">
      <div className="px-5 py-3.5 border-b border-brand-100/70 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-brand-100 text-brand-700 flex items-center justify-center shrink-0">
            <Sparkles className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-slate-900">تحديثات سريعة</p>
            <p className="text-xs text-slate-500">اكتب أي معلومة جديدة، ولا تقلق بشأن المكان — سيتم تصنيفها لاحقاً.</p>
          </div>
        </div>
        <span className="hidden sm:inline-flex items-center px-2 py-1 rounded-md bg-amber-100 text-amber-800 text-[10px] font-semibold">
          AI
        </span>
      </div>

      <div className="p-5 space-y-3">
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          rows={4}
          className="input text-sm leading-relaxed"
          placeholder="مثال: السبت إجازة هذا الأسبوع، أو: شحن الراجحي ينتهي قبل 2 ظهراً، أو: منتج العسل اليمني نفد مؤقتاً، أو: وصفة عسل + ليمون لنزلات البرد."
          maxLength={4000}
        />

        {/* Attached media chips */}
        <div className="flex items-center gap-1.5 flex-wrap">
          {attachedMedia.map(m => (
            <span
              key={m.id}
              className="inline-flex items-center gap-1.5 pl-1.5 pr-1 py-0.5 rounded-md bg-slate-100 border border-slate-200 text-[11px] text-slate-700"
            >
              {mediaIcon(m.media_type)}
              <span className="truncate max-w-[140px]">{m.title}</span>
              <button
                type="button"
                onClick={() => setAttached(prev => prev.filter(id => id !== m.id))}
                className="text-slate-400 hover:text-red-500"
              >
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
          <button
            type="button"
            onClick={() => setPickerOpen(true)}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium text-brand-700 hover:bg-brand-50 border border-dashed border-brand-300"
          >
            <Paperclip className="w-3 h-3" /> أرفق وسائط
          </button>
        </div>

        {error && (
          <p className="text-xs text-red-600 inline-flex items-center gap-1">
            <AlertCircle className="w-3.5 h-3.5" /> {error}
          </p>
        )}

        <div className="flex items-center justify-between gap-3 flex-wrap">
          <p className="text-[11px] text-slate-500 leading-relaxed flex-1 min-w-[200px]">
            <Info className="w-3 h-3 inline-block ms-1" />
            <span className="font-semibold">حفظ كملاحظة</span> يضع النص في "التحديثات السريعة".
            {' '}
            <span className="font-semibold">تنسيق ودمج بالذكاء</span> يصنّف النص ويُريك معاينة قبل الحفظ.
          </p>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={() => runWith('format')}
              disabled={busyKind !== null || !text.trim()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-600 hover:to-orange-600 text-white text-xs font-semibold disabled:opacity-50"
            >
              {busyKind === 'format' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
              تنسيق ودمج بالذكاء
            </button>
            <button
              type="button"
              onClick={() => runWith('save')}
              disabled={busyKind !== null || !text.trim()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-xs font-semibold disabled:opacity-50"
            >
              {busyKind === 'save' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              حفظ كملاحظة
            </button>
          </div>
        </div>
      </div>

      {/* Quick-update media picker */}
      <MediaPicker
        open={pickerOpen}
        media={mediaPool}
        alreadyLinkedIds={attached}
        onPick={async (mediaId) => {
          setAttached(prev => (prev.includes(mediaId) ? prev : [...prev, mediaId]))
        }}
        onClose={() => setPickerOpen(false)}
      />
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Phase 2 — Draft preview drawer
// ───────────────────────────────────────────────────────────────────────────

interface DraftPreviewProps {
  draft: KnowledgeDraft | null
  kindLabelByKind: Map<string, string>
  sectionsById: Map<number, KnowledgeSection>
  mediaPool: AIMediaItem[]
  onApprove: (draftId: number, opIds: string[]) => Promise<void>
  onReject: (draftId: number) => Promise<void>
  onClose: () => void
}

const CONFLICT_KIND_LABELS: Record<string, string> = {
  platform_price: 'سعر يخالف منصة التجارة',
  platform_stock: 'مخزون يخالف منصة التجارة',
  platform_name: 'اسم منتج يخالف منصة التجارة',
  platform_url: 'رابط يخالف منصة التجارة',
  existing_section: 'تكرار مع قسم موجود',
}

const OP_LABELS: Record<string, string> = {
  create: 'إنشاء قسم جديد',
  update: 'تحديث قسم موجود',
  merge: 'دمج مع قسم موجود',
  link_media: 'ربط وسائط',
  link_product: 'ربط منتج (تطابق ذكي)',
}

function DraftPreviewDrawer({
  draft,
  kindLabelByKind,
  sectionsById,
  mediaPool,
  onApprove,
  onReject,
  onClose,
}: DraftPreviewProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Conflicts that should hard-block approval of related ops.
  //
  // Mirrors the server-side guard in
  // ``backend/routers/knowledge.py::approve_draft``: a hard platform
  // conflict (price / stock / name / url) blocks an op when either
  //  (a) both sides explicitly reference the same section_id, OR
  //  (b) the conflict is tenant-wide (with_section_id == null) AND the
  //      op's body looks like a price/stock claim.
  // The previous version short-circuited on ``op.target_section_id``
  // being truthy, which silently let ``create`` ops slip past the
  // tenant-wide platform_price conflict (the most common case).
  const blockingOpsByConflict = useMemo(() => {
    if (!draft) return new Set<string>()
    const blocked = new Set<string>()
    const hardKinds = new Set<string>([
      'platform_price',
      'platform_stock',
      'platform_name',
      'platform_url',
    ])
    const priceRe = /(\d+[\.,]?\d*)\s*(ريال|ر\.س|sar|درهم|aed|usd|\$)/i
    const stockRe = /(متوفر|غير متوفر|نفد|نفذ|out of stock|in stock)/i
    const looksLikePlatformClaim = (body: string) =>
      !!body && (priceRe.test(body) || stockRe.test(body))

    for (const c of draft.conflicts || []) {
      if (!hardKinds.has(String(c.kind))) continue
      for (const op of draft.proposal.proposed_ops) {
        if (op.op === 'link_media' || op.op === 'link_product') continue
        const sameTarget =
          c.with_section_id != null &&
          op.target_section_id != null &&
          c.with_section_id === op.target_section_id
        const tenantwideClaim =
          c.with_section_id == null && looksLikePlatformClaim(op.body || '')
        if (sameTarget || tenantwideClaim) blocked.add(op.op_id)
      }
    }
    return blocked
  }, [draft])

  // Default selection: all ops EXCEPT those blocked by hard conflicts.
  useEffect(() => {
    if (!draft) return
    const initial = new Set<string>()
    for (const op of draft.proposal.proposed_ops) {
      if (!blockingOpsByConflict.has(op.op_id)) initial.add(op.op_id)
    }
    setSelected(initial)
    setError(null)
  }, [draft, blockingOpsByConflict])

  if (!draft) return null

  const ops = draft.proposal.proposed_ops || []
  const conflicts = draft.conflicts || []
  const isFallback = !!draft.proposal.fallback_used

  const handleApprove = async () => {
    setBusy('approve')
    setError(null)
    try {
      await onApprove(draft.id, Array.from(selected))
    } catch (err) {
      setError((err as Error).message || 'فشل التطبيق')
    } finally {
      setBusy(null)
    }
  }

  const handleReject = async () => {
    setBusy('reject')
    setError(null)
    try {
      await onReject(draft.id)
    } catch (err) {
      setError((err as Error).message || 'فشل الرفض')
    } finally {
      setBusy(null)
    }
  }

  const toggle = (opId: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(opId)) next.delete(opId)
      else next.add(opId)
      return next
    })
  }

  return (
    <div className="fixed inset-0 z-40 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-xl">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-amber-500 to-orange-500 text-white flex items-center justify-center shrink-0">
              <Wand2 className="w-4 h-4" />
            </div>
            <div>
              <p className="text-sm font-semibold text-slate-900">معاينة تنسيق الذكاء</p>
              <p className="text-[11px] text-slate-500">
                راجع الاقتراحات قبل الحفظ. الثقة:{' '}
                <span className="font-semibold">
                  {Math.round((draft.proposal.confidence || 0) * 100)}%
                </span>
                {draft.proposal.model ? ` • ${draft.proposal.model}` : ''}
              </p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {/* Original text */}
          <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
            <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold mb-1">
              نص التاجر الأصلي
            </p>
            <p className="text-sm text-slate-700 whitespace-pre-wrap break-words">
              {draft.raw_text}
            </p>
          </div>

          {/* Conflicts banner */}
          {conflicts.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
              <p className="text-xs font-semibold text-amber-900 mb-1.5 inline-flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5" /> تعارضات اكتشفها الذكاء
              </p>
              <ul className="space-y-1 text-xs text-amber-800">
                {conflicts.map((c, idx) => (
                  <li key={idx} className="flex items-start gap-1.5">
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 mt-1.5 shrink-0" />
                    <span>
                      <span className="font-semibold">
                        {CONFLICT_KIND_LABELS[c.kind] || c.kind}:
                      </span>{' '}
                      {c.explanation}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Fallback notice */}
          {isFallback && (
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
              <p className="font-semibold mb-1 inline-flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5" /> الذكاء لم يستطع التصنيف
              </p>
              <p>تم حفظ النص كملاحظة سريعة. يمكنك نقله يدوياً إلى القسم المناسب لاحقاً.</p>
            </div>
          )}

          {/* Proposed ops */}
          {ops.length === 0 ? (
            <p className="text-sm text-slate-400 py-6 text-center">
              لم يُنتج الذكاء أي اقتراحات.
            </p>
          ) : (
            <ul className="space-y-2.5">
              {ops.map(op => (
                <OpPreviewCard
                  key={op.op_id}
                  op={op}
                  selected={selected.has(op.op_id)}
                  blocked={blockingOpsByConflict.has(op.op_id)}
                  kindLabel={kindLabelByKind.get(op.kind) || op.kind}
                  targetSection={
                    typeof op.target_section_id === 'number'
                      ? sectionsById.get(op.target_section_id) || null
                      : null
                  }
                  attachedMedia={
                    op.media_id != null
                      ? mediaPool.find(m => m.id === op.media_id) || null
                      : null
                  }
                  onToggle={() => toggle(op.op_id)}
                />
              ))}
            </ul>
          )}

          {error && (
            <p className="text-xs text-red-600 inline-flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" /> {error}
            </p>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100 bg-slate-50/40 flex items-center justify-between gap-3 flex-wrap">
          <span className="text-xs text-slate-500">
            مختار: {selected.size} من {ops.length}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleReject}
              disabled={busy !== null}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-700 hover:bg-slate-100 border border-slate-200"
            >
              {busy === 'reject' ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : 'تجاهل'}
            </button>
            <button
              type="button"
              onClick={handleApprove}
              disabled={busy !== null || selected.size === 0}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white text-xs font-semibold disabled:opacity-50"
            >
              {busy === 'approve' ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <CheckCircle className="w-3.5 h-3.5" />
              )}
              تطبيق المختار
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function OpPreviewCard({
  op,
  selected,
  blocked,
  kindLabel,
  targetSection,
  attachedMedia,
  onToggle,
}: {
  op: ProposedOp
  selected: boolean
  blocked: boolean
  kindLabel: string
  targetSection: KnowledgeSection | null
  attachedMedia: AIMediaItem | null
  onToggle: () => void
}) {
  return (
    <li
      className={classNames(
        'rounded-xl border p-3 bg-white transition-all',
        blocked
          ? 'border-amber-300 bg-amber-50/30 opacity-90'
          : selected
            ? 'border-emerald-300 ring-2 ring-emerald-100'
            : 'border-slate-200',
      )}
    >
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          disabled={blocked}
          className="mt-1 rounded shrink-0"
          aria-label={`اختيار العملية ${op.op_id}`}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-brand-50 text-brand-700">
              {OP_LABELS[op.op] || op.op}
            </span>
            <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">
              {kindLabel}
            </span>
            {blocked && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 inline-flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" /> محجوب بسبب تعارض
              </span>
            )}
          </div>
          {op.title && (
            <p className="text-sm font-semibold text-slate-900 mb-0.5">{op.title}</p>
          )}
          {targetSection && (
            <p className="text-[11px] text-slate-500 mb-1">
              ↪ القسم المستهدف: <span className="font-medium">{targetSection.title || targetSection.kind}</span>
            </p>
          )}
          {attachedMedia && (
            <p className="text-[11px] text-slate-500 mb-1 inline-flex items-center gap-1">
              {mediaIcon(attachedMedia.media_type)}
              {attachedMedia.title}
              {op.link_role ? ` (${LINK_ROLE_LABELS_AR[op.link_role]})` : ''}
            </p>
          )}
          {op.op === 'link_product' && op.product_id && (
            <p className="text-[11px] text-emerald-700 mb-1 inline-flex items-center gap-1">
              <Package className="w-3 h-3" />
              منتج #{op.product_id}
              {op.confidence != null && (
                <span className="text-amber-700">
                  • ثقة {Math.round(op.confidence * 100)}%
                </span>
              )}
            </p>
          )}
          {op.body && (
            <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap break-words mt-1">
              {op.body}
            </p>
          )}
          {op.rationale && (
            <p className="text-[11px] text-slate-400 mt-1.5 italic">
              {op.rationale}
            </p>
          )}
        </div>
      </div>
    </li>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Section card (one row per `MerchantKnowledgeSection`)
// ───────────────────────────────────────────────────────────────────────────

interface SectionCardProps {
  section: KnowledgeSection
  kindLabel: string
  onEdit: () => void
  onDelete: () => Promise<void>
  onToggle: () => Promise<void>
  onAttachMedia: () => void
  onDetachMedia: (linkId: number) => Promise<void>
  onAttachProduct: () => void
  onDetachProduct: (linkId: number) => Promise<void>
}

function SectionCard({
  section,
  kindLabel,
  onEdit,
  onDelete,
  onToggle,
  onAttachMedia,
  onDetachMedia,
  onAttachProduct,
  onDetachProduct,
}: SectionCardProps) {
  const [busyLink, setBusyLink] = useState<number | null>(null)
  const [busyProductLink, setBusyProductLink] = useState<number | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [toggling, setToggling] = useState(false)

  return (
    <div
      className={classNames(
        'rounded-xl border p-3.5 bg-white',
        section.is_active ? 'border-slate-200' : 'border-slate-200 bg-slate-50/60 opacity-70',
      )}
    >
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-0.5">
            <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-brand-50 text-brand-700">
              {kindLabel}
            </span>
            {section.source === 'imported' && (
              <span className="text-[10px] text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded">
                مستورد
              </span>
            )}
            {!section.is_active && (
              <span className="text-[10px] text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded">
                غير مفعّل
              </span>
            )}
          </div>
          {section.title && (
            <p className="text-sm font-semibold text-slate-900 truncate">{section.title}</p>
          )}
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          <button
            type="button"
            onClick={async () => {
              setToggling(true)
              try {
                await onToggle()
              } finally {
                setToggling(false)
              }
            }}
            disabled={toggling}
            title={section.is_active ? 'إيقاف' : 'تفعيل'}
            className="p-1.5 rounded hover:bg-slate-100 text-slate-500"
          >
            {toggling ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : section.is_active ? (
              <Power className="w-3.5 h-3.5" />
            ) : (
              <PowerOff className="w-3.5 h-3.5" />
            )}
          </button>
          <button
            type="button"
            onClick={onEdit}
            title="تعديل"
            className="p-1.5 rounded hover:bg-slate-100 text-slate-500"
          >
            <Edit2 className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            title="حذف"
            className="p-1.5 rounded hover:bg-red-50 text-red-500"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap break-words">
        {section.body || <span className="text-slate-400 italic">— لا يوجد محتوى —</span>}
      </p>

      {/* Linked media chips */}
      <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
        {(section.media_links || []).map(link => (
          <MediaChip
            key={link.id}
            link={link}
            busy={busyLink === link.id}
            onRemove={async () => {
              setBusyLink(link.id)
              try {
                await onDetachMedia(link.id)
              } finally {
                setBusyLink(null)
              }
            }}
          />
        ))}
        <button
          type="button"
          onClick={onAttachMedia}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium text-brand-700 hover:bg-brand-50 border border-dashed border-brand-300"
        >
          <Paperclip className="w-3 h-3" /> اربط وسائط
        </button>
      </div>

      {/* Linked product chips (Phase 3) */}
      <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
        {(section.product_links || []).map(link => (
          <ProductChip
            key={link.id}
            link={link}
            busy={busyProductLink === link.id}
            onRemove={async () => {
              setBusyProductLink(link.id)
              try {
                await onDetachProduct(link.id)
              } finally {
                setBusyProductLink(null)
              }
            }}
          />
        ))}
        <button
          type="button"
          onClick={onAttachProduct}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium text-emerald-700 hover:bg-emerald-50 border border-dashed border-emerald-300"
        >
          <Package className="w-3 h-3" /> اربط منتجات
        </button>
        {(!section.product_links || section.product_links.length === 0) && (
          <span
            className="text-[10px] text-slate-400"
            title="هذا القسم عام — يظهر للذكاء في كل المحادثات."
          >
            (عام)
          </span>
        )}
      </div>

      {confirmDelete && (
        <div className="mt-2.5 p-2.5 rounded-lg bg-red-50 border border-red-200 flex items-center gap-2 text-xs text-red-800">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          <span className="flex-1">حذف هذا القسم نهائياً؟</span>
          <button
            type="button"
            disabled={deleting}
            onClick={async () => {
              setDeleting(true)
              try {
                await onDelete()
              } finally {
                setDeleting(false)
                setConfirmDelete(false)
              }
            }}
            className="px-2.5 py-1 rounded bg-red-500 hover:bg-red-600 text-white font-semibold disabled:opacity-50 inline-flex items-center gap-1"
          >
            {deleting && <Loader2 className="w-3 h-3 animate-spin" />}
            حذف
          </button>
          <button
            type="button"
            onClick={() => setConfirmDelete(false)}
            className="px-2 py-1 text-red-700 hover:bg-red-100 rounded"
          >
            إلغاء
          </button>
        </div>
      )}
    </div>
  )
}

function ProductChip({
  link,
  busy,
  onRemove,
}: {
  link: ProductLinkRow
  busy: boolean
  onRemove: () => Promise<void>
}) {
  const label = link.product?.title || `منتج #${link.product_id}`
  const isFuzzy = link.source === 'ai_fuzzy_match'
  return (
    <span
      className={classNames(
        'inline-flex items-center gap-1.5 pl-1.5 pr-1 py-0.5 rounded-md border text-[11px] max-w-full',
        isFuzzy
          ? 'bg-amber-50 border-amber-200 text-amber-800'
          : 'bg-emerald-50 border-emerald-200 text-emerald-800',
      )}
      title={
        link.product?.sku
          ? `${label} • SKU: ${link.product.sku}`
          : label
      }
    >
      <Package className="w-3 h-3" />
      <span className="truncate max-w-[160px]">{label}</span>
      {isFuzzy && link.confidence != null && (
        <span className="text-[9px] text-amber-700 shrink-0">
          {Math.round(link.confidence * 100)}%
        </span>
      )}
      <button
        type="button"
        onClick={onRemove}
        disabled={busy}
        className="text-current opacity-50 hover:opacity-100 ms-0.5"
        title="إلغاء الربط"
      >
        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}
      </button>
    </span>
  )
}

function MediaChip({
  link,
  busy,
  onRemove,
}: {
  link: MediaLinkRow
  busy: boolean
  onRemove: () => Promise<void>
}) {
  const m = link.media
  const label = m?.title || `وسيط #${link.media_id}`
  return (
    <span
      className="inline-flex items-center gap-1.5 pl-1.5 pr-1 py-0.5 rounded-md bg-slate-100 border border-slate-200 text-[11px] text-slate-700 max-w-full"
      title={`${label} • ${LINK_ROLE_LABELS_AR[link.link_role]}`}
    >
      {mediaIcon(m?.media_type)}
      <span className="truncate max-w-[160px]">{label}</span>
      <span className="text-[9px] text-slate-500 shrink-0">
        {LINK_ROLE_LABELS_AR[link.link_role]}
      </span>
      <button
        type="button"
        onClick={onRemove}
        disabled={busy}
        className="text-slate-400 hover:text-red-500 ms-0.5"
        title="إلغاء الربط"
      >
        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}
      </button>
    </span>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Group accordion
// ───────────────────────────────────────────────────────────────────────────

interface SectionGroupProps {
  groupId: number
  label: string
  description?: string
  sections: KnowledgeSection[]
  kinds: SectionKindMeta[]
  defaultOpen: boolean
  onAdd: (kind: string) => void
  renderChild: (section: KnowledgeSection) => JSX.Element
}

function SectionGroup({
  groupId,
  label,
  description,
  sections,
  kinds,
  defaultOpen,
  onAdd,
  renderChild,
}: SectionGroupProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [pickerOpen, setPickerOpen] = useState(false)
  const kindsInGroup = useMemo(() => kinds.filter(k => k.group === groupId), [kinds, groupId])
  const visibleSections = sections.filter(s => s.group === groupId)

  return (
    <div className="card overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(prev => !prev)}
        className="w-full px-5 py-3.5 flex items-center justify-between gap-3 hover:bg-slate-50/60"
      >
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-brand-50 text-brand-700 flex items-center justify-center shrink-0 font-bold text-sm">
            {groupId}
          </div>
          <div className="text-start min-w-0">
            <p className="text-sm font-semibold text-slate-900">{label}</p>
            {description && (
              <p className="text-[11px] text-slate-500 mt-0.5">{description}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-[11px] text-slate-500 px-2 py-0.5 rounded-full bg-slate-100">
            {visibleSections.length}
          </span>
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </div>
      </button>

      {open && (
        <div className="px-5 pb-4 pt-1 border-t border-slate-100 space-y-2.5">
          {visibleSections.length === 0 && (
            <p className="text-xs text-slate-400 py-3 text-center">
              لم تُضف أقسام بعد في هذه المجموعة.
            </p>
          )}
          {visibleSections.map(s => renderChild(s))}

          {/* Add button with kind dropdown */}
          {kindsInGroup.length > 0 && (
            <div className="pt-2 relative">
              <button
                type="button"
                onClick={() => setPickerOpen(prev => !prev)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-xs font-semibold"
              >
                <Plus className="w-3.5 h-3.5" /> أضف قسماً
                {pickerOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              </button>
              {pickerOpen && (
                <div className="absolute z-10 mt-1 start-0 bg-white border border-slate-200 rounded-lg shadow-lg p-1 min-w-[200px]">
                  {kindsInGroup.map(k => (
                    <button
                      key={k.kind}
                      type="button"
                      onClick={() => {
                        setPickerOpen(false)
                        onAdd(k.kind)
                      }}
                      className="block w-full text-start px-3 py-1.5 text-xs hover:bg-brand-50 rounded text-slate-700"
                    >
                      {k.label_ar}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Group 6 — media library quick view (read-only mirror of linked media)
// ───────────────────────────────────────────────────────────────────────────

function LinkedMediaSummary({ sections }: { sections: KnowledgeSection[] }) {
  const allLinks = useMemo(() => {
    const out: { link: MediaLinkRow; section: KnowledgeSection }[] = []
    for (const s of sections) {
      for (const lk of s.media_links || []) {
        out.push({ link: lk, section: s })
      }
    }
    return out
  }, [sections])

  return (
    <div className="card overflow-hidden">
      <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-brand-50 text-brand-700 flex items-center justify-center shrink-0 font-bold text-sm">
            6
          </div>
          <div>
            <p className="text-sm font-semibold text-slate-900">مكتبة الوسائط المرتبطة</p>
            <p className="text-[11px] text-slate-500 mt-0.5">
              كل الوسائط المرتبطة بأقسام قاعدة المعرفة. لإدارة المكتبة كاملة، افتح صفحة "نحلة الذكية".
            </p>
          </div>
        </div>
        <a
          href="/intelligence"
          className="text-xs text-brand-700 hover:text-brand-900 font-medium inline-flex items-center gap-1"
        >
          فتح مكتبة الوسائط <Link2 className="w-3 h-3" />
        </a>
      </div>
      <div className="p-4">
        {allLinks.length === 0 ? (
          <p className="text-xs text-slate-400 py-6 text-center">
            لم يتم ربط وسائط بأي قسم بعد. استخدم زر "اربط وسائط" داخل أي بطاقة.
          </p>
        ) : (
          <ul className="space-y-2">
            {allLinks.map(({ link, section }) => (
              <li
                key={link.id}
                className="flex items-center gap-3 p-2 rounded-lg border border-slate-200 bg-white"
              >
                <div className="w-9 h-9 rounded bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
                  {link.media?.thumbnail_url ||
                  (link.media?.media_type === 'image' && link.media?.file_url) ? (
                    <img
                      src={link.media?.thumbnail_url || link.media?.file_url}
                      alt={link.media?.title || ''}
                      className="w-full h-full object-cover"
                      onError={e => {
                        ;(e.currentTarget as HTMLImageElement).style.display = 'none'
                      }}
                    />
                  ) : (
                    mediaIcon(link.media?.media_type)
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-900 truncate">
                    {link.media?.title || `وسيط #${link.media_id}`}
                  </p>
                  <p className="text-[11px] text-slate-500 truncate">
                    {section.title || section.kind} • {LINK_ROLE_LABELS_AR[link.link_role]}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Main page
// ───────────────────────────────────────────────────────────────────────────

export default function KnowledgeBase() {
  const { t } = useLanguage()
  const [registry, setRegistry] = useState<SectionKindsResponse | null>(null)
  const [sections, setSections] = useState<KnowledgeSection[]>([])
  const [mediaPool, setMediaPool] = useState<AIMediaItem[]>([])
  const [legacy, setLegacy] = useState<LegacyKnowledgeBaseResponse | null>(null)
  const [legacyDismissed, setLegacyDismissed] = useState(false)
  const [platformLabel, setPlatformLabel] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  // Modal state
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorInitial, setEditorInitial] = useState<KnowledgeSection | null>(null)
  const [editorDefaultKind, setEditorDefaultKind] = useState('store_story')

  const [pickerSectionId, setPickerSectionId] = useState<number | null>(null)
  // Phase 3 — product picker
  const [productPickerSectionId, setProductPickerSectionId] = useState<number | null>(null)

  // Phase 2 — draft preview drawer
  const [activeDraft, setActiveDraft] = useState<KnowledgeDraft | null>(null)

  // ── Initial load ────────────────────────────────────────────────────────
  const loadAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [reg, list, mediaList, settings, legacyRes] = await Promise.all([
        knowledgeApi.getSectionKinds(),
        knowledgeApi.listSections(),
        intelligenceLibrariesApi.listAIMedia(true),
        settingsApi.getAll().catch(() => null),
        knowledgeApi.getLegacyKnowledgeBase().catch(() => null),
      ])
      setRegistry(reg)
      setSections(list.items)
      setMediaPool(mediaList.items)
      if (settings) {
        const platform = settings.store?.platform_type
        const hasSallaToken = !!settings.store?.salla_access_token
        const hasZidToken = !!settings.store?.zid_client_id
        const hasShopifyToken = !!settings.store?.shopify_access_token
        if (platform === 'salla' && hasSallaToken) setPlatformLabel('سلة')
        else if (platform === 'zid' && hasZidToken) setPlatformLabel('زد')
        else if (platform === 'shopify' && hasShopifyToken) setPlatformLabel('شوبيفاي')
        else setPlatformLabel(null)
      }
      setLegacy(legacyRes && legacyRes.text ? legacyRes : null)
    } catch (err) {
      setError((err as Error).message || 'تعذّر تحميل قاعدة المعرفة')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  const refreshSections = useCallback(async () => {
    const list = await knowledgeApi.listSections()
    setSections(list.items)
  }, [])

  // ── Handlers ────────────────────────────────────────────────────────────
  const handleQuickSave = useCallback(
    async (text: string, attachedMediaIds: number[]) => {
      const section = await knowledgeApi.createSection({
        kind: 'quick_update',
        body: text,
        priority: 50, // Quick updates sit at the top of group 1
        is_active: true,
      })
      for (const mid of attachedMediaIds) {
        try {
          await knowledgeApi.linkMedia(section.id, { media_id: mid, link_role: 'primary' })
        } catch {
          // swallow — visible in the section card after refresh
        }
      }
      await refreshSections()
      setToast('تم حفظ التحديث السريع')
    },
    [refreshSections],
  )

  const handleFormatWithAI = useCallback(
    async (text: string, attachedMediaIds: number[]) => {
      const draft = await knowledgeApi.formatQuickUpdate({
        raw_text: text,
        attached_media_ids: attachedMediaIds,
      })
      setActiveDraft(draft)
    },
    [],
  )

  const handleApproveDraft = useCallback(
    async (draftId: number, opIds: string[]) => {
      const updated = await knowledgeApi.approveDraft(draftId, opIds)
      setActiveDraft(null)
      await refreshSections()
      setToast(`تم تطبيق ${updated.applied_op_ids.length} اقتراحاً من الذكاء`)
    },
    [refreshSections],
  )

  const handleRejectDraft = useCallback(
    async (draftId: number) => {
      await knowledgeApi.rejectDraft(draftId)
      setActiveDraft(null)
      setToast('تم تجاهل اقتراحات الذكاء')
    },
    [],
  )

  const openCreate = useCallback((kind: string) => {
    setEditorInitial(null)
    setEditorDefaultKind(kind)
    setEditorOpen(true)
  }, [])

  const openEdit = useCallback((section: KnowledgeSection) => {
    setEditorInitial(section)
    setEditorDefaultKind(section.kind)
    setEditorOpen(true)
  }, [])

  const handleSaveSection = useCallback(
    async (payload: SectionInput, sectionId: number | null) => {
      if (sectionId == null) {
        await knowledgeApi.createSection(payload)
        setToast('تم إنشاء القسم')
      } else {
        await knowledgeApi.updateSection(sectionId, payload)
        setToast('تم تحديث القسم')
      }
      await refreshSections()
    },
    [refreshSections],
  )

  const handleDeleteSection = useCallback(
    async (sectionId: number) => {
      await knowledgeApi.deleteSection(sectionId)
      setToast('تم حذف القسم')
      await refreshSections()
    },
    [refreshSections],
  )

  const handleToggleSection = useCallback(
    async (sectionId: number) => {
      await knowledgeApi.toggleSection(sectionId)
      await refreshSections()
    },
    [refreshSections],
  )

  const handleLinkMedia = useCallback(
    async (sectionId: number, mediaId: number, role: LinkRole) => {
      await knowledgeApi.linkMedia(sectionId, { media_id: mediaId, link_role: role })
      await refreshSections()
      setToast('تم ربط الوسيط')
    },
    [refreshSections],
  )

  const handleUnlinkMedia = useCallback(
    async (sectionId: number, linkId: number) => {
      await knowledgeApi.unlinkMedia(sectionId, linkId)
      await refreshSections()
    },
    [refreshSections],
  )

  const handleLinkProduct = useCallback(
    async (sectionId: number, productId: number) => {
      await knowledgeApi.linkProduct(sectionId, { product_id: productId })
      await refreshSections()
      setToast('تم ربط المنتج بالقسم')
    },
    [refreshSections],
  )

  const handleUnlinkProduct = useCallback(
    async (sectionId: number, linkId: number) => {
      await knowledgeApi.unlinkProduct(sectionId, linkId)
      await refreshSections()
    },
    [refreshSections],
  )

  // ── Derived state ───────────────────────────────────────────────────────
  const kindLabelByKind = useMemo(() => {
    const map = new Map<string, string>()
    for (const k of registry?.kinds || []) map.set(k.kind, k.label_ar)
    return map
  }, [registry])

  const sectionsById = useMemo(() => {
    const map = new Map<number, KnowledgeSection>()
    for (const s of sections) map.set(s.id, s)
    return map
  }, [sections])

  const groups = registry?.groups || []
  const kinds = registry?.kinds || []

  const pickerSection = pickerSectionId != null
    ? sections.find(s => s.id === pickerSectionId) || null
    : null

  // ── Render ──────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="مركز معرفة المتجر الذكي"
          subtitle={t(tr => tr.pages.knowledgeBase.subtitle)}
        />
        <div className="flex items-center justify-center py-20 gap-2 text-slate-400 text-sm">
          <Loader2 className="w-4 h-4 animate-spin text-brand-500" />
          جاري التحميل...
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="مركز معرفة المتجر الذكي"
        subtitle={t(tr => tr.pages.knowledgeBase.subtitle)}
        action={
          <span className="hidden sm:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-brand-50 text-brand-700 text-[11px] font-semibold">
            <BookOpen className="w-3 h-3" /> Phase 1
          </span>
        }
      />

      {/* Toasts */}
      {toast && <SuccessToast text={toast} onDismiss={() => setToast(null)} />}
      {error && <ErrorToast text={error} onDismiss={() => setError(null)} />}

      {/* Platform precedence reminder */}
      <PlatformPrecedenceCard platformLabel={platformLabel} />

      {/* Legacy migration */}
      {legacy && !legacyDismissed && (
        <LegacyMigrationBanner
          legacy={legacy}
          onImported={async () => {
            setLegacy(null)
            setLegacyDismissed(true)
            setToast('تم استيراد النص القديم بنجاح')
            await refreshSections()
          }}
          onDismiss={() => setLegacyDismissed(true)}
        />
      )}

      {/* Quick Updates */}
      <QuickUpdateCard
        mediaPool={mediaPool}
        onSaveQuick={handleQuickSave}
        onFormatWithAI={handleFormatWithAI}
      />

      {/* Groups 1..5 */}
      {groups
        .filter(g => g.id >= 1 && g.id <= 5)
        .map(g => (
          <SectionGroup
            key={g.id}
            groupId={g.id}
            label={g.label_ar}
            description={
              g.id === 1
                ? 'ملاحظات سريعة كتبتها مؤخراً (سيتم تصنيفها في Phase 2).'
                : g.id === 2
                ? 'القصة، النبرة، اللهجة، أوقات العمل، الفروع.'
                : g.id === 3
                ? 'الدفع، التحويل البنكي، الدفع عند الاستلام، الإرجاع، الضمان.'
                : g.id === 4
                ? 'الشركات، المناطق، الشحن المبرد، ملاحظات الصيف.'
                : 'طريقة الاستخدام، الوصفات، الفوائد، التخزين، الفروقات.'
            }
            sections={sections}
            kinds={kinds}
            defaultOpen={g.id === 1 || g.id === 2}
            onAdd={openCreate}
            renderChild={s => (
              <SectionCard
                key={s.id}
                section={s}
                kindLabel={kindLabelByKind.get(s.kind) || s.kind}
                onEdit={() => openEdit(s)}
                onDelete={() => handleDeleteSection(s.id)}
                onToggle={() => handleToggleSection(s.id)}
                onAttachMedia={() => setPickerSectionId(s.id)}
                onDetachMedia={linkId => handleUnlinkMedia(s.id, linkId)}
                onAttachProduct={() => setProductPickerSectionId(s.id)}
                onDetachProduct={linkId => handleUnlinkProduct(s.id, linkId)}
              />
            )}
          />
        ))}

      {/* Group 6 — linked media summary */}
      <LinkedMediaSummary sections={sections} />

      {/* Modals */}
      <SectionEditor
        open={editorOpen}
        initial={editorInitial}
        defaultKind={editorDefaultKind}
        kinds={kinds}
        onClose={() => setEditorOpen(false)}
        onSave={handleSaveSection}
      />

      <MediaPicker
        open={pickerSection != null}
        media={mediaPool}
        alreadyLinkedIds={(pickerSection?.media_links || []).map(l => l.media_id)}
        onPick={async (mediaId, role) => {
          if (pickerSection) await handleLinkMedia(pickerSection.id, mediaId, role)
        }}
        onClose={() => setPickerSectionId(null)}
      />

      <ProductPicker
        open={productPickerSectionId != null}
        alreadyLinkedIds={
          productPickerSectionId != null
            ? ((sections.find(s => s.id === productPickerSectionId)?.product_links || []).map(l => l.product_id))
            : []
        }
        onPick={async productId => {
          if (productPickerSectionId != null) {
            await handleLinkProduct(productPickerSectionId, productId)
          }
        }}
        onClose={() => setProductPickerSectionId(null)}
      />

      <DraftPreviewDrawer
        draft={activeDraft}
        kindLabelByKind={kindLabelByKind}
        sectionsById={sectionsById}
        mediaPool={mediaPool}
        onApprove={handleApproveDraft}
        onReject={handleRejectDraft}
        onClose={() => setActiveDraft(null)}
      />
    </div>
  )
}
