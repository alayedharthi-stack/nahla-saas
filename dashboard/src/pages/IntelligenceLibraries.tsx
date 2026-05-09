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

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  Edit2,
  FileText,
  Image as ImageIcon,
  Link as LinkIcon,
  Loader2,
  Music,
  Plus,
  Power,
  PowerOff,
  Save,
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
      if (form.id == null) {
        await intelligenceLibrariesApi.createManualCoupon(payload)
      } else {
        await intelligenceLibrariesApi.updateManualCoupon(form.id, payload)
      }
      setFormOpen(false)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر حفظ الكوبون')
    } finally {
      setSaving(false)
    }
  }

  const toggle = async (row: ManualCoupon) => {
    try {
      await intelligenceLibrariesApi.toggleManualCoupon(row.id)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تغيير الحالة')
    }
  }

  const remove = async (row: ManualCoupon) => {
    if (!window.confirm(`حذف الكوبون "${row.code}"؟`)) return
    try {
      await intelligenceLibrariesApi.deleteManualCoupon(row.id)
      await load()
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
}: {
  form: CouponFormState
  setForm: (f: CouponFormState) => void
  onClose: () => void
  onSubmit: () => void
  saving: boolean
}) {
  const isEdit = form.id != null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200">
          <h3 className="text-base font-semibold text-slate-900">
            {isEdit ? 'تعديل كوبون يدوي' : 'إضافة كوبون يدوي'}
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-3">
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
              className="input-base"
            />
          </Field>
          <Field label="متى يستخدمه الذكاء">
            <textarea
              value={form.usage_context}
              onChange={(e) => setForm({ ...form, usage_context: e.target.value })}
              rows={2}
              placeholder="إذا طلب العميل خصم أو كوبون. إذا كان متردد. إذا كانت السلة عالية."
              className="input-base"
            />
          </Field>
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

function _MediaIcon({ type, className = 'w-4 h-4' }: { type: AIMediaType; className?: string }) {
  if (type === 'image') return <ImageIcon className={className} />
  if (type === 'video') return <VideoIcon className={className} />
  if (type === 'audio') return <Music className={className} />
  return <FileText className={className} />
}

export function AIMediaLibraryPanel() {
  const [items, setItems] = useState<AIMediaItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
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
    setFormOpen(true)
  }

  const submit = async () => {
    const title = form.title.trim()
    if (!title) {
      setError('عنوان الملف مطلوب')
      return
    }
    setSaving(true)
    setError(null)
    const tagsArr = form.tags
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    try {
      if (form.id == null) {
        // Create flow — either upload a file or save an external URL.
        if (uploadFile) {
          await intelligenceLibrariesApi.uploadAIMedia({
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
            setError('ارفع ملف أو الصق رابطاً عاماً للوسيط')
            setSaving(false)
            return
          }
          await intelligenceLibrariesApi.createAIMedia({
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
      } else {
        await intelligenceLibrariesApi.updateAIMedia(form.id, {
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
      }
      setFormOpen(false)
      setUploadFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر حفظ الوسيط')
    } finally {
      setSaving(false)
    }
  }

  const toggle = async (row: AIMediaItem) => {
    try {
      await intelligenceLibrariesApi.toggleAIMedia(row.id)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تغيير الحالة')
    }
  }

  const remove = async (row: AIMediaItem) => {
    if (!window.confirm(`حذف الوسيط "${row.title}"؟`)) return
    try {
      await intelligenceLibrariesApi.deleteAIMedia(row.id)
      await load()
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
            لا توجد وسائط بعد. ارفع صورة أو ملف PDF واكتب &quot;متى يستخدمه الذكاء&quot;
            ليتم إرفاقه تلقائياً مع ردود نحلة.
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {items.map((m) => {
              const usage = m.usage_context ?? ''
              return (
                <li key={m.id} className={`px-4 py-3 ${m.is_active ? '' : 'opacity-60'}`}>
                  <div className="flex items-start gap-3">
                    <div className="w-12 h-12 rounded-lg bg-slate-100 flex items-center justify-center text-slate-500 shrink-0">
                      {m.media_type === 'image' && m.file_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={m.thumbnail_url || m.file_url}
                          alt={m.title}
                          className="w-12 h-12 rounded-lg object-cover"
                        />
                      ) : (
                        <_MediaIcon type={m.media_type} className="w-5 h-5" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-semibold text-slate-900 truncate">{m.title}</span>
                        <span className="inline-flex items-center gap-1 text-xs text-slate-500 rounded-full bg-slate-100 px-2 py-0.5">
                          <_MediaIcon type={m.media_type} className="w-3 h-3" />
                          {_MEDIA_TYPE_LABELS[m.media_type]}
                        </span>
                        {m.is_active ? (
                          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 text-emerald-700 px-2 py-0.5 text-xs">
                            <Power className="w-3 h-3" /> فعّال
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 text-slate-600 px-2 py-0.5 text-xs">
                            <PowerOff className="w-3 h-3" /> متوقف
                          </span>
                        )}
                        <span className="text-xs text-slate-400">أولوية {m.priority}</span>
                      </div>
                      {usage && (
                        <p className="text-xs text-slate-600 mt-1 line-clamp-2" title={usage}>
                          {usage}
                        </p>
                      )}
                      {!!(m.tags && m.tags.length) && (
                        <div className="flex items-center gap-1 mt-1.5 flex-wrap">
                          {m.tags.map((t) => (
                            <span
                              key={t}
                              className="inline-flex items-center gap-1 rounded-full bg-blue-50 text-blue-700 px-2 py-0.5 text-[11px]"
                            >
                              <Tag className="w-3 h-3" /> {t}
                            </span>
                          ))}
                        </div>
                      )}
                      <div className="flex items-center gap-1 mt-1 text-[11px] text-slate-400 truncate">
                        <LinkIcon className="w-3 h-3" />
                        <a
                          href={m.file_url}
                          target="_blank"
                          rel="noreferrer"
                          className="hover:underline truncate"
                          title={m.file_url}
                        >
                          {m.file_url}
                        </a>
                        <span className="ms-2">معرف للذكاء: <code>[MEDIA:{m.id}]</code></span>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button onClick={() => openEdit(m)} className="text-slate-500 hover:text-slate-800" title="تعديل">
                        <Edit2 className="w-4 h-4" />
                      </button>
                      <button onClick={() => toggle(m)} className="text-slate-500 hover:text-slate-800" title={m.is_active ? 'تعطيل' : 'تفعيل'}>
                        {m.is_active ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                      </button>
                      <button onClick={() => remove(m)} className="text-red-500 hover:text-red-700" title="حذف">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                </li>
              )
            })}
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
          onClose={() => { setFormOpen(false); setUploadFile(null) }}
          onSubmit={submit}
          saving={saving}
        />
      )}
    </div>
  )
}

function MediaEditModal({
  form,
  setForm,
  uploadFile,
  setUploadFile,
  fileInputRef,
  onClose,
  onSubmit,
  saving,
}: {
  form: MediaFormState
  setForm: (f: MediaFormState) => void
  uploadFile: File | null
  setUploadFile: (f: File | null) => void
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>
  onClose: () => void
  onSubmit: () => void
  saving: boolean
}) {
  const isEdit = form.id != null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200">
          <h3 className="text-base font-semibold text-slate-900">
            {isEdit ? 'تعديل وسيط' : 'إضافة وسيط للذكاء'}
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-3">
          <Field label="العنوان *">
            <input
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="باركود التحويل البنكي"
              className="input-base"
            />
          </Field>
          <Field label="الوصف">
            <textarea
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              rows={2}
              className="input-base"
            />
          </Field>

          <Field label="نوع الوسيط">
            <select
              value={form.media_type}
              onChange={(e) => setForm({ ...form, media_type: e.target.value as AIMediaType })}
              className="input-base"
            >
              <option value="image">صورة</option>
              <option value="video">فيديو</option>
              <option value="pdf">PDF</option>
              <option value="document">ملف</option>
              <option value="audio">صوت</option>
            </select>
          </Field>

          {!isEdit && (
            <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-3 text-sm">
              <div className="font-medium text-slate-700 mb-1.5">اختر طريقة إضافة الوسيط:</div>
              <label className="flex items-center gap-2 cursor-pointer text-slate-700">
                <Upload className="w-4 h-4" />
                <span>رفع ملف من جهازي</span>
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
                />
                {uploadFile && <span className="text-xs text-slate-500 ms-2">({uploadFile.name})</span>}
              </label>
              <div className="mt-2">
                <Field label="أو ألصق رابطاً عاماً (اختياري إذا كنت ترفع ملفاً)">
                  <input
                    value={form.file_url}
                    onChange={(e) => setForm({ ...form, file_url: e.target.value })}
                    placeholder="https://example.com/image.png"
                    className="input-base"
                  />
                </Field>
              </div>
            </div>
          )}
          {isEdit && (
            <Field label="رابط الملف">
              <input
                value={form.file_url}
                onChange={(e) => setForm({ ...form, file_url: e.target.value })}
                className="input-base"
              />
            </Field>
          )}

          <Field label="رابط الصورة المصغّرة (اختياري)">
            <input
              value={form.thumbnail_url}
              onChange={(e) => setForm({ ...form, thumbnail_url: e.target.value })}
              className="input-base"
            />
          </Field>

          <Field label="متى يستخدمه الذكاء">
            <textarea
              value={form.usage_context}
              onChange={(e) => setForm({ ...form, usage_context: e.target.value })}
              rows={2}
              placeholder="إذا طلب العميل بيانات التحويل البنكي. إذا سأل عن عسل السمر."
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
// Shared atoms
// ──────────────────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-600 mb-1 block">{label}</span>
      {children}
    </label>
  )
}
