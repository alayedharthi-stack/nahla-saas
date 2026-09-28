/**
 * Static UI labels for the live-campaign hero cards, the filtered statistics
 * strip and the "edit content" dialog on the campaigns page.
 *
 * Every string here is fixed dashboard chrome. Runtime facts (counts, times,
 * reasons) arrive from the API as structured fields and are formatted into
 * these templates; nothing customer-facing is composed here.
 */
import type { Lang } from './types'

export interface CampaignHeroLabels {
  hero: {
    sectionTitle: string
    sectionHint: string
    messagePreview: string
    progress: string                 // "{done} of {total} recipients reached"
    progressPlanned: string          // "{total} planned"
    lastUpdated: string              // "Updated {time}"
    lastProviderEvent: string        // "last WhatsApp receipt {time}"
    providerLag: string
    primary: { reached: string; read: string; clicked: string }
    secondary: {
      remaining: string
      acceptedUnconfirmed: string
      failedFinal: string
      excluded: string
      recipientLimit: string
      uncertain: string
    }
    uniqueNote: string
    clickUnavailable: string
    clickPending: string
    clickPartial: string             // "of {n} tracked messages"
    clickReasons: Record<string, string>
    diagnostics: string
    diagnosticsHide: string
    actions: { pause: string; resume: string; resuming: string; edit: string; diagnose: string }
    status: {
      sendingNow: string
      sendingIdle: string
      pendingStart: string
      waitingScheduler: string
      stalledRecovering: string
      capacityWait: string
      capacityWaitAt: string         // "{time}"
      capacityWaitExactAt: string
      capacityWaitUnknown: string
      rateLimitAt: string
      rateLimitUnknown: string
      merchantPaused: string
      contentRevised: string
      needsReview: string
      offerExpired: string
      offerExpiredAction: string
      providerThrottled: string
      providerThrottledUntil: string
      reviewGeneric: string
      completed: string
      nextCheck: string              // "next check {time}"
      nextCheckUnknown: string
    }
    facts: {
      pauseReason: string
      pauseDetail: string
      heartbeat: string
      workerRunning: string
      workerIdle: string
      capacity: string               // "{used} / {budget} of Meta limit {limit}"
      stallAttempt: string
      revision: string
      offerExpires: string
      noExpiry: string
      dispatchErrors: string
      errorBreakdown: string
      none: string
    }
  }
  filters: {
    title: string
    scope: string
    scopeLatest: string
    scopeAll: string
    scopeRunning: string
    scopeCampaign: string
    period: string
    periods: Record<string, string>
    from: string
    to: string
    basisNote: string
    tiles: {
      reached: string
      read: string
      clicked: string
      accepted: string
      failedFinal: string
      remaining: string
      excluded: string
      recipientLimit: string
    }
    remainingNote: string
    asOf: string
    loading: string
    loadFailed: string
    notAvailable: string
    detailScopeNote: string
  }
  edit: {
    title: string
    subtitle: string
    template: string
    templateHint: string
    variables: string
    variableLabel: string            // "Variable {n}"
    coupon: string
    couponHint: string
    expiry: string
    expiryHint: string
    clearExpiry: string
    note: string
    notePlaceholder: string
    preview: string
    previewNote: string
    history: string
    revision: string                 // "Version {n}"
    current: string
    sends: string                    // "{accepted} sent · {delivered} delivered · {read} read"
    noSends: string
    save: string
    saving: string
    saveAndResume: string
    cancel: string
    saved: string
    savedResumeHint: string
    errorNoChange: string
    errorActive: string
    errorWorker: string
    errorGeneric: string
    loading: string
  }
  alerts: {
    resumeRefusedOfferExpired: string
    resumeRefusedThrottled: string
    actionFailed: string
  }
}

