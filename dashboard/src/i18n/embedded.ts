/**
 * Embedded i18n — minimal Arabic/English dictionary used by Salla-embedded
 * surfaces (SallaEmbedded.tsx + SallaEntryScreen.tsx).  Kept separate from
 * the dashboard's main translation tree so that:
 *   • the embedded screen has zero coupling to the full Translations shape
 *   • we can iterate on copy without touching giant ar.ts / en.ts
 *   • these strings can ship even if dashboard pages haven't been translated
 *
 * If a new string is added, update BOTH ar and en blocks.
 */
export type EmbeddedLang = 'ar' | 'en'

export interface EmbeddedStrings {
  app: {
    brand:       string
    badgeAI:     string
    tagline:     string
  }
  loader: {
    initializing:  string
    checking:      string
    verifying:     string
    entering:      string
    redirecting:   string
    completingLink: string
    retrying:      string
  }
  errors: {
    title:           string
    noAuthToken:     string
    invalidResponse: string
    verifyFailed:    string
    timeout:         string
    network:         string
    watchdog:        string
    contactSupport:  string
    retry:           string
    sessionExpired:  string
  }
  welcome: {
    title:        string
    storeLinked:  string
    openingNahla: string
    skip:         string
    headline:     string
    subhead:      string
    openDashboard: string
  }
  status: {
    section:           string
    sallaEmbedded:     string
    apiFull:           string
    whatsapp:          string
    subscription:      string
    nahla:             string
    connected:         string
    notConnected:      string
    easyMode:          string
    complete:          string
    incomplete:        string
    running:           string
    stopped:           string
    storeName:         string
    storeNameEmpty:    string
    refresh:           string
    refreshing:        string
  }
  steps: {
    section:       string
    nextStep:      string
    completed:     string
    s1Title:       string
    s1Desc:        string
    s2Title:       string
    s2Desc:        string
    s3Title:       string
    s3Desc:        string
    s4Title:       string
    s4Desc:        string
  }
  metrics: {
    section:       string
    today:         string
    conversations: string
    waOrders:      string
    waRevenue:     string
    aiReplyRate:   string
    noDataYet:     string
    partialFallback: string
  }
  cta: {
    openAdvanced:  string
    opening:       string
    connectWhatsapp: string
    connectStore:    string
    syncAppNotReady: string
    storeLinkBlurb:  string
    storeLinkLead:   string
  }
  sub: {
    active:        string
    trial:         string
    trialBlocked:  string
    cancelled:     string
    none:          string
    blockedTitle:  string
    blockedBody:   string
    subscribeNow:  string
    blockedHeadline:    string
    blockedHeadlineSub: string
  }
  subscription: {
    name:          string
  }
  footer: {
    saudi:         string
  }
}

