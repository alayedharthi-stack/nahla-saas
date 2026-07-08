/**
 * Landing page copy — Arabic and English.
 * Bespoke marketing strings for /landing only (not in dashboard ar.ts/en.ts).
 */

import type { Lang } from './types'

export interface LandingNavLink {
  label: string
  id: string
}

export interface LandingFaqItem {
  q: string
  a: string
}

export interface LandingFeatureItem {
  title: string
  desc: string
  outcome?: string
  highlight?: boolean
}

export interface LandingStepItem {
  num: string
  title: string
  desc: string
  time: string
}

export interface LandingTestimonial {
  quote: string
  name: string
  store: string
  result: string
}

export interface LandingStatItem {
  value: string
  label: string
  sub: string
}

export interface LandingCopy {
  brandName: string
  nav: LandingNavLink[]
  navLogin: string
  navTrial: string
  navTrialMobile: string
  navLoginMobile: string
  menuOpen: string
  menuClose: string
  langSwitch: string
  hero: {
    badge: string
    titleLine1: string
    titleLine2: string
    subtitle: string
    ctaTrialMobile: string
    ctaTrialDesktop: string
    ctaHow: string
    pill: string
    stackAria: string
    stackWa: string
    stackAi: string
    stackCampaign: string
    stackNote: string
    riskReversal: string
    socialStores: string
    socialRating: string
    socialTrial: string
  }
  problem: {
    heading: string
    items: string[]
    closing: string
  }
  how: {
    eyebrow: string
    title: string
    subtitle: string
    steps: LandingStepItem[]
  }
  demo: {
    eyebrow: string
    titleLine1: string
    titleLine2: string
    subtitle: string
    bullets: { emoji: string; title: string; desc: string }[]
    cta: string
  }
  inbox: {
    eyebrow: string
    titleLine1: string
    titleLine2: string
    subtitle: string
    subtitleHint: string
    interactiveBadge: string
    capabilities: { title: string; desc: string }[]
  }
  features: {
    eyebrow: string
    title: string
    subtitle: string
    featuredBadge: string
    items: LandingFeatureItem[]
  }
  testimonials: {
    eyebrow: string
    title: string
    subtitle: string
    items: LandingTestimonial[]
    stats: LandingStatItem[]
  }
  pricing: {
    eyebrow: string
    title: string
    subtitle: string
    promo: string
    guarantees: string[]
    cta: string
    ctaNote: string
    savePercent: (n: number) => string
  }
  trust: {
    eyebrow: string
    title: string
    body: string
    bullets: { text: string; highlight?: boolean }[]
    stats: { value: string; label: string; prefix?: string }[]
    refund: string
  }
  finalCta: {
    title: string
    body: string
    note: string
    primary: string
    whatsapp: string
    whatsappHref: string
  }
  faq: {
    eyebrow: string
    title: string
    items: LandingFaqItem[]
  }
  mobileApp: {
    soon: string
    label: string
    title: string
    titleAccent: string
    body: string
    chips: string[]
    storeSoon: string
    pending: string
  }
  footer: {
    tagline: string
    cta: string
    platformHeading: string
    platformLinks: LandingNavLink[]
    accountHeading: string
    register: string
    login: string
    contact: string
    copyright: string
    madeIn: string
  }
}

