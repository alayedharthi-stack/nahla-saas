/**
 * AdminTools — أدوات مالك المنصة
 *
 * تحتوي على:
 *  1. قائمة المتاجر المكررة (نفس store_id من سلة)
 *  2. دمج التكرارات وحذف الزائد
 *  3. فصل واتساب قسراً لأي متجر
 */
import { useEffect, useState } from 'react'
import {
  Wrench, AlertTriangle, RefreshCw, ChevronDown, ChevronUp,
  Trash2, WifiOff, CheckCircle, Loader2, Copy,
} from 'lucide-react'
import { apiCall } from '../api/client'

// ── Types ──────────────────────────────────────────────────────────────────

interface DuplicateEntry {
  integration_id: number
  tenant_id:      number
  tenant_name:    string
  is_active:      boolean
  store_id:       string
  store_name:     string
  enabled:        boolean
  created_at:     string | null
  wa_status:      string
  wa_connected:   boolean
  user_count:     number
  user_emails:    string[]
}

interface DuplicateGroup {
  store_id: string
  count:    number
  entries:  DuplicateEntry[]
}

interface DuplicatesResponse {
  total_duplicate_groups: number
  total_extra_tenants:    number
  groups:                 DuplicateGroup[]
}

// ── Helpers ────────────────────────────────────────────────────────────────

function copyText(text: string) {
  navigator.clipboard.writeText(text).catch(() => {})
}

// ── Sub-components ──────────────────────────────────────────────────────────

function WaBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    connected:     { label: 'مربوط',      cls: 'bg-emerald-100 text-emerald-700' },
    disconnected:  { label: 'مفصول',      cls: 'bg-slate-100 text-slate-500' },
    not_connected: { label: 'غير مربوط',  cls: 'bg-slate-100 text-slate-400' },
    pending:       { label: 'معلق',       cls: 'bg-yellow-100 text-yellow-700' },
  }
  const s = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

// ── Duplicate Group Card ───────────────────────────────────────────────────