const ar: EmbeddedStrings = {
  app: {
    brand:    'نحلة AI',
    badgeAI:  'AI',
    tagline:  'بأيدي سعودية 100% 🇸🇦 · Nahla AI',
  },
  loader: {
    initializing:   'جاري تهيئة الاتصال...',
    checking:       'جاري التحقق من جلستك...',
    verifying:      'جاري التحقق من هويتك...',
    entering:       'جاري الدخول...',
    redirecting:    'جاري تحويلك...',
    completingLink: 'جاري إكمال الربط مع سلة...',
    retrying:       'جاري إعادة المحاولة...',
  },
  errors: {
    title:           'تعذّر الاتصال بسلة',
    noAuthToken:     'لم يتم استقبال رمز المصادقة من سلة.\nتأكد من أن رابط التطبيق في بوابة الشركاء يشير إلى:\nhttps://app.nahlah.ai/app/salla',
    invalidResponse: 'الخادم أرجع استجابة غير صالحة. حاول مجدداً.',
    verifyFailed:    'تعذّر التحقق من هويتك. أغلق التطبيق وأعد فتحه.',
    timeout:         'استغرق الخادم وقتاً طويلاً. تحقق من اتصالك وأعد المحاولة.',
    network:         'تعذر الوصول إلى الخادم. تحقق من اتصالك بالإنترنت.',
    watchdog:        'استغرق التحميل وقتاً طويلاً. أعد فتح التطبيق أو تواصل مع الدعم.',
    contactSupport:  'تواصل مع الدعم',
    retry:           'إعادة المحاولة',
    sessionExpired:  'انتهت الجلسة، أعد فتح التطبيق من سلة.',
  },
  welcome: {
    title:        'تم ربط متجرك بنجاح!',
    storeLinked:  'جاري فتح لوحة نحلة...',
    openingNahla: 'جاري فتح لوحة نحلة...',
    skip:         'تخطي',
    headline:     'مرحباً بك في نحلة 👋',
    subhead:      'اربط واتساب وابدأ الرد الذكي لزيادة مبيعات متجرك',
    openDashboard: 'فتح لوحة نحلة المتقدمة',
  },
  status: {
    section:        'الحالة',
    sallaEmbedded:  'سلة Embedded',
    apiFull:        'ربط API الكامل',
    whatsapp:       'واتساب',
    subscription:   'الاشتراك',
    nahla:          'نحلة',
    connected:      'متصل',
    notConnected:   'غير متصل',
    easyMode:       'Easy Mode',
    complete:       'مكتمل',
    incomplete:     'غير مكتمل',
    running:        'تعمل',
    stopped:        'متوقفة',
    storeName:      'اسم المتجر',
    storeNameEmpty: '',
    refresh:        'تحديث',
    refreshing:     '...',
  },
  steps: {
    section:    'خطوات البدء',
    nextStep:   'الخطوة التالية',
    completed:  'مكتمل ✓',
    s1Title:    'ربط واتساب',
    s1Desc:     'اربط حساب واتساب بزنس بمتجرك',
    s2Title:    'تفعيل الرد الذكي',
    s2Desc:     'فعّل نحلة لترد على عملائك تلقائياً',
    s3Title:    'تجربة أول محادثة',
    s3Desc:     'ابدأ محادثة واتساب مع عميل أول',
    s4Title:    'متابعة النتائج',
    s4Desc:     'راقب الإحصائيات ومعدلات الرد الذكي',
  },
  metrics: {
    section:        'إحصائيات اليوم',
    today:          'اليوم',
    conversations:  'المحادثات اليوم',
    waOrders:       'طلبات واتساب اليوم',
    waRevenue:      'إيرادات واتساب اليوم',
    aiReplyRate:    'معدل الرد بالذكاء',
    noDataYet:      'ستظهر الإحصائيات بعد أول محادثات واتساب',
    partialFallback: 'تعذر تحميل بعض البيانات، اضغط تحديث.',
  },
  cta: {
    openAdvanced:    '🚀 فتح لوحة نحلة المتقدمة',
    opening:         '⏳ جارٍ الفتح...',
    connectWhatsapp: '💬 ربط واتساب الآن',
    connectStore:    'ربط المتجر لتفعيل جميع الميزات',
    syncAppNotReady: 'لم يتم تكوين تطبيق المزامنة بعد. تواصل مع الدعم لإكمال الإعداد.',
    storeLinkBlurb:  'اربط متجرك عبر OAuth لتفعيل: مزامنة المنتجات والعملاء، إنشاء الطلبات من المحادثة، تتبع الطلبات، وتشغيل الأتمتة في الخلفية بدون انقطاع.',
    storeLinkLead:   '🔑 لتمكين المزامنة الكاملة للمنتجات والطلبات والعملاء',
  },
  sub: {
    active:       'نشط',
    trial:        'تجريبي',
    trialBlocked: 'تجربة مستخدمة',
    cancelled:    'ملغى',
    none:         'غير نشط',
    blockedTitle: 'الردود التلقائية والأتمتة مقفلة',
    blockedBody:  'يمكنك رؤية المحادثات الواردة والفرص والسلات المتروكة، لكن لن يرد نحلة تلقائياً ولن تُنفَّذ أي إجراءات حتى تفعيل الاشتراك.',
    subscribeNow: '💳 اشترك الآن',
    blockedHeadline:    '⚠️ تم استخدام التجربة المجانية — الرد التلقائي متوقف',
    blockedHeadlineSub: 'يمكنك الاطلاع على المحادثات الواردة، لكن نحلة لن ترد تلقائياً حتى تفعيل الاشتراك.',
  },
  subscription: {
    name: 'الاشتراك',
  },
  footer: {
    saudi: 'فريق سعودي 100% 🇸🇦 · Nahla AI',
  },
}

