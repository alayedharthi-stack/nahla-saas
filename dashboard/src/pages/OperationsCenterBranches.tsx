import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Building2, MapPin, Plus, Pencil, Trash2 } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import ConfirmModal from '../components/ui/ConfirmModal'
import { useLanguage } from '../i18n/context'
import {
  operationsCenterApi,
  type BranchInput,
  type MerchantBranch,
} from '../api/operationsCenter'

const emptyBranch: BranchInput = {
  name: '',
  city: '',
  district: '',
  address: '',
  maps_url: '',
  is_active: true,
  sort_order: 0,
}

export default function OperationsCenterBranches({
  embedded = false,
  branchLinkPrefix = '/operations-center/branches',
}: {
  embedded?: boolean
  branchLinkPrefix?: string
}) {
  const { t } = useLanguage()
  const navigate = useNavigate()
  const [branches, setBranches] = useState<MerchantBranch[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<MerchantBranch | null>(null)
  const [form, setForm] = useState<BranchInput>(emptyBranch)
  const [saving, setSaving] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<MerchantBranch | null>(null)
  const [deleteLoading, setDeleteLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await operationsCenterApi.listBranches()
      setBranches(res.branches || [])
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل الفروع')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const openCreate = () => {
    setEditing(null)
    setForm(emptyBranch)
    setModalOpen(true)
  }

  const openEdit = (branch: MerchantBranch) => {
    setEditing(branch)
    setForm({
      name: branch.name,
      city: branch.city,
      district: branch.district,
      address: branch.address,
      maps_url: branch.maps_url,
      is_active: branch.is_active,
      sort_order: branch.sort_order,
    })
    setModalOpen(true)
  }

  const save = async () => {
    if (!form.name.trim()) return
    setSaving(true)
    try {
      if (editing) {
        await operationsCenterApi.updateBranch(editing.id, form)
      } else {
        await operationsCenterApi.createBranch(form)
      }
      setModalOpen(false)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ الفرع')
    } finally {
      setSaving(false)
    }
  }

  const confirmRemove = async () => {
    if (!deleteTarget) return
    setDeleteLoading(true)
    try {
      await operationsCenterApi.deleteBranch(deleteTarget.id)
      setDeleteTarget(null)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حذف الفرع')
    } finally {
      setDeleteLoading(false)
    }
  }

  const toggleActive = async (branch: MerchantBranch) => {
    try {
      if (branch.is_active) {
        await operationsCenterApi.deactivateBranch(branch.id)
      } else {
        await operationsCenterApi.activateBranch(branch.id)
      }
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحديث الحالة')
    }
  }

  return (
    <div className="space-y-6">
      {!embedded && (
        <PageHeader
          title={t(tr => tr.pages.operationsCenter.title)}
          subtitle={t(tr => tr.pages.operationsCenter.subtitle)}
          action={
            <button type="button" className="btn-primary flex items-center gap-2" onClick={openCreate}>
              <Plus className="w-4 h-4" />
              إضافة فرع
            </button>
          }
        />
      )}

      {embedded && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-slate-600">{t(tr => tr.pages.operationsCenter.subtitle)}</p>
          <button type="button" className="btn-primary flex items-center gap-2" onClick={openCreate}>
            <Plus className="w-4 h-4" />
            إضافة فرع
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="card overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-slate-500 text-sm">جاري التحميل…</div>
        ) : branches.length === 0 ? (
          <div className="p-10 text-center">
            <Building2 className="w-10 h-10 mx-auto text-slate-300 mb-3" />
            <p className="text-slate-600 text-sm">لا توجد فروع بعد. أضف أول فرع لمتجرك.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-slate-500">
                  <th className="text-right p-3 font-medium">الفرع</th>
                  <th className="text-right p-3 font-medium">المدينة</th>
                  <th className="text-right p-3 font-medium">الحي</th>
                  <th className="text-right p-3 font-medium">الموقع</th>
                  <th className="text-right p-3 font-medium">الموظفون</th>
                  <th className="text-right p-3 font-medium">الحالة</th>
                  <th className="text-right p-3 font-medium">إجراءات</th>
                </tr>
              </thead>
              <tbody>
                {branches.map(branch => (
                  <tr key={branch.id} className="border-b border-slate-50 hover:bg-slate-50/60">
                    <td className="p-3 font-medium text-slate-900">
                      <Link
                        to={`${branchLinkPrefix}/${branch.id}`}
                        className="text-brand-600 hover:underline"
                      >
                        {branch.name}
                      </Link>
                    </td>
                    <td className="p-3 text-slate-600">{branch.city || '—'}</td>
                    <td className="p-3 text-slate-600">{branch.district || '—'}</td>
                    <td className="p-3">
                      {branch.maps_url ? (
                        <a
                          href={branch.maps_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-brand-600 hover:underline"
                        >
                          <MapPin className="w-3.5 h-3.5" />
                          خريطة
                        </a>
                      ) : '—'}
                    </td>
                    <td className="p-3 text-slate-600">{branch.contact_count}</td>
                    <td className="p-3">
                      <button type="button" onClick={() => toggleActive(branch)}>
                        <Badge
                          label={branch.is_active ? 'نشط' : 'معطّل'}
                          variant={branch.is_active ? 'green' : 'slate'}
                        />
                      </button>
                    </td>
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500"
                          title="تعديل"
                          onClick={() => openEdit(branch)}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          className="p-1.5 rounded-lg hover:bg-red-50 text-red-500"
                          title="حذف"
                          onClick={() => setDeleteTarget(branch)}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          className="text-xs text-brand-600 hover:underline"
                          onClick={() => navigate(`${branchLinkPrefix}/${branch.id}`)}
                        >
                          التفاصيل
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {modalOpen && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
          <div className="bg-white rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-xl">
            <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
              <h3 className="font-semibold text-slate-900">
                {editing ? 'تعديل فرع' : 'إضافة فرع'}
              </h3>
              <button type="button" className="text-slate-400 hover:text-slate-600" onClick={() => setModalOpen(false)}>×</button>
            </div>
            <div className="p-5 space-y-3">
              <label className="block text-sm">
                <span className="text-slate-600 mb-1 block">اسم الفرع *</span>
                <input className="input w-full" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm">
                  <span className="text-slate-600 mb-1 block">المدينة</span>
                  <input className="input w-full" value={form.city || ''} onChange={e => setForm({ ...form, city: e.target.value })} />
                </label>
                <label className="block text-sm">
                  <span className="text-slate-600 mb-1 block">الحي</span>
                  <input className="input w-full" value={form.district || ''} onChange={e => setForm({ ...form, district: e.target.value })} />
                </label>
              </div>
              <label className="block text-sm">
                <span className="text-slate-600 mb-1 block">العنوان</span>
                <textarea className="input w-full min-h-[72px]" value={form.address || ''} onChange={e => setForm({ ...form, address: e.target.value })} />
              </label>
              <label className="block text-sm">
                <span className="text-slate-600 mb-1 block">رابط Google Maps</span>
                <input className="input w-full" dir="ltr" value={form.maps_url || ''} onChange={e => setForm({ ...form, maps_url: e.target.value })} />
              </label>
            </div>
            <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2">
              <button type="button" className="btn-secondary" onClick={() => setModalOpen(false)}>إلغاء</button>
              <button type="button" className="btn-primary" disabled={saving} onClick={save}>
                {saving ? 'جاري الحفظ…' : 'حفظ'}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmModal
        open={!!deleteTarget}
        title="حذف الفرع"
        message={deleteTarget ? `هل تريد حذف فرع «${deleteTarget.name}»؟` : ''}
        confirmLabel="حذف"
        destructive
        loading={deleteLoading}
        onCancel={() => { if (!deleteLoading) setDeleteTarget(null) }}
        onConfirm={confirmRemove}
      />
    </div>
  )
}
