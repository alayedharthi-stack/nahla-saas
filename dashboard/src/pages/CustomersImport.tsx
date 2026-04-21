// ── Customers Import Wizard ──────────────────────────────────────────────────
// Four-step flow:
//   1) رفع الملف           → POST /customers/import/upload
//   2) مطابقة الأعمدة      → POST /customers/import/{id}/mapping
//   3) المعاينة + التكرار  → GET  /customers/import/{id}/rows?status=...
//   4) تأكيد الاستيراد     → POST /customers/import/{id}/commit
//
// State is kept in component memory; the heavy lifting (parsing,
// dedupe classification, merging) all happens server-side. The wizard
// only orchestrates the four POST/GET calls.

import { useState, useMemo, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Upload,
  ArrowRight,
  ArrowLeft,
  CheckCircle2,
  AlertTriangle,
  UserPlus,
  UserCheck,
  HelpCircle,
  XCircle,
  Loader2,
  FileSpreadsheet,
} from 'lucide-react'

import PageHeader from '../components/ui/PageHeader'
import {
  customerImportApi,
  type ImportBatch,
  type ImportClassification,
  type ClassifiedRow,
  type SuspectCandidate,
} from '../api/customerImport'

const FIELD_LABELS: Record<string, string> = {
  name:   'الاسم',
  phone:  'رقم الهاتف',
  email:  'البريد الإلكتروني',
  city:   'المدينة',
  notes:  'ملاحظات',
  source: 'المصدر',
}
const REQUIRED_FIELDS = ['phone'] as const

const CLASSIFICATION_LABELS: Record<ImportClassification, string> = {
  new:     'جديد',
  exact:   'موجود مسبقاً (سيتم الدمج)',
  suspect: 'مشتبه تكراره',
  invalid: 'غير صالح (سيتم تجاوزه)',
}

const INVALID_REASON_LABELS: Record<string, string> = {
  missing_phone:        'رقم الهاتف مفقود',
  invalid_phone_format: 'صيغة الرقم غير صحيحة',
  invalid_email_format: 'صيغة البريد غير صحيحة',
}

// ── Step container ───────────────────────────────────────────────────────────

function StepHeader({ step, total, title }: { step: number; total: number; title: string }) {
  return (
    <div className="flex items-center justify-between mb-4">
      <h2 className="text-lg font-bold">{title}</h2>
      <span className="text-xs text-slate-500">
        الخطوة {step} من {total}
      </span>
    </div>
  )
}

function ProgressDots({ step }: { step: number }) {
  return (
    <div className="flex items-center gap-2 mb-6">
      {[1, 2, 3, 4].map((n) => (
        <div
          key={n}
          className={`h-2 flex-1 rounded-full transition-colors ${
            n <= step ? 'bg-brand-600' : 'bg-slate-200'
          }`}
        />
      ))}
    </div>
  )
}

// ── Step 1: Upload ───────────────────────────────────────────────────────────

