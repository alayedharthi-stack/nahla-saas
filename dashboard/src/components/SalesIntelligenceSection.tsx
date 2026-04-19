/**
 * SalesIntelligenceSection.tsx
 * ─────────────────────────────
 * Marketing section appended below the registration form.
 * Explains the collective sales-learning value proposition
 * to merchants without altering the existing form layout.
 *
 * Notes
 * ─────
 * * Bilingual (ar/en) — content is intentionally inlined here
 *   instead of going through the i18n dictionary because it is
 *   bespoke marketing copy.
 * * Dark theme to match the slate-900 page background; cards use
 *   subtle white/transparent surfaces so the form remains the
 *   visual focal point.
 * * RTL-safe: uses logical Tailwind utilities (ms-*, me-*) and
 *   relies on the parent's ``dir`` attribute.
 */
import {
  Users,
  FlaskConical,
  Clock,
  UserRoundCog,
  Network,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react'

interface Props {
  lang: 'ar' | 'en'
}

interface Card {
  icon:    LucideIcon
  title:   string
  body:    string
  /** Optional callout line rendered in a highlighted pill below the body. */
  callout?: string
}

const CONTENT: Record<'ar' | 'en', {
  badge:    string
  title:    string
  subtitle: string
  cards:    Card[]
  privacy:  string
}> = {
  ar: {
    badge:    'لماذا نحلة؟',
    title:    'كيف تساعد نحلة متجرك على البيع أكثر؟',
    subtitle: 'خبرة مبيعات جماعية تتعلم من آلاف المحادثات لتزيد مبيعات متجرك.',
    cards: [
      {
        icon:  Users,
        title: 'خبرة مبيعات من آلاف المحادثات',
        body:
          'موظف المبيعات يتعلم عادةً من عشرات أو مئات العملاء فقط، ' +
          'لكن نحلة تتعلم من آلاف العملاء عبر مئات المتاجر.\n\n' +
          'إذا كان 300 متجر يستخدم نحلة، وكل متجر لديه 200 محادثة يوميًا، ' +
          'فهذا يعني أن نحلة تتعلم من أكثر من:',
        callout: '60,000 محادثة بيع يوميًا',
      },
      {
        icon:  FlaskConical,
        title: 'تجربة مستمرة لتحسين المبيعات',
        body:
          'بدلاً من استخدام طريقة واحدة في البيع، نحلة تجرّب طرقًا مختلفة:\n' +
          '• طريقة عرض المنتجات\n' +
          '• عدد المنتجات المقترحة\n' +
          '• ترتيب المنتجات\n' +
          '• أسلوب الرد على العميل\n\n' +
          'ثم تقيس النتائج لتعرف ما الذي يجعل العميل يشتري، وما الذي يجعله يتردد، ' +
          'وتستخدم الطريقة التي تحقق أفضل نتيجة لمتجرك.',
      },
      {
        icon:  Clock,
        title: 'تعمل 24 ساعة بدون توقف',
        body:
          'نحلة لا تتعب ولا تنشغل:\n' +
          '• ترد فورًا على كل رسالة\n' +
          '• لا تنسى أي عميل\n' +
          '• تتابع العملاء المترددين\n' +
          '• تعمل 24 ساعة يوميًا\n\n' +
          'وكأن لديك فريق مبيعات يعمل داخل واتساب طوال الوقت.',
      },
      {
        icon:  UserRoundCog,
        title: 'أسلوب بيع مختلف لكل عميل',
        body:
          'ليس كل العملاء متشابهين: بعضهم يريد الشراء بسرعة، ' +
          'وبعضهم يحب المقارنة، وبعضهم يحتاج معلومات أكثر قبل اتخاذ القرار.\n\n' +
          'مع الوقت تتعلم نحلة هذه الأنماط، فتغيّر أسلوب البيع حسب كل عميل.',
      },
      {
        icon:  Network,
        title: 'شبكة تعلم المبيعات',
        body:
          'كل متجر يستخدم نحلة يضيف معرفة جديدة للنظام، ' +
          'والأجمل أن هذه المعرفة تعود بالفائدة على الجميع.\n\n' +
          'عندما ينضم متجرك إلى نحلة فإنه لا يبدأ من الصفر، ' +
          'بل يبدأ باستخدام خبرة متراكمة من آلاف المحادثات.',
        callout: 'هذا ما نسميه: شبكة تعلم المبيعات',
      },
    ],
    privacy:
      'خصوصية بيانات المتاجر أمر أساسي في نحلة. ' +
      'بيانات متجرك وعملائك تبقى خاصة بك فقط، ويتم حفظها وفق الأنظمة ' +
      'واللوائح المعمول بها في المملكة العربية السعودية. ' +
      'نحلة تتعلم من الأنماط العامة للمبيعات فقط دون الكشف عن أي بيانات خاصة بالمتاجر.',
  },
  en: {
    badge:    'Why Nahla?',
    title:    'How does Nahla help your store sell more?',
    subtitle: 'Collective sales intelligence learning from thousands of conversations to grow your store.',
    cards: [
      {
        icon:  Users,
        title: 'Sales experience from thousands of conversations',
        body:
          'A sales agent typically learns from tens or hundreds of customers. ' +
          'Nahla learns from thousands of customers across hundreds of stores.\n\n' +
          'If 300 stores use Nahla, with 200 conversations per store per day, ' +
          'that means Nahla learns from more than:',
        callout: '60,000 sales conversations daily',
      },
      {
        icon:  FlaskConical,
        title: 'Continuous experimentation to grow sales',
        body:
          'Instead of using a single sales playbook, Nahla tries different approaches:\n' +
          '• How products are presented\n' +
          '• Number of suggested products\n' +
          '• Product ordering\n' +
          '• Reply style\n\n' +
          'It then measures results to learn what makes customers buy, ' +
          'and uses the approach that delivers the best outcome for your store.',
      },
      {
        icon:  Clock,
        title: 'Works 24 hours, never tires',
        body:
          'Nahla does not get tired or distracted:\n' +
          '• Replies instantly to every message\n' +
          '• Never forgets a customer\n' +
          '• Follows up with hesitant buyers\n' +
          '• Works 24 hours a day\n\n' +
          'Like having a sales team running inside WhatsApp around the clock.',
      },
      {
        icon:  UserRoundCog,
        title: 'A different sales style for every customer',
        body:
          'Not all customers are alike: some buy quickly, ' +
          'some compare options, and some need more information first.\n\n' +
          'Over time Nahla learns these patterns and adapts its sales style ' +
          'to each individual customer.',
      },
      {
        icon:  Network,
        title: 'A sales-learning network',
        body:
          'Every store using Nahla adds new knowledge to the system, ' +
          'and that knowledge benefits everyone.\n\n' +
          'When your store joins Nahla you do not start from scratch — ' +
          'you start with experience accumulated from thousands of conversations.',
        callout: 'This is what we call: the Sales Learning Network',
      },
    ],
    privacy:
      'Store-data privacy is fundamental to Nahla. ' +
      'Your store and customer data remain private to you and are stored ' +
      'in compliance with the regulations of the Kingdom of Saudi Arabia. ' +
      'Nahla learns only from general sales patterns and never exposes ' +
      'any private store data.',
  },
}

export default function SalesIntelligenceSection({ lang }: Props) {
  const c = CONTENT[lang]

  return (
    <section
      aria-label={c.title}
      className="relative bg-slate-900 px-4 pt-12 pb-16 sm:pt-16 sm:pb-20"
    >
      {/* subtle radial glow at top to visually separate from the form above */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 -top-px h-px
                   bg-gradient-to-r from-transparent via-amber-500/40 to-transparent"
      />

      <div className="mx-auto max-w-5xl">
        {/* Header */}
        <div className="text-center mb-10 sm:mb-14">
          <span
            className="inline-flex items-center px-3 py-1 rounded-full
                       bg-amber-500/15 border border-amber-500/40
                       text-amber-300 text-xs font-semibold tracking-wide mb-4"
          >
            <span className="me-1.5">🐝</span>
            {c.badge}
          </span>
          <h2 className="text-2xl sm:text-3xl md:text-4xl font-bold text-white leading-tight">
            {c.title}
          </h2>
          <p className="mt-3 text-slate-400 text-sm sm:text-base max-w-2xl mx-auto">
            {c.subtitle}
          </p>
        </div>

        {/* Cards grid */}
        <div className="grid gap-4 sm:gap-5 sm:grid-cols-2">
          {c.cards.map((card, idx) => {
            const Icon = card.icon
            // The first card spans both columns on sm+ screens to give the
            // hero "60,000 conversations daily" stat extra visual weight.
            const span = idx === 0 ? 'sm:col-span-2' : ''
            return (
              <article
                key={card.title}
                className={`group relative rounded-2xl border border-white/10
                            bg-white/5 hover:bg-white/[0.07] transition-colors
                            p-5 sm:p-6 ${span}`}
              >
                <div className="flex items-start gap-3 sm:gap-4">
                  <div
                    className="shrink-0 w-10 h-10 sm:w-11 sm:h-11 rounded-xl
                               bg-amber-500/15 border border-amber-500/30
                               flex items-center justify-center
                               text-amber-400 group-hover:text-amber-300
                               transition-colors"
                  >
                    <Icon className="w-5 h-5 sm:w-5.5 sm:h-5.5" strokeWidth={1.8} />
                  </div>
                  <div className="min-w-0">
                    <h3 className="text-white font-semibold text-base sm:text-lg leading-snug">
                      {card.title}
                    </h3>
                    <p className="mt-2 text-slate-300/90 text-sm leading-relaxed whitespace-pre-line">
                      {card.body}
                    </p>
                    {card.callout && (
                      <div className="mt-4 inline-flex items-center
                                      px-3.5 py-2 rounded-xl
                                      bg-gradient-to-r from-amber-500/20 to-amber-500/10
                                      border border-amber-500/40
                                      text-amber-200 text-sm font-bold tracking-wide">
                        {card.callout}
                      </div>
                    )}
                  </div>
                </div>
              </article>
            )
          })}
        </div>

        {/* Privacy reassurance */}
        <div className="mt-8 sm:mt-10 rounded-2xl border border-emerald-500/20
                        bg-emerald-500/5 p-5 sm:p-6 flex items-start gap-3 sm:gap-4">
          <div className="shrink-0 w-10 h-10 rounded-xl
                          bg-emerald-500/15 border border-emerald-500/30
                          flex items-center justify-center text-emerald-400">
            <ShieldCheck className="w-5 h-5" strokeWidth={1.8} />
          </div>
          <p className="text-emerald-100/90 text-sm leading-relaxed">
            {c.privacy}
          </p>
        </div>
      </div>
    </section>
  )
}
