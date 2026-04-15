/**
 * WhatsAppManualSetup.tsx
 * ────────────────────────
 * صفحة شرح تفصيلي للتاجر — كيفية استخراج بيانات Meta لربط واتساب يدويًا
 * (Phone Number ID / WABA ID / Permanent Access Token)
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ArrowLeft, BookOpen, CheckCircle2,
  ChevronDown, ChevronUp, Copy, ExternalLink,
  Info, MessageCircle, Phone, ShieldCheck,
} from 'lucide-react'

// ── Image placeholder ─────────────────────────────────────────────────────────
// Replace `src` with real screenshots when available.
function StepImage({ alt, note }: { alt: string; note?: string }) {
  return (
    <div className="w-full rounded-xl overflow-hidden border-2 border-dashed border-slate-200 bg-slate-50 flex flex-col items-center justify-center py-10 gap-2 text-slate-400">
      <BookOpen className="w-8 h-8 text-slate-300" />
      <p className="text-sm font-medium">{alt}</p>
      {note && <p className="text-xs text-center px-8 text-slate-400">{note}</p>}
    </div>
  )
}

// ── Tip / Warning boxes ───────────────────────────────────────────────────────
function TipBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 flex gap-3">
      <Info className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
      <div className="text-sm text-blue-800">{children}</div>
    </div>
  )
}
function WarnBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 flex gap-3">
      <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <div className="text-sm text-amber-800">{children}</div>
    </div>
  )
}

// ── CopyField ─────────────────────────────────────────────────────────────────
function CopyableValue({ label, example }: { label: string; example: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex items-center gap-2 bg-slate-900 rounded-lg px-4 py-2.5">
      <span className="text-xs text-slate-400 shrink-0">{label}:</span>
      <code className="flex-1 text-emerald-400 text-sm font-mono truncate">{example}</code>
      <button
        onClick={() => { navigator.clipboard.writeText(example); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
        className="p-1 hover:text-white text-slate-400 transition"
        title="نسخ"
      >
        {copied ? <CheckCircle2 className="w-4 h-4 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
    </div>
  )
}

// ── Accordion Step ────────────────────────────────────────────────────────────
function StepCard({
  num, title, duration, children,
  defaultOpen = false,
}: {
  num: number; title: string; duration?: string
  children: React.ReactNode; defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen(p => !p)}
        className="w-full flex items-center gap-4 px-5 py-4 text-right hover:bg-slate-50 transition"
      >
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center text-base font-black shrink-0 ${open ? 'bg-emerald-500 text-white' : 'bg-slate-100 text-slate-500'}`}>
          {num}
        </div>
        <div className="flex-1">
          <p className="font-bold text-slate-800 text-sm">{title}</p>
          {duration && <p className="text-xs text-slate-400 mt-0.5">{duration}</p>}
        </div>
        {open ? <ChevronUp className="w-4 h-4 text-slate-400 shrink-0" /> : <ChevronDown className="w-4 h-4 text-slate-400 shrink-0" />}
      </button>
      {open && (
        <div className="px-5 pb-5 space-y-4 border-t border-slate-50">
          {children}
        </div>
      )}
    </div>
  )
}

// ── SubStep ───────────────────────────────────────────────────────────────────
function SubStep({ n, text }: { n: number; text: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <span className="w-6 h-6 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">
        {n}
      </span>
      <div className="text-sm text-slate-700">{text}</div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────
export default function WhatsAppManualSetup() {
  const navigate = useNavigate()

  return (
    <div className="max-w-2xl mx-auto px-4 py-8 space-y-6" dir="rtl">

      {/* ── Back navigation ────────────────────────────────────────────────── */}
      <button
        onClick={() => navigate(-1)}
        className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 transition"
      >
        <ArrowLeft className="w-4 h-4" />
        رجوع
      </button>

      {/* ── Page header ────────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-2xl bg-emerald-500 flex items-center justify-center shadow-lg shadow-emerald-500/25">
            <MessageCircle className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-black text-slate-900">ربط واتساب يدويًا</h1>
            <p className="text-sm text-slate-500">دليل خطوة بخطوة للحصول على بيانات Meta</p>
          </div>
        </div>

        <WarnBox>
          <p className="font-semibold mb-1">⚠️ تنبيه مهم</p>
          <p>
            هذه الطريقة اليدوية هي <strong>الطريقة المعتمدة حاليًا</strong> في نحلة.
            بعد اكتمال اعتماد Meta الرسمي سيتم تفعيل الربط السهل التلقائي.
          </p>
        </WarnBox>
      </div>

      {/* ── What you need ──────────────────────────────────────────────────── */}
      <div className="bg-slate-50 rounded-2xl border border-slate-100 p-5">
        <p className="font-bold text-slate-800 mb-3">ما ستحتاجه عند الانتهاء</p>
        <div className="space-y-2">
          {[
            { icon: Phone,       label: 'Phone Number ID',   desc: 'رقم تعريفي للهاتف — أرقام فقط (~15 رقمًا)' },
            { icon: MessageCircle, label: 'WABA ID',          desc: 'معرّف حساب واتساب للأعمال — أرقام فقط' },
            { icon: ShieldCheck, label: 'Permanent Access Token', desc: 'رمز وصول دائم — يبدأ بـ EAA...' },
          ].map(({ icon: Icon, label, desc }) => (
            <div key={label} className="flex items-start gap-3 bg-white rounded-xl p-3 border border-slate-100">
              <Icon className="w-4 h-4 text-emerald-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-semibold text-slate-800 font-mono">{label}</p>
                <p className="text-xs text-slate-500">{desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Steps ──────────────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <p className="font-bold text-slate-800">الخطوات</p>

        {/* Step 1 */}
        <StepCard num={1} title="إنشاء حساب Meta Business (إذا لم يكن لديك)" duration="5–10 دقائق" defaultOpen>
          <div className="pt-2 space-y-3">
            <SubStep n={1} text={<>افتح <a href="https://business.facebook.com" target="_blank" rel="noreferrer" className="text-emerald-600 underline font-medium">business.facebook.com <ExternalLink className="w-3 h-3 inline" /></a> وسجّل الدخول بحساب فيسبوك شخصي</>} />
            <SubStep n={2} text="انقر على «إنشاء حساب» وأدخل اسم شركتك ومعلوماتك" />
            <SubStep n={3} text="أكمل إعداد الحساب وتحقق من البريد الإلكتروني" />
            <StepImage alt="صورة: صفحة إنشاء Meta Business Suite" note="سيتم إضافة الصورة قريبًا" />
            <TipBox>إذا كان لديك حساب Meta Business بالفعل، انتقل مباشرةً إلى الخطوة التالية.</TipBox>
          </div>
        </StepCard>

        {/* Step 2 */}
        <StepCard num={2} title="إضافة منتج WhatsApp Business لتطبيقك" duration="3–5 دقائق">
          <div className="pt-2 space-y-3">
            <SubStep n={1} text={<>افتح <a href="https://developers.facebook.com" target="_blank" rel="noreferrer" className="text-emerald-600 underline font-medium">developers.facebook.com <ExternalLink className="w-3 h-3 inline" /></a></>} />
            <SubStep n={2} text="من القائمة العلوية، انقر «My Apps» ثم «إنشاء تطبيق»" />
            <SubStep n={3} text='اختر نوع التطبيق: "Business"' />
            <SubStep n={4} text="أدخل اسم التطبيق وربطه بحساب Meta Business الذي أنشأته" />
            <SubStep n={5} text='انتقل إلى «Add Products» وأضف «WhatsApp»' />
            <StepImage alt="صورة: إضافة منتج WhatsApp في Meta Developer" />
            <WarnBox>تأكد من اختيار "Business" وليس "Consumer" عند إنشاء التطبيق.</WarnBox>
          </div>
        </StepCard>

        {/* Step 3 */}
        <StepCard num={3} title="الحصول على Phone Number ID و WABA ID" duration="2 دقائق">
          <div className="pt-2 space-y-3">
            <SubStep n={1} text='من صفحة التطبيق، انقر على "WhatsApp" في القائمة الجانبية' />
            <SubStep n={2} text='انقر على "API Setup"' />
            <SubStep n={3} text="ستجد في الأعلى قسمًا بعنوان «Step 1: Select phone numbers»" />
            <SubStep n={4} text={<>ستظهر لك قيمتان:<br /><strong>Phone Number ID</strong> — تحت رقم الهاتف مباشرةً<br /><strong>WhatsApp Business Account ID (WABA ID)</strong> — في الأسفل</>} />
            <StepImage alt="صورة: موقع Phone Number ID و WABA ID في API Setup" note="انظر القسم المحاط باللون الأخضر في الصورة" />

            <div className="space-y-2 pt-1">
              <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide">مثال على الشكل</p>
              <CopyableValue label="Phone Number ID" example="123456789012345" />
              <CopyableValue label="WABA ID" example="987654321098765" />
            </div>

            <TipBox>
              <strong>هل الرقم رقم اختبار؟</strong> في بداية إعداد API ستجد رقم اختبار مؤقت من Meta.
              يمكنك استخدامه للاختبار، لكن عليك لاحقًا إضافة رقمك الفعلي وإكمال التحقق.
            </TipBox>
          </div>
        </StepCard>

        {/* Step 4 */}
        <StepCard num={4} title="إنشاء Permanent Access Token" duration="5 دقائق">
          <div className="pt-2 space-y-3">
            <SubStep n={1} text={<>افتح <a href="https://business.facebook.com/settings/system-users" target="_blank" rel="noreferrer" className="text-emerald-600 underline font-medium">System Users في Meta Business Settings <ExternalLink className="w-3 h-3 inline" /></a></>} />
            <SubStep n={2} text='انقر «Add» وأنشئ مستخدم نظام جديد بصلاحية "Admin"' />
            <SubStep n={3} text='انقر على المستخدم الجديد ثم «Add Assets» وأضف تطبيق WhatsApp Business الخاص بك' />
            <SubStep n={4} text='انقر «Generate New Token»، اختر تطبيقك، وحدد الصلاحيات التالية:' />

            <div className="bg-slate-900 rounded-xl p-4 space-y-1">
              {[
                'whatsapp_business_messaging',
                'whatsapp_business_management',
                'business_management',
              ].map(p => (
                <div key={p} className="flex items-center gap-2">
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  <code className="text-sm text-emerald-400 font-mono">{p}</code>
                </div>
              ))}
            </div>

            <SubStep n={5} text='انقر «Generate Token» ثم انسخ الـ Token فورًا — لن يُعرض مرة أخرى' />
            <StepImage alt="صورة: إنشاء System User Token في Meta Business" note="احرص على اختيار Never Expire عند إنشاء الـ Token" />

            <WarnBox>
              <p className="font-semibold mb-1">تحذير مهم جدًا</p>
              <ul className="list-disc list-inside space-y-1">
                <li>لا تشارك هذا الـ Token مع أي أحد</li>
                <li>لا تنشره في كود مصدر عام أو Git</li>
                <li>إذا سرّبته، أعد إنشاءه فورًا</li>
              </ul>
            </WarnBox>

            <div className="pt-1">
              <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">شكل الـ Token</p>
              <CopyableValue label="Access Token" example="EAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" />
            </div>
          </div>
        </StepCard>

        {/* Step 5 */}
        <StepCard num={5} title="إدخال البيانات في نحلة" duration="أقل من دقيقة">
          <div className="pt-2 space-y-3">
            <SubStep n={1} text={<>ارجع إلى صفحة <a href="/whatsapp-connect" className="text-emerald-600 underline font-medium">ربط واتساب في نحلة</a></>} />
            <SubStep n={2} text='تأكد أن التبويب المحدد هو "الربط اليدوي"' />
            <SubStep n={3} text={<>أدخل:<br /><strong>Phone Number ID</strong> في الحقل الأول<br /><strong>WABA ID</strong> في الحقل الثاني<br /><strong>Access Token</strong> في الحقل الثالث</>} />
            <SubStep n={4} text='انقر "ربط واتساب يدويًا"' />
            <SubStep n={5} text="ستظهر رسالة نجاح وسيتحول حساب واتساب إلى «مرتبط»" />
            <StepImage alt="صورة: نموذج الربط اليدوي في نحلة" />

            <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-4">
              <p className="font-semibold text-emerald-800 text-sm mb-2">✅ بعد الربط الناجح ستتمكن من:</p>
              <ul className="text-sm text-emerald-700 space-y-1">
                {[
                  'استقبال رسائل العملاء والرد التلقائي عبر نحلة AI',
                  'إرسال حملات تسويقية عبر واتساب',
                  'عرض المحادثات في لوحة التحكم',
                ].map(t => (
                  <li key={t} className="flex items-start gap-2">
                    <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                    {t}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </StepCard>
      </div>

      {/* ── FAQ ────────────────────────────────────────────────────────────── */}
      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
        <p className="font-bold text-slate-800 mb-4">أسئلة شائعة</p>
        <div className="space-y-4">
          {[
            {
              q: 'ماذا يحدث إذا غيّرت الـ Token؟',
              a: 'يتوقف الربط تلقائيًا. ستحتاج للدخول إلى صفحة ربط واتساب وإعادة الربط بـ Token الجديد.',
            },
            {
              q: 'هل يمكنني استخدام رقم مسجّل بالفعل على واتساب الشخصي؟',
              a: 'لا. الرقم الذي تستخدمه يجب أن لا يكون مسجلاً على واتساب الشخصي أو Business App. إذا كان مسجلاً، حذف الحساب من التطبيق أولاً.',
            },
            {
              q: 'هل الـ Token الذي أحصل عليه من "Generate Access Token" في API Setup يكفي؟',
              a: 'لا. الـ Token من API Setup مؤقت (يصلح لـ 24 ساعة فقط). تحتاج Token دائمًا (Permanent) من System Users كما شرحنا في الخطوة 4.',
            },
            {
              q: 'متى ستتوفر الطريقة السهلة (Meta Embedded Signup)؟',
              a: 'بعد اكتمال عملية اعتماد Meta الرسمية لنحلة. سيتم إشعارك عند التفعيل.',
            },
          ].map(({ q, a }) => (
            <div key={q} className="border-b border-slate-50 pb-4 last:border-0 last:pb-0">
              <p className="text-sm font-semibold text-slate-800 mb-1">{q}</p>
              <p className="text-sm text-slate-600">{a}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── CTA ────────────────────────────────────────────────────────────── */}
      <div className="text-center py-4">
        <a
          href="/whatsapp-connect"
          className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-6 py-3 rounded-xl transition shadow-lg shadow-emerald-600/20"
        >
          <MessageCircle className="w-4 h-4" />
          ابدأ ربط واتساب الآن
        </a>
      </div>
    </div>
  )
}
