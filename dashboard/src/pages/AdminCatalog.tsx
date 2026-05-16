/**
 * AdminCatalog.tsx
 * ─────────────────
 * Cross-tenant audit screen for the WhatsApp Catalog wire-up.
 *
 * Reads /admin/catalog/audit and renders a sortable table with the
 * fields support needs at a glance: connection state, catalog
 * config, eligibility verdict + reason, and retailer_id coverage.
 *
 * Power actions per row:
 *  • View detailed status (loads /admin/catalog/status for that tenant)
 *  • Edit config inline (PATCH /admin/catalog/config)
 *
 * Why this lives in admin/, not in the per-tenant impersonation flow
 * ──────────────────────────────────────────────────────────────────
 * Support typically discovers the issue ("catalog isn't sending for
 * three different merchants today") by scanning a list — drilling
 * into each tenant individually would slow that down. The audit
 * endpoint serves the table directly so we don't need to fan out
 * 1 request per tenant client-side.
 */
import { useEffect, useState } from 'react'
import {
  AlertTriangle, CheckCircle2, Loader2, Pencil, RefreshCw, Send, XCircle,
} from 'lucide-react'
import {
  adminCatalogApi,
  type AdminCatalogAuditRow,
  type CatalogStatus,
} from '../api/catalog'

// ── helpers ──────────────────────────────────────────────────────────

function Bool({ ok }: { ok: boolean }) {
  return ok
    ? <CheckCircle2 className="w-4 h-4 text-emerald-600 inline" />
    : <XCircle className="w-4 h-4 text-rose-400 inline" />
}

