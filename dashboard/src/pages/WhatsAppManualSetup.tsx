/**
 * WhatsAppManualSetup.tsx
 * ────────────────────────
 * /help/whatsapp-manual-setup
 *
 * Merchant-facing guide for preparing Meta Business and connecting WhatsApp.
 * Image assets live under dashboard/public/help/whatsapp-manual-setup/ (see README there).
 */
import { useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  ImageIcon,
  Info,
  MessageCircle,
} from 'lucide-react'

const HELP_IMAGE_BASE = '/help/whatsapp-manual-setup'

const HELP_MANUAL_SETUP_IMAGES = [
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

const BEFORE_YOU_START = [
  'حساب فيسبوك يمكنك الدخول إليه',
  'صلاحية إدارة النشاط التجاري في Meta',
  'رقم واتساب أعمال يمكنه استقبال رمز التحقق',
  'اسم النشاط التجاري ومعلوماته الأساسية',
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
    body: 'إذا واجهت صعوبة، اختر «طلب ربط بمساعدة فريق نحلة». فريقنا يكمل الربط معك بأمان خطوة بخطوة.',
  },
]

function HelpStepImage({ filename, alt }: { filename: string; alt: string }) {
  const [missing, setMissing] = useState(false)
  const src = `${HELP_IMAGE_BASE}/${filename}`

  if (missing) {
    return (
      <div className="w-full rounded-xl overflow-hidden border-2 border-dashed border-violet-200 bg-gradient-to-br from-slate-50 to-violet-50/40 flex flex-col items-center justify-center py-12 gap-3 text-slate-500">
        <ImageIcon className="w-10 h-10 text-violet-300" />
        <p className="text-sm font-medium text-slate-600">صورة توضيحية — قريباً</p>
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

function InfoSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-3">
      <p className="font-bold text-slate-800">{title}</p>
      {children}
    </div>
  )
}

export default function WhatsAppManualSetup() {
  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-8" dir="rtl">

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
          هذا الدليل يساعدك على تجهيز حساب واتساب الأعمال وربطه بنحلة.
          يمكنك الإكمال عبر Meta مباشرة، أو طلب مساعدة فريق نحلة إذا احتجت.
        </TipBox>
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
              <p className="text-sm bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 text-slate-600">
                {step.path}
              </p>
            )}
            <HelpStepImage filename={step.image} alt={step.title} />
          </StepCard>
        ))}
      </div>

      <InfoSection title="ماذا تحتاج قبل البدء؟">
        <ul className="space-y-2">
          {BEFORE_YOU_START.map(item => (
            <li key={item} className="flex items-start gap-3 text-sm text-slate-700">
              <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0 mt-2" />
              <span className="leading-relaxed">{item}</span>
            </li>
          ))}
        </ul>
      </InfoSection>

      <InfoSection title="ماذا يحدث بعد طلب المساعدة؟">
        <p className="text-sm text-slate-600 leading-relaxed">
          بعد إرسال طلب الربط، سيتواصل معك فريق نحلة لإكمال الخطوات بطريقة آمنة.
          لا ترسل أي بيانات حساسة أو رموز دخول في محادثة عامة.
          سنرشدك خطوة بخطوة من داخل حسابك في Meta عند الحاجة.
        </p>
      </InfoSection>

      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-4">
        <p className="font-bold text-slate-800">الكتالوج خطوة لاحقة</p>
        <p className="text-sm text-slate-600 leading-relaxed">
          ربط المنتجات والكتالوج يتم لاحقاً بعد اكتمال ربط واتساب.
          لا تحتاج لإعداد الكتالوج الآن لإكمال ربط الرقم.
        </p>
        <HelpStepImage filename="10-commerce-catalog-later.png" alt="الكتالوج — خطوة لاحقة" />
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
