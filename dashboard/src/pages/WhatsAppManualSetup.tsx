/**
 * WhatsAppManualSetup.tsx
 * ────────────────────────
 * /help/whatsapp-manual-setup
 *
 * دليل عربي لتجهيز Meta Business وربط حساب واتساب للأعمال بنحلة.
 *
 * الصور (dashboard/public/help/whatsapp-manual-setup/):
 *   01-meta-business-home.png
 *   02-business-settings.png
 *   03-whatsapp-accounts.png
 *   04-add-or-select-waba.png
 *   05-add-phone-number.png
 *   06-verify-phone-number.png
 *   07-whatsapp-manager-status.png
 *   08-nahlah-whatsapp-connect.png
 *   09-nahlah-assisted-request.png
 *   10-commerce-catalog-later.png  (اختياري — الكتالوج لاحقاً)
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  ImageIcon,
  Info,
  Link2,
  MessageCircle,
  ShieldCheck,
} from 'lucide-react'

const HELP_IMAGE_BASE = '/help/whatsapp-manual-setup'

export const HELP_MANUAL_SETUP_IMAGES = [
  '01-meta-business-home.png',
  '02-business-settings.png',
  '03-whatsapp-accounts.png',
  '04-add-or-select-waba.png',
  '05-add-phone-number.png',
  '06-verify-phone-number.png',
  '07-whatsapp-manager-status.png',
  '08-nahlah-whatsapp-connect.png',
  '09-nahlah-assisted-request.png',
  '10-commerce-catalog-later.png',
] as const

const QUICK_LINKS = [
  { label: 'لوحة نحلة', href: 'https://app.nahlah.ai' },
  { label: 'صفحة ربط واتساب', href: 'https://app.nahlah.ai/whatsapp-connect' },
  { label: 'Meta Business', href: 'https://business.facebook.com/' },
  { label: 'إعدادات النشاط التجاري', href: 'https://business.facebook.com/settings' },
  { label: 'WhatsApp Manager', href: 'https://business.facebook.com/latest/whatsapp_manager' },
] as const

const PATH_EXISTING_ACCOUNT = [
  'افتح «ربط عبر Meta» في نحلة',
  'سجّل دخولك بحساب فيسبوك',
  'اختر النشاط التجاري',
  'اختر حساب واتساب للأعمال الموجود',
  'اختر رقم واتساب الأعمال',
  'أكمل التحقق والربط',
]

const PATH_NEW_ACCOUNT = [
  'افتح «ربط عبر Meta» في نحلة',
  'سجّل دخولك بحساب فيسبوك',
  'أنشئ أو اختر Meta Business',
  'أنشئ حساب واتساب للأعمال أثناء خطوات Meta إذا ظهر لك الخيار',
  'أضف رقم واتساب الأعمال',
  'أكمل التحقق',
]

type GuideStep = {
  num: number
  title: string
  body: string
  image: (typeof HELP_MANUAL_SETUP_IMAGES)[number]
  href?: string
  path?: string
}

const GUIDE_STEPS: GuideStep[] = [
  {
    num: 1,
    title: 'فتح Meta Business',
    href: 'https://business.facebook.com/',
    image: '01-meta-business-home.png',
    body: 'افتح Meta Business وسجّل الدخول بحساب فيسبوك المرتبط بنشاطك التجاري. إذا لم يكن لديك Business Portfolio، أنشئ واحداً باسم نشاطك.',
  },
  {
    num: 2,
    title: 'التأكد من Business Portfolio',
    href: 'https://business.facebook.com/settings',
    image: '02-business-settings.png',
    body: 'من إعدادات النشاط التجاري تأكد أن لديك صلاحية Admin وأن اسم النشاط صحيح.',
  },
  {
    num: 3,
    title: 'فتح حسابات واتساب',
    path: 'Accounts → WhatsApp accounts  ·  الحسابات → حسابات واتساب',
    image: '03-whatsapp-accounts.png',
    body: 'تأكد من وجود حساب واتساب للأعمال مرتبط بنشاطك، أو جهّز نفسك لإنشاء حساب جديد في الخطوة التالية.',
  },
  {
    num: 4,
    title: 'ربط أو إنشاء حساب واتساب للأعمال',
    image: '04-add-or-select-waba.png',
    body: 'إذا كان لديك حساب واتساب للأعمال مسبقاً، اختره. إذا لم يكن لديك، أنشئ حساباً جديداً أثناء خطوات Meta أو من WhatsApp Manager عندما يتوفر الخيار.',
  },
  {
    num: 5,
    title: 'إضافة رقم واتساب الأعمال',
    image: '05-add-phone-number.png',
    body: 'أضف رقم واتساب الأعمال الذي تريد استخدامه مع نحلة. يجب أن يكون الرقم قابلاً لاستقبال رمز التحقق (SMS أو مكالمة).',
  },
  {
    num: 6,
    title: 'التحقق من الرقم',
    image: '06-verify-phone-number.png',
    body: 'أكمل التحقق من الرقم داخل Meta. بدون تحقق ناجح لن يعمل الربط مع نحلة.',
  },
  {
    num: 7,
    title: 'مراجعة الحالة في WhatsApp Manager',
    href: 'https://business.facebook.com/latest/whatsapp_manager',
    image: '07-whatsapp-manager-status.png',
    body: 'من WhatsApp Manager تحقق أن الرقم ظاهر وحالته جاهزة أو قيد التفعيل قبل العودة إلى نحلة.',
  },
  {
    num: 8,
    title: 'العودة إلى نحلة — تجربة ربط Meta',
    href: 'https://app.nahlah.ai/whatsapp-connect',
    image: '08-nahlah-whatsapp-connect.png',
    body: 'ارجع إلى صفحة ربط واتساب في نحلة واضغط «ربط عبر Meta» لإكمال الربط إذا كان مسار Meta متاحاً لحسابك.',
  },
  {
    num: 9,
    title: 'أو طلب مساعدة فريق نحلة',
    href: 'https://app.nahlah.ai/whatsapp-connect',
    image: '09-nahlah-assisted-request.png',
    body: 'إذا واجهت صعوبة، اختر «طلب ربط بمساعدة فريق نحلة». لا تدخل Access Token أو معرفات Meta في واجهة التاجر — فريقنا يكمل الربط معك بأمان.',
  },
]

const NAHLA_NEEDS = [
  'Business ID',
  'WhatsApp Business Account ID',
  'Phone Number ID',
  'رقم واتساب المعروض',
  'Permanent System User Access Token عند الحاجة فقط',
  'Meta Catalog ID لاحقاً إذا أردنا ربط الكتالوج',
]

function HelpStepImage({ filename, alt }: { filename: string; alt: string }) {
  const [missing, setMissing] = useState(false)
  const src = `${HELP_IMAGE_BASE}/${filename}`

  if (missing) {
    return (
      <div className="w-full rounded-xl overflow-hidden border-2 border-dashed border-violet-200 bg-gradient-to-br from-slate-50 to-violet-50/40 flex flex-col items-center justify-center py-12 gap-3 text-slate-500">
        <ImageIcon className="w-10 h-10 text-violet-300" />
        <p className="text-sm font-medium text-slate-600">سيتم إضافة الصورة هنا:</p>
        <code className="text-xs font-mono bg-white/80 border border-slate-200 px-3 py-1.5 rounded-lg text-violet-700">
          {filename}
        </code>
      </div>
    )
  }

  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setMissing(true)}
      className="w-full rounded-xl border border-slate-200 shadow-sm bg-white object-contain max-h-[420px]"
    />
  )
}

function TipBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 flex gap-3">
      <Info className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
      <div className="text-sm text-blue-800 leading-relaxed">{children}</div>
    </div>
  )
}

function WarnBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 flex gap-3">
      <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <div className="text-sm text-amber-900 leading-relaxed">{children}</div>
    </div>
  )
}

function ExternalHref({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-emerald-600 hover:text-emerald-700 underline font-medium break-all"
    >
      {children}
      <ExternalLink className="w-3.5 h-3.5 shrink-0" />
    </a>
  )
}

function StepCard({
  num,
  title,
  defaultOpen = false,
  children,
}: {
  num: number
  title: string
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(p => !p)}
        className="w-full flex items-center gap-4 px-5 py-4 text-right hover:bg-slate-50 transition"
      >
        <div
          className={`w-9 h-9 rounded-xl flex items-center justify-center text-base font-black shrink-0 ${
            open ? 'bg-emerald-500 text-white' : 'bg-slate-100 text-slate-500'
          }`}
        >
          {num}
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-bold text-slate-800 text-sm">{title}</p>
        </div>
        {open
          ? <ChevronUp className="w-4 h-4 text-slate-400 shrink-0" />
          : <ChevronDown className="w-4 h-4 text-slate-400 shrink-0" />}
      </button>
      {open && (
        <div className="px-5 pb-5 space-y-4 border-t border-slate-50 pt-4">
          {children}
        </div>
      )}
    </div>
  )
}

function PathCard({ title, steps }: { title: string; steps: string[] }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
      <p className="font-semibold text-slate-800 text-sm">{title}</p>
      <ol className="space-y-2">
        {steps.map((step, i) => (
          <li key={step} className="flex items-start gap-3 text-sm text-slate-700">
            <span className="w-6 h-6 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center text-xs font-bold shrink-0">
              {i + 1}
            </span>
            <span className="leading-relaxed">{step}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

export default function WhatsAppManualSetup() {
  const navigate = useNavigate()

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-8" dir="rtl">

      <button
        type="button"
        onClick={() => navigate(-1)}
        className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 transition"
      >
        <ArrowLeft className="w-4 h-4" />
        رجوع
      </button>

      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-2xl bg-emerald-500 flex items-center justify-center shadow-lg shadow-emerald-500/25">
            <MessageCircle className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-black text-slate-900">دليل ربط واتساب الأعمال بنحلة</h1>
            <p className="text-sm text-slate-500">تجهيز Meta Business وربط حساب واتساب للأعمال</p>
          </div>
        </div>

        <TipBox>
          هذا الدليل يركز على <strong>ربط رقم واتساب الأعمال بنحلة</strong>.
          لا تحتاج لإرسال Access Token أو معرفات Meta من واجهة التاجر.
          عند اختيار «طلب ربط بمساعدة فريق نحلة»، فريقنا يتولى الربط الآمن.
        </TipBox>

        <WarnBox>
          لا ترسل Access Token أو بيانات حساسة داخل محادثة عامة.
          فريق نحلة سيطلب البيانات بالطريقة المناسبة عند الحاجة.
        </WarnBox>
      </div>

      <div className="space-y-3">
        <p className="font-bold text-slate-800">مساران من صفحة ربط واتساب في نحلة</p>
        <PathCard
          title="المسار الأول — لديك حساب واتساب للأعمال مسبقاً"
          steps={PATH_EXISTING_ACCOUNT}
        />
        <PathCard
          title="المسار الثاني — لا يوجد لديك حساب واتساب للأعمال بعد"
          steps={PATH_NEW_ACCOUNT}
        />
      </div>

      <div className="bg-slate-50 rounded-2xl border border-slate-100 p-5 space-y-3">
        <div className="flex items-center gap-2 text-slate-800 font-bold text-sm">
          <Link2 className="w-4 h-4 text-emerald-500" />
          روابط سريعة
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {QUICK_LINKS.map(link => (
            <a
              key={link.href}
              href={link.href}
              target="_blank"
              rel="noreferrer"
              className="flex items-center justify-between gap-2 px-3 py-2.5 rounded-xl bg-white border border-slate-200 text-sm text-slate-700 hover:border-emerald-300 hover:text-emerald-700 transition"
            >
              <span>{link.label}</span>
              <ExternalLink className="w-3.5 h-3.5 shrink-0 text-slate-400" />
            </a>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        <p className="font-bold text-slate-800">خطوات التجهيز في Meta</p>
        {GUIDE_STEPS.map(step => (
          <StepCard key={step.num} num={step.num} title={step.title} defaultOpen={step.num === 1}>
            <p className="text-sm text-slate-700 leading-relaxed">{step.body}</p>
            {step.href && (
              <p className="text-sm">
                <span className="text-slate-500">الرابط: </span>
                <ExternalHref href={step.href}>{step.href}</ExternalHref>
              </p>
            )}
            {step.path && (
              <p className="text-sm bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 font-mono text-slate-600">
                {step.path}
              </p>
            )}
            <HelpStepImage filename={step.image} alt={step.title} />
          </StepCard>
        ))}
      </div>

      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-4">
        <p className="font-bold text-slate-800">الكتالوج خطوة لاحقة</p>
        <p className="text-sm text-slate-600 leading-relaxed">
          بعد اكتمال ربط واتساب، يمكن لاحقاً ربط Meta Catalog حتى تظهر المنتجات داخل واتساب.
          هذه الخطوة <strong>ليست مطلوبة</strong> لإتمام ربط رقم واتساب بنحلة.
        </p>
        <HelpStepImage filename="10-commerce-catalog-later.png" alt="Meta Catalog — خطوة لاحقة" />
      </div>

      <div className="bg-white rounded-2xl border border-emerald-200 shadow-sm p-5 space-y-4">
        <div className="flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-emerald-600" />
          <p className="font-bold text-slate-800">بيانات قد يطلبها فريق نحلة عند الربط اليدوي</p>
        </div>
        <p className="text-xs text-slate-500">
          للاستخدام الداخلي مع فريق نحلة فقط — لا تُدخل في واجهة التاجر.
        </p>
        <ul className="space-y-2">
          {NAHLA_NEEDS.map(item => (
            <li
              key={item}
              className="flex items-center gap-2 text-sm text-slate-700 bg-emerald-50/60 border border-emerald-100 rounded-xl px-3 py-2.5"
            >
              <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
              <span className="font-mono text-xs sm:text-sm">{item}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="bg-slate-900 rounded-2xl p-5 text-slate-300 space-y-2">
        <p className="text-sm font-bold text-white flex items-center gap-2">
          <BookOpen className="w-4 h-4" />
          قائمة الصور المطلوبة
        </p>
        <ul className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 pt-1">
          {HELP_MANUAL_SETUP_IMAGES.map(name => (
            <li key={name} className="text-xs font-mono text-emerald-400/90">{name}</li>
          ))}
        </ul>
      </div>

      <div className="text-center py-2">
        <a
          href="/whatsapp-connect"
          className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-6 py-3 rounded-xl transition shadow-lg shadow-emerald-600/20"
        >
          <MessageCircle className="w-4 h-4" />
          انتقل إلى صفحة ربط واتساب
        </a>
      </div>
    </div>
  )
}