function Pct({ value }: { value: number }) {
  const color =
    value >= 95 ? 'text-emerald-700 bg-emerald-50'
    : value >= 50 ? 'text-amber-700 bg-amber-50'
    : 'text-rose-700 bg-rose-50'
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${color}`}>
      {value.toFixed(0)}%
    </span>
  )
}

// ── Edit modal ───────────────────────────────────────────────────────

function EditModal(props: {
  tenantId: number
  currentId: string | null
  currentEnabled: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const [catalogId, setCatalogId] = useState(props.currentId ?? '')
  const [enabled, setEnabled]     = useState(props.currentEnabled)
  const [saving, setSaving]       = useState(false)
  const [error, setError]         = useState<string | null>(null)

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await adminCatalogApi.patch({
        tenant_id:       props.tenantId,
        meta_catalog_id: catalogId.trim(),
        catalog_enabled: enabled,
      })
      props.onSaved()
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر حفظ الإعدادات.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 p-4" onClick={props.onClose}>
      <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full p-6 space-y-4" dir="rtl" onClick={e => e.stopPropagation()}>
        <h3 className="text-lg font-bold text-slate-900">
          تعديل كتالوج التاجر <span className="text-emerald-600">#{props.tenantId}</span>
        </h3>

        {error && (
          <div className="bg-rose-50 border border-rose-200 text-rose-800 text-sm rounded-lg p-3">
            {error}
          </div>
        )}

        <div>
          <label className="block text-sm font-semibold text-slate-700 mb-1">Catalog ID</label>
          <input
            dir="ltr"
            value={catalogId}
            onChange={e => setCatalogId(e.target.value)}
            className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-400"
          />
        </div>

        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
          تفعيل الكتالوج
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <button onClick={props.onClose} className="px-4 py-2 rounded-xl text-sm text-slate-600 hover:bg-slate-100">إلغاء</button>
          <button
            onClick={save}
            disabled={saving}
            className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            حفظ
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Detail modal ─────────────────────────────────────────────────────

function DetailModal(props: { tenantId: number; onClose: () => void }) {
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    adminCatalogApi.status(props.tenantId).then(s => {
      if (alive) { setStatus(s); setLoading(false) }
    }).catch(() => alive && setLoading(false))
    return () => { alive = false }
  }, [props.tenantId])

  return (
    <div className="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 p-4" onClick={props.onClose}>
      <div className="bg-white rounded-2xl shadow-2xl max-w-2xl w-full p-6 max-h-[85vh] overflow-y-auto" dir="rtl" onClick={e => e.stopPropagation()}>
        <h3 className="text-lg font-bold text-slate-900 mb-3">
          تفاصيل كتالوج التاجر #{props.tenantId}
        </h3>
        {loading && <div className="text-sm text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> جاري التحميل...</div>}
        {status && (
          <pre className="text-xs bg-slate-50 border border-slate-100 rounded-lg p-3 overflow-x-auto" dir="ltr">
            {JSON.stringify(status, null, 2)}
          </pre>
        )}
        <div className="flex justify-end mt-4">
          <button onClick={props.onClose} className="px-4 py-2 rounded-xl text-sm bg-slate-900 text-white">إغلاق</button>
        </div>
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────

export default function AdminCatalog() {
  const [rows, setRows]         = useState<AdminCatalogAuditRow[]>([])
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState<string | null>(null)
  const [onlyConnected, setOnlyConnected] = useState(true)
  const [editTenant, setEditTenant] = useState<AdminCatalogAuditRow | null>(null)
  const [detailTenant, setDetailTenant] = useState<number | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await adminCatalogApi.audit(onlyConnected, 500)
      setRows(res.rows)
    } catch (e: any) {
      setError(e?.message ?? 'تعذّر جلب قائمة الكتالوج.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [onlyConnected])

  return (
    <div className="p-6 space-y-5 max-w-7xl mx-auto" dir="rtl">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-black text-slate-900">تدقيق كتالوج واتساب</h1>
          <p className="text-sm text-slate-500 mt-1">
            عرض شامل لكل التجار: حالة الربط، تفعيل الكتالوج، تغطية retailer_id،
            وسبب الفشل إن وُجد.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input
              type="checkbox"
              checked={onlyConnected}
              onChange={e => setOnlyConnected(e.target.checked)}
            />
            المتصلون فقط
          </label>
          <button
            onClick={load}
            disabled={loading}
            className="inline-flex items-center gap-1.5 bg-white border border-slate-200 hover:bg-slate-50 text-slate-700 px-3 py-1.5 rounded-xl text-sm"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            تحديث
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded-xl px-4 py-3 flex items-start gap-2">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <p className="text-sm">{error}</p>
        </div>
      )}

      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-xs text-slate-500 uppercase">
              <tr>
                <th className="text-right px-3 py-2.5">#</th>
                <th className="text-right px-3 py-2.5">التاجر</th>
                <th className="text-right px-3 py-2.5">واتساب</th>
                <th className="text-right px-3 py-2.5">مُفعّل</th>
                <th className="text-right px-3 py-2.5">Catalog ID</th>
                <th className="text-right px-3 py-2.5">جاهز؟</th>
                <th className="text-right px-3 py-2.5">السبب</th>
                <th className="text-right px-3 py-2.5">المنتجات</th>
                <th className="text-right px-3 py-2.5">تغطية</th>
                <th className="text-right px-3 py-2.5">إجراءات</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.tenant_id} className="border-t border-slate-100 hover:bg-slate-50/60">
                  <td className="px-3 py-2 text-slate-500">{r.tenant_id}</td>
                  <td className="px-3 py-2 text-slate-800 font-medium">{r.merchant_name ?? '—'}</td>
                  <td className="px-3 py-2"><Bool ok={r.whatsapp_connected} /></td>
                  <td className="px-3 py-2"><Bool ok={r.catalog_enabled} /></td>
                  <td className="px-3 py-2"><Bool ok={r.meta_catalog_id_set} /></td>
                  <td className="px-3 py-2"><Bool ok={r.eligibility_ok} /></td>
                  <td className="px-3 py-2 text-xs text-slate-500">{r.eligibility_reason}</td>
                  <td className="px-3 py-2 text-xs text-slate-700">{r.products_with_rid}/{r.products_total}</td>
                  <td className="px-3 py-2"><Pct value={r.products_with_rid_pct} /></td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <button
                      onClick={() => setDetailTenant(r.tenant_id)}
                      className="inline-flex items-center gap-1 text-slate-600 hover:text-slate-900 text-xs px-2 py-1 rounded-md hover:bg-slate-100"
                      title="عرض التفاصيل"
                    >
                      <Send className="w-3.5 h-3.5" /> تفاصيل
                    </button>
                    <button
                      onClick={() => setEditTenant(r)}
                      className="inline-flex items-center gap-1 text-emerald-700 hover:text-emerald-900 text-xs px-2 py-1 rounded-md hover:bg-emerald-50"
                      title="تعديل"
                    >
                      <Pencil className="w-3.5 h-3.5" /> تعديل
                    </button>
                  </td>
                </tr>
              ))}
              {!rows.length && !loading && (
                <tr>
                  <td colSpan={10} className="text-center text-sm text-slate-500 py-10">
                    لا توجد سجلات حاليّاً.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {editTenant && (
        <EditModal
          tenantId={editTenant.tenant_id}
          currentId={editTenant.meta_catalog_id_set ? '' : ''}
          currentEnabled={editTenant.catalog_enabled}
          onClose={() => setEditTenant(null)}
          onSaved={() => { setEditTenant(null); void load() }}
        />
      )}
      {detailTenant !== null && (
        <DetailModal tenantId={detailTenant} onClose={() => setDetailTenant(null)} />
      )}
    </div>
  )
}
