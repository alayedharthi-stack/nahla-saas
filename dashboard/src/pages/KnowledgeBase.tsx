/**
 * KnowledgeBase.tsx — /knowledge-base
 * ───────────────────────────────────
 * Single-textarea page where the merchant types **everything they want
 * the AI to know about their store and products** as free-form text.
 *
 * Architectural intent (kept deliberately simple for v1):
 *   - This page edits exactly ONE field: `ai.manual_knowledge_base`.
 *   - It is independent of `ai.owner_instructions`. Owner instructions
 *     control the assistant's *behaviour*; the knowledge base feeds it
 *     *facts* it can cite.
 *   - When Salla sync is connected, prices / stock / variants / direct
 *     product URLs always come from Salla's data (loaded via
 *     `core.store_knowledge.build_merchant_context` on the backend),
 *     even if the merchant pasted different values here. The runtime
 *     overlay at `backend/modules/ai/prompts/tenant_overlay.py` makes
 *     this rule explicit to the model — see section 8 of the overlay.
 *
 * No multi-field schema, no sectioned editors, no PDF/CSV import yet.
 * That's a future Phase — see the original architecture brief.
 */
import { useEffect, useState } from 'react'
import {
  BookOpen,
  Loader2,
  CheckCircle,
  AlertCircle,
  Save,
  Info,
  Store,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { settingsApi, type AISettings } from '../api/settings'
import { useLanguage } from '../i18n/context'

const PLACEHOLDER_AR =
  '— أمثلة على ما يمكن إضافته —\n\n' +
  '• المنتجات: الأسماء، المواصفات، طرق الاستخدام، الفوائد المميزة، الفئات المستهدفة.\n' +
  '• الشحن: مدة التوصيل، المناطق المغطاة، أسعار الشحن، الشحن المجاني.\n' +
  '• الضمان والإرجاع: مدة الضمان، شروط الإرجاع، طريقة استلام البديل.\n' +
  '• الدفع: الطرق المتاحة (مدى، فيزا، الدفع عند الاستلام، تابي، تمارا...).\n' +
  '• الأسئلة الشائعة: أسئلة العملاء المتكررة وإجاباتها الجاهزة.\n' +
  '• ملاحظات داخلية: أي نصائح بيع أو نقاط تميّز يجب أن تستخدمها نحلة في إقناع العميل.\n\n' +
  'اكتب بحرية بالأسلوب الذي تفضّله — لا حاجة لتنسيق خاص.'

export default function KnowledgeBase() {
  const { t } = useLanguage()
  const [ai, setAi]           = useState<AISettings | null>(null)
  const [draft, setDraft]     = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving]   = useState(false)
  const [saved, setSaved]     = useState(false)
  const [error, setError]     = useState<string | null>(null)
  const [sallaConnected, setSallaConnected] = useState<boolean | null>(null)

  useEffect(() => {
    settingsApi.getAll()
      .then(s => {
        setAi(s.ai)
        setDraft(s.ai.manual_knowledge_base || '')
        // Treat Salla as "connected" for UI hints when the platform is
        // selected AND a token is stored. We never display the masked
        // token — only its presence is needed to show the warning card.
        const isSalla = s.store?.platform_type === 'salla'
        const hasToken = !!s.store?.salla_access_token
        setSallaConnected(isSalla && hasToken)
      })
      .catch(() => setError('تعذّر تحميل قاعدة المعرفة — حاول لاحقاً.'))
      .finally(() => setLoading(false))
  }, [])

  const dirty = ai !== null && draft !== (ai.manual_knowledge_base || '')

  const handleSave = async () => {
    if (!ai) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const next: AISettings = { ...ai, manual_knowledge_base: draft }
      const res = await settingsApi.update({ ai: next })
      setAi(res.ai)
      setDraft(res.ai.manual_knowledge_base || '')
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch {
      setError('فشل الحفظ — حاول مجدداً.')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <PageHeader
          title={t(tr => tr.pages.knowledgeBase.title)}
          subtitle={t(tr => tr.pages.knowledgeBase.subtitle)}
        />
        <div className="flex items-center justify-center py-20 gap-2 text-slate-400 text-sm">
          <Loader2 className="w-4 h-4 animate-spin text-brand-500" />
          جاري التحميل...
        </div>
      </div>
    )
  }

  if (!ai) {
    return (
      <div className="space-y-6">
        <PageHeader
          title={t(tr => tr.pages.knowledgeBase.title)}
          subtitle={t(tr => tr.pages.knowledgeBase.subtitle)}
        />
        <div className="card p-6 text-center text-sm text-red-500">
          <AlertCircle className="w-5 h-5 mx-auto mb-2" />
          {error ?? 'تعذّر تحميل الإعدادات'}
        </div>
      </div>
    )
  }

  const charCount = draft.length

  return (
    <div className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.knowledgeBase.title)}
        subtitle={t(tr => tr.pages.knowledgeBase.subtitle)}
      />

      {/* ── Intro / scope card ────────────────────────────────────────── */}
      <div className="card p-4 border-brand-100 bg-brand-50/40">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-brand-100 text-brand-600 flex items-center justify-center shrink-0">
            <BookOpen className="w-4.5 h-4.5" />
          </div>
          <div className="flex-1 text-xs text-slate-700 leading-relaxed">
            <p className="font-semibold text-slate-900 text-sm mb-1">
              ما الفرق بين تعليمات المالك وقاعدة المعرفة؟
            </p>
            <p>
              <span className="font-bold">تعليمات المالك</span> تتحكم في
              <span className="font-bold"> أسلوب وشخصية </span>الردود
              (نبرة، طول، قواعد التصعيد...).
              <br />
              <span className="font-bold">قاعدة المعرفة</span> تُغذّي الذكاء
              <span className="font-bold"> بمعلومات متجرك</span>
              {' '}(منتجات، شحن، ضمان، أسئلة شائعة...) ليستخدمها في الردود.
            </p>
          </div>
        </div>
      </div>

      {/* ── Salla precedence warning (always shown, emphasised when connected) ── */}
      <div
        className={[
          'card p-4',
          sallaConnected
            ? 'border-amber-200 bg-amber-50/60'
            : 'border-slate-200 bg-slate-50/60',
        ].join(' ')}
      >
        <div className="flex items-start gap-3">
          <div
            className={[
              'w-9 h-9 rounded-xl flex items-center justify-center shrink-0',
              sallaConnected
                ? 'bg-amber-100 text-amber-700'
                : 'bg-slate-200 text-slate-600',
            ].join(' ')}
          >
            <Store className="w-4.5 h-4.5" />
          </div>
          <div className="flex-1 text-xs leading-relaxed">
            <p
              className={[
                'font-semibold text-sm mb-1',
                sallaConnected ? 'text-amber-900' : 'text-slate-800',
              ].join(' ')}
            >
              الأولوية لبيانات سلة في الأسعار والمخزون
            </p>
            <p className={sallaConnected ? 'text-amber-800' : 'text-slate-600'}>
              إذا كان متجرك مربوطاً بسلة، تبقى أسعار المنتجات والمخزون
              والمتغيرات والروابط المباشرة من سلة هي
              <span className="font-bold"> المصدر الرسمي</span>،
              وتُستخدم قاعدة المعرفة للمعلومات الإضافية مثل الفوائد
              وطريقة الاستخدام والشحن والأسئلة الشائعة.
              {sallaConnected
                ? ' (متجرك مربوط بسلة حالياً ✓)'
                : ' (متجرك غير مربوط بسلة — جميع المعلومات هنا هي المصدر.)'}
            </p>
          </div>
        </div>
      </div>

      {/* ── Editor ───────────────────────────────────────────────────────── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <BookOpen className="w-4 h-4 text-brand-500 shrink-0" />
            <h2 className="text-sm font-semibold text-slate-900">
              معلومات المتجر والمنتجات
            </h2>
          </div>
          <span className="text-[11px] text-slate-400 shrink-0">
            {charCount.toLocaleString('ar-SA')} حرف
          </span>
        </div>

        <div className="p-5 space-y-3">
          <p className="text-xs text-slate-500 leading-relaxed flex items-start gap-1.5">
            <Info className="w-3.5 h-3.5 text-slate-400 shrink-0 mt-0.5" />
            أضف هنا كل ما تريد أن يعرفه الذكاء عن متجرك: المنتجات، الأسعار،
            المواصفات، الروابط، طريقة الاستخدام، الشحن، الضمان، الأسئلة
            الشائعة، معلومات الدفع، وأي ملاحظات مهمة.
          </p>

          <textarea
            className="input min-h-[480px] resize-y leading-relaxed"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={PLACEHOLDER_AR}
            dir="auto"
            spellCheck={false}
          />
        </div>

        {/* ── Save bar ────────────────────────────────────────────────── */}
        <div className="px-5 py-4 border-t border-slate-100 flex items-center justify-between gap-3 bg-slate-50/50 rounded-b-xl">
          <div className="text-xs text-slate-500 min-w-0">
            {error && (
              <span className="text-red-600 inline-flex items-center gap-1">
                <AlertCircle className="w-3.5 h-3.5" /> {error}
              </span>
            )}
            {!error && saved && (
              <span className="text-emerald-600 inline-flex items-center gap-1">
                <CheckCircle className="w-3.5 h-3.5" /> تم الحفظ — ستُطبَّق على
                المحادثات الجديدة فوراً.
              </span>
            )}
            {!error && !saved && dirty && (
              <span className="text-amber-600">يوجد تغييرات غير محفوظة</span>
            )}
            {!error && !saved && !dirty && (
              <span>التغييرات تُطبَّق فوراً على المحادثات الجديدة بعد الحفظ.</span>
            )}
          </div>

          <button
            type="button"
            onClick={handleSave}
            disabled={saving || !dirty}
            className={[
              'inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold',
              'transition-all shrink-0',
              saving || !dirty
                ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                : 'bg-brand-500 hover:bg-brand-600 text-white',
            ].join(' ')}
          >
            {saving ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" /> جارٍ الحفظ...
              </>
            ) : saved ? (
              <>
                <CheckCircle className="w-4 h-4" /> تم الحفظ
              </>
            ) : (
              <>
                <Save className="w-4 h-4" /> حفظ التغييرات
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