function DuplicateGroupCard({
  group,
  onFixed,
  onDisconnectWA,
}: {
  group: DuplicateGroup
  onFixed: () => void
  onDisconnectWA: (tenantId: number) => void
}) {
  const [expanded, setExpanded]   = useState(false)
  const [keepId, setKeepId]       = useState<number>(Math.max(...group.entries.map(e => e.tenant_id)))
  const [dryRun, setDryRun]       = useState(true)
  const [fixing, setFixing]       = useState(false)
  const [result, setResult]       = useState<string | null>(null)

  const handleFix = async () => {
    if (!dryRun && !window.confirm(
      `سيتم حذف ${group.count - 1} متجر مكرر لـ store_id: ${group.store_id}\nوالاحتفاظ بـ Tenant #${keepId}.\n\nتأكيد؟`
    )) return

    setFixing(true)
    setResult(null)
    try {
      const data = await apiCall<{ delete_tenant_ids?: number[]; keep_tenant_id?: number; deleted_tenant_ids?: number[] }>(
        '/admin/tools/fix-duplicates',
        { method: 'POST', body: JSON.stringify({ store_id: group.store_id, keep_tenant_id: keepId, dry_run: dryRun }) },
      )
      if (dryRun) {
        setResult(`[معاينة] سيُحذف: ${(data.delete_tenant_ids ?? []).join(', ')} — سيُحتفظ بـ: ${data.keep_tenant_id}`)
      } else {
        setResult(`✅ تم الحذف — تمت إزالة: ${(data.deleted_tenant_ids ?? []).join(', ')}`)
        onFixed()
      }
    } catch (e: unknown) {
      setResult(`❌ ${e instanceof Error ? e.message : 'حدث خطأ'}`)
    } finally {
      setFixing(false)
    }
  }

  return (
    <div className="bg-white border border-orange-200 rounded-2xl overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-orange-50/50 transition text-right"
      >
        <div className="flex items-center gap-3 min-w-0">
          <AlertTriangle className="w-4 h-4 text-orange-500 shrink-0" />
          <div className="min-w-0">
            <p className="font-bold text-slate-800 text-sm truncate">{group.entries[0]?.store_name || group.store_id}</p>
            <p className="text-[10px] text-slate-400 font-mono">{group.store_id}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs font-bold text-orange-600 bg-orange-100 px-2 py-0.5 rounded-full">
            {group.count} نسخ
          </span>
          {expanded ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-orange-100 p-4 space-y-4">
          {/* Entries table */}
          <div className="overflow-x-auto rounded-xl border border-slate-100">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-50">
                  {['احتفاظ', 'Tenant ID', 'اسم المتجر', 'واتساب', 'المستخدمون', 'تاريخ الإنشاء', 'إجراء'].map(h => (
                    <th key={h} className="text-right px-3 py-2 text-slate-500 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {group.entries.map(e => (
                  <tr key={e.tenant_id} className={`${keepId === e.tenant_id ? 'bg-emerald-50' : ''}`}>
                    <td className="px-3 py-2">
                      <input
                        type="radio"
                        name={`keep-${group.store_id}`}
                        checked={keepId === e.tenant_id}
                        onChange={() => setKeepId(e.tenant_id)}
                        className="accent-emerald-500"
                      />
                    </td>
                    <td className="px-3 py-2">
                      <span className="font-mono text-slate-700 flex items-center gap-1">
                        #{e.tenant_id}
                        <button onClick={() => copyText(String(e.tenant_id))} className="text-slate-300 hover:text-slate-500">
                          <Copy className="w-3 h-3" />
                        </button>
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-700 font-medium">{e.tenant_name}</td>
                    <td className="px-3 py-2"><WaBadge status={e.wa_status} /></td>
                    <td className="px-3 py-2 text-slate-500">{e.user_count} مستخدم</td>
                    <td className="px-3 py-2 text-slate-400">
                      {e.created_at ? new Date(e.created_at).toLocaleDateString('ar-SA') : '—'}
                    </td>
                    <td className="px-3 py-2">
                      {e.wa_status !== 'not_connected' && (
                        <button
                          onClick={() => onDisconnectWA(e.tenant_id)}
                          title="فصل واتساب"
                          className="p-1 rounded-lg text-orange-400 hover:text-orange-600 hover:bg-orange-50 transition"
                        >
                          <WifiOff className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Fix controls */}
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={e => setDryRun(e.target.checked)}
                className="accent-amber-500 w-3.5 h-3.5"
              />
              معاينة فقط (بدون حذف)
            </label>

            <button
              onClick={handleFix}
              disabled={fixing}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold transition disabled:opacity-60
                ${dryRun
                  ? 'bg-slate-100 hover:bg-slate-200 text-slate-700'
                  : 'bg-red-500 hover:bg-red-600 text-white'
                }`}
            >
              {fixing
                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> جارٍ...</>
                : dryRun
                  ? <><RefreshCw className="w-3.5 h-3.5" /> معاينة</>
                  : <><Trash2 className="w-3.5 h-3.5" /> حذف التكرارات</>
              }
            </button>
          </div>

          {result && (
            <div className={`text-xs px-3 py-2 rounded-xl ${result.startsWith('✅') ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : result.startsWith('[معاينة]') ? 'bg-slate-50 text-slate-700 border border-slate-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
              {result}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Force Disconnect WA Panel ──────────────────────────────────────────────

function ForceDisconnectPanel() {
  const [tenantId, setTenantId] = useState('')
  const [busy, setBusy]         = useState(false)
  const [msg, setMsg]           = useState<{ ok: boolean; text: string } | null>(null)

  const handleDisconnect = async () => {
    const tid = parseInt(tenantId.trim())
    if (!tid || isNaN(tid)) { setMsg({ ok: false, text: 'أدخل Tenant ID صحيح' }); return }
    if (!window.confirm(`فصل واتساب للـ Tenant #${tid}؟ سيتم حذف WABA_ID و PHONE_NUMBER_ID و ACCESS_TOKEN.`)) return
    setBusy(true); setMsg(null)
    try {
      await apiCall(`/admin/whatsapp/disconnect/${tid}`, { method: 'POST' })
      setMsg({ ok: true, text: `✅ تم فصل واتساب للـ Tenant #${tid} بنجاح` })
      setTenantId('')
    } catch (e: unknown) {
      setMsg({ ok: false, text: `❌ ${e instanceof Error ? e.message : 'حدث خطأ'}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white border border-slate-200 rounded-2xl p-5 space-y-4">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-orange-100 flex items-center justify-center">
          <WifiOff className="w-4 h-4 text-orange-600" />
        </div>
        <div>
          <p className="font-bold text-slate-800 text-sm">فصل واتساب قسراً</p>
          <p className="text-xs text-slate-400">يمسح WABA_ID وPHONE_NUMBER_ID وACCESS_TOKEN</p>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          type="number"
          value={tenantId}
          onChange={e => setTenantId(e.target.value)}
          placeholder="Tenant ID"
          className="flex-1 border border-slate-200 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-orange-400"
          dir="ltr"
        />
        <button
          onClick={handleDisconnect}
          disabled={busy || !tenantId.trim()}
          className="flex items-center gap-1.5 px-4 py-2 bg-orange-500 hover:bg-orange-600 disabled:opacity-60 text-white font-bold rounded-xl text-sm transition"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <WifiOff className="w-4 h-4" />}
          فصل
        </button>
      </div>

      {msg && (
        <div className={`text-xs px-3 py-2 rounded-xl border ${msg.ok ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-red-50 text-red-700 border-red-200'}`}>
          {msg.text}
        </div>
      )}
    </div>
  )
}

// ── Delete Tenant Panel ────────────────────────────────────────────────────

function DeleteTenantPanel() {
  const [tenantId, setTenantId] = useState('')
  const [busy, setBusy]         = useState(false)
  const [msg, setMsg]           = useState<{ ok: boolean; text: string } | null>(null)

  const handleDelete = async () => {
    const tid = parseInt(tenantId.trim())
    if (!tid || isNaN(tid)) { setMsg({ ok: false, text: 'أدخل Tenant ID صحيح' }); return }
    if (!window.confirm(
      `⚠️ تحذير شديد: سيتم حذف Tenant #${tid} وجميع بياناته نهائياً!\n\nيشمل: المستخدمون، التكاملات، واتساب، الطلبات، المحادثات.\n\nلا يمكن التراجع. تأكيد؟`
    )) return
    setBusy(true); setMsg(null)
    try {
      await apiCall(`/admin/tenants/${tid}`, { method: 'DELETE' })
      setMsg({ ok: true, text: `✅ تم حذف Tenant #${tid} بنجاح` })
      setTenantId('')
    } catch (e: unknown) {
      setMsg({ ok: false, text: `❌ ${e instanceof Error ? e.message : 'حدث خطأ'}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white border border-red-200 rounded-2xl p-5 space-y-4">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-red-100 flex items-center justify-center">
          <Trash2 className="w-4 h-4 text-red-600" />
        </div>
        <div>
          <p className="font-bold text-slate-800 text-sm">حذف متجر نهائياً</p>
          <p className="text-xs text-slate-400">يحذف Tenant وكل بياناته (لا يمكن التراجع)</p>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          type="number"
          value={tenantId}
          onChange={e => setTenantId(e.target.value)}
          placeholder="Tenant ID"
          className="flex-1 border border-slate-200 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
          dir="ltr"
        />
        <button
          onClick={handleDelete}
          disabled={busy || !tenantId.trim()}
          className="flex items-center gap-1.5 px-4 py-2 bg-red-500 hover:bg-red-600 disabled:opacity-60 text-white font-bold rounded-xl text-sm transition"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          حذف
        </button>
      </div>

      {msg && (
        <div className={`text-xs px-3 py-2 rounded-xl border ${msg.ok ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-red-50 text-red-700 border-red-200'}`}>
          {msg.text}
        </div>
      )}
    </div>
  )
}

// ── Main Page ───────────────────────────────────────────────────────────────

export default function AdminTools() {
  // loading starts TRUE so the spinner shows immediately — never a blank frame
  const [loading, setLoading]       = useState(true)
  const [data, setData]             = useState<DuplicatesResponse | null>(null)
  const [error, setError]           = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState(false)

  const fetchDuplicates = async () => {
    setLoading(true); setError(null)
    try {
      const result = await apiCall<DuplicatesResponse>('/admin/tools/duplicate-salla-stores')
      setData(result)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'خطأ في تحميل البيانات')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    console.log('[AdminTools] mounted — fetching duplicates')
    fetchDuplicates()
  }, [])

  const handleForceDisconnectWA = async (tenantId: number) => {
    if (!window.confirm(`فصل واتساب للـ Tenant #${tenantId}؟`)) return
    setDisconnecting(true)
    try {
      await apiCall(`/admin/whatsapp/disconnect/${tenantId}`, { method: 'POST' })
      alert(`✅ تم فصل واتساب للـ Tenant #${tenantId}`)
      fetchDuplicates()
    } catch (e: unknown) {
      alert(`❌ ${e instanceof Error ? e.message : 'حدث خطأ'}`)
    } finally {
      setDisconnecting(false)
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto" dir="rtl">

      {/* ── Header — always visible, never conditional ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-600 flex items-center justify-center shadow-lg shadow-violet-500/30">
            <Wrench className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">أدوات إدارة المنصة</h1>
            <p className="text-slate-400 text-xs">تنظيف التكرارات، فصل واتساب، حذف المتاجر</p>
          </div>
        </div>
        <button
          onClick={fetchDuplicates}
          disabled={loading}
          className="flex items-center gap-1.5 px-4 py-2 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition disabled:opacity-60"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          تحديث
        </button>
      </div>

      {/* ── Quick action panels — always visible ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ForceDisconnectPanel />
        <DeleteTenantPanel />
      </div>

      {/* ── Duplicates section ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-black text-slate-800 text-base">المتاجر المكررة</h2>
          {data && !loading && (
            <div className="flex items-center gap-3">
              {data.total_duplicate_groups === 0 ? (
                <span className="flex items-center gap-1.5 text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 px-3 py-1 rounded-full font-medium">
                  <CheckCircle className="w-3.5 h-3.5" /> لا توجد تكرارات
                </span>
              ) : (
                <>
                  <span className="text-xs text-orange-700 bg-orange-50 border border-orange-200 px-3 py-1 rounded-full font-medium">
                    {data.total_duplicate_groups} متجر مكرر
                  </span>
                  <span className="text-xs text-red-700 bg-red-50 border border-red-200 px-3 py-1 rounded-full font-medium">
                    {data.total_extra_tenants} متجر زائد
                  </span>
                </>
              )}
            </div>
          )}
        </div>

        {/* Loading state */}
        {loading && (
          <div className="flex items-center justify-center h-32 text-slate-400 bg-white border border-slate-100 rounded-2xl">
            <Loader2 className="w-6 h-6 animate-spin ml-2 text-violet-500" />
            <span className="text-sm">جارٍ البحث عن التكرارات...</span>
          </div>
        )}

        {/* Error state — explicit, never silent */}
        {!loading && error && (
          <div className="bg-red-50 border border-red-200 rounded-2xl px-4 py-4 space-y-2">
            <p className="text-sm font-bold text-red-700">تعذّر تحميل البيانات</p>
            <p className="text-xs text-red-600 font-mono break-all">{error}</p>
            <button
              onClick={fetchDuplicates}
              className="mt-2 flex items-center gap-1.5 text-xs text-red-700 border border-red-300 rounded-lg px-3 py-1.5 hover:bg-red-100 transition"
            >
              <RefreshCw className="w-3.5 h-3.5" /> إعادة المحاولة
            </button>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && data?.total_duplicate_groups === 0 && (
          <div className="bg-emerald-50 border border-emerald-200 rounded-2xl px-5 py-6 text-center">
            <CheckCircle className="w-8 h-8 text-emerald-500 mx-auto mb-2" />
            <p className="text-sm font-bold text-emerald-800">لا توجد متاجر مكررة</p>
            <p className="text-xs text-emerald-600 mt-1">جميع store_ids فريدة — المنصة نظيفة ✨</p>
          </div>
        )}

        {/* Not-yet-loaded placeholder (before first fetch completes) */}
        {!loading && !error && !data && (
          <div className="bg-slate-50 border border-slate-200 rounded-2xl px-5 py-6 text-center">
            <p className="text-sm text-slate-400">اضغط «تحديث» لتحميل البيانات</p>
          </div>
        )}

        {/* Results */}
        {!loading && !error && data && data.groups.length > 0 && (
          <div className="space-y-3">
            {data.groups.map(group => (
              <DuplicateGroupCard
                key={group.store_id}
                group={group}
                onFixed={fetchDuplicates}
                onDisconnectWA={tenantId => {
                  if (!disconnecting) handleForceDisconnectWA(tenantId)
                }}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