const EN: CampaignHeroLabels = {
  hero: {
    sectionTitle: 'Live campaigns',
    sectionHint: 'Counters are unique customers, read from the send log. WhatsApp receipts can lag by minutes.',
    messagePreview: 'Message',
    progress: '{done} of {total} customers reached',
    progressPlanned: '{total} planned recipients',
    lastUpdated: 'Updated {time}',
    lastProviderEvent: 'last WhatsApp receipt {time}',
    providerLag: 'WhatsApp reports delivery, reads and taps with a delay — these are confirmed events, not estimates.',
    primary: { reached: 'Reached', read: 'Read', clicked: 'Tapped the button' },
    secondary: {
      remaining: 'Remaining',
      acceptedUnconfirmed: 'Accepted by WhatsApp, delivery not confirmed',
      failedFinal: 'Failed (final)',
      excluded: 'Excluded',
      recipientLimit: 'Recipient reached their WhatsApp marketing limit',
      uncertain: 'Unknown outcome',
    },
    uniqueNote: 'Unique customers · {messages} messages attempted',
    clickUnavailable: 'Not available',
    clickPending: 'Measured from the next sends',
    clickPartial: 'of {n} tracked messages',
    clickReasons: {
      no_quick_reply_buttons: 'This template has no quick-reply buttons. WhatsApp does not report taps on link or copy-code buttons.',
      sent_before_tracking: 'These messages were sent before tap measurement existed; earlier taps are not attributed.',
      measured_from_next_sends: 'Taps are measured for messages sent from now on.',
      template_unknown: 'The template could not be read.',
      quick_reply: 'Quick-reply taps on tracked messages.',
    },
    diagnostics: 'Diagnostic details',
    diagnosticsHide: 'Hide diagnostic details',
    actions: { pause: 'Pause', resume: 'Resume', resuming: 'Resuming…', edit: 'Edit offer & content', diagnose: 'Diagnose' },
    status: {
      sendingNow: 'Sending now.',
      sendingIdle: 'Sending — waiting for the next batch.',
      pendingStart: 'Waiting to start sending.',
      waitingScheduler: 'Scheduled — the platform starts it at the planned time.',
      stalledRecovering: 'The sending worker stopped (deploy or restart) — it resumes automatically where it left off.',
      capacityWait: 'Waiting for sending capacity — resumes automatically.',
      capacityWaitAt: 'Next check around {time}.',
      capacityWaitExactAt: 'A slot frees around {time}.',
      capacityWaitUnknown: 'Time not yet known; checked every few minutes.',
      rateLimitAt: 'WhatsApp asked to slow down — resumes automatically around {time}.',
      rateLimitUnknown: 'WhatsApp asked to slow down — resumes automatically shortly.',
      merchantPaused: 'You paused this campaign. Resume whenever you are ready.',
      contentRevised: 'A new content version is saved. Resume to send it to the remaining customers.',
      needsReview: 'Stopped — please review before resuming.',
      offerExpired: 'The offer end date has passed. Nothing more is sent until you update the offer or its date.',
      offerExpiredAction: 'Edit the offer to continue with the remaining customers.',
      providerThrottled: 'Sending paused after repeated provider errors. Review the details before resuming.',
      providerThrottledUntil: 'A resume can send again from {time}.',
      reviewGeneric: 'Stopped for a reason that needs your review.',
      completed: 'Completed.',
      nextCheck: 'Next automatic check {time}.',
      nextCheckUnknown: 'Next check time not yet known.',
    },
    facts: {
      pauseReason: 'Pause reason',
      pauseDetail: 'Technical detail',
      heartbeat: 'Last worker heartbeat',
      workerRunning: 'A worker is sending now',
      workerIdle: 'No worker is sending now',
      capacity: '{used} / {budget} of the Meta limit ({limit}) used in the last 24h',
      stallAttempt: 'Automatic recovery attempt {n}',
      revision: 'Content version {n}',
      offerExpires: 'Offer ends {time}',
      noExpiry: 'No offer end date',
      dispatchErrors: 'Dispatcher notes',
      errorBreakdown: 'Failure reasons',
      none: '—',
    },
  },
  filters: {
    title: 'Statistics',
    scope: 'Campaign',
    scopeLatest: 'Latest campaign',
    scopeAll: 'All campaigns',
    scopeRunning: 'Live campaigns',
    scopeCampaign: 'Specific campaign',
    period: 'Period',
    periods: { today: 'Today', week: 'This week', month: 'This month', year: 'This year', all: 'All time', custom: 'Custom' },
    from: 'From',
    to: 'To',
    basisNote: 'The period is applied to when each event happened (accepted, delivered, read, tapped, failed) — not to when the campaign was created.',
    tiles: {
      reached: 'Reached',
      read: 'Read',
      clicked: 'Tapped',
      accepted: 'Accepted by WhatsApp',
      failedFinal: 'Failed (final)',
      remaining: 'Remaining now',
      excluded: 'Excluded',
      recipientLimit: 'Recipient limit',
    },
    remainingNote: 'Remaining is the live queue, not time-bounded.',
    asOf: 'As of {time}',
    loading: 'Loading statistics…',
    loadFailed: 'Statistics could not be loaded.',
    notAvailable: 'Not available',
    detailScopeNote: 'Showing this campaign only.',
  },
  edit: {
    title: 'Edit offer & content',
    subtitle: 'Changes apply to customers not yet sent. Customers who already received the message are never sent again.',
    template: 'Template (Meta-approved)',
    templateHint: 'Only approved templates can be used.',
    variables: 'Variables',
    variableLabel: 'Variable {n}',
    coupon: 'Coupon code',
    couponHint: 'Leave empty for none.',
    expiry: 'Offer end date',
    expiryHint: 'Sending stops on its own when this passes.',
    clearExpiry: 'No end date',
    note: 'Note (optional)',
    notePlaceholder: 'Why this version changed',
    preview: 'Preview',
    previewNote: 'How the next messages will look.',
    history: 'Versions & send history',
    revision: 'Version {n}',
    current: 'current',
    sends: '{accepted} sent · {delivered} delivered · {read} read',
    noSends: 'No messages sent with this version',
    save: 'Save version',
    saving: 'Saving…',
    saveAndResume: 'Save & resume',
    cancel: 'Cancel',
    saved: 'Version saved.',
    savedResumeHint: 'Resume the campaign to send it to the remaining customers.',
    errorNoChange: 'Nothing changed.',
    errorActive: 'Pause the campaign before editing.',
    errorWorker: 'A send is in progress or has an unknown outcome — try again in a moment.',
    errorGeneric: 'Could not save: {msg}',
    loading: 'Loading…',
  },
  alerts: {
    resumeRefusedOfferExpired: 'The offer has expired — edit the offer or its end date before resuming.',
    resumeRefusedThrottled: 'WhatsApp is still restricting this number; try again after the shown time.',
    actionFailed: 'The action could not be completed: {msg}',
  },
}