const en: EmbeddedStrings = {
  app: {
    brand:    'Nahla AI',
    badgeAI:  'AI',
    tagline:  'Built in Saudi Arabia 🇸🇦 · Nahla AI',
  },
  loader: {
    initializing:   'Initializing connection…',
    checking:       'Verifying your session…',
    verifying:      'Verifying your identity…',
    entering:       'Entering…',
    redirecting:    'Redirecting…',
    completingLink: 'Completing link with Salla…',
    retrying:       'Retrying…',
  },
  errors: {
    title:           'Could not connect to Salla',
    noAuthToken:     'No authentication token received from Salla.\nMake sure the app URL in Salla Partners points to:\nhttps://app.nahlah.ai/app/salla',
    invalidResponse: 'The server returned an invalid response. Please try again.',
    verifyFailed:    'Could not verify your identity. Close the app and reopen it.',
    timeout:         'The server took too long to respond. Check your connection and try again.',
    network:         'Cannot reach the server. Check your internet connection.',
    watchdog:        'Loading took too long. Reopen the app or contact support.',
    contactSupport:  'Contact support',
    retry:           'Retry',
    sessionExpired:  'Your session has expired. Reopen the app from Salla.',
  },
  welcome: {
    title:        'Your store is linked!',
    storeLinked:  'Opening Nahla dashboard…',
    openingNahla: 'Opening Nahla dashboard…',
    skip:         'Skip',
    headline:     'Welcome to Nahla 👋',
    subhead:      'Connect WhatsApp and let Nahla reply intelligently to grow your sales',
    openDashboard: 'Open advanced Nahla dashboard',
  },
  status: {
    section:        'Status',
    sallaEmbedded:  'Salla Embedded',
    apiFull:        'Full API connection',
    whatsapp:       'WhatsApp',
    subscription:   'Subscription',
    nahla:          'Nahla',
    connected:      'Connected',
    notConnected:   'Not connected',
    easyMode:       'Easy Mode',
    complete:       'Complete',
    incomplete:     'Incomplete',
    running:        'Active',
    stopped:        'Stopped',
    storeName:      'Store name',
    storeNameEmpty: '',
    refresh:        'Refresh',
    refreshing:     '…',
  },
  steps: {
    section:    'Onboarding steps',
    nextStep:   'Next step',
    completed:  'Done ✓',
    s1Title:    'Connect WhatsApp',
    s1Desc:     'Link your WhatsApp Business account to the store',
    s2Title:    'Enable smart replies',
    s2Desc:     'Let Nahla reply to your customers automatically',
    s3Title:    'Try your first conversation',
    s3Desc:     'Start a WhatsApp chat with your first customer',
    s4Title:    'Track results',
    s4Desc:     'Monitor stats and AI reply rates',
  },
  metrics: {
    section:        "Today's stats",
    today:          'Today',
    conversations:  'Conversations today',
    waOrders:       'WhatsApp orders today',
    waRevenue:      'WhatsApp revenue today',
    aiReplyRate:    'AI reply rate',
    noDataYet:      'Stats will appear after your first WhatsApp chats',
    partialFallback: 'Some data could not load — tap refresh.',
  },
  cta: {
    openAdvanced:    '🚀 Open advanced Nahla dashboard',
    opening:         '⏳ Opening…',
    connectWhatsapp: '💬 Connect WhatsApp now',
    connectStore:    'Link store to enable all features',
    syncAppNotReady: 'Sync app is not configured yet. Contact support to finish setup.',
    storeLinkBlurb:  'Link your store via OAuth to enable product & customer sync, in-chat order creation, order tracking, and uninterrupted background automations.',
    storeLinkLead:   '🔑 Enable full sync for products, orders, and customers',
  },
  sub: {
    active:       'Active',
    trial:        'Trial',
    trialBlocked: 'Trial used',
    cancelled:    'Cancelled',
    none:         'Inactive',
    blockedTitle: 'Auto-replies and automations are locked',
    blockedBody:  'You can still see incoming chats, opportunities, and abandoned carts, but Nahla will not reply automatically and no actions will run until your subscription is active.',
    subscribeNow: '💳 Subscribe now',
    blockedHeadline:    '⚠️ Free trial used — auto-reply is off',
    blockedHeadlineSub: 'You can still view incoming chats, but Nahla will not reply automatically until your subscription is active.',
  },
  subscription: {
    name: 'Subscription',
  },
  footer: {
    saudi: 'Saudi team 100% 🇸🇦 · Nahla AI',
  },
}

export const EMBEDDED_STRINGS: Record<EmbeddedLang, EmbeddedStrings> = { ar, en }
