// dashboard/src/pages/IntelligenceLibraries.tsx
//
// Two merchant-facing panels rendered as tabs inside the "نحلة الذكية"
// page (Intelligence.tsx):
//
//   • ManualCouponsPanel — CRUD for /intelligence/manual-coupons
//   • AIMediaLibraryPanel — CRUD for /intelligence/ai-media
//
// Both libraries are independent of Salla / automatic coupon engines
// and are consumed by the merchant brain when the customer asks for
// a discount or when the brain decides to attach an image / video /
// document to its reply.
//
// UX contract for save flows:
//
//   1. POST/PATCH/UPLOAD returns the new row → we OPTIMISTICALLY prepend
//      it to the in-memory list before refetching, so the merchant sees
//      the item appear instantly and never gets the "did my save work?"
//      anxiety the previous version produced.
//   2. Modal stays open until the API actually returns 2xx. On 4xx/5xx
//      we surface the backend `detail` and keep the form in place so the
//      merchant can fix and retry.
//   3. We then call `load()` to reconcile with the server, and pop a
//      green success banner that auto-dismisses after a few seconds.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Edit2,
  FileText,
  Image as ImageIcon,
  Info,
  Link as LinkIcon,
  Loader2,
  Music,
  Plus,
  Power,
  PowerOff,
  Save,
  Sparkles,
  Tag,
  Trash2,
  Upload,
  Video as VideoIcon,
  X,
} from 'lucide-react'

import {
  intelligenceLibrariesApi,
  type AIMediaItem,
  type AIMediaType,
  type ManualCoupon,
  type ManualCouponInput,
} from '../api/intelligenceLibraries'

// ──────────────────────────────────────────────────────────────────────────
// Shared toast
// ──────────────────────────────────────────────────────────────────────────

