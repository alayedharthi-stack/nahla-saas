/**
 * SalesIntelligenceSection.tsx
 * ─────────────────────────────
 * Marketing section explaining Nahla's collective sales-learning value.
 *
 * Lives on the Landing page (between the "Problem strip" and the
 * "How does Nahla work?" section). It used to be appended below the
 * registration / login forms — that placement distracted from the
 * primary form CTA, so it now belongs on the landing page where it
 * helps convert visitors before they reach the sign-up flow.
 *
 * Notes
 * ─────
 * * Bilingual (ar/en) — content is intentionally inlined here instead of
 *   going through the i18n dictionary because it is bespoke marketing copy.
 * * Visual style mirrors the landing-page section conventions:
 *   small amber "kicker" label → big white headline → supporting subtitle
 *   → cards. Each card uses a subtly different accent tone (amber, blue,
 *   emerald, violet) to add variety without breaking the brand identity.
 * * RTL-safe: uses logical Tailwind utilities and relies on the parent's
 *   ``dir`` attribute (the landing page sets ``dir="rtl"``).
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
  /** UI language. Landing page is Arabic-only today, but the component
   *  remains bilingual in case it is reused elsewhere later. */
  lang?: 'ar' | 'en'
}

type Accent = 'amber' | 'blue' | 'emerald' | 'violet'

interface Card {
  icon:    LucideIcon
  title:   string
  body:    string
  /** Optional callout line rendered in a highlighted pill below the body. */
  callout?: string
  accent:   Accent
}

