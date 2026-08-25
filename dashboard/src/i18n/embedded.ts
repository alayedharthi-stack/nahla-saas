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
    appIdMisconfigured: string
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
    needsReauth:       string
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
    reconnectStore:  string
    syncAppNotReady: string
    storeLinkBlurb:  string
    storeLinkLead:   string
    linkComplete:    string
    reauthMessage:   string
    couponSyncReady: string
    openedFromSalla: string
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
  /** /salla-callback — after Salla App Store install */
  callback: {
    successInstalled:   string
    successRenewed:     string
    storePrefix:        string
    howToStartTitle:    string
    /** With placeholders {appsLabel} and {useAppLabel} inlined as <b>. */
    howToStartBody:     string
    howToStartAppsLabel:    string
    howToStartUseAppLabel:  string
    errorTitle:         string
    errorBody:          string
    closePage:          string
  }
  /** /integrations/salla/success — OAuth Sync success landing */
  oauthSuccess: {
    title:       string
    /** "Linked store {storeName} to Nahla AI" */
    subtitleWithStore: string
    storeIdLabel:string
    whatNow:     string
    bullet1:     string
    bullet2:     string
    bullet3:     string
    btnSettings: string
    btnHome:     string
  }
  /** /integrations/salla/error — OAuth Sync error landing */
  oauthError: {
    title:       string
    /** Used when the reason code is unknown — placeholder is appended verbatim. */
    fallbackReason: string
    reasons: {
      missing_code:          string
      token_exchange_failed: string
      app_not_configured:    string
      db_save_failed:        string
      network_error:         string
      access_denied:         string
    }
    howToFixTitle: string
    fix1:        string
    fix2:        string
    fix3:        string
    btnBack:     string
    btnRetry:    string
  }
  /** /app/salla/launch — short-lived launch token consumer */
  launch: {
    loadingTitle:    string
    loadingSubtitle: string
    errorTitle:      string
    errorInvalidLink:string
    errorGeneric:    string
    btnBackToSalla:  string
  }
  /** /app/salla/setup — currently auto-redirects, kept for completeness */
  setup: {
    brand:    string
    skip:     string
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
    appIdMisconfigured: 'إعداد تطبيق سلة غير مكتمل على منصة نحلة. لا تكرر المحاولة — تواصل مع الدعم لإكمال الإعداد.',
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
    sallaEmbedded:  'سلة',
    apiFull:        'ربط سلة',
    needsReauth:    'يحتاج إعادة ربط',
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
    connectStore:    'إكمال ربط سلة',
    reconnectStore:  'إعادة ربط سلة',
    syncAppNotReady: 'لم يتم تكوين تطبيق المزامنة بعد. تواصل مع الدعم لإكمال الإعداد.',
    storeLinkBlurb:  'مطلوب لمزامنة الكوبونات والطلبات والمنتجات مع سلة.',
    storeLinkLead:   'أكمل ربط سلة لتفعيل المزامنة',
    linkComplete:    'الربط مكتمل',
    reauthMessage:   'انتهت صلاحية ربط سلة. أعد الربط لتفعيل المزامنة.',
    couponSyncReady: 'مزامنة الكوبونات جاهزة.',
    openedFromSalla: 'تم فتح تطبيق نحلة من سلة',
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
  callback: {
    successInstalled:      'تم تثبيت نحلة بنجاح!',
    successRenewed:        'تم تجديد الربط بنجاح!',
    storePrefix:           'المتجر',
    howToStartTitle:       'للبدء باستخدام نحلة:',
    howToStartBody:        'عُد إلى متجرك في سلة، وستجد نحلة الآن في قسم {appsLabel} مع زر {useAppLabel} جاهزاً للضغط.',
    howToStartAppsLabel:   '«تطبيقاتي»',
    howToStartUseAppLabel: '«استخدام التطبيق»',
    errorTitle:            'حدث خطأ أثناء ربط المتجر',
    errorBody:             'يمكنك إعادة المحاولة من متجر تطبيقات سلة.',
    closePage:             'إغلاق هذه الصفحة',
  },
  oauthSuccess: {
    title:             'تم ربط المتجر بنجاح',
    subtitleWithStore: 'تم ربط متجر {storeName} بنحلة AI',
    storeIdLabel:      'معرّف المتجر:',
    whatNow:           'ماذا يمكنك الآن؟',
    bullet1:           '• جلب المنتجات والطلبات مباشرة من سلة',
    bullet2:           '• إنشاء طلبات حقيقية عبر وكيل المبيعات',
    bullet3:           '• التحقق من رموز الخصم تلقائياً',
    btnSettings:       'إعدادات الربط',
    btnHome:           'الصفحة الرئيسية',
  },
  oauthError: {
    title:          'فشل ربط المتجر',
    fallbackReason: 'حدث خطأ أثناء ربط المتجر.',
    reasons: {
      missing_code:          'لم يتم استلام رمز التفويض من سلة.',
      token_exchange_failed: 'فشل تبادل رمز التفويض مع سلة. تأكد من صحة بيانات التطبيق.',
      app_not_configured:    'التطبيق غير مهيأ بالكامل. تواصل مع الدعم.',
      db_save_failed:        'فشل حفظ بيانات الربط في قاعدة البيانات.',
      network_error:         'حدث خطأ في الشبكة أثناء التواصل مع سلة. حاول مرة أخرى.',
      access_denied:         'رفض المستخدم منح الصلاحيات لنحلة AI.',
    },
    howToFixTitle: 'خطوات لحل المشكلة:',
    fix1:          '• تأكد أن التطبيق مفعّل في لوحة تحكم سلة',
    fix2:          '• تأكد أن Redirect URI صحيح: api.nahlah.ai/oauth/salla/callback',
    fix3:          '• تواصل مع الدعم إذا استمرت المشكلة',
    btnBack:       'العودة',
    btnRetry:      'حاول مجدداً',
  },
  launch: {
    loadingTitle:    'جارٍ تسجيل الدخول…',
    loadingSubtitle: 'سيتم توجيهك تلقائياً خلال لحظات',
    errorTitle:      'تعذر تسجيل الدخول',
    errorInvalidLink:'رابط الدخول غير صالح أو منتهي الصلاحية، حاول فتح التطبيق من سلة مجدداً.',
    errorGeneric:    'تعذر تسجيل الدخول من سلة، حاول فتح التطبيق مرة أخرى.',
    btnBackToSalla:  'العودة إلى سلة',
  },
  setup: {
    brand: 'نحلة AI',
    skip:  'تخطي الإعداد والدخول للوحة التحكم',
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
    appIdMisconfigured: 'Salla app configuration is incomplete on Nahla. Do not retry — contact support to finish setup.',
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
    sallaEmbedded:  'Salla',
    apiFull:        'Salla sync link',
    needsReauth:    'Needs re-link',
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
    connectStore:    'Complete Salla link',
    reconnectStore:  'Re-link Salla',
    syncAppNotReady: 'Sync app is not configured yet. Contact support to finish setup.',
    storeLinkBlurb:  'Required to sync coupons, orders, and products with Salla.',
    storeLinkLead:   'Complete Salla link to enable sync',
    linkComplete:    'Link complete',
    reauthMessage:   'Your Salla link expired. Re-link to restore sync.',
    couponSyncReady: 'Coupon sync is ready.',
    openedFromSalla: 'Nahla opened from Salla',
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
  callback: {
    successInstalled:      'Nahla installed successfully!',
    successRenewed:        'Connection renewed successfully!',
    storePrefix:           'Store',
    howToStartTitle:       'To start using Nahla:',
    howToStartBody:        'Go back to your store on Salla — Nahla is now under {appsLabel} with the {useAppLabel} button ready to click.',
    howToStartAppsLabel:   '"My Apps"',
    howToStartUseAppLabel: '"Use App"',
    errorTitle:            'Something went wrong while linking your store',
    errorBody:             'You can retry from the Salla App Store.',
    closePage:             'Close this page',
  },
  oauthSuccess: {
    title:             'Store linked successfully',
    subtitleWithStore: 'Store {storeName} is now linked to Nahla AI',
    storeIdLabel:      'Store ID:',
    whatNow:           'What can you do now?',
    bullet1:           '• Pull products and orders directly from Salla',
    bullet2:           '• Create real orders through the sales agent',
    bullet3:           '• Verify discount codes automatically',
    btnSettings:       'Integration settings',
    btnHome:           'Go to dashboard',
  },
  oauthError: {
    title:          'Store link failed',
    fallbackReason: 'Something went wrong while linking the store.',
    reasons: {
      missing_code:          'Salla did not return an authorization code.',
      token_exchange_failed: 'Token exchange with Salla failed. Verify your app credentials.',
      app_not_configured:    'The app is not fully configured. Contact support.',
      db_save_failed:        'Failed to save the link in the database.',
      network_error:         'A network error occurred while talking to Salla. Try again.',
      access_denied:         'The user denied granting permissions to Nahla AI.',
    },
    howToFixTitle: 'Steps to fix it:',
    fix1:          '• Make sure the app is enabled in your Salla partner dashboard',
    fix2:          '• Confirm the redirect URI is: api.nahlah.ai/oauth/salla/callback',
    fix3:          '• Contact support if the issue persists',
    btnBack:       'Back',
    btnRetry:      'Try again',
  },
  launch: {
    loadingTitle:    'Signing you in…',
    loadingSubtitle: "You'll be redirected automatically in a moment",
    errorTitle:      'Sign-in failed',
    errorInvalidLink:'The sign-in link is invalid or expired. Reopen the app from Salla.',
    errorGeneric:    'Could not sign you in via Salla. Try opening the app again.',
    btnBackToSalla:  'Back to Salla',
  },
  setup: {
    brand: 'Nahla AI',
    skip:  'Skip setup and go to the dashboard',
  },
}

export const EMBEDDED_STRINGS: Record<EmbeddedLang, EmbeddedStrings> = { ar, en }