export const LANDING_COPY: Record<Lang, LandingCopy> = {
  ar: {
    brandName: 'نحلة',
    nav: [
      { label: 'لماذا نحلة', id: 'why' },
      { label: 'كيف تعمل', id: 'how' },
      { label: 'شاهد نحلة', id: 'demo' },
      { label: 'صندوق الوارد', id: 'inbox' },
      { label: 'المميزات', id: 'features' },
      { label: 'الأسعار', id: 'pricing' },
      { label: 'الأسئلة الشائعة', id: 'faq' },
    ],
    navLogin: 'دخول',
    navTrial: 'جرّب مجاناً 14 يوم',
    navTrialMobile: 'جرّب مجاناً 14 يوم — بلا بطاقة',
    navLoginMobile: 'تسجيل الدخول',
    menuOpen: 'فتح القائمة',
    menuClose: 'إغلاق القائمة',
    langSwitch: 'English',
    hero: {
      badge: 'عرض الإطلاق — خصم 50٪ لأول شهرين',
      titleLine1: 'واتسابك يبقى معك…',
      titleLine2: 'ونحلة تبيع معك',
      subtitle:
        'اربط واتساب الأعمال بالذكاء والحملات، واستمر في استخدام جوالك كالمعتاد بينما نحلة ترد، تقترح، وتتابع الطلبات عنك.',
      ctaTrialMobile: 'جرّب مجانًا لمدة 14 يوم',
      ctaTrialDesktop: 'ابدأ تجربتك المجانية الآن',
      ctaHow: 'كيف يعمل مع واتساب الأعمال؟',
      pill: 'واتساب الأعمال + الذكاء + الحملات في مكان واحد',
      stackAria: 'واتساب الأعمال ثم الذكاء ثم الحملات',
      stackWa: 'واتساب الأعمال',
      stackAi: 'ذكاء يرد ويقترح',
      stackCampaign: 'حملات ومتابعة',
      stackNote:
        'لا تغيّر طريقة عملك — نحلة تضيف الردود التلقائية والحملات فوق واتسابك الحالي على الجوال.',
      riskReversal: 'بلا بطاقة ائتمانية · بلا عقود · تلغي في أي وقت',
      socialStores: '+500 متجر نشط',
      socialRating: '4.9/5 تقييم',
      socialTrial: '14 يوم مجاناً',
    },
    problem: {
      heading: 'هل تعاني من هذه المشاكل؟',
      items: [
        'رسائل واتساب بلا رد تعني عملاء يشترون من منافسك',
        'فريق الدعم غارق في نفس الأسئلة المتكررة يومياً',
        'عملاء يتركون السلة لأن لا أحد يتابع معهم',
      ],
      closing: 'نحلة تحل هذه المشاكل الثلاث تلقائياً، من اليوم الأول. 🐝',
    },
    how: {
      eyebrow: 'في 4 خطوات فقط',
      title: 'كيف تعمل نحلة؟',
      subtitle: 'إعداد كامل في أقل من ساعة واحدة',
      steps: [
        {
          num: '١',
          title: 'اربط متجرك',
          desc: 'أضف متجرك على سلة أو منصتك التجارية — التكامل فوري وبلا تعقيدات تقنية.',
          time: '5 دقائق',
        },
        {
          num: '٢',
          title: 'اربط واتساب',
          desc: 'ربط رقم واتساب Business الخاص بمتجرك مع نحلة بخطوات موضّحة داخل اللوحة.',
          time: '10 دقائق',
        },
        {
          num: '٣',
          title: 'درّب نحلة على متجرك',
          desc: 'أدخل منتجاتك وعروضك وسياسات الشحن والإرجاع — كل ما تعرفه عن متجرك، علّمه لنحلة.',
          time: '20 دقيقة',
        },
        {
          num: '٤',
          title: 'شغّل نحلة واسترح',
          desc: 'نحلة تبدأ الرد على عملائك فور تفعيلها — ترد، تبيع، وتُتمّ الطلبات بدون أي تدخل منك.',
          time: 'الآن وإلى الأبد',
        },
      ],
    },
    demo: {
      eyebrow: 'تجربة حقيقية',
      titleLine1: 'كيف تتحدث نحلة',
      titleLine2: 'مع عملائك؟',
      subtitle:
        'شاهد مثالاً حقيقياً لكيفية رد نحلة على العملاء ومساعدتهم على إتمام الطلب مباشرة عبر واتساب.',
      bullets: [
        { emoji: '💬', title: 'رد فوري', desc: 'نحلة ترد في ثوانٍ — لا انتظار، لا تفويت.' },
        { emoji: '📦', title: 'تحقق من المخزون', desc: 'تؤكد التوفر والسعر من بيانات متجرك الفعلية.' },
        { emoji: '🛒', title: 'رابط الشراء', desc: 'ترسل رابط الدفع مباشرة داخل المحادثة.' },
        { emoji: '💳', title: 'خيارات الدفع', desc: 'بطاقة، Apple Pay، أو تحويل بنكي — كل شيء داخل الشات.' },
      ],
      cta: 'ابدأ مجاناً — مثل ما شاهدت',
    },
    inbox: {
      eyebrow: 'صندوق الوارد الذكي',
      titleLine1: 'واجهة واتساب التي تعرفها',
      titleLine2: 'بقدرات مبيعات احترافية',
      subtitle:
        'كل محادثات متجرك في مكان واحد. شارات ذكية تخبرك دائماً من يرد: الذكاء الاصطناعي، موظفك، حملاتك، أو الطيار الآلي.',
      subtitleHint: 'جرّب الفلاتر والمحادثات بنفسك 👇',
      interactiveBadge: 'تجربة تفاعلية · اضغط الفلاتر والمحادثات',
      capabilities: [
        { title: 'ينبهك حين يطلب موظف', desc: 'بطاقة حمراء فورية لأي عميل يقول "أبغى موظف".' },
        { title: 'يميّز ردّك البشري', desc: 'حين تتدخل، تتحول البطاقة تلقائياً إلى "ردّ بشري".' },
        { title: 'يعرض الحملات بأزرارها', desc: 'القوالب التسويقية تظهر بأزرارها كما يراها العميل.' },
        { title: 'الطيار الآلي ينفّذ', desc: 'تأكيد الطلب، الشحن والمتابعة — تظهر برسائل النظام.' },
      ],
    },
    features: {
      eyebrow: 'قدرات نحلة',
      title: 'نحلة تعمل كفريق مبيعات متكامل',
      subtitle: 'لا توظيف، لا رواتب، لا إجازات — فقط مبيعات متواصلة على مدار الساعة.',
      featuredBadge: '⭐ مميزة',
      items: [
        {
          title: 'ردود ذكية طبيعية',
          desc: 'تفهم أسئلة العملاء بالعامية والفصحى وترد بأسلوب متجرك تماماً.',
          outcome: 'يقلل متوسط وقت الرد من ساعات إلى ثوانٍ',
        },
        {
          title: 'الطيار الآلي',
          desc: 'شغّله مرة واحدة ثم ارتاح — نحلة تتولى الرد والمتابعة وإتمام الطلب من أوله لآخره بدون أي تدخل منك.',
          outcome: 'متجرك يبيع وأنت نائم، 24/7 بلا انقطاع',
          highlight: true,
        },
        {
          title: 'استرجاع السلات المتروكة',
          desc: 'تراقب من يترك الطلب وترسل تذكيرات ذكية في الوقت المناسب.',
          outcome: 'تسترجع ما يصل إلى 30٪ من الطلبات المفقودة',
        },
        {
          title: 'إعادة الطلب التنبؤي',
          desc: 'نحلة تتذكر كل عميل وتُرسل له رسالة في اللحظة المناسبة — مثال: "سلمى، مرت 3 أسابيع على طلبك الأخير من كريم الترطيب، هل تريدين إعادة الطلب؟ 🍯"',
          outcome: 'يزيد معدل تكرار الشراء حتى 40٪ — بدون إعلانات',
          highlight: true,
        },
        {
          title: 'توصيات المنتجات',
          desc: 'تقترح منتجات مكملة بناءً على ما يريده العميل لزيادة قيمة الطلب.',
          outcome: 'ترفع متوسط قيمة الطلب بنسبة تصل لـ 35٪',
        },
        {
          title: 'روابط الدفع الفورية',
          desc: 'ترسل رابط الدفع للعميل مباشرة داخل المحادثة فيتم الشراء في ثوانٍ.',
          outcome: 'تحوّل الاستفسار إلى شراء في نفس المحادثة',
        },
        {
          title: 'كوبونات ذكية',
          desc: 'تُنشئ كوبونات شخصية للعملاء المترددين لدفعهم للإتمام.',
          outcome: 'تزيد معدل التحويل لدى العملاء المترددين',
        },
        {
          title: 'طلبات داخل الواتساب',
          desc: 'العميل يطلب ويدفع ويتأكد كل شيء داخل واتساب دون مغادرته.',
          outcome: 'تجربة شراء سلسة = عميل راضٍ يعود',
        },
        {
          title: 'حملات مجدولة',
          desc: 'أرسل عروضك وإشعارات الطلبات والمناسبات لآلاف العملاء بضغطة واحدة.',
          outcome: 'وصول مضمون أعلى من البريد الإلكتروني',
        },
        {
          title: 'مكتبة قوالب نحلة الجاهزة',
          desc: 'مكتبة قوالب احترافية مكتوبة بعناية ومتوافقة مع شروط ميتا — جاهزة للاعتماد بضغطة واحدة لاسترجاع السلات والحملات وتأكيد الطلبات والشحن.',
          outcome: 'انطلق فورًا بدون كتابة قوالب أو رفض من ميتا',
        },
        {
          title: 'عروض المناسبات الذكية',
          desc: 'نحلة تتعرف تلقائيًا على المناسبات (رمضان، العيد، اليوم الوطني، الجمعة البيضاء…) وتُجهّز لك عروضًا وكوبونات وحملات مخصصة لكل مناسبة في وقتها المثالي.',
          outcome: 'موسم ذروة بدون جهد — مبيعات تواكب كل مناسبة',
          highlight: true,
        },
        {
          title: 'لوحة تحكم كاملة',
          desc: 'تابع المحادثات والمبيعات والتحويلات والمشكلات من مكان واحد.',
          outcome: 'قرارات مبنية على بيانات حقيقية',
        },
      ],
    },
    testimonials: {
      eyebrow: 'قصص نجاح حقيقية',
      title: 'التجار يتكلمون',
      subtitle: 'ليست أرقام، هذه نتائج متاجر حقيقية',
      items: [
        {
          quote:
            'كنت أرد يدوياً على 200 رسالة يومياً. الآن نحلة تتولى 90٪ منها وأنا أتابع فقط الحالات الاستثنائية.',
          name: 'محمد العتيبي',
          store: 'متجر ملابس — الرياض',
          result: '+3 ساعات يومياً',
        },
        {
          quote:
            'في أول أسبوع استرجعت 7 طلبات كانت ستضيع. نحلة ترسل للعميل في الوقت الصح وبالكلام الصح.',
          name: 'نورة الشمري',
          store: 'متجر عطور — جدة',
          result: '+23٪ في المبيعات',
        },
        {
          quote:
            'أفضل استثمار عملته لمتجري. الإعداد أخذ أقل من ساعة والنتائج ظهرت من اليوم الأول.',
          name: 'خالد المنصور',
          store: 'متجر إلكترونيات — الدمام',
          result: 'ROI في أسبوع',
        },
      ],
      stats: [
        { value: '+500', label: 'متجر نشط', sub: 'في 3 دول خليجية' },
        { value: '98٪', label: 'رضا التجار', sub: 'بعد أول شهر' },
        { value: '+2.4M', label: 'محادثة معالجة', sub: 'هذا الشهر' },
        { value: '14 يوم', label: 'تجربة مجانية كاملة', sub: 'بلا قيود' },
      ],
    },
    pricing: {
      eyebrow: 'الأسعار والباقات',
      title: 'استثمار يعود عليك من أول أسبوع',
      subtitle: 'كل خطة تشمل تجربة مجانية 14 يوم. لا يلزم بطاقة ائتمانية.',
      promo: 'عرض الإطلاق: الأسعار المعروضة بخصم 50٪ لأول شهرين — ينتهي قريباً',
      guarantees: [
        '14 يوم مجاناً بلا شروط',
        'بلا عقود طويلة، ألغِ متى شئت',
        'استرداد كامل خلال 7 أيام إن لم تقتنع',
      ],
      cta: 'ابدأ تجربتك المجانية الآن',
      ctaNote: 'بلا بطاقة ائتمانية · تلغي في أي وقت · الإعداد في أقل من ساعة',
      savePercent: (n) => `وفّر ${n}٪`,
    },
    trust: {
      eyebrow: 'نتائج حقيقية',
      title: 'ليست أداة — بل شريك نمو لمتجرك',
      body:
        'نحلة لم تُبنَ لتكون بوتاً للردود. بُنيت لتفهم متجرك كما تفهمه أنت، وتتحدث مع عملائك بأسلوبك، وتحوّل كل محادثة إلى إيراد حقيقي.',
      bullets: [
        { text: 'تزيد إيرادات واتساب بمتوسط 35٪ في أول 3 أشهر', highlight: true },
        { text: 'توفّر على فريقك 3–5 ساعات يومياً من الردود المتكررة' },
        { text: 'تحوّل 30٪ من السلات المتروكة إلى طلبات مكتملة' },
        { text: 'إعداد كامل في أقل من ساعة — دون خبرة تقنية' },
        { text: 'مبنية خصيصاً للمتاجر السعودية والخليجية' },
      ],
      stats: [
        { value: '35٪', label: 'زيادة متوسطة في إيرادات واتساب' },
        { value: '3 ساعات', label: 'توفّر يومياً من وقت فريق الدعم' },
        { value: '30٪', label: 'من السلات المتروكة تُسترجع' },
        { value: '1 ساعة', label: 'الإعداد الكامل من الصفر', prefix: 'أقل من' },
      ],
      refund: 'ضمان استرداد كامل خلال 7 أيام',
    },
    finalCta: {
      title: 'متجرك يستحق مساعداً لا يتعب',
      body: 'ابدأ اليوم، وخلال أسبوع ستسأل نفسك: لماذا لم أفعل هذا قبل كذا؟',
      note: 'تجربة 14 يوم مجانية · الإعداد في ساعة واحدة · إلغاء بضغطة واحدة',
      primary: 'أنشئ حسابك الآن مجاناً',
      whatsapp: 'تحدث معنا على واتساب',
      whatsappHref: 'https://wa.me/966500000000?text=أريد معرفة المزيد عن نحلة',
    },
    faq: {
      eyebrow: 'لديك تساؤلات؟',
      title: 'أسئلة شائعة',
      items: [
        {
          q: 'كم تستغرق عملية الإعداد الكاملة؟',
          a: 'في المتوسط أقل من ساعة. ربط سلة يأخذ 5 دقائق، ربط واتساب 10 دقائق، وإدخال المنتجات والعروض يعتمد على حجم كتالوجك. فريقنا يساعدك في كل خطوة.',
        },
        {
          q: 'هل تحتاج نحلة إلى WhatsApp Business API؟',
          a: 'نعم، نحلة تعمل مع WhatsApp Cloud API من Meta لتقديم تجربة موثوقة ومتوافقة. نساعدك في الحصول على الوصول وإعداد الحساب ضمن باقة الاشتراك.',
        },
        {
          q: 'هل تدعم نحلة منصة سلة؟',
          a: 'نعم، نحلة مبنية أصلاً للتكامل مع سلة. يمكنها جلب منتجاتك وأسعارك وحالة المخزون تلقائياً، وإنشاء الطلبات مباشرة داخل متجرك.',
        },
        {
          q: 'هل يمكنني التحكم الكامل في ما تقوله نحلة؟',
          a: 'بالكامل. أنت تحدد المنتجات، العروض، أسلوب التواصل، والقيود. نحلة لا تتجاوز ما أذنت له — أي معلومة خارج ما أدخلته، تحوّل المحادثة لك.',
        },
        {
          q: 'ماذا يحدث بعد انتهاء التجربة المجانية؟',
          a: 'ستتلقى إشعاراً قبل 3 أيام من انتهاء التجربة. لا يُخصم أي مبلغ تلقائياً — اختر الخطة التي تناسبك، أو ألغِ بلا أي رسوم.',
        },
        {
          q: 'هل يمكن لنحلة إنشاء الطلبات ومعالجة الدفع؟',
          a: 'نعم. نحلة تستطيع إنشاء الطلب داخل متجرك وإرسال رابط الدفع للعميل مباشرة عبر واتساب، سواء عبر مدى أو فيزا أو الدفع عند الاستلام.',
        },
        {
          q: 'ما الفرق بين نحلة وبوتات واتساب الأخرى؟',
          a: 'البوتات التقليدية تعمل بقوائم وكلمات مفتاحية ثابتة. نحلة تفهم السياق والنية وتُجري محادثة طبيعية، وترتبط بمتجرك لتعرف منتجاتك وطلباتك وعروضك في الوقت الفعلي.',
        },
      ],
    },
    mobileApp: {
      soon: 'قريباً',
      label: 'تطبيق الجوال',
      title: 'نحلة في جيبك',
      titleAccent: 'دائماً',
      body:
        'تابع محادثات متجرك، راجع الطلبات، وأدِر مساعدك الذكي من هاتفك في أي وقت ومن أي مكان. التطبيق قادم قريباً على App Store وGoogle Play.',
      chips: ['ردود واتساب فورية', 'إدارة الطلبات', 'إحصائيات حية', 'إشعارات لحظية'],
      storeSoon: 'قريباً على',
      pending: 'في انتظار المراجعة · سيُعلن عند الإطلاق',
    },
    footer: {
      tagline: 'منصة ذكية تحوّل واتساب إلى قناة مبيعات كاملة لمتجرك — ردود، طلبات، ودفع تلقائي.',
      cta: 'ابدأ مجاناً',
      platformHeading: 'المنصة',
      platformLinks: [
        { label: 'كيف تعمل', id: 'how' },
        { label: 'شاهد نحلة', id: 'demo' },
        { label: 'المميزات', id: 'features' },
        { label: 'الأسعار', id: 'pricing' },
        { label: 'الأسئلة الشائعة', id: 'faq' },
      ],
      accountHeading: 'الحساب',
      register: 'إنشاء حساب جديد',
      login: 'تسجيل الدخول',
      contact: 'تواصل معنا',
      copyright: '© 2026 نحلة AI — جميع الحقوق محفوظة',
      madeIn: 'صُنع بعناية في المملكة العربية السعودية 🇸🇦',
    },
  },

  en: {
    brandName: 'Nahla',
    nav: [
      { label: 'Why Nahla', id: 'why' },
      { label: 'How it works', id: 'how' },
      { label: 'See Nahla', id: 'demo' },
      { label: 'Inbox', id: 'inbox' },
      { label: 'Features', id: 'features' },
      { label: 'Pricing', id: 'pricing' },
      { label: 'FAQ', id: 'faq' },
    ],
    navLogin: 'Log in',
    navTrial: 'Start 14-day free trial',
    navTrialMobile: 'Start 14-day free trial — no card',
    navLoginMobile: 'Log in',
    menuOpen: 'Open menu',
    menuClose: 'Close menu',
    langSwitch: 'العربية',
    hero: {
      badge: 'Launch offer — 50% off your first two months',
      titleLine1: 'Keep WhatsApp on your phone.',
      titleLine2: 'Let Nahla sell with you.',
      subtitle:
        'Connect WhatsApp Business with AI and campaigns. Keep using your phone as usual while Nahla replies, suggests products, and follows up on orders.',
      ctaTrialMobile: 'Try free for 14 days',
      ctaTrialDesktop: 'Start your free trial',
      ctaHow: 'How does it work with WhatsApp Business?',
      pill: 'WhatsApp Business + AI + campaigns in one place',
      stackAria: 'WhatsApp Business, then AI, then campaigns',
      stackWa: 'WhatsApp Business',
      stackAi: 'AI that replies & suggests',
      stackCampaign: 'Campaigns & follow-up',
      stackNote:
        'No change to how you work — Nahla adds automated replies and campaigns on top of your existing WhatsApp on mobile.',
      riskReversal: 'No credit card · No contracts · Cancel anytime',
      socialStores: '500+ active stores',
      socialRating: '4.9/5 rating',
      socialTrial: '14-day free trial',
    },
    problem: {
      heading: 'Sound familiar?',
      items: [
        'Unanswered WhatsApp messages mean customers buy elsewhere',
        'Your support team spends hours on the same questions every day',
        'Shoppers abandon carts because no one follows up',
      ],
      closing: 'Nahla helps address these three challenges from day one. 🐝',
    },
    how: {
      eyebrow: 'Four simple steps',
      title: 'How Nahla works',
      subtitle: 'Full setup in under one hour',
      steps: [
        {
          num: '1',
          title: 'Connect your store',
          desc: 'Link your Salla or e-commerce store — integration is quick and requires no technical setup.',
          time: '5 min',
        },
        {
          num: '2',
          title: 'Connect WhatsApp',
          desc: 'Connect your store WhatsApp Business number to Nahla with guided steps inside the dashboard.',
          time: '10 min',
        },
        {
          num: '3',
          title: 'Train Nahla on your store',
          desc: 'Add your products, offers, and shipping policies — teach Nahla what you know about your business.',
          time: '20 min',
        },
        {
          num: '4',
          title: 'Go live',
          desc: 'Once enabled, Nahla starts replying to customers — answering questions, assisting sales, and completing orders.',
          time: 'Ongoing',
        },
      ],
    },
    demo: {
      eyebrow: 'Live example',
      titleLine1: 'How Nahla talks',
      titleLine2: 'to your customers',
      subtitle:
        'See a realistic example of how Nahla replies to customers and helps them complete an order directly in WhatsApp.',
      bullets: [
        { emoji: '💬', title: 'Fast replies', desc: 'Nahla responds in seconds — fewer missed conversations.' },
        { emoji: '📦', title: 'Stock checks', desc: 'Confirms availability and price from your live store data.' },
        { emoji: '🛒', title: 'Checkout links', desc: 'Sends a payment link inside the chat.' },
        { emoji: '💳', title: 'Payment options', desc: 'Card, Apple Pay, or bank transfer — within the conversation.' },
      ],
      cta: 'Start free — just like the demo',
    },
    inbox: {
      eyebrow: 'Smart inbox',
      titleLine1: 'The WhatsApp interface you know',
      titleLine2: 'with professional sales tools',
      subtitle:
        'All store conversations in one place. Smart badges show who is replying: AI, your team, campaigns, or autopilot.',
      subtitleHint: 'Try the filters and conversations below 👇',
      interactiveBadge: 'Interactive demo · tap filters and chats',
      capabilities: [
        { title: 'Alerts when a human is requested', desc: 'Instant highlight when a customer asks to speak with staff.' },
        { title: 'Marks your manual replies', desc: 'When you step in, the badge switches to "Human reply".' },
        { title: 'Shows campaigns with buttons', desc: 'Marketing templates appear with the same buttons customers see.' },
        { title: 'Autopilot actions', desc: 'Order confirmation, shipping, and follow-ups appear as system messages.' },
      ],
    },
    features: {
      eyebrow: 'Nahla capabilities',
      title: 'A sales assistant for your store',
      subtitle: 'Automated WhatsApp commerce — without hiring a full support team.',
      featuredBadge: '⭐ Featured',
      items: [
        {
          title: 'Natural AI replies',
          desc: 'Understands customer questions in everyday Arabic and English, and replies in your store tone.',
          outcome: 'Helps reduce average reply time from hours to seconds',
        },
        {
          title: 'Autopilot',
          desc: 'Set it up once — Nahla handles replies, follow-ups, and order completion with minimal manual work.',
          outcome: 'Keeps your store responsive around the clock',
          highlight: true,
        },
        {
          title: 'Abandoned cart recovery',
          desc: 'Detects incomplete checkouts and sends timely reminders.',
          outcome: 'Helps recover orders that would otherwise be lost',
        },
        {
          title: 'Repeat-order reminders',
          desc: 'Remembers customers and sends well-timed reorder messages based on purchase history.',
          outcome: 'Supports repeat purchases without extra ad spend',
          highlight: true,
        },
        {
          title: 'Product recommendations',
          desc: 'Suggests complementary products based on what the customer is asking for.',
          outcome: 'Can help increase average order value',
        },
        {
          title: 'Instant payment links',
          desc: 'Sends a checkout link directly in the conversation.',
          outcome: 'Turns inquiries into purchases in the same chat',
        },
        {
          title: 'Smart coupons',
          desc: 'Creates personalized offers for hesitant shoppers.',
          outcome: 'Helps improve conversion for undecided customers',
        },
        {
          title: 'Orders inside WhatsApp',
          desc: 'Customers can browse, order, and pay without leaving WhatsApp.',
          outcome: 'A smoother buying experience for returning customers',
        },
        {
          title: 'Scheduled campaigns',
          desc: 'Send offers, order updates, and seasonal messages to your customer list.',
          outcome: 'Direct reach on a channel customers already use',
        },
        {
          title: 'Ready-made template library',
          desc: 'Professional WhatsApp templates aligned with Meta policies — for carts, campaigns, order confirmation, and shipping.',
          outcome: 'Launch faster without writing templates from scratch',
        },
        {
          title: 'Seasonal promotions',
          desc: 'Prepare offers, coupons, and campaigns for key retail seasons (Ramadan, Eid, National Day, and more).',
          outcome: 'Stay ready for peak seasons with less manual work',
          highlight: true,
        },
        {
          title: 'Full dashboard',
          desc: 'Monitor conversations, sales, conversions, and issues from one place.',
          outcome: 'Decisions based on real store data',
        },
      ],
    },
    testimonials: {
      eyebrow: 'Merchant stories',
      title: 'What store owners say',
      subtitle: 'Examples from real merchants using Nahla',
      items: [
        {
          quote:
            'I used to reply manually to 200 messages a day. Now Nahla handles most of them and I only step in for exceptions.',
          name: 'Mohammed Al-Otaibi',
          store: 'Clothing store — Riyadh',
          result: '+3 hours saved daily',
        },
        {
          quote:
            'In the first week we recovered several orders that would have been lost. Nahla follows up at the right time with the right message.',
          name: 'Noura Al-Shammari',
          store: 'Perfume store — Jeddah',
          result: 'Sales uplift in week one',
        },
        {
          quote:
            'Straightforward setup in under an hour. We started seeing value from the first day.',
          name: 'Khalid Al-Mansour',
          store: 'Electronics store — Dammam',
          result: 'Positive ROI early on',
        },
      ],
      stats: [
        { value: '500+', label: 'Active stores', sub: 'Across the GCC' },
        { value: 'High', label: 'Merchant satisfaction', sub: 'Based on early feedback' },
        { value: 'Millions', label: 'Conversations handled', sub: 'On the platform' },
        { value: '14 days', label: 'Full free trial', sub: 'No feature lock-in' },
      ],
    },
    pricing: {
      eyebrow: 'Pricing & plans',
      title: 'Plans built for growing stores',
      subtitle: 'Every plan includes a 14-day free trial. No credit card required.',
      promo: 'Launch pricing: 50% off shown rates for your first two months — limited time',
      guarantees: [
        '14-day free trial',
        'No long-term contracts — cancel anytime',
        'Full refund within 7 days if you are not satisfied',
      ],
      cta: 'Start your free trial',
      ctaNote: 'No credit card · Cancel anytime · Setup in under one hour',
      savePercent: (n) => `Save ${n}%`,
    },
    trust: {
      eyebrow: 'Built for merchants',
      title: 'More than a chatbot',
      body:
        'Nahla is built to understand your store, speak in your voice, and help turn WhatsApp conversations into completed orders.',
      bullets: [
        { text: 'Designed to help grow WhatsApp revenue', highlight: true },
        { text: 'Reduces time spent on repetitive support replies' },
        { text: 'Helps recover abandoned cart orders' },
        { text: 'Typical setup in under one hour — no technical expertise required' },
        { text: 'Built for Saudi and GCC e-commerce merchants' },
      ],
      stats: [
        { value: '↑', label: 'WhatsApp revenue support' },
        { value: 'Hours', label: 'Saved on repetitive replies' },
        { value: 'Carts', label: 'Recovery workflows included' },
        { value: '1 hr', label: 'Typical full setup', prefix: 'Under' },
      ],
      refund: '7-day money-back guarantee',
    },
    finalCta: {
      title: 'Your store deserves reliable support',
      body: 'Start today and see how WhatsApp commerce can work with less manual effort.',
      note: '14-day free trial · Setup in about one hour · Cancel anytime',
      primary: 'Create your free account',
      whatsapp: 'Chat with us on WhatsApp',
      whatsappHref: 'https://wa.me/966500000000?text=I%20want%20to%20learn%20more%20about%20Nahla',
    },
    faq: {
      eyebrow: 'Questions?',
      title: 'Frequently asked questions',
      items: [
        {
          q: 'How long does full setup take?',
          a: 'Typically under one hour. Salla connection takes about 5 minutes, WhatsApp about 10 minutes, and catalog setup depends on your product count. Our team guides you through each step.',
        },
        {
          q: 'Does Nahla require the WhatsApp Business API?',
          a: 'Yes. Nahla works with the WhatsApp Cloud API from Meta for a reliable, policy-compliant experience. We help you with access and account setup as part of your subscription.',
        },
        {
          q: 'Does Nahla support Salla?',
          a: 'Yes. Nahla integrates natively with Salla — syncing products, prices, inventory, and creating orders directly in your store.',
        },
        {
          q: 'Can I control what Nahla says?',
          a: 'Yes. You define products, offers, tone, and boundaries. Nahla stays within what you configure — if it cannot answer from your data, it escalates to your team.',
        },
        {
          q: 'What happens after the free trial?',
          a: 'You receive a reminder 3 days before the trial ends. Nothing is charged automatically — choose a plan that fits, or cancel at no cost.',
        },
        {
          q: 'Can Nahla create orders and handle payments?',
          a: 'Yes. Nahla can create orders in your store and send payment links via WhatsApp — including card, Mada, and cash on delivery where supported.',
        },
        {
          q: 'How is Nahla different from other WhatsApp bots?',
          a: 'Traditional bots rely on fixed menus and keywords. Nahla understands context, holds natural conversations, and connects to your store for live product and order data.',
        },
      ],
    },
    mobileApp: {
      soon: 'Coming soon',
      label: 'Mobile app',
      title: 'Nahla in your pocket',
      titleAccent: 'always',
      body:
        'Monitor store conversations, review orders, and manage your AI assistant from your phone. The mobile app is coming to the App Store and Google Play.',
      chips: ['Instant WhatsApp replies', 'Order management', 'Live stats', 'Real-time alerts'],
      storeSoon: 'Coming soon on',
      pending: 'Pending review · announcement at launch',
    },
    footer: {
      tagline:
        'An AI platform that turns WhatsApp into a sales channel for your store — replies, orders, and payments.',
      cta: 'Start free',
      platformHeading: 'Platform',
      platformLinks: [
        { label: 'How it works', id: 'how' },
        { label: 'See Nahla', id: 'demo' },
        { label: 'Features', id: 'features' },
        { label: 'Pricing', id: 'pricing' },
        { label: 'FAQ', id: 'faq' },
      ],
      accountHeading: 'Account',
      register: 'Create account',
      login: 'Log in',
      contact: 'Contact us',
      copyright: '© 2026 Nahla AI — All rights reserved',
      madeIn: 'Built in Saudi Arabia 🇸🇦',
    },
  },
}