const AR: CampaignHeroLabels = {
  hero: {
    sectionTitle: 'الحملات الجارية',
    sectionHint: 'العدّادات عملاء فريدون من سجل الإرسال الموثوق. إشعارات واتساب قد تتأخر دقائق.',
    messagePreview: 'الرسالة',
    progress: 'وصلت إلى {done} من {total} عميلاً',
    progressPlanned: '{total} مستلماً مخططاً',
    lastUpdated: 'آخر تحديث {time}',
    lastProviderEvent: 'آخر إشعار من واتساب {time}',
    providerLag: 'واتساب يبلّغ عن التسليم والقراءة والضغط بتأخير — هذه أحداث مؤكدة لا تقديرات.',
    primary: { reached: 'وصلت إليهم', read: 'قرؤوها', clicked: 'ضغطوا الزر' },
    secondary: {
      remaining: 'المتبقي',
      acceptedUnconfirmed: 'قبلها واتساب ولم يثبت التسليم',
      failedFinal: 'فشل نهائي',
      excluded: 'مستبعدون',
      recipientLimit: 'بلغ العميل حد رسائل واتساب التسويقية',
      uncertain: 'نتيجة غير محسومة',
    },
    uniqueNote: 'عملاء فريدون · {messages} محاولة إرسال',
    clickUnavailable: 'غير متاح',
    clickPending: 'يُقاس من الإرسالات القادمة',
    clickPartial: 'من {n} رسالة مُتابَعة',
    clickReasons: {
      no_quick_reply_buttons: 'هذا القالب بلا أزرار رد سريع. واتساب لا يبلّغ عن الضغط على أزرار الروابط أو نسخ الكود.',
      sent_before_tracking: 'أُرسلت هذه الرسائل قبل تفعيل قياس الضغط؛ لا يُنسب ضغط سابق بأثر رجعي.',
      measured_from_next_sends: 'يُقاس الضغط للرسائل التي تُرسل من الآن.',
      template_unknown: 'تعذر قراءة القالب.',
      quick_reply: 'ضغطات أزرار الرد السريع على الرسائل المُتابَعة.',
    },
    diagnostics: 'تفاصيل التشخيص',
    diagnosticsHide: 'إخفاء تفاصيل التشخيص',
    actions: { pause: 'إيقاف', resume: 'استئناف', resuming: 'جارٍ الاستئناف…', edit: 'تعديل العرض والمحتوى', diagnose: 'تشخيص' },
    status: {
      sendingNow: 'جارٍ الإرسال الآن.',
      sendingIdle: 'جارٍ الإرسال — بانتظار الدفعة التالية.',
      pendingStart: 'بانتظار بدء الإرسال.',
      waitingScheduler: 'مجدولة — تبدأ تلقائياً في الوقت المحدد.',
      stalledRecovering: 'توقف عامل الإرسال (إعادة نشر أو تشغيل) — يُستأنف تلقائياً من حيث توقف.',
      capacityWait: 'بانتظار توفر سعة الإرسال — سيستأنف تلقائياً.',
      capacityWaitAt: 'الفحص التالي قرابة {time}.',
      capacityWaitExactAt: 'تتوفر سعة قرابة {time}.',
      capacityWaitUnknown: 'الموعد غير محدد بعد؛ يُفحص كل بضع دقائق.',
      rateLimitAt: 'طلب واتساب الإبطاء — سيستأنف تلقائياً قرابة {time}.',
      rateLimitUnknown: 'طلب واتساب الإبطاء — سيستأنف تلقائياً بعد قليل.',
      merchantPaused: 'أوقفتَ هذه الحملة. استأنفها متى شئت.',
      contentRevised: 'حُفظت نسخة جديدة من المحتوى. استأنف لإرسالها للعملاء المتبقين.',
      needsReview: 'متوقفة — تحتاج مراجعتك قبل الاستئناف.',
      offerExpired: 'انتهى تاريخ العرض. لن يُرسل شيء حتى تحدّث العرض أو تاريخه.',
      offerExpiredAction: 'عدّل العرض لتكمل للعملاء المتبقين.',
      providerThrottled: 'توقف الإرسال بعد تكرر أخطاء المزوّد. راجع التفاصيل قبل الاستئناف.',
      providerThrottledUntil: 'يمكن للاستئناف الإرسال من {time}.',
      reviewGeneric: 'متوقفة لسبب يستلزم مراجعتك.',
      completed: 'اكتملت.',
      nextCheck: 'الفحص التلقائي التالي {time}.',
      nextCheckUnknown: 'موعد الفحص التالي غير محدد بعد.',
    },
    facts: {
      pauseReason: 'سبب التوقف',
      pauseDetail: 'التفصيل التقني',
      heartbeat: 'آخر نبضة من العامل',
      workerRunning: 'يوجد عامل يُرسل الآن',
      workerIdle: 'لا يوجد عامل يُرسل الآن',
      capacity: 'استُخدم {used} / {budget} من حد Meta ({limit}) خلال 24 ساعة',
      stallAttempt: 'محاولة التعافي التلقائي رقم {n}',
      revision: 'نسخة المحتوى {n}',
      offerExpires: 'ينتهي العرض {time}',
      noExpiry: 'لا يوجد تاريخ انتهاء للعرض',
      dispatchErrors: 'ملاحظات المُرسِل',
      errorBreakdown: 'أسباب الفشل',
      none: '—',
    },
  },
  filters: {
    title: 'الإحصاءات',
    scope: 'الحملة',
    scopeLatest: 'آخر حملة',
    scopeAll: 'جميع الحملات',
    scopeRunning: 'الحملات الجارية',
    scopeCampaign: 'حملة محددة',
    period: 'الفترة',
    periods: { today: 'اليوم', week: 'هذا الأسبوع', month: 'هذا الشهر', year: 'هذه السنة', all: 'منذ البداية', custom: 'فترة مخصصة' },
    from: 'من',
    to: 'إلى',
    basisNote: 'تُطبَّق الفترة على وقت وقوع كل حدث (القبول، التسليم، القراءة، الضغط، الفشل) لا على وقت إنشاء الحملة.',
    tiles: {
      reached: 'وصلت إليهم',
      read: 'قرؤوها',
      clicked: 'ضغطوا الزر',
      accepted: 'قبلها واتساب',
      failedFinal: 'فشل نهائي',
      remaining: 'المتبقي الآن',
      excluded: 'مستبعدون',
      recipientLimit: 'حد المستلم',
    },
    remainingNote: 'المتبقي هو الطابور الحالي ولا يتقيّد بالفترة.',
    asOf: 'حتى {time}',
    loading: 'جارٍ تحميل الإحصاءات…',
    loadFailed: 'تعذر تحميل الإحصاءات.',
    notAvailable: 'غير متاح',
    detailScopeNote: 'تُعرض إحصاءات هذه الحملة وحدها.',
  },
  edit: {
    title: 'تعديل العرض والمحتوى',
    subtitle: 'يُطبَّق التعديل على العملاء الذين لم تُرسل لهم بعد. من وصلتهم الرسالة لا يُعاد إرسالها لهم أبداً.',
    template: 'القالب (معتمد من Meta)',
    templateHint: 'لا يمكن استخدام إلا القوالب المعتمدة.',
    variables: 'المتغيرات',
    variableLabel: 'المتغير {n}',
    coupon: 'كود الخصم',
    couponHint: 'اتركه فارغاً إن لم يوجد.',
    expiry: 'تاريخ انتهاء العرض',
    expiryHint: 'يتوقف الإرسال تلقائياً عند انقضائه.',
    clearExpiry: 'بلا تاريخ انتهاء',
    note: 'ملاحظة (اختياري)',
    notePlaceholder: 'سبب تغيير هذه النسخة',
    preview: 'المعاينة',
    previewNote: 'هكذا ستبدو الرسائل القادمة.',
    history: 'النسخ وسجل الإرسال',
    revision: 'النسخة {n}',
    current: 'الحالية',
    sends: '{accepted} أُرسلت · {delivered} وصلت · {read} قُرئت',
    noSends: 'لم تُرسل رسائل بهذه النسخة',
    save: 'حفظ النسخة',
    saving: 'جارٍ الحفظ…',
    saveAndResume: 'حفظ واستئناف',
    cancel: 'إلغاء',
    saved: 'حُفظت النسخة.',
    savedResumeHint: 'استأنف الحملة لإرسالها للعملاء المتبقين.',
    errorNoChange: 'لا يوجد تغيير.',
    errorActive: 'أوقف الحملة قبل التعديل.',
    errorWorker: 'يوجد إرسال قيد التنفيذ أو محاولة لم تُحسم نتيجتها — أعد المحاولة بعد لحظات.',
    errorGeneric: 'تعذر الحفظ: {msg}',
    loading: 'جارٍ التحميل…',
  },
  alerts: {
    resumeRefusedOfferExpired: 'انتهى العرض — عدّل العرض أو تاريخ انتهائه قبل الاستئناف.',
    resumeRefusedThrottled: 'واتساب ما زال يقيّد هذا الرقم؛ أعد المحاولة بعد الوقت المعروض.',
    actionFailed: 'تعذر تنفيذ الإجراء: {msg}',
  },
}

export function campaignHeroLabels(lang: Lang): CampaignHeroLabels {
  return lang === 'en' ? EN : AR
}

/** Replace `{key}` tokens in a static label with formatted runtime values. */
export function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, k) => (values[k] !== undefined ? String(values[k]) : `{${k}}`))
}