function SuccessBanner({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  useEffect(() => {
    const t = window.setTimeout(onDismiss, 4000)
    return () => window.clearTimeout(t)
  }, [text, onDismiss])
  return (
    <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 flex items-center gap-2">
      <CheckCircle2 className="w-4 h-4" />
      <span>{text}</span>
      <button
        onClick={onDismiss}
        className="ms-auto text-emerald-700/60 hover:text-emerald-900"
        aria-label="إغلاق"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Manual coupons
// ──────────────────────────────────────────────────────────────────────────

interface CouponFormState {
  id: number | null
  code: string
  title: string
  description: string
  discount_text: string
  usage_context: string
  priority: number
  is_active: boolean
  starts_at: string
  expires_at: string
}

const _emptyCouponForm = (): CouponFormState => ({
  id: null,
  code: '',
  title: '',
  description: '',
  discount_text: '',
  usage_context: '',
  priority: 100,
  is_active: true,
  starts_at: '',
  expires_at: '',
})

function _toIso(local: string): string | null {
  if (!local) return null
  const d = new Date(local)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

function _fromIso(iso: string | null | undefined): string {
  if (!iso) return ''
  // datetime-local expects YYYY-MM-DDTHH:mm — drop seconds + tz suffix.
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

export function ManualCouponsPanel() {
  const [items, setItems] = useState<ManualCoupon[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<CouponFormState>(_emptyCouponForm())
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { items: rows } = await intelligenceLibrariesApi.listManualCoupons()
      setItems(rows)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تحميل الكوبونات')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openCreate = () => {
    setForm(_emptyCouponForm())
    setError(null)
    setFormOpen(true)
  }

  const openEdit = (row: ManualCoupon) => {
    setForm({
      id: row.id,
      code: row.code,
      title: row.title ?? '',
      description: row.description ?? '',
      discount_text: row.discount_text ?? '',
      usage_context: row.usage_context ?? '',
      priority: row.priority,
      is_active: row.is_active,
      starts_at: _fromIso(row.starts_at),
      expires_at: _fromIso(row.expires_at),
    })
    setError(null)
    setFormOpen(true)
  }

  const submit = async () => {
    const code = form.code.trim()
    if (!code) {
      setError('كود الكوبون مطلوب')
      return
    }
    setSaving(true)
    setError(null)
    const payload: ManualCouponInput = {
      code,
      title: form.title.trim() || null,
      description: form.description.trim() || null,
      discount_text: form.discount_text.trim() || null,
      usage_context: form.usage_context.trim() || null,
      priority: Number.isFinite(form.priority) ? form.priority : 100,
      is_active: form.is_active,
      starts_at: _toIso(form.starts_at),
      expires_at: _toIso(form.expires_at),
    }
    try {
      let saved: ManualCoupon
      if (form.id == null) {
        saved = await intelligenceLibrariesApi.createManualCoupon(payload)
        // Optimistic prepend so the merchant sees the new row instantly.
        setItems((prev) => [saved, ...prev.filter((r) => r.id !== saved.id)])
        setSuccess(`تم حفظ الكوبون "${saved.code}" بنجاح`)
      } else {
        saved = await intelligenceLibrariesApi.updateManualCoupon(form.id, payload)
        setItems((prev) => prev.map((r) => (r.id === saved.id ? saved : r)))
        setSuccess(`تم تحديث الكوبون "${saved.code}"`)
      }
      setFormOpen(false)
      // Reconcile in background (catches any merchant-multi-tab edits).
      void load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر حفظ الكوبون')
    } finally {
      setSaving(false)
    }
  }

  const toggle = async (row: ManualCoupon) => {
    try {
      const saved = await intelligenceLibrariesApi.toggleManualCoupon(row.id)
      setItems((prev) => prev.map((r) => (r.id === saved.id ? saved : r)))
      setSuccess(saved.is_active ? `تم تفعيل ${saved.code}` : `تم إيقاف ${saved.code}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تغيير الحالة')
    }
  }

  const remove = async (row: ManualCoupon) => {
    if (!window.confirm(`حذف الكوبون "${row.code}"؟`)) return
    try {
      await intelligenceLibrariesApi.deleteManualCoupon(row.id)
      setItems((prev) => prev.filter((r) => r.id !== row.id))
      setSuccess(`تم حذف الكوبون "${row.code}"`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر الحذف')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">الكوبونات اليدوية</h2>
          <p className="text-sm text-slate-500 mt-0.5">
            أكواد خصم تستخدمها نحلة عند طلب العميل خصم — مستقلة عن الكوبونات التلقائية وعن سلة.
          </p>
        </div>
        <button onClick={openCreate} className="btn-primary text-sm flex items-center gap-2">
          <Plus className="w-4 h-4" /> إضافة كوبون
        </button>
      </div>

      {success && <SuccessBanner text={success} onDismiss={() => setSuccess(null)} />}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      )}

      <div className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        {loading ? (
          <div className="p-6 flex items-center justify-center text-slate-500 gap-2 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> جاري التحميل…
          </div>
        ) : items.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">
            لا توجد كوبونات بعد. أضِف أول كوبون يدوي حتى تستطيع نحلة عرضه عند طلب العميل خصم.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="text-right px-3 py-2 font-medium">الكود</th>
                <th className="text-right px-3 py-2 font-medium">العنوان</th>
                <th className="text-right px-3 py-2 font-medium">الخصم</th>
                <th className="text-right px-3 py-2 font-medium">متى يستخدمه الذكاء</th>
                <th className="text-right px-3 py-2 font-medium">الأولوية</th>
                <th className="text-right px-3 py-2 font-medium">الحالة</th>
                <th className="text-right px-3 py-2 font-medium">إجراءات</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((c) => (
                <tr key={c.id} className={c.is_active ? '' : 'opacity-60'}>
                  <td className="px-3 py-2 font-mono font-semibold text-slate-900">{c.code}</td>
                  <td className="px-3 py-2 text-slate-700">{c.title || '—'}</td>
                  <td className="px-3 py-2 text-slate-700">{c.discount_text || '—'}</td>
                  <td className="px-3 py-2 text-slate-600 max-w-xs truncate" title={c.usage_context ?? ''}>
                    {c.usage_context || '—'}
                  </td>
                  <td className="px-3 py-2 text-slate-700">{c.priority}</td>
                  <td className="px-3 py-2">
                    {c.is_active ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 text-emerald-700 px-2 py-0.5 text-xs">
                        <Power className="w-3 h-3" /> فعّال
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 text-slate-600 px-2 py-0.5 text-xs">
                        <PowerOff className="w-3 h-3" /> متوقف
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => openEdit(c)}
                        className="text-slate-500 hover:text-slate-800"
                        title="تعديل"
                      >
                        <Edit2 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => toggle(c)}
                        className="text-slate-500 hover:text-slate-800"
                        title={c.is_active ? 'تعطيل' : 'تفعيل'}
                      >
                        {c.is_active ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                      </button>
                      <button
                        onClick={() => remove(c)}
                        className="text-red-500 hover:text-red-700"
                        title="حذف"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {formOpen && (
        <CouponEditModal
          form={form}
          setForm={setForm}
          onClose={() => setFormOpen(false)}
          onSubmit={submit}
          saving={saving}
          error={error}
        />
      )}
    </div>
  )
}

function CouponEditModal({
  form,
  setForm,
  onClose,
  onSubmit,
  saving,
  error,
}: {
  form: CouponFormState
  setForm: (f: CouponFormState) => void
  onClose: () => void
  onSubmit: () => void
  saving: boolean
  error: string | null
}) {
  const isEdit = form.id != null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200">
          <h3 className="text-base font-semibold text-slate-900">
            {isEdit ? 'تعديل كوبون يدوي' : 'إضافة كوبون يدوي'}
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600" disabled={saving}>
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
            </div>
          )}

          <SectionCard title="معلومات الكوبون" icon={<Tag className="w-4 h-4" />}>
            <Field label="كود الكوبون *">
              <input
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value })}
                placeholder="AYNE26"
                className="input-base"
              />
            </Field>
            <Field label="العنوان">
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="خصم العسل"
                className="input-base"
              />
            </Field>
            <Field label="نص الخصم">
              <input
                value={form.discount_text}
                onChange={(e) => setForm({ ...form, discount_text: e.target.value })}
                placeholder="خصم 15% أو شحن مجاني"
                className="input-base"
              />
            </Field>
            <Field label="الوصف">
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                rows={2}
                placeholder="وصف داخلي للكوبون يساعدك في تذكره — لا يظهر للعميل."
                className="input-base"
              />
            </Field>
          </SectionCard>

          <SectionCard
            title="ذكاء الاستخدام"
            icon={<Sparkles className="w-4 h-4" />}
            hint="نحلة تستخدم هذا النص لتقرر متى ترسل الكوبون تلقائياً."
          >
            <Field label="متى يستخدمه الذكاء">
              <textarea
                value={form.usage_context}
                onChange={(e) => setForm({ ...form, usage_context: e.target.value })}
                rows={3}
                placeholder="مثال: إذا طلب العميل خصم أو كوبون. إذا كان متردد. إذا كانت السلة عالية."
                className="input-base"
              />
            </Field>
          </SectionCard>

          <SectionCard title="الحالة والأولوية" icon={<Power className="w-4 h-4" />}>
            <div className="grid grid-cols-2 gap-3">
              <Field label="الأولوية (الأقل = أهم)">
                <input
                  type="number"
                  value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}
                  min={0}
                  max={10000}
                  className="input-base"
                />
              </Field>
              <Field label="الحالة">
                <label className="inline-flex items-center gap-2 mt-2">
                  <input
                    type="checkbox"
                    checked={form.is_active}
                    onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                  />
                  <span className="text-sm text-slate-700">فعّال</span>
                </label>
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="بداية الصلاحية">
                <input
                  type="datetime-local"
                  value={form.starts_at}
                  onChange={(e) => setForm({ ...form, starts_at: e.target.value })}
                  className="input-base"
                />
              </Field>
              <Field label="نهاية الصلاحية">
                <input
                  type="datetime-local"
                  value={form.expires_at}
                  onChange={(e) => setForm({ ...form, expires_at: e.target.value })}
                  className="input-base"
                />
              </Field>
            </div>
          </SectionCard>
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-slate-200 bg-slate-50">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={saving}>
            إلغاء
          </button>
          <button onClick={onSubmit} disabled={saving} className="btn-primary text-sm flex items-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            حفظ
          </button>
        </div>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// AI media library
// ──────────────────────────────────────────────────────────────────────────

interface MediaFormState {
  id: number | null
  title: string
  description: string
  media_type: AIMediaType
  file_url: string
  thumbnail_url: string
  usage_context: string
  tags: string
  priority: number
  is_active: boolean
}

const _emptyMediaForm = (): MediaFormState => ({
  id: null,
  title: '',
  description: '',
  media_type: 'image',
  file_url: '',
  thumbnail_url: '',
  usage_context: '',
  tags: '',
  priority: 100,
  is_active: true,
})

const _MEDIA_TYPE_LABELS: Record<AIMediaType, string> = {
  image: 'صورة',
  video: 'فيديو',
  pdf: 'PDF',
  document: 'ملف',
  audio: 'صوت',
}

const _MEDIA_TYPE_EMOJI: Record<AIMediaType, string> = {
  image: '🖼',
  video: '🎥',
  pdf: '📄',
  document: '📁',
  audio: '🎧',
}

function _MediaIcon({ type, className = 'w-4 h-4' }: { type: AIMediaType; className?: string }) {
  if (type === 'image') return <ImageIcon className={className} />
  if (type === 'video') return <VideoIcon className={className} />
  if (type === 'audio') return <Music className={className} />
  return <FileText className={className} />
}

function _inferMediaTypeFromFile(file: File): AIMediaType {
  const mime = (file.type || '').toLowerCase()
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('video/')) return 'video'
  if (mime.startsWith('audio/')) return 'audio'
  if (mime === 'application/pdf') return 'pdf'
  return 'document'
}

function _formatBytes(bytes?: number | null): string {
  if (bytes == null || !Number.isFinite(bytes)) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function AIMediaLibraryPanel() {
  const [items, setItems] = useState<AIMediaItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<MediaFormState>(_emptyMediaForm())
  const [saving, setSaving] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { items: rows } = await intelligenceLibrariesApi.listAIMedia()
      setItems(rows)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تحميل الوسائط')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openCreate = () => {
    setForm(_emptyMediaForm())
    setUploadFile(null)
    setError(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
    setFormOpen(true)
  }

  const openEdit = (row: AIMediaItem) => {
    setForm({
      id: row.id,
      title: row.title,
      description: row.description ?? '',
      media_type: row.media_type,
      file_url: row.file_url,
      thumbnail_url: row.thumbnail_url ?? '',
      usage_context: row.usage_context ?? '',
      tags: (row.tags || []).join(', '),
      priority: row.priority,
      is_active: row.is_active,
    })
    setUploadFile(null)
    setError(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
    setFormOpen(true)
  }

  const submit = async () => {
    const title = form.title.trim()
    if (!title) {
      setError('عنوان الوسيط مطلوب')
      return
    }
    setSaving(true)
    setError(null)
    const tagsArr = form.tags
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    try {
      let saved: AIMediaItem
      if (form.id == null) {
        // Create flow — either upload a file or save an external URL.
        if (uploadFile) {
          saved = await intelligenceLibrariesApi.uploadAIMedia({
            file: uploadFile,
            title,
            media_type: form.media_type,
            description: form.description || undefined,
            usage_context: form.usage_context || undefined,
            tags: tagsArr,
            priority: form.priority,
            is_active: form.is_active,
          })
        } else {
          if (!form.file_url.trim()) {
            setError('ارفع ملفاً من جهازك أو الصق رابطاً عاماً للوسيط.')
            setSaving(false)
            return
          }
          saved = await intelligenceLibrariesApi.createAIMedia({
            title,
            description: form.description || null,
            media_type: form.media_type,
            file_url: form.file_url.trim(),
            thumbnail_url: form.thumbnail_url.trim() || null,
            usage_context: form.usage_context || null,
            tags: tagsArr,
            priority: form.priority,
            is_active: form.is_active,
          })
        }
        // Optimistic prepend so the merchant sees the item the moment
        // the modal closes — no "did the save go through?" anxiety.
        setItems((prev) => [saved, ...prev.filter((r) => r.id !== saved.id)])
        setSuccess(`تم حفظ الوسيط "${saved.title}" بنجاح ✓`)
      } else {
        saved = await intelligenceLibrariesApi.updateAIMedia(form.id, {
          title,
          description: form.description || null,
          media_type: form.media_type,
          ...(form.file_url.trim() ? { file_url: form.file_url.trim() } : {}),
          thumbnail_url: form.thumbnail_url.trim() || null,
          usage_context: form.usage_context || null,
          tags: tagsArr,
          priority: form.priority,
          is_active: form.is_active,
        })
        setItems((prev) => prev.map((r) => (r.id === saved.id ? saved : r)))
        setSuccess(`تم تحديث الوسيط "${saved.title}"`)
      }
      // Only close the modal AFTER the API confirms the save.
      setFormOpen(false)
      setUploadFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      // Reconcile with the server (catches multi-tab edits & confirms
      // the ordering the backend uses).
      void load()
    } catch (e) {
      // Keep the modal open so the merchant can fix whatever the
      // backend rejected without losing their input.
      setError(e instanceof Error ? e.message : 'تعذر حفظ الوسيط')
    } finally {
      setSaving(false)
    }
  }

  const toggle = async (row: AIMediaItem) => {
    try {
      const saved = await intelligenceLibrariesApi.toggleAIMedia(row.id)
      setItems((prev) => prev.map((r) => (r.id === saved.id ? saved : r)))
      setSuccess(saved.is_active ? `تم تفعيل "${saved.title}"` : `تم إيقاف "${saved.title}"`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تغيير الحالة')
    }
  }

  const remove = async (row: AIMediaItem) => {
    if (!window.confirm(`حذف الوسيط "${row.title}"؟`)) return
    try {
      await intelligenceLibrariesApi.deleteAIMedia(row.id)
      setItems((prev) => prev.filter((r) => r.id !== row.id))
      setSuccess(`تم حذف الوسيط "${row.title}"`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر الحذف')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">مكتبة الوسائط</h2>
          <p className="text-sm text-slate-500 mt-0.5">
            صور وفيديوهات وملفات ترسلها نحلة تلقائياً مع ردها — مثل باركود التحويل، صور المنتجات، ملفات PDF.
          </p>
        </div>
        <button onClick={openCreate} className="btn-primary text-sm flex items-center gap-2">
          <Plus className="w-4 h-4" /> إضافة وسيط
        </button>
      </div>

      {success && <SuccessBanner text={success} onDismiss={() => setSuccess(null)} />}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      )}

      <div className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        {loading ? (
          <div className="p-6 flex items-center justify-center text-slate-500 gap-2 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> جاري التحميل…
          </div>
        ) : items.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">
            لا توجد وسائط بعد. ارفع صورة أو ملف PDF واكتب «متى يستخدمه الذكاء»
            ليتم إرفاقه تلقائياً مع ردود نحلة.
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {items.map((m) => (
              <MediaListRow
                key={m.id}
                item={m}
                onEdit={() => openEdit(m)}
                onToggle={() => toggle(m)}
                onDelete={() => remove(m)}
              />
            ))}
          </ul>
        )}
      </div>

      {formOpen && (
        <MediaEditModal
          form={form}
          setForm={setForm}
          uploadFile={uploadFile}
          setUploadFile={setUploadFile}
          fileInputRef={fileInputRef}
          onClose={() => { setFormOpen(false); setUploadFile(null); setError(null) }}
          onSubmit={submit}
          saving={saving}
          error={error}
        />
      )}
    </div>
  )
}

// ── Library list row ────────────────────────────────────────────────────────

function MediaListRow({
  item,
  onEdit,
  onToggle,
  onDelete,
}: {
  item: AIMediaItem
  onEdit: () => void
  onToggle: () => void
  onDelete: () => void
}) {
  const usage = item.usage_context ?? ''
  const isImage = item.media_type === 'image' && !!item.file_url
  return (
    <li className={`px-4 py-3 ${item.is_active ? '' : 'opacity-60'}`}>
      <div className="flex items-start gap-3">
        {/* Thumbnail / type icon — bumped to 16x16 so an image preview is
            actually recognisable instead of a tiny pixel block. */}
        <div className="w-16 h-16 rounded-lg bg-slate-100 flex items-center justify-center text-slate-500 shrink-0 overflow-hidden border border-slate-200">
          {isImage ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={item.thumbnail_url || item.file_url}
              alt={item.title}
              className="w-16 h-16 object-cover"
              loading="lazy"
              onError={(e) => {
                // Failed-to-load → fall back to the type icon so the row
                // doesn't render as an empty white square.
                ;(e.currentTarget as HTMLImageElement).style.display = 'none'
              }}
            />
          ) : (
            <div className="flex flex-col items-center justify-center gap-1 text-slate-500">
              <span className="text-xl leading-none">{_MEDIA_TYPE_EMOJI[item.media_type]}</span>
              <span className="text-[10px] uppercase">{_MEDIA_TYPE_LABELS[item.media_type]}</span>
            </div>
          )}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-slate-900 truncate">{item.title}</span>
            <span className="inline-flex items-center gap-1 text-xs text-slate-600 rounded-full bg-slate-100 px-2 py-0.5">
              <_MediaIcon type={item.media_type} className="w-3 h-3" />
              {_MEDIA_TYPE_LABELS[item.media_type]}
            </span>
            {item.is_active ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 text-emerald-700 px-2 py-0.5 text-xs">
                <Power className="w-3 h-3" /> فعّال
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 text-slate-600 px-2 py-0.5 text-xs">
                <PowerOff className="w-3 h-3" /> متوقف
              </span>
            )}
            <span className="text-xs text-slate-400">أولوية {item.priority}</span>
            {item.file_size_bytes ? (
              <span className="text-xs text-slate-400">{_formatBytes(item.file_size_bytes)}</span>
            ) : null}
          </div>

          {item.description ? (
            <p className="text-xs text-slate-700 mt-1 line-clamp-2" title={item.description ?? ''}>
              {item.description}
            </p>
          ) : null}

          {usage && (
            <p className="text-xs text-slate-500 mt-1 line-clamp-2" title={usage}>
              <span className="text-slate-400">متى يستخدمه الذكاء: </span>
              {usage}
            </p>
          )}

          {!!(item.tags && item.tags.length) && (
            <div className="flex items-center gap-1 mt-1.5 flex-wrap">
              {item.tags.map((t) => (
                <span
                  key={t}
                  className="inline-flex items-center gap-1 rounded-full bg-blue-50 text-blue-700 px-2 py-0.5 text-[11px]"
                >
                  <Tag className="w-3 h-3" /> {t}
                </span>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2 mt-1.5 text-[11px] text-slate-400">
            <span className="inline-flex items-center gap-1">
              <LinkIcon className="w-3 h-3" />
              <a
                href={item.file_url}
                target="_blank"
                rel="noreferrer"
                className="hover:underline truncate max-w-[260px]"
                title={item.file_url}
              >
                {item.file_url}
              </a>
            </span>
            <span>•</span>
            <span>
              معرف للذكاء: <code className="font-mono">[MEDIA:{item.id}]</code>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button onClick={onEdit} className="text-slate-500 hover:text-slate-800" title="تعديل">
            <Edit2 className="w-4 h-4" />
          </button>
          <button onClick={onToggle} className="text-slate-500 hover:text-slate-800" title={item.is_active ? 'تعطيل' : 'تفعيل'}>
            {item.is_active ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
          </button>
          <button onClick={onDelete} className="text-red-500 hover:text-red-700" title="حذف">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </div>
    </li>
  )
}

// ── Media edit modal ────────────────────────────────────────────────────────

function MediaEditModal({
  form,
  setForm,
  uploadFile,
  setUploadFile,
  fileInputRef,
  onClose,
  onSubmit,
  saving,
  error,
}: {
  form: MediaFormState
  setForm: (f: MediaFormState) => void
  uploadFile: File | null
  setUploadFile: (f: File | null) => void
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>
  onClose: () => void
  onSubmit: () => void
  saving: boolean
  error: string | null
}) {
  const isEdit = form.id != null

  // Build a temporary object URL so the merchant can preview the file
  // they're about to upload before they commit. Revoke when the file
  // changes / the modal unmounts to avoid memory leaks.
  const previewUrl = useMemo(() => {
    if (!uploadFile) return ''
    try {
      return URL.createObjectURL(uploadFile)
    } catch {
      return ''
    }
  }, [uploadFile])
  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl)
    }
  }, [previewUrl])

  // When the merchant picks a file, infer the media_type unless they
  // already overrode it explicitly. Also surface filename as a fallback
  // title to save them a step.
  const handleFilePick = (file: File | null) => {
    setUploadFile(file)
    if (!file) return
    const inferred = _inferMediaTypeFromFile(file)
    setForm({
      ...form,
      media_type: inferred,
      title: form.title.trim() || file.name.replace(/\.[^/.]+$/, ''),
    })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <div className="bg-slate-50 rounded-2xl shadow-xl w-full max-w-2xl max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 bg-white sticky top-0 z-10">
          <div>
            <h3 className="text-base font-semibold text-slate-900">
              {isEdit ? 'تعديل وسيط' : 'إضافة وسيط للذكاء'}
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              يبني هذا النموذج «ذاكرة بصرية» لنحلة — أي ملف ترفعه هنا يصبح
              متاحاً للذكاء لإرساله تلقائياً عند الحاجة.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 disabled:opacity-50"
            disabled={saving}
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
            </div>
          )}

          {/* ── 1. Media info ─────────────────────────────────────────── */}
          <SectionCard title="معلومات الوسيط" icon={<Info className="w-4 h-4" />}>
            <Field label="عنوان الوسيط *">
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="باركود التحويل البنكي"
                className="input-base"
                autoFocus
              />
            </Field>
            <Field label="الوصف (اختياري)">
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                rows={2}
                placeholder="وصف داخلي يساعدك في تنظيم المكتبة — لا يظهر للعميل."
                className="input-base"
              />
            </Field>
            <Field label="نوع الوسيط">
              <div className="grid grid-cols-5 gap-2">
                {(['image', 'video', 'pdf', 'document', 'audio'] as AIMediaType[]).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setForm({ ...form, media_type: t })}
                    className={`rounded-lg border px-2 py-2 text-xs flex flex-col items-center gap-1 transition ${
                      form.media_type === t
                        ? 'border-blue-500 bg-blue-50 text-blue-700 ring-2 ring-blue-100'
                        : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
                    }`}
                  >
                    <span className="text-lg leading-none">{_MEDIA_TYPE_EMOJI[t]}</span>
                    <span>{_MEDIA_TYPE_LABELS[t]}</span>
                  </button>
                ))}
              </div>
            </Field>
          </SectionCard>

          {/* ── 2. Upload / URL ───────────────────────────────────────── */}
          <SectionCard
            title="رفع الوسيط"
            icon={<Upload className="w-4 h-4" />}
            hint={isEdit
              ? 'يمكنك تعديل الرابط فقط هنا. لتغيير الملف الفعلي احذف الوسيط وارفعه من جديد.'
              : 'ارفع ملفاً من جهازك مباشرة، أو ألصق رابطاً عاماً (HTTPS) إذا كان الملف مرفوعاً مسبقاً.'}
          >
            {!isEdit && (
              <>
                <div
                  className={`relative rounded-xl border-2 border-dashed p-4 text-sm transition ${
                    uploadFile
                      ? 'border-blue-400 bg-blue-50/40'
                      : 'border-slate-300 bg-white hover:border-slate-400'
                  }`}
                >
                  <input
                    ref={fileInputRef}
                    type="file"
                    className="absolute inset-0 opacity-0 cursor-pointer"
                    onChange={(e) => handleFilePick(e.target.files?.[0] ?? null)}
                    accept="image/*,video/*,audio/*,application/pdf,.pdf,.doc,.docx,.xls,.xlsx"
                  />
                  {uploadFile ? (
                    <div className="flex items-center gap-3">
                      {previewUrl && form.media_type === 'image' ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={previewUrl}
                          alt="preview"
                          className="w-24 h-24 rounded-lg object-cover border border-slate-200"
                        />
                      ) : previewUrl && form.media_type === 'video' ? (
                        <video
                          src={previewUrl}
                          className="w-32 h-24 rounded-lg object-cover border border-slate-200 bg-slate-900"
                          controls={false}
                        />
                      ) : (
                        <div className="w-24 h-24 rounded-lg bg-white border border-slate-200 flex flex-col items-center justify-center text-slate-500">
                          <span className="text-3xl leading-none">{_MEDIA_TYPE_EMOJI[form.media_type]}</span>
                          <span className="text-[10px] uppercase mt-1">{_MEDIA_TYPE_LABELS[form.media_type]}</span>
                        </div>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="font-medium text-slate-800 truncate">{uploadFile.name}</div>
                        <div className="text-xs text-slate-500 mt-0.5">
                          {_formatBytes(uploadFile.size)} • {uploadFile.type || _MEDIA_TYPE_LABELS[form.media_type]}
                        </div>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.preventDefault()
                            handleFilePick(null)
                            if (fileInputRef.current) fileInputRef.current.value = ''
                          }}
                          className="mt-2 text-xs text-red-600 hover:text-red-800"
                        >
                          إزالة وإعادة الاختيار
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center justify-center text-center text-slate-600 py-4 pointer-events-none">
                      <Upload className="w-7 h-7 mb-2" />
                      <div className="font-medium">اضغط لاختيار ملف من جهازك</div>
                      <div className="text-xs text-slate-500 mt-1">
                        صور / فيديو / PDF / صوت — حتى 100MB
                      </div>
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-3 my-2 text-xs text-slate-400">
                  <div className="flex-1 h-px bg-slate-200" />
                  <span>أو</span>
                  <div className="flex-1 h-px bg-slate-200" />
                </div>

                <Field label="رابط عام للملف (اختياري إذا رفعت ملفاً)">
                  <input
                    value={form.file_url}
                    onChange={(e) => setForm({ ...form, file_url: e.target.value })}
                    placeholder="https://example.com/image.png"
                    className="input-base"
                    disabled={!!uploadFile}
                  />
                </Field>
              </>
            )}

            {isEdit && (
              <>
                <Field label="رابط الملف">
                  <input
                    value={form.file_url}
                    onChange={(e) => setForm({ ...form, file_url: e.target.value })}
                    className="input-base"
                  />
                </Field>
                {form.media_type === 'image' && form.file_url && (
                  <div className="mt-2 rounded-lg border border-slate-200 p-2 bg-white inline-block">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={form.thumbnail_url || form.file_url}
                      alt={form.title}
                      className="max-h-40 rounded"
                    />
                  </div>
                )}
              </>
            )}

            <Field label="رابط الصورة المصغّرة (اختياري)">
              <input
                value={form.thumbnail_url}
                onChange={(e) => setForm({ ...form, thumbnail_url: e.target.value })}
                placeholder="رابط صورة مصغّرة لعرضها في القائمة"
                className="input-base"
              />
            </Field>
          </SectionCard>

          {/* ── 3. AI usage ───────────────────────────────────────────── */}
          <SectionCard
            title="ذكاء الاستخدام والسياق"
            icon={<Sparkles className="w-4 h-4" />}
            hint="نحلة تختار الوسيط بناءً على هذا النص + الوسوم — كن واضحاً ومحدداً."
          >
            <Field label="متى يستخدمه الذكاء">
              <textarea
                value={form.usage_context}
                onChange={(e) => setForm({ ...form, usage_context: e.target.value })}
                rows={3}
                placeholder="مثال: أرسله إذا طلب العميل بيانات التحويل البنكي، أو سأل عن طريقة الدفع."
                className="input-base"
              />
            </Field>
            <Field label="وسوم (مفصولة بفواصل)">
              <input
                value={form.tags}
                onChange={(e) => setForm({ ...form, tags: e.target.value })}
                placeholder="دفع، تحويل، باركود"
                className="input-base"
              />
            </Field>
          </SectionCard>

          {/* ── 4. State & priority ───────────────────────────────────── */}
          <SectionCard title="الحالة والأولوية" icon={<Power className="w-4 h-4" />}>
            <div className="grid grid-cols-2 gap-3">
              <Field label="الأولوية (الأقل = أهم)">
                <input
                  type="number"
                  value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}
                  min={0}
                  max={10000}
                  className="input-base"
                />
              </Field>
              <Field label="الحالة">
                <label className="inline-flex items-center gap-2 mt-2">
                  <input
                    type="checkbox"
                    checked={form.is_active}
                    onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                  />
                  <span className="text-sm text-slate-700">فعّال</span>
                </label>
              </Field>
            </div>
          </SectionCard>
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-slate-200 bg-white sticky bottom-0">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={saving}>
            إلغاء
          </button>
          <button
            onClick={onSubmit}
            disabled={saving}
            className="btn-primary text-sm flex items-center gap-2"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {saving ? 'جاري الحفظ…' : 'حفظ الوسيط'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Shared atoms
// ──────────────────────────────────────────────────────────────────────────

function SectionCard({
  title,
  icon,
  hint,
  children,
}: {
  title: string
  icon?: React.ReactNode
  hint?: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 space-y-3 shadow-sm">
      <header>
        <div className="flex items-center gap-2 text-slate-800 font-semibold text-sm">
          {icon ? <span className="text-blue-600">{icon}</span> : null}
          <span>{title}</span>
        </div>
        {hint ? <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">{hint}</p> : null}
      </header>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-semibold text-slate-700 mb-1.5 block">{label}</span>
      {children}
    </label>
  )
}