function Step1Upload({
  onParsed,
}: {
  onParsed: (b: ImportBatch, headers: string[], suggested: Record<string, string>, sample: Record<string, string>[]) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function handleUpload() {
    if (!file) return
    setLoading(true)
    setError('')
    try {
      const res = await customerImportApi.upload(file)
      onParsed(res.batch, res.headers, res.suggested_mapping || {}, res.sample_rows || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'فشل رفع الملف')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-white rounded-xl border p-6 space-y-5">
      <StepHeader step={1} total={4} title="رفع الملف" />

      <p className="text-sm text-slate-600">
        ارفع ملف عملاء بصيغة CSV أو XLSX. الحد الأقصى 10,000 سجل وحجم 5 ميجابايت.
      </p>

      <label className="flex flex-col items-center justify-center gap-3 border-2 border-dashed border-slate-300 rounded-xl p-8 cursor-pointer hover:bg-slate-50 transition">
        <FileSpreadsheet className="w-10 h-10 text-brand-600" />
        <div className="text-sm font-medium text-slate-700">
          {file ? file.name : 'اضغط لاختيار ملف CSV أو XLSX'}
        </div>
        <input
          type="file"
          accept=".csv,.xlsx,.xls,.tsv,.txt"
          className="hidden"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </label>

      {error && (
        <div className="bg-rose-50 border border-rose-200 text-rose-700 text-sm rounded-md p-3">
          {error}
        </div>
      )}

      <div className="flex justify-end">
        <button
          className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50"
          disabled={!file || loading}
          onClick={handleUpload}
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
          رفع الملف
        </button>
      </div>
    </div>
  )
}

// ── Step 2: Mapping ──────────────────────────────────────────────────────────

function Step2Mapping({
  headers,
  suggested,
  sampleRows,
  onSubmit,
  onBack,
  loading,
  error,
}: {
  headers: string[]
  suggested: Record<string, string>
  sampleRows: Record<string, string>[]
  onSubmit: (mapping: Record<string, string>) => void
  onBack: () => void
  loading: boolean
  error: string
}) {
  const [mapping, setMapping] = useState<Record<string, string>>(suggested)

  useEffect(() => { setMapping(suggested) }, [suggested])

  const phoneSet = !!(mapping.phone && mapping.phone.trim())

  return (
    <div className="bg-white rounded-xl border p-6 space-y-5">
      <StepHeader step={2} total={4} title="مطابقة الأعمدة" />

      <p className="text-sm text-slate-600">
        اختار العمود في الملف الذي يقابل كل حقل من حقول العميل في نحلة. عمود الهاتف إجباري لأن نحلة لا تنشئ عميل بدون رقم.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {Object.keys(FIELD_LABELS).map((field) => {
          const required = (REQUIRED_FIELDS as readonly string[]).includes(field)
          return (
            <div key={field}>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                {FIELD_LABELS[field]}
                {required && <span className="text-rose-500 mr-1">*</span>}
              </label>
              <select
                value={mapping[field] ?? ''}
                onChange={(e) =>
                  setMapping((prev) => ({ ...prev, [field]: e.target.value }))
                }
                className="w-full border border-slate-300 rounded-md px-2 py-2 text-sm bg-white"
              >
                <option value="">— لا يوجد —</option>
                {headers.map((h) => (
                  <option key={h} value={h}>{h}</option>
                ))}
              </select>
            </div>
          )
        })}
      </div>

      {/* Sample rows preview */}
      {sampleRows.length > 0 && (
        <div className="border rounded-lg overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50">
              <tr>
                {headers.map((h) => (
                  <th key={h} className="px-3 py-2 text-right font-medium text-slate-600">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sampleRows.map((row, i) => (
                <tr key={i} className="border-t">
                  {headers.map((h) => (
                    <td key={h} className="px-3 py-2 text-slate-700" dir="auto">
                      {row[h] ?? ''}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {error && (
        <div className="bg-rose-50 border border-rose-200 text-rose-700 text-sm rounded-md p-3">
          {error}
        </div>
      )}

      <div className="flex justify-between">
        <button className="btn-secondary text-sm flex items-center gap-2" onClick={onBack}>
          <ArrowRight className="w-4 h-4" />
          رجوع
        </button>
        <button
          className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50"
          disabled={!phoneSet || loading}
          onClick={() => onSubmit(mapping)}
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
          متابعة للمعاينة
          <ArrowLeft className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}

// ── Step 3: Preview ──────────────────────────────────────────────────────────

function classificationBadge(c: ImportClassification) {
  const map: Record<ImportClassification, string> = {
    new:     'bg-emerald-50 text-emerald-700 border-emerald-200',
    exact:   'bg-sky-50 text-sky-700 border-sky-200',
    suspect: 'bg-amber-50 text-amber-700 border-amber-200',
    invalid: 'bg-rose-50 text-rose-700 border-rose-200',
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${map[c]}`}>
      {CLASSIFICATION_LABELS[c]}
    </span>
  )
}

function suspectDecisionLabel(decision: string, candidates: SuspectCandidate[]): string {
  if (decision === 'skip') return 'تجاوز'
  if (decision === 'create_new') return 'إنشاء جديد'
  if (decision.startsWith('merge_into:')) {
    const cid = decision.slice('merge_into:'.length)
    const c = candidates.find((x) => String(x.customer_id) === cid)
    return c ? `دمج مع ${c.name || c.normalized_phone || `#${cid}`}` : `دمج مع #${cid}`
  }
  return decision
}

function Step3Preview({
  batch,
  decisions,
  onDecisionsChange,
  onContinue,
  onBack,
}: {
  batch: ImportBatch
  decisions: Record<number, string>
  onDecisionsChange: (d: Record<number, string>) => void
  onContinue: () => void
  onBack: () => void
}) {
  const [tab, setTab] = useState<ImportClassification>('new')
  const [rows, setRows] = useState<ClassifiedRow[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const pageSize = 50

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await customerImportApi.listRows(batch.id, {
        status: tab, page, pageSize,
      })
      setRows(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [batch.id, tab, page])

  useEffect(() => { load() }, [load])

  function setDecision(rowIndex: number, decision: string) {
    onDecisionsChange({ ...decisions, [rowIndex]: decision })
  }

  const summaryCards: { c: ImportClassification; n: number; icon: any; tone: string }[] = [
    { c: 'new',     n: batch.summary.new,      icon: UserPlus,    tone: 'text-emerald-600 bg-emerald-50' },
    { c: 'exact',   n: batch.summary.matched,  icon: UserCheck,   tone: 'text-sky-600 bg-sky-50' },
    { c: 'suspect', n: batch.summary.suspects, icon: HelpCircle,  tone: 'text-amber-600 bg-amber-50' },
    { c: 'invalid', n: batch.summary.invalid,  icon: XCircle,     tone: 'text-rose-600 bg-rose-50' },
  ]

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="bg-white rounded-xl border p-6 space-y-5">
      <StepHeader step={3} total={4} title="مراجعة النتائج وكشف التكرار" />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {summaryCards.map(({ c, n, icon: Icon, tone }) => (
          <button
            key={c}
            onClick={() => { setTab(c); setPage(1) }}
            className={`text-right border rounded-lg p-3 transition ${
              tab === c ? 'border-brand-500 ring-2 ring-brand-100' : 'border-slate-200 hover:border-slate-300'
            }`}
          >
            <div className={`inline-flex items-center justify-center w-8 h-8 rounded-full ${tone} mb-2`}>
              <Icon className="w-4 h-4" />
            </div>
            <div className="text-xs text-slate-500">{CLASSIFICATION_LABELS[c]}</div>
            <div className="text-xl font-bold mt-1">{n}</div>
          </button>
        ))}
      </div>

      <div className="border rounded-lg overflow-hidden">
        <div className="bg-slate-50 px-4 py-2 text-xs text-slate-600 border-b flex items-center justify-between">
          <span>عرض: {CLASSIFICATION_LABELS[tab]}</span>
          <span>{total} سجل</span>
        </div>

        {loading ? (
          <div className="p-8 text-center text-slate-500 text-sm">
            <Loader2 className="w-5 h-5 animate-spin inline-block ml-2" />
            جاري التحميل…
          </div>
        ) : rows.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-sm">
            لا يوجد سجلات في هذه الفئة.
          </div>
        ) : (
          <div className="divide-y">
            {rows.map((row) => (
              <PreviewRow
                key={row.row_index}
                row={row}
                decision={decisions[row.row_index]}
                onDecision={(d) => setDecision(row.row_index, d)}
              />
            ))}
          </div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <button className="btn-secondary text-xs" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            السابق
          </button>
          <span className="text-slate-500">الصفحة {page} من {totalPages}</span>
          <button className="btn-secondary text-xs" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            التالي
          </button>
        </div>
      )}

      <div className="flex justify-between">
        <button className="btn-secondary text-sm flex items-center gap-2" onClick={onBack}>
          <ArrowRight className="w-4 h-4" />
          رجوع
        </button>
        <button className="btn-primary text-sm flex items-center gap-2" onClick={onContinue}>
          متابعة للتأكيد
          <ArrowLeft className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}

function PreviewRow({
  row,
  decision,
  onDecision,
}: {
  row: ClassifiedRow
  decision: string | undefined
  onDecision: (d: string) => void
}) {
  const n = row.normalized
  const isSuspect = row.classification === 'suspect'
  const isInvalid = row.classification === 'invalid'

  return (
    <div className="px-4 py-3 text-sm">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            {classificationBadge(row.classification)}
            <span className="text-xs text-slate-500">سطر {row.row_index}</span>
          </div>
          <div className="font-medium text-slate-800">{n.name || '—'}</div>
          <div className="text-xs text-slate-600 flex flex-wrap gap-x-4 gap-y-1 mt-1" dir="auto">
            {n.normalized_phone && <span>📱 {n.normalized_phone}</span>}
            {n.email && <span>✉️ {n.email}</span>}
            {n.city && <span>🏙️ {n.city}</span>}
          </div>
          {isInvalid && (
            <div className="text-xs text-rose-700 mt-1">
              {n.invalid_reasons.map((r) => INVALID_REASON_LABELS[r] ?? r).join(' · ')}
              {row.match_reason && row.match_reason.startsWith('duplicate_in_file') && (
                <span> · مكرر داخل الملف</span>
              )}
            </div>
          )}
        </div>

        {isSuspect && (
          <div className="w-full md:w-auto md:min-w-[260px] bg-amber-50/40 border border-amber-200 rounded-lg p-2 space-y-1">
            <div className="text-xs font-medium text-amber-800">قرار للمشتبه فيه:</div>
            <div className="space-y-1">
              {row.suspect_candidates.filter((c) => c.customer_id).map((c) => (
                <label key={c.customer_id} className="flex items-center gap-2 text-xs">
                  <input
                    type="radio"
                    name={`d-${row.row_index}`}
                    checked={decision === `merge_into:${c.customer_id}`}
                    onChange={() => onDecision(`merge_into:${c.customer_id}`)}
                  />
                  <span className="truncate">
                    دمج مع: <strong>{c.name || c.normalized_phone || `#${c.customer_id}`}</strong>
                    <span className="text-slate-500"> ({c.reason})</span>
                  </span>
                </label>
              ))}
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="radio"
                  name={`d-${row.row_index}`}
                  checked={decision === 'create_new'}
                  onChange={() => onDecision('create_new')}
                />
                إنشاء عميل جديد على أي حال
              </label>
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="radio"
                  name={`d-${row.row_index}`}
                  checked={!decision || decision === 'skip'}
                  onChange={() => onDecision('skip')}
                />
                تجاوز هذا السجل
              </label>
              {decision && (
                <div className="text-[11px] text-amber-700 mt-1">
                  ✓ {suspectDecisionLabel(decision, row.suspect_candidates)}
                </div>
              )}
            </div>
          </div>
        )}

        {row.classification === 'exact' && row.match_customer_id && (
          <div className="text-xs text-sky-700">
            سيتم الدمج مع العميل #{row.match_customer_id}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Step 4: Commit ───────────────────────────────────────────────────────────

function Step4Commit({
  batch,
  decisions,
  onCommit,
  onBack,
  loading,
  error,
  result,
}: {
  batch: ImportBatch
  decisions: Record<number, string>
  onCommit: (opts: { apply_new: boolean; update_existing: boolean }) => void
  onBack: () => void
  loading: boolean
  error: string
  result: { created: number; updated: number; skipped: number; errors: number } | null
}) {
  const [applyNew, setApplyNew] = useState(true)
  const [updateExisting, setUpdateExisting] = useState(true)

  const decisionStats = useMemo(() => {
    let merge = 0, create = 0, skip = 0
    Object.values(decisions).forEach((d) => {
      if (d === 'create_new') create += 1
      else if (d.startsWith('merge_into:')) merge += 1
      else skip += 1
    })
    return { merge, create, skip }
  }, [decisions])

  if (result) {
    return (
      <div className="bg-white rounded-xl border p-6 space-y-5">
        <StepHeader step={4} total={4} title="تم الاستيراد" />
        <div className="text-center py-6">
          <CheckCircle2 className="w-14 h-14 text-emerald-500 mx-auto mb-3" />
          <div className="text-lg font-bold mb-1">اكتمل الاستيراد بنجاح</div>
          <div className="text-sm text-slate-500">
            تم تحديث قاعدة عملائك مع الحفاظ على البيانات الموثوقة من سلة.
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-center">
          <div className="bg-emerald-50 rounded-lg p-3">
            <div className="text-2xl font-bold text-emerald-700">{result.created}</div>
            <div className="text-xs text-emerald-700">عميل جديد</div>
          </div>
          <div className="bg-sky-50 rounded-lg p-3">
            <div className="text-2xl font-bold text-sky-700">{result.updated}</div>
            <div className="text-xs text-sky-700">تم تحديثه</div>
          </div>
          <div className="bg-slate-100 rounded-lg p-3">
            <div className="text-2xl font-bold text-slate-700">{result.skipped}</div>
            <div className="text-xs text-slate-700">تم تجاوزه</div>
          </div>
          <div className="bg-rose-50 rounded-lg p-3">
            <div className="text-2xl font-bold text-rose-700">{result.errors}</div>
            <div className="text-xs text-rose-700">أخطاء</div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-white rounded-xl border p-6 space-y-5">
      <StepHeader step={4} total={4} title="تأكيد الاستيراد" />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-center">
        <div className="bg-slate-50 rounded-lg p-3">
          <div className="text-xs text-slate-500">إجمالي</div>
          <div className="text-2xl font-bold">{batch.total_rows}</div>
        </div>
        <div className="bg-emerald-50 rounded-lg p-3">
          <div className="text-xs text-emerald-700">جديد</div>
          <div className="text-2xl font-bold text-emerald-700">{batch.summary.new}</div>
        </div>
        <div className="bg-sky-50 rounded-lg p-3">
          <div className="text-xs text-sky-700">سيتم الدمج</div>
          <div className="text-2xl font-bold text-sky-700">{batch.summary.matched}</div>
        </div>
        <div className="bg-amber-50 rounded-lg p-3">
          <div className="text-xs text-amber-700">قرارات مشتبهين</div>
          <div className="text-2xl font-bold text-amber-700">
            {decisionStats.merge + decisionStats.create}/{batch.summary.suspects}
          </div>
        </div>
      </div>

      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={applyNew} onChange={(e) => setApplyNew(e.target.checked)} />
          إضافة العملاء الجدد ({batch.summary.new})
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={updateExisting}
            onChange={(e) => setUpdateExisting(e.target.checked)}
          />
          تحديث الحقول الناقصة فقط في العملاء الموجودين ({batch.summary.matched})
        </label>
        <div className="text-xs text-slate-500 leading-relaxed pt-1">
          نحلة لن تستبدل البيانات الموجودة من سلة ببيانات أضعف من الملف. التحديث يلمس فقط الحقول الفارغة، ويُسجّل
          المصدر الإضافي ضمن <code className="bg-slate-100 px-1 rounded">source_tags</code>.
        </div>
      </div>

      {batch.summary.suspects > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm text-amber-800">
          <div className="flex items-center gap-2 mb-1 font-medium">
            <AlertTriangle className="w-4 h-4" />
            مشتبه التكرار: {batch.summary.suspects} سجل
          </div>
          <div className="text-xs">
            تم اتخاذ قرار لـ {decisionStats.merge + decisionStats.create} منها (
            دمج: {decisionStats.merge} · إنشاء: {decisionStats.create}). الباقي ({batch.summary.suspects - decisionStats.merge - decisionStats.create}) سيتم تجاوزه افتراضياً.
          </div>
        </div>
      )}

      {error && (
        <div className="bg-rose-50 border border-rose-200 text-rose-700 text-sm rounded-md p-3">
          {error}
        </div>
      )}

      <div className="flex justify-between">
        <button className="btn-secondary text-sm flex items-center gap-2" onClick={onBack} disabled={loading}>
          <ArrowRight className="w-4 h-4" />
          رجوع
        </button>
        <button
          className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50"
          disabled={loading}
          onClick={() => onCommit({ apply_new: applyNew, update_existing: updateExisting })}
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
          تنفيذ الاستيراد
        </button>
      </div>
    </div>
  )
}

// ── Top-level page ───────────────────────────────────────────────────────────

export default function CustomersImport() {
  const navigate = useNavigate()
  const [step, setStep] = useState<1 | 2 | 3 | 4>(1)

  const [batch, setBatch] = useState<ImportBatch | null>(null)
  const [headers, setHeaders] = useState<string[]>([])
  const [suggestedMapping, setSuggestedMapping] = useState<Record<string, string>>({})
  const [sampleRows, setSampleRows] = useState<Record<string, string>[]>([])

  const [decisions, setDecisions] = useState<Record<number, string>>({})

  const [mappingLoading, setMappingLoading] = useState(false)
  const [mappingError, setMappingError] = useState('')

  const [commitLoading, setCommitLoading] = useState(false)
  const [commitError, setCommitError] = useState('')
  const [commitResult, setCommitResult] = useState<{
    created: number; updated: number; skipped: number; errors: number
  } | null>(null)

  function reset() {
    setStep(1)
    setBatch(null)
    setHeaders([])
    setSuggestedMapping({})
    setSampleRows([])
    setDecisions({})
    setMappingError('')
    setCommitError('')
    setCommitResult(null)
  }

  async function handleSubmitMapping(mapping: Record<string, string>) {
    if (!batch) return
    setMappingLoading(true)
    setMappingError('')
    try {
      const res = await customerImportApi.submitMapping(batch.id, mapping)
      setBatch(res.batch)
      setStep(3)
    } catch (err) {
      setMappingError(err instanceof Error ? err.message : 'فشل تطبيق المطابقة')
    } finally {
      setMappingLoading(false)
    }
  }

  async function handleCommit(opts: { apply_new: boolean; update_existing: boolean }) {
    if (!batch) return
    setCommitLoading(true)
    setCommitError('')
    try {
      const res = await customerImportApi.commit(batch.id, {
        ...opts,
        suspect_decisions: decisions,
      })
      setBatch(res.batch)
      setCommitResult({
        created: res.result.created,
        updated: res.result.updated,
        skipped: res.result.skipped,
        errors:  res.result.errors,
      })
    } catch (err) {
      setCommitError(err instanceof Error ? err.message : 'فشل تنفيذ الاستيراد')
    } finally {
      setCommitLoading(false)
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="استيراد العملاء"
        subtitle="ارفع ملف عملاء من خارج المتجر مع كشف ذكي للتكرار"
        action={
          commitResult ? (
            <button
              className="btn-primary text-sm"
              onClick={() => navigate('/customers')}
            >
              العودة لقائمة العملاء
            </button>
          ) : (
            <button className="btn-secondary text-sm" onClick={reset}>
              بدء من جديد
            </button>
          )
        }
      />

      <ProgressDots step={commitResult ? 4 : step} />

      {step === 1 && (
        <Step1Upload
          onParsed={(b, h, sm, sample) => {
            setBatch(b)
            setHeaders(h)
            setSuggestedMapping(sm)
            setSampleRows(sample)
            setStep(2)
          }}
        />
      )}

      {step === 2 && batch && (
        <Step2Mapping
          headers={headers}
          suggested={suggestedMapping}
          sampleRows={sampleRows}
          onSubmit={handleSubmitMapping}
          onBack={() => setStep(1)}
          loading={mappingLoading}
          error={mappingError}
        />
      )}

      {step === 3 && batch && (
        <Step3Preview
          batch={batch}
          decisions={decisions}
          onDecisionsChange={setDecisions}
          onContinue={() => setStep(4)}
          onBack={() => setStep(2)}
        />
      )}

      {step === 4 && batch && (
        <Step4Commit
          batch={batch}
          decisions={decisions}
          onCommit={handleCommit}
          onBack={() => { if (!commitResult) setStep(3) }}
          loading={commitLoading}
          error={commitError}
          result={commitResult}
        />
      )}
    </div>
  )
}
