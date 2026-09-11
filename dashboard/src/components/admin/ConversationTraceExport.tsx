import { useEffect, useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { apiCall } from '../../api/client'
import { isAdmin } from '../../auth'

interface TraceExport {
  schema_version: number
  tenant_id: number
  conversation_id: number
  messages: unknown[]
  has_more_messages: boolean
}

export default function ConversationTraceExport() {
  const [tenant, setTenant] = useState('')
  const [conversation, setConversation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [file, setFile] = useState<{ url: string; name: string; count: number; hasMore: boolean } | null>(null)

  useEffect(() => () => { if (file) URL.revokeObjectURL(file.url) }, [file])

  if (!isAdmin()) return null

  const prepare = async (event: React.FormEvent) => {
    event.preventDefault()
    const tenantId = Number(tenant), conversationId = Number(conversation)
    if (![tenantId, conversationId].every(n => Number.isSafeInteger(n) && n > 0)) {
      setError('أدخل رقم متجر ورقم محادثة صحيحين.')
      return
    }
    setLoading(true); setError(''); setFile(null)
    try {
      const params = new URLSearchParams({ tenant_id: String(tenantId), conversation_id: String(conversationId) })
      const data = await apiCall<TraceExport>(`/admin/debug/conversation-trace-export?${params}`)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
      setFile({ url: URL.createObjectURL(blob), name: `nahla-trace-${tenantId}-${conversationId}.json`,
        count: data.messages.length, hasMore: data.has_more_messages })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر تجهيز ملف التشخيص.')
    } finally {
      setLoading(false)
    }
  }

  return <section className="bg-white border border-slate-200 rounded-2xl p-4 space-y-3">
    <h2 className="font-bold text-slate-800">تصدير تشخيص محادثة</h2>
    <p className="text-xs text-slate-500">آخر 200 رسالة مع مصدر الرد والموديل والتحويلات المسجّلة. البيانات غير المسجّلة تبقى غير معروفة.</p>
    <form onSubmit={prepare} className="flex flex-wrap items-end gap-3">
      <label className="text-xs text-slate-600 space-y-1">
        <span className="block">رقم المتجر</span>
        <input type="number" inputMode="numeric" min="1" step="1" required disabled={loading}
          value={tenant} onChange={e => { setTenant(e.target.value); setFile(null); setError('') }}
          className="input w-36" />
      </label>
      <label className="text-xs text-slate-600 space-y-1">
        <span className="block">رقم المحادثة</span>
        <input type="number" inputMode="numeric" min="1" step="1" required disabled={loading}
          value={conversation} onChange={e => { setConversation(e.target.value); setFile(null); setError('') }}
          className="input w-36" />
      </label>
      <button type="submit" disabled={loading} className="flex items-center gap-2 rounded-xl px-4 py-2 bg-violet-600 text-white text-sm disabled:opacity-60">
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {loading ? 'جارٍ تجهيز الملف…' : 'تصدير التشخيص'}
      </button>
    </form>
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {file && <div role="status" className="text-sm space-y-2">
      <p>{file.hasMore ? 'جُهّزت آخر' : 'جُهّزت'} {file.count} رسالة. حالة المحادثة الحالية لا تمثّل بالضرورة حالتها وقت الردود السابقة.</p>
      <a href={file.url} download={file.name} className="inline-flex items-center gap-2 font-bold text-violet-700 underline">
        <Download className="w-4 h-4" /> تنزيل ملف التشخيص
      </a>
    </div>}
  </section>
}