const CONTENT: Record<'ar' | 'en', {
  kicker:   string
  title:    string
  subtitle: string
  cards:    Card[]
  privacy:  string
}> = {
  ar: {
    kicker:   'ميزة نحلة الفريدة',
    title:    'كيف تساعد نحلة متجرك على البيع أكثر؟',
    subtitle: 'خبرة مبيعات جماعية تتعلم من آلاف المحادثات لتزيد مبيعات متجرك.',
    cards: [
      {
        icon:  Users,
        accent: 'amber',
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
        accent: 'blue',
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
        accent: 'emerald',
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
        accent: 'violet',
        title: 'أسلوب بيع مختلف لكل عميل',
        body:
          'ليس كل العملاء متشابهين: بعضهم يريد الشراء بسرعة، ' +
          'وبعضهم يحب المقارنة، وبعضهم يحتاج معلومات أكثر قبل اتخاذ القرار.\n\n' +
          'مع الوقت تتعلم نحلة هذه الأنماط، فتغيّر أسلوب البيع حسب كل عميل.',
      },
      {
        icon:  Network,
        accent: 'amber',
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
    kicker:   "Nahla's Edge",
    title:    'How does Nahla help your store sell more?',
    subtitle: 'Collective sales intelligence learning from thousands of conversations to grow your store.',
    cards: [
      {
        icon:  Users,
        accent: 'amber',
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
        accent: 'blue',
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
        accent: 'emerald',
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
        accent: 'violet',
        title: 'A different sales style for every customer',
        body:
          'Not all customers are alike: some buy quickly, ' +
          'some compare options, and some need more information first.\n\n' +
          'Over time Nahla learns these patterns and adapts its sales style ' +
          'to each individual customer.',
      },
      {
        icon:  Network,
        accent: 'amber',
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

// Tailwind class lookup table — keeps every utility a literal string so
// the JIT compiler does not strip them. Each accent has matching
// background, border, icon, glow and hover-border tokens.
const ACCENT: Record<Accent, {
  iconBg:   string
  iconRing: string
  iconText: string
  hoverBorder: string
  glow:     string
}> = {
  amber: {
    iconBg:      'bg-amber-500/15',
    iconRing:    'ring-amber-400/30',
    iconText:    'text-amber-300',
    hoverBorder: 'hover:border-amber-400/40',
    glow:        'group-hover:shadow-amber-500/15',
  },
  blue: {
    iconBg:      'bg-blue-500/15',
    iconRing:    'ring-blue-400/30',
    iconText:    'text-blue-300',
    hoverBorder: 'hover:border-blue-400/40',
    glow:        'group-hover:shadow-blue-500/15',
  },
  emerald: {
    iconBg:      'bg-emerald-500/15',
    iconRing:    'ring-emerald-400/30',
    iconText:    'text-emerald-300',
    hoverBorder: 'hover:border-emerald-400/40',
    glow:        'group-hover:shadow-emerald-500/15',
  },
  violet: {
    iconBg:      'bg-violet-500/15',
    iconRing:    'ring-violet-400/30',
    iconText:    'text-violet-300',
    hoverBorder: 'hover:border-violet-400/40',
    glow:        'group-hover:shadow-violet-500/15',
  },
}

export default function SalesIntelligenceSection({ lang = 'ar' }: Props) {
  const c = CONTENT[lang]

  return (
    <section
      id="why"
      aria-label={c.title}
      className="relative bg-slate-900 py-24 overflow-hidden"
    >
      {/* Faint honeycomb texture — matches sibling landing sections. */}
      <svg
        aria-hidden
        className="absolute inset-0 w-full h-full opacity-[0.04] pointer-events-none select-none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <pattern id="why-hex" x="0" y="0" width="60" height="52" patternUnits="userSpaceOnUse">
            <polygon points="30,2 58,17 58,47 30,62 2,47 2,17" fill="none" stroke="#F59E0B" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#why-hex)" />
      </svg>

      {/* Soft ambient glow */}
      <div
        aria-hidden
        className="absolute top-1/3 right-1/4 w-[480px] h-[480px] bg-amber-500/8 rounded-full blur-[100px] pointer-events-none"
      />

      <div className="relative z-10 mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        {/* Section header — same kicker/title/subtitle pattern used
            elsewhere on the landing page for visual consistency. */}
        <div className="text-center mb-14">
          <p className="text-amber-500 font-bold text-xs uppercase tracking-widest mb-3">
            {c.kicker}
          </p>
          <h2 className="text-4xl sm:text-5xl font-black text-white leading-tight mb-4 tracking-tight">
            {c.title}
          </h2>
          <p className="text-slate-300 max-w-2xl mx-auto leading-relaxed text-base sm:text-lg">
            {c.subtitle}
          </p>
        </div>

        {/* Cards grid — first card spans both columns to give the
            "60,000 conversations daily" stat extra visual weight. */}
        <div className="grid gap-5 sm:gap-6 sm:grid-cols-2">
          {c.cards.map((card, idx) => {
            const Icon  = card.icon
            const tone  = ACCENT[card.accent]
            const span  = idx === 0 ? 'sm:col-span-2' : ''

            return (
              <article
                key={card.title}
                className={[
                  'group relative rounded-2xl p-6 sm:p-7',
                  'bg-slate-800/60 backdrop-blur-sm',
                  'border border-white/8',
                  'shadow-lg shadow-black/10',
                  'transition-all duration-300',
                  'hover:-translate-y-0.5 hover:bg-slate-800/80',
                  'hover:shadow-xl',
                  tone.hoverBorder,
                  tone.glow,
                  span,
                ].join(' ')}
              >
                <div className="flex items-start gap-4 sm:gap-5">
                  {/* Accent-tinted icon tile */}
                  <div
                    className={[
                      'shrink-0 w-12 h-12 sm:w-[52px] sm:h-[52px] rounded-xl',
                      'flex items-center justify-center',
                      'ring-1 transition-colors',
                      tone.iconBg,
                      tone.iconRing,
                      tone.iconText,
                    ].join(' ')}
                  >
                    <Icon className="w-5 h-5 sm:w-[22px] sm:h-[22px]" strokeWidth={1.9} />
                  </div>

                  <div className="min-w-0 flex-1">
                    {/* Card heading — bumped weight + size for clearer hierarchy */}
                    <h3 className="text-white font-black text-lg sm:text-xl leading-snug mb-3">
                      {card.title}
                    </h3>

                    {/* Body copy — lighter slate for higher contrast on dark */}
                    <p className="text-slate-300 text-sm sm:text-[15px] leading-loose whitespace-pre-line">
                      {card.body}
                    </p>

                    {card.callout && (
                      <div
                        className="mt-5 inline-flex items-center
                                   px-4 py-2.5 rounded-xl
                                   bg-gradient-to-l from-amber-500/25 to-amber-500/10
                                   border border-amber-400/40
                                   text-amber-200 text-sm sm:text-base font-black tracking-wide
                                   shadow-md shadow-amber-500/10"
                      >
                        {card.callout}
                      </div>
                    )}
                  </div>
                </div>
              </article>
            )
          })}
        </div>

        {/* Privacy reassurance — kept its emerald accent because privacy
            messaging is universally recognised by green/shield language. */}
        <div className="mt-10 rounded-2xl border border-emerald-500/25
                        bg-emerald-500/5 backdrop-blur-sm p-5 sm:p-6
                        flex items-start gap-4">
          <div className="shrink-0 w-11 h-11 rounded-xl
                          bg-emerald-500/15 ring-1 ring-emerald-400/30
                          flex items-center justify-center text-emerald-300">
            <ShieldCheck className="w-5 h-5" strokeWidth={1.9} />
          </div>
          <p className="text-emerald-100/90 text-sm sm:text-[15px] leading-loose">
            {c.privacy}
          </p>
        </div>
      </div>
    </section>
  )
}
