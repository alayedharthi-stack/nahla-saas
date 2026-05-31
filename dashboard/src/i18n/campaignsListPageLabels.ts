/**
 * Campaigns list page UI labels — static chrome only (wizard lives in campaignsMgmt).
 */
export interface CampaignsListLabels {
  pageSubtitle: string
  newCampaign: string
  stats: {
    completed: string
    totalSent: string
    totalSentTooltipBoth: string
    totalSentTooltipAccepted: string
    totalSentFailedSuffix: string
    openRateDelivered: string
    openRateAccepted: string
    openRateTooltipDelivered: string
    openRateTooltipAccepted: string
    openRateTooltipNone: string
    conversionRate: string
  }
  failedBanner: string
  table: {
    campaign: string
    type: string
    status: string
    audience: string
    sent: string
    openRate: string
    conversion: string
    actions: string
  }
  loading: string
  emptyTitle: string
  emptyHint: string
  waveAdaptive: string
  waveBatched: string
  status: {
    active: string
    scheduled: string
    completed: string
    paused: string
    draft: string
    failed: string
  }
  lifecycle: {
    draft: string
    waiting_scheduler: string
    pending_dispatch: string
    sending: string
    sent: string
    partial: string
    partial_minor: string
    no_whatsapp_recipients: string
    excluded_before_send: string
    orphaned_materialized_rows: string
    unknown_status: string
    completed_empty: string
    failed: string
    failed_all: string
    unknown: string
  }
  types: {
    broadcast: string
    abandoned_cart: string
    vip: string
    new_arrivals: string
    win_back: string
  }
  bulk: {
    selected: string
    deleteSelected: string
    cancel: string
  }
  row: {
    hideDetails: string
    showFailureReason: string
    failedCount: string
    pause: string
    resume: string
    launch: string
    diagnose: string
    diagnosing: string
    diagnoseTitle: string
    dispatchNow: string
    dispatching: string
    dispatchTitle: string
    ignoreFreqCap: string
    delete: string
    copyTechnicalErrorTitle: string
    errorCopied: string
    copyFailed: string
    failureDetailsTitle: string
  }
  waves: {
    loadFailed: string
    title: string
    strategyAdaptive: string
    strategyManual: string
    waveCount: string
    perBatch: string
    failedSuffix: string
    waveOf: string
    statuses: {
      pending: string
      dispatching: string
      completed: string
      failed: string
      paused: string
      cancelled: string
    }
  }
  admin: {
    mediaCheck: string
    mediaCheckTitle: string
    directSend: string
    directSendTitle: string
  }
  diagnostics: {
    debugTemplate: {
      loading: string
      hide: string
      show: string
      loadFailed: string
    }
    providerBlock: {
      title: string
      fallbackBody: string
      bundleLoading: string
      bundleCopied: string
      bundleError: string
      copyBundle: string
      bundleHint: string
    }
    excluded: {
      title: string
      noPhone: string
      notePrefix: string
      noteMiddle: string
      noteSuffix: string
      noteStrongUnknown: string
      noteStrongNo: string
    }
    fieldFlags: {
      phone: string
      normalized: string
      unsubscribed: string
      pendingUnsub: string
      marketingOptOut: string
      whatsapp: string
    }
    fieldValues: {
      yes: string
      no: string
      unknown: string
    }
    unknownMeta: {
      title: string
      body: string
      copyTitle: string
      copy: string
    }
    rawMeta: {
      title: string
      copyAllTitle: string
      copyAll: string
      intro: string
      templateMismatch: string
      payloadDiffTitle: string
      copySampleTitle: string
      copySample: string
      requestPayload: string
      responsePayload: string
    }
    delivery: {
      title: string
      deliveredOf: string
      missingWamid: string
      missingWamidSuffix: string
      sampleTitle: string
      noWamid: string
      stages: {
        accepted_by_provider: string
        delivered: string
        read: string
        failed_after_accept: string
        unknown_delivery: string
      }
    }
    retryHealth: {
      title: string
      stormHeadline: string
      ceilingHeadline: string
      zombieHeadline: string
      okHeadline: string
      maxAttempts: string
      rowsAtCeiling: string
      zombieRows: string
      maxSendAttempts: string
      stormNote: string
      zombieNote: string
    }
    statusBreakdown: {
      title: string
      exoticTitle: string
      rows: Record<string, string>
    }
    sampleRows: {
      title: string
      colId: string
      colPhone: string
      colStatus: string
      colSkip: string
      colError: string
      colAttempts: string
      colUpdated: string
    }
    dispatchErrors: {
      title: string
    }
    report: {
      sentSummary: string
      sentFailedSuffix: string
      sentSkippedSuffix: string
      templateLine: string
      whatsappLine: string
      schedulerLine: string
      funnelHeader: string
      funnelRaw: string
      funnelReachable: string
      funnelMaterialized: string
      funnelQueued: string
      funnelFreqCap: string
      excludedHeader: string
      excludedItem: string
      deliveryHeader: string
      deliveryAccepted: string
      deliveryDelivered: string
      deliveryRead: string
      deliveryFailedAfter: string
      deliveryUnknown: string
      deliveryMissingWamid: string
      failureHeader: string
      hintsPrefix: string
      freqCapHeader: string
      freqCapLatest: string
      freqCapCampaignSuffix: string
      freqCapRow: string
      diagnoseFailed: string
      dispatchConfirm: string
      dispatchConfirmFreqCap: string
      dispatchStarted: string
      dispatchSkipped: string
      dispatchFailed: string
      dispatchRescheduled: string
      dispatchRevived: string
      dispatchProgress: string
      dispatchDone: string
      pollSent: string
      pollQueued: string
      pollFailed: string
      pollExcluded: string
      pollLifecycle: string
    }
  }
  runtime: {
    schedulerOn: string
    schedulerOff: string
    noConnection: string
    templateMissing: string
    excludeReasons: Record<string, string>
    errorCodes: Record<string, { label: string; advice?: string }>
  }
}

const ERROR_CODES_EN: CampaignsListLabels['runtime']['errorCodes'] = {
  not_on_whatsapp: {
    label: 'Number is not on WhatsApp',
    advice: 'This customer cannot be reached on WhatsApp — skip or use another channel.',
  },
  invalid_phone: {
    label: 'Invalid phone number',
    advice: 'Use E.164 format (e.g. +9665XXXXXXXX).',
  },
  out_of_24h_window: {
    label: '24-hour service window expired',
    advice: 'Use an approved marketing template instead of a free-form message.',
  },
  user_not_opted_in: {
    label: 'Customer has not opted in to marketing',
    advice: 'Collect opt-in before sending this campaign.',
  },
  marketing_blocked: {
    label: 'Meta currently blocks marketing to this customer',
    advice: 'Try again later — Meta may re-evaluate the customer.',
  },
  client_payment_blocked: {
    label: 'Number restricted by WhatsApp (payment / Meta limits)',
    advice: 'This is a Meta-side restriction on the customer account. Skip and continue — it will not hurt sender quality.',
  },
  rate_limit: {
    label: 'Rate limit exceeded — wait a minute',
    advice: 'Retry after a few minutes — Meta enforces a per-minute cap.',
  },
  spam_rate_limit: {
    label: 'Daily campaign send limit exceeded',
    advice: 'Wait 24 hours or improve your number quality rating with Meta.',
  },
  template_param_mismatch: {
    label: 'Template variable count does not match Meta approval',
    advice: 'Open the template and ensure every {{1}}, {{2}}… has a value.',
  },
  template_not_found: {
    label: 'Template not found in Meta',
    advice: 'Check the template name and language code in WhatsApp Templates.',
  },
  template_paused: {
    label: 'Template is paused in Meta',
    advice: 'Re-enable or replace the template in Meta Business Manager.',
  },
  template_disabled: {
    label: 'Template is disabled in Meta',
    advice: 'Submit a new template or restore this one in Meta.',
  },
  policy_violation: {
    label: 'Message blocked by Meta policy',
    advice: 'Review template content and category with Meta guidelines.',
  },
  account_locked: {
    label: 'WhatsApp Business account locked',
    advice: 'Contact 360dialog / Meta support — dashboard retries will not help.',
  },
  service_unavailable: {
    label: 'WhatsApp service temporarily unavailable',
    advice: 'Retry later — this is usually transient on Meta\'s side.',
  },
  media_error: {
    label: 'Media attachment error',
    advice: 'Check header media URL and format in the template.',
  },
  auth_error: {
    label: 'Authentication error with Meta / 360dialog',
    advice: 'Reconnect WhatsApp or refresh the access token.',
  },
  no_message_id: {
    label: 'No message ID returned from provider',
    advice: 'Check send logs — the provider accepted but did not return an ID.',
  },
  exception: {
    label: 'Unexpected send error',
    advice: 'Copy the technical error for support.',
  },
  unknown: {
    label: 'Unclassified Meta error',
    advice: 'Inspect raw Meta samples below and send to support.',
  },
  retry_exhausted: {
    label: 'Maximum send attempts reached',
    advice: 'Fix the underlying error before retrying this row.',
  },
  retry_storm: {
    label: 'Retry storm detected — circuit breaker tripped',
    advice: 'Check server logs for campaign_send_retry_storm.',
  },
  watchdog_timeout: {
    label: 'Row stuck in sending — watchdog timed out',
    advice: 'Will be returned to queued on the next dispatch.',
  },
  recipient_quality_low: {
    label: 'Recipient quality too low for send',
    advice: 'Meta flagged this number — consider excluding from campaigns.',
  },
  blocked_by_user: {
    label: 'Customer blocked your business on WhatsApp',
    advice: 'Do not retry — respect the block.',
  },
  country_restricted: {
    label: 'Country / region restriction',
    advice: 'This destination is not allowed for your WABA.',
  },
  temporary_failure: {
    label: 'Temporary delivery failure',
    advice: 'May succeed on retry after a delay.',
  },
  permanent_failure: {
    label: 'Permanent delivery failure',
    advice: 'Do not retry without fixing the root cause.',
  },
}

const ERROR_CODES_AR: CampaignsListLabels['runtime']['errorCodes'] = {
  not_on_whatsapp: {
    label: 'الرقم لا يملك حساب واتساب',
    advice: 'هذا العميل لا يمكن مراسلته على واتساب — تجاهله أو تواصل عبر قناة أخرى.',
  },
  invalid_phone: {
    label: 'رقم الهاتف غير صالح',
    advice: 'تأكد من صيغة الرقم E.164 (مثال: +9665XXXXXXXX).',
  },
  out_of_24h_window: {
    label: 'انتهت نافذة 24 ساعة لخدمة العميل',
    advice: 'استخدم قالب تسويقي معتمد بدل الرسالة الحرة.',
  },
  user_not_opted_in: {
    label: 'العميل لم يوافق على استقبال الرسائل التسويقية',
    advice: 'اطلب موافقة العميل (opt-in) قبل إرسال الحملة.',
  },
  marketing_blocked: {
    label: 'Meta تمنع الرسائل التسويقية لهذا العميل حالياً',
    advice: 'حاول لاحقاً — قد تكون Meta أعادت تقييم العميل.',
  },
  client_payment_blocked: {
    label: 'الرقم مقيّد من واتساب بسبب مشكلة دفع أو قيود Meta',
    advice: 'هذه قيود من Meta على حساب العميل ولا يمكن استعادتها من جانبنا.',
  },
  rate_limit: {
    label: 'تجاوزت الحصة المسموح بها — انتظر دقيقة',
    advice: 'أعد الإرسال بعد بضع دقائق — Meta تطبّق حد رسائل في الدقيقة.',
  },
  spam_rate_limit: {
    label: 'حد إرسال الحملات تجاوز السقف اليومي',
    advice: 'انتظر 24 ساعة أو ارفع تقييم رقمك لدى Meta.',
  },
  template_param_mismatch: {
    label: 'عدد متغيّرات القالب لا يطابق ما اعتمدته Meta',
    advice: 'افتح القالب وتأكد أن كل {{1}}، {{2}}… ممرَّر بقيمة.',
  },
  template_not_found: {
    label: 'القالب غير موجود في Meta',
    advice: 'تحقق من اسم القالب ورمز اللغة في صفحة قوالب واتساب.',
  },
  template_paused: {
    label: 'القالب موقوف في Meta',
    advice: 'أعد تفعيل القالب أو استبدله في Meta Business Manager.',
  },
  template_disabled: {
    label: 'القالب معطّل في Meta',
    advice: 'أنشئ قالباً جديداً أو استعد هذا القالب في Meta.',
  },
  policy_violation: {
    label: 'Meta منعت الرسالة لانتهاك السياسة',
    advice: 'راجع محتوى القالب والفئة وفق إرشادات Meta.',
  },
  account_locked: {
    label: 'حساب WhatsApp Business مقفل',
    advice: 'تواصل مع 360dialog / دعم Meta — إعادة المحاولة من اللوحة لن تفيد.',
  },
  service_unavailable: {
    label: 'خدمة واتساب غير متاحة مؤقتاً',
    advice: 'أعد المحاولة لاحقاً — عادةً مؤقت من جانب Meta.',
  },
  media_error: {
    label: 'خطأ في مرفق الوسائط',
    advice: 'تحقق من رابط وصيغة وسائط الهيدر في القالب.',
  },
  auth_error: {
    label: 'خطأ مصادقة مع Meta / 360dialog',
    advice: 'أعد ربط واتساب أو حدّث رمز الوصول.',
  },
  no_message_id: {
    label: 'لم يُرجع المزود معرّف رسالة',
    advice: 'راجع سجل الإرسال — المزود قبل لكن بدون message ID.',
  },
  exception: {
    label: 'خطأ إرسال غير متوقع',
    advice: 'انسخ الخطأ التقني للدعم.',
  },
  unknown: {
    label: 'خطأ Meta غير مصنّف',
    advice: 'افحص عيّنات Meta الخام أدناه وأرسلها للدعم.',
  },
  retry_exhausted: {
    label: 'بلغت الصف الحد الأقصى للمحاولات',
    advice: 'أصلح سبب الخطأ قبل إعادة المحاولة.',
  },
  retry_storm: {
    label: 'تم رصد retry storm — قاطع الدائرة مفعّل',
    advice: 'راجع لوغات الخادم campaign_send_retry_storm.',
  },
  watchdog_timeout: {
    label: 'صف عالق في sending — انتهت مهلة المراقبة',
    advice: 'سيُعاد إلى queued عند الإرسال التالي.',
  },
  recipient_quality_low: {
    label: 'جودة المستلم منخفضة للإرسال',
    advice: 'Meta علّمت هذا الرقم — فكّر في استبعاده من الحملات.',
  },
  blocked_by_user: {
    label: 'العميل حظر نشاطك التجاري على واتساب',
    advice: 'لا تعِد المحاولة — احترم الحظر.',
  },
  country_restricted: {
    label: 'قيود بلد / منطقة',
    advice: 'الوجهة غير مسموحة لرقم WABA الخاص بك.',
  },
  temporary_failure: {
    label: 'فشل تسليم مؤقت',
    advice: 'قد ينجح عند إعادة المحاولة بعد تأخير.',
  },
  permanent_failure: {
    label: 'فشل تسليم دائم',
    advice: 'لا تعِد المحاولة دون إصلاح السبب الجذري.',
  },
}

const EXCLUDE_REASONS_EN: Record<string, string> = {
  no_phone: 'No phone number',
  phone_not_normalized: 'Phone not normalized',
  unsubscribed: 'Unsubscribed',
  pending_unsubscribe: 'Pending unsubscribe',
  marketing_opt_out: 'Marketing opt-out',
  no_whatsapp_confirmed: 'WhatsApp unavailable (confirmed)',
  unknown: 'Unknown reason',
}

const EXCLUDE_REASONS_AR: Record<string, string> = {
  no_phone: 'بدون رقم جوال',
  phone_not_normalized: 'رقم غير مُطبَّع',
  unsubscribed: 'ألغى الاشتراك',
  pending_unsubscribe: 'قيد الإلغاء',
  marketing_opt_out: 'إلغاء تسويق يدوي',
  no_whatsapp_confirmed: 'لا واتساب (مؤكَّد)',
  unknown: 'سبب غير معروف',
}

const STATUS_BREAKDOWN_EN: Record<string, string> = {
  queued: 'Queued',
  sending: 'Sending',
  sent: 'Sent',
  failed: 'Failed',
  skipped_duplicate: 'Skipped (duplicate)',
  skipped_invalid: 'Invalid data',
  skipped_unsubscribed: 'Unsubscribed',
  skipped_unreachable: 'Unreachable',
  skipped_manual_exclusion: 'Manually excluded',
  unknown_status: 'Unknown status',
}

const STATUS_BREAKDOWN_AR: Record<string, string> = {
  queued: 'في الطابور',
  sending: 'جارٍ الإرسال',
  sent: 'تم الإرسال',
  failed: 'فشل',
  skipped_duplicate: 'تخطّي تكرار',
  skipped_invalid: 'بيانات غير صالحة',
  skipped_unsubscribed: 'ألغى الاشتراك',
  skipped_unreachable: 'غير قابل للوصول',
  skipped_manual_exclusion: 'مستبعد يدوياً',
  unknown_status: 'حالة غير معروفة',
}

const REPORT_EN: CampaignsListLabels['diagnostics']['report'] = {
  sentSummary: '📤 Sent to {sent} of {total} customers{failed}{skipped}',
  sentFailedSuffix: ' — {n} failed',
  sentSkippedSuffix: ' — {n} skipped',
  templateLine: '📨 Template: {tpl}',
  whatsappLine: '📞 WhatsApp: {wa}',
  schedulerLine: '🕐 Scheduler: {state}',
  funnelHeader: '📊 Audience funnel:',
  funnelRaw: '  • Raw audience: {n}',
  funnelReachable: '  • Reachable: {n}',
  funnelMaterialized: '  • Materialized rows: {n}',
  funnelQueued: '  • Queued: {n}',
  funnelFreqCap: '  • Frequency-cap skipped: {n}',
  excludedHeader: '🚫 Excluded {count} customers before send:',
  excludedItem: '  • {label} ({count})',
  deliveryHeader: '📬 Delivery status:',
  deliveryAccepted: '  • Meta accepted: {n}',
  deliveryDelivered: '  • Delivered to customer: {n}',
  deliveryRead: '  • Read by customer: {n}',
  deliveryFailedAfter: '  ⚠️ Failed after Meta accept: {n}',
  deliveryUnknown: '  • Not delivered yet (no Meta receipt): {n}',
  deliveryMissingWamid: '  ⛔ {n} row(s) marked sent without provider_message_id — not truly sent.',
  failureHeader: '🚨 Failure breakdown:',
  hintsPrefix: '💡',
  freqCapHeader: '⏱️ Frequency cap ({days} days): skipped {count} customer(s) due to a recent successful marketing send (Meta-logged only).',
  freqCapLatest: '   Latest successful send in log: {at}',
  freqCapCampaignSuffix: ' (campaign #{id})',
  freqCapRow: '   • {phone}: last success {at} ({campaign})',
  diagnoseFailed: 'Could not run diagnostics: {msg}',
  dispatchConfirm: 'Sending for campaign "{name}" will start in the background.\n\nRecipients already sent will not be sent again.',
  dispatchConfirmFreqCap: '\n\n⚠️ «Ignore frequency cap for this campaign» is enabled — this run will include customers who received a successful marketing message recently (testing only).',
  dispatchStarted: '⏳ Sending started in the background — monitoring progress…',
  dispatchSkipped: 'Dispatch was skipped.',
  dispatchFailed: '❌ Could not start sending: {msg}',
  dispatchRescheduled: '🔁 Rescheduled {n} failed row(s) within attempt limits.',
  dispatchRevived: '🧟 Revived {n} stuck sending row(s) back to queued.',
  dispatchProgress: '⏳ Monitoring progress…',
  dispatchDone: '✅ Sending started in the background. Refresh the page to see results.',
  pollSent: '📤 Sent to {sent} of {total} customers',
  pollQueued: '⏳ Queued: {n}',
  pollFailed: '❌ Failed: {n}',
  pollExcluded: '🚫 Excluded: {n}',
  pollLifecycle: '🚦 Status: {label}',
}

const REPORT_AR: CampaignsListLabels['diagnostics']['report'] = {
  sentSummary: '📤 تم الإرسال إلى {sent} من {total} عملاء{failed}{skipped}',
  sentFailedSuffix: ' — فشل {n}',
  sentSkippedSuffix: ' — تخطّي {n}',
  templateLine: '📨 القالب: {tpl}',
  whatsappLine: '📞 الواتساب: {wa}',
  schedulerLine: '🕐 المُجدول: {state}',
  funnelHeader: '📊 مسار الجمهور:',
  funnelRaw: '  • العدد الأولي: {n}',
  funnelReachable: '  • قابل للوصول: {n}',
  funnelMaterialized: '  • صفوف فعليّة: {n}',
  funnelQueued: '  • في الطابور: {n}',
  funnelFreqCap: '  • تخطّى التكرار: {n}',
  excludedHeader: '🚫 تم استبعاد {count} عميل قبل الإرسال:',
  excludedItem: '  • {label} ({count})',
  deliveryHeader: '📬 حالة التسليم:',
  deliveryAccepted: '  • قبلتها Meta: {n}',
  deliveryDelivered: '  • وصلت للعميل: {n}',
  deliveryRead: '  • قرأها العميل: {n}',
  deliveryFailedAfter: '  ⚠️ فشلت بعد قبول Meta: {n}',
  deliveryUnknown: '  • لم تصل بعد (لم نستلم إشعار من Meta): {n}',
  deliveryMissingWamid: '  ⛔ {n} صف مُعلَّم "تم الإرسال" بدون provider_message_id — لا يجوز اعتبارها مُرسلة فعلاً.',
  failureHeader: '🚨 تفصيل الفشل:',
  hintsPrefix: '💡',
  freqCapHeader: '⏱️ حد التكرار ({days} يوماً): تم تخطّي {count} عميل بسبب إرسال تسويقي ناجح سابق (مسجّل لدى Meta فقط).',
  freqCapLatest: '   أحدث إرسال ناجح في السجل: {at}',
  freqCapCampaignSuffix: ' (حملة #{id})',
  freqCapRow: '   • {phone}: آخر نجاح {at} ({campaign})',
  diagnoseFailed: 'تعذر تشغيل التشخيص: {msg}',
  dispatchConfirm: 'سيتم تشغيل الإرسال للحملة "{name}" الآن في الخلفية.\n\nلن يُعاد إرسال أي مستلم تم إرساله مسبقاً.',
  dispatchConfirmFreqCap: '\n\n⚠️ تم تفعيل «تجاهل حد التكرار لهذه الحملة» — ستُرسل هذه الجولة حتى للعملاء الذين تلقّوا رسالة تسويقية ناجحة مؤخراً (استخدام للاختبار).',
  dispatchStarted: '⏳ بدأ الإرسال في الخلفية — جاري متابعة التقدّم…',
  dispatchSkipped: 'تم تجاوز الإرسال.',
  dispatchFailed: '❌ تعذر تشغيل الإرسال: {msg}',
  dispatchRescheduled: '🔁 تمت إعادة جدولة {n} صف فاشل ضمن حدّ المحاولات.',
  dispatchRevived: '🧟 تم تحرير {n} صف عالق في sending وإعادته إلى queued.',
  dispatchProgress: '⏳ جاري متابعة التقدّم…',
  dispatchDone: '✅ تم تشغيل الإرسال في الخلفية. حدّث الصفحة لرؤية النتيجة.',
  pollSent: '📤 تم الإرسال إلى {sent} من {total} عملاء',
  pollQueued: '⏳ في الطابور: {n}',
  pollFailed: '❌ فشل: {n}',
  pollExcluded: '🚫 مستبعدون: {n}',
  pollLifecycle: '🚦 الحالة: {label}',
}

export const campaignsListEn: CampaignsListLabels = {
  pageSubtitle: 'Smart WhatsApp campaigns built on Nahla segments and Meta-approved templates',
  newCampaign: 'New campaign',
  stats: {
    completed: 'Completed campaigns',
    totalSent: 'Total sent (Meta accepted)',
    totalSentTooltipBoth: 'Meta accepted: {accepted} · Delivered: {delivered}',
    totalSentTooltipAccepted: 'Meta accepted: {accepted}',
    totalSentFailedSuffix: ' / {n} failed',
    openRateDelivered: 'Open rate (of delivered)',
    openRateAccepted: 'Open rate (of accepted)',
    openRateTooltipDelivered: 'Open rate of delivered = {read} / {delivered}',
    openRateTooltipAccepted: 'Open rate of Meta accepted = {read} / {accepted} — no «delivered to customer» receipts yet',
    openRateTooltipNone: 'No accepted messages yet',
    conversionRate: 'Conversion rate',
  },
  failedBanner: '{count} campaign(s) failed to send. Click «Show failure reason» in the row to see details.',
  table: {
    campaign: 'Campaign',
    type: 'Type',
    status: 'Status',
    audience: 'Audience',
    sent: 'Sent',
    openRate: 'Open rate',
    conversion: 'Conversion',
    actions: 'Actions',
  },
  loading: 'Loading campaigns…',
  emptyTitle: 'No campaigns yet.',
  emptyHint: 'Create your first WhatsApp campaign for your customers.',
  waveAdaptive: 'Adaptive wave sending',
  waveBatched: 'Wave sending',
  status: {
    active: 'Active',
    scheduled: 'Scheduled',
    completed: 'Completed',
    paused: 'Paused',
    draft: 'Draft',
    failed: 'Failed',
  },
  lifecycle: {
    draft: 'Draft',
    waiting_scheduler: 'Waiting for scheduler',
    pending_dispatch: 'Waiting to start sending',
    sending: 'Sending',
    sent: 'Sent',
    partial: 'Partially sent',
    partial_minor: 'Sent successfully',
    no_whatsapp_recipients: 'No customers on WhatsApp',
    excluded_before_send: 'All customers excluded before send',
    orphaned_materialized_rows: 'Missing log rows',
    unknown_status: 'Unknown send status',
    completed_empty: 'Completed with no recipients',
    failed: 'Send failed',
    failed_all: 'Failed for everyone',
    unknown: 'Unknown',
  },
  types: {
    broadcast: 'Broadcast',
    abandoned_cart: 'Abandoned cart',
    vip: 'VIP',
    new_arrivals: 'New arrivals',
    win_back: 'Win-back',
  },
  bulk: {
    selected: '{count} campaign(s) selected',
    deleteSelected: 'Delete selected',
    cancel: 'Cancel',
  },
  row: {
    hideDetails: 'Hide details',
    showFailureReason: 'Show failure reason',
    failedCount: '{count} failed',
    pause: 'Pause',
    resume: 'Resume',
    launch: 'Launch',
    diagnose: 'Diagnose',
    diagnosing: 'Running…',
    diagnoseTitle: 'Diagnose send status',
    dispatchNow: 'Send now',
    dispatching: 'Sending…',
    dispatchTitle: 'Start sending manually now',
    ignoreFreqCap: 'Ignore frequency cap for this campaign',
    delete: 'Delete',
    copyTechnicalErrorTitle: 'Copy technical error for support',
    errorCopied: '📋 Technical error copied to clipboard',
    copyFailed: 'Could not copy — copy manually.',
    failureDetailsTitle: 'Send failure details ({failed} of {total})',
  },
  waves: {
    loadFailed: 'Could not load waves',
    title: 'Wave schedule',
    strategyAdaptive: 'Adaptive strategy',
    strategyManual: 'Manual strategy',
    waveCount: '{count} waves',
    perBatch: '/batch',
    failedSuffix: ' · {n} failed',
    waveOf: 'Wave {index} of {total}',
    statuses: {
      pending: 'Pending launch',
      dispatching: 'Sending',
      completed: 'Completed',
      failed: 'Failed',
      paused: 'Paused',
      cancelled: 'Cancelled',
    },
  },
  admin: {
    mediaCheck: 'Media check',
    mediaCheckTitle: 'Check server media settings (OpenAI / storage / ffmpeg) — Admin only',
    directSend: 'Direct test send',
    directSendTitle: 'Send a WhatsApp template directly via provider — bypasses campaigns (Admin only)',
  },
  diagnostics: {
    debugTemplate: {
      loading: 'Inspecting…',
      hide: 'Hide diagnostics',
      show: 'Inspect template & outbound payload',
      loadFailed: 'Could not load template diagnostics',
    },
    providerBlock: {
      title: 'WhatsApp provider or billing issue — contact 360dialog',
      fallbackBody: 'This state is on the provider side (Meta / 360dialog) and cannot be recovered from our dashboard. Auto-resend has been stopped for this campaign.',
      bundleLoading: 'Preparing…',
      bundleCopied: '✓ Copied — paste into a 360dialog ticket',
      bundleError: 'Could not copy — try again',
      copyBundle: 'Copy support report',
      bundleHint: 'The report includes phone_number_id, template name, and a raw Meta response sample.',
    },
    excluded: {
      title: 'Excluded details (first {count}):',
      noPhone: '— no number —',
      notePrefix: 'Note: ',
      noteMiddle: ' is not an exclusion reason — we send via Meta and it confirms. Only confirmed ',
      noteSuffix: ' from a prior failure blocks the send.',
      noteStrongUnknown: 'WhatsApp=unknown',
      noteStrongNo: 'WhatsApp=no',
    },
    fieldFlags: {
      phone: 'Phone',
      normalized: 'Normalized',
      unsubscribed: 'Unsubscribed',
      pendingUnsub: 'Pending unsub',
      marketingOptOut: 'Marketing opt-out',
      whatsapp: 'WhatsApp',
    },
    fieldValues: { yes: 'Yes', no: 'No', unknown: 'Unknown' },
    unknownMeta: {
      title: 'Meta returned an unclassified error — inspect raw responses below ({count})',
      body: 'Each failed sample was classified as «unclassified». Check the «Raw Meta samples» section below — it contains the full Meta response per attempt (request + response + code + subcode + type + message). Send screenshots to support to add the code to the classifier.',
      copyTitle: 'Copy full technical line',
      copy: 'Copy',
    },
    rawMeta: {
      title: 'Raw Meta samples ({count})',
      copyAllTitle: 'Copy all samples as JSON',
      copyAll: 'Copy all',
      intro: 'Each sample includes the full request and response — useful to verify template.name, language.code, and variable counts before opening a support ticket. When the approved template structure differs from the payload, a «template mismatch» badge appears with details.',
      templateMismatch: 'Template mismatch ({count})',
      payloadDiffTitle: 'Mismatch between approved template and sent payload',
      copySampleTitle: 'Copy full technical line for this sample',
      copySample: 'Copy raw Meta error',
      requestPayload: 'Request payload (masked):',
      responsePayload: 'Response payload:',
    },
    delivery: {
      title: 'Campaign delivery (from Meta status webhook)',
      deliveredOf: '{delivered}/{total} delivered',
      missingWamid: '{n} row(s) marked',
      missingWamidSuffix: '"sent" without provider_message_id — cannot count as truly sent. Check send logs.',
      sampleTitle: 'Recent send sample:',
      noWamid: '(no wamid)',
      stages: {
        accepted_by_provider: 'Meta accepted',
        delivered: 'Delivered to customer',
        read: 'Read by customer',
        failed_after_accept: 'Failed after accept',
        unknown_delivery: 'Not delivered yet',
      },
    },
    retryHealth: {
      title: 'Retry health',
      stormHeadline: 'Retry storm detected — attempt limit exceeded {limit}',
      ceilingHeadline: '{count} row(s) reached max attempts',
      zombieHeadline: '{count} row(s) stuck in sending',
      okHeadline: 'Attempt protection active — all rows within safe limits',
      maxAttempts: 'Max attempts',
      rowsAtCeiling: 'Rows at limit',
      zombieRows: 'Stuck rows (sending)',
      maxSendAttempts: 'MAX_SEND_ATTEMPTS',
      stormNote: 'Affected rows were stopped automatically (error_code=retry_storm). Check Railway logs for campaign_send_retry_storm.',
      zombieNote: 'Watchdog will return them to queued on the next dispatch (timeout = {seconds}s).',
    },
    statusBreakdown: {
      title: 'Send row status breakdown ({total})',
      exoticTitle: 'Non-canonical status values detected:',
      rows: STATUS_BREAKDOWN_EN,
    },
    sampleRows: {
      title: 'First {count} send-log rows',
      colId: '#',
      colPhone: 'Customer phone',
      colStatus: 'Status',
      colSkip: 'Skip reason',
      colError: 'Error code',
      colAttempts: 'Attempts',
      colUpdated: 'Last updated',
    },
    dispatchErrors: {
      title: 'Send failure details ({failed} of {total})',
    },
    report: REPORT_EN,
  },
  runtime: {
    schedulerOn: 'Enabled',
    schedulerOff: 'Disabled',
    noConnection: 'Not connected',
    templateMissing: 'Template not found',
    excludeReasons: EXCLUDE_REASONS_EN,
    errorCodes: ERROR_CODES_EN,
  },
}

export const campaignsListAr: CampaignsListLabels = {
  pageSubtitle: 'حملات واتساب ذكية مبنية على شرائح نحلة وقوالب Meta المعتمدة',
  newCampaign: 'حملة جديدة',
  stats: {
    completed: 'حملات مكتملة',
    totalSent: 'إجمالي المُرسَل (قبلتها Meta)',
    totalSentTooltipBoth: 'قبلتها Meta: {accepted} · وصلت: {delivered}',
    totalSentTooltipAccepted: 'قبلتها Meta: {accepted}',
    totalSentFailedSuffix: ' / {n} فشلت',
    openRateDelivered: 'معدل القراءة (من الواصل)',
    openRateAccepted: 'معدل القراءة (من المُقبَل)',
    openRateTooltipDelivered: 'معدل القراءة من الواصل = {read} / {delivered}',
    openRateTooltipAccepted: 'معدل القراءة من المُقبَل (Meta) = {read} / {accepted} — لم تصلنا بعد إيصالات «وصلت للعميل»',
    openRateTooltipNone: 'لا توجد رسائل مُقبَلة بعد',
    conversionRate: 'معدل التحويل',
  },
  failedBanner: 'يوجد {count} حملة فشلت في الإرسال. اضغط على «عرض سبب الفشل» في العمود لمعرفة التفاصيل.',
  table: {
    campaign: 'الحملة',
    type: 'النوع',
    status: 'الحالة',
    audience: 'الجمهور',
    sent: 'الإرسال',
    openRate: 'معدل القراءة',
    conversion: 'التحويل',
    actions: 'إجراءات',
  },
  loading: 'جارٍ تحميل الحملات…',
  emptyTitle: 'لا توجد حملات بعد.',
  emptyHint: 'ابدأ بإنشاء أول حملة واتساب لعملائك.',
  waveAdaptive: 'إرسال تلقائي على دفعات',
  waveBatched: 'إرسال على دفعات',
  status: {
    active: 'نشطة',
    scheduled: 'مجدولة',
    completed: 'مكتملة',
    paused: 'موقوفة',
    draft: 'مسودة',
    failed: 'فشلت',
  },
  lifecycle: {
    draft: 'مسودة',
    waiting_scheduler: 'بانتظار المُجدول',
    pending_dispatch: 'ينتظر بدء الإرسال',
    sending: 'جاري الإرسال',
    sent: 'تم الإرسال',
    partial: 'أُرسل جزئياً',
    partial_minor: 'أُرسل بنجاح',
    no_whatsapp_recipients: 'لا يوجد عملاء على واتساب',
    excluded_before_send: 'استبعد كل العملاء قبل الإرسال',
    orphaned_materialized_rows: 'صفوف مفقودة من السجل',
    unknown_status: 'حالة إرسال غير معروفة',
    completed_empty: 'اكتملت بلا مستلمين',
    failed: 'فشل الإرسال',
    failed_all: 'فشل الإرسال للجميع',
    unknown: 'غير معروفة',
  },
  types: {
    broadcast: 'بث جماعي',
    abandoned_cart: 'عربة متروكة',
    vip: 'VIP',
    new_arrivals: 'وصول جديد',
    win_back: 'استرجاع',
  },
  bulk: {
    selected: 'تم تحديد {count} حملة',
    deleteSelected: 'حذف المحدد',
    cancel: 'إلغاء',
  },
  row: {
    hideDetails: 'إخفاء التفاصيل',
    showFailureReason: 'عرض سبب الفشل',
    failedCount: '{count} فشلت',
    pause: 'إيقاف',
    resume: 'استئناف',
    launch: 'إطلاق',
    diagnose: 'تشخيص',
    diagnosing: 'جاري…',
    diagnoseTitle: 'تشخيص حالة الإرسال',
    dispatchNow: 'إرسال الآن',
    dispatching: 'جاري…',
    dispatchTitle: 'تشغيل الإرسال يدوياً الآن',
    ignoreFreqCap: 'تجاهل حد التكرار لهذه الحملة',
    delete: 'حذف',
    copyTechnicalErrorTitle: 'نسخ الخطأ التقني للدعم',
    errorCopied: '📋 تم نسخ الخطأ التقني إلى الحافظة',
    copyFailed: 'تعذر النسخ — انسخ يدوياً.',
    failureDetailsTitle: 'تفاصيل فشل الإرسال ({failed} من {total})',
  },
  waves: {
    loadFailed: 'تعذر تحميل الدفعات',
    title: 'جدول الدفعات',
    strategyAdaptive: 'استراتيجية تلقائية',
    strategyManual: 'استراتيجية يدوية',
    waveCount: '{count} دفعة',
    perBatch: '/دفعة',
    failedSuffix: ' · فشل {n}',
    waveOf: 'دفعة {index} من {total}',
    statuses: {
      pending: 'بانتظار الإطلاق',
      dispatching: 'جارٍ الإرسال',
      completed: 'مكتملة',
      failed: 'فشلت',
      paused: 'موقوفة',
      cancelled: 'ملغية',
    },
  },
  admin: {
    mediaCheck: 'فحص الوسائط',
    mediaCheckTitle: 'فحص إعدادات الوسائط على الخادم (OpenAI / تخزين / ffmpeg) — Admin only',
    directSend: 'إرسال اختبار مباشر',
    directSendTitle: 'إرسال قالب واتساب مباشرة عبر المزود — يتجاوز نظام الحملات (Admin only)',
  },
  diagnostics: {
    debugTemplate: {
      loading: 'جارٍ الفحص…',
      hide: 'إخفاء التشخيص',
      show: 'فحص القالب والحمولة المُرسلة',
      loadFailed: 'فشل جلب بيانات التشخيص',
    },
    providerBlock: {
      title: 'مشكلة من مزود واتساب أو الدفع — تواصل مع 360dialog',
      fallbackBody: 'هذه الحالة من جانب المزود (Meta / 360dialog) ولا يمكن استعادتها من جانبنا. تم إيقاف إعادة الإرسال التلقائي لهذه الحملة.',
      bundleLoading: 'جاري التحضير…',
      bundleCopied: '✓ تم النسخ — الصق في تذكرة 360dialog',
      bundleError: 'تعذر النسخ — حاول مجدداً',
      copyBundle: 'نسخ تقرير الدعم',
      bundleHint: 'التقرير يتضمن phone_number_id واسم القالب وعيّنة من ردّ Meta الخام.',
    },
    excluded: {
      title: 'تفاصيل المستبعدين (أول {count}):',
      noPhone: '— بدون رقم —',
      notePrefix: 'ملاحظة: ',
      noteMiddle: ' ليست سبب استبعاد — نُرسل عبر Meta وهو من يؤكّد. فقط ',
      noteSuffix: ' المؤكَّد من فشل سابق هو الحاجز.',
      noteStrongUnknown: 'واتساب=غير معروف',
      noteStrongNo: 'واتساب=لا',
    },
    fieldFlags: {
      phone: 'رقم',
      normalized: 'مُطبَّع',
      unsubscribed: 'ألغى الاشتراك',
      pendingUnsub: 'قيد الإلغاء',
      marketingOptOut: 'إلغاء تسويق',
      whatsapp: 'واتساب',
    },
    fieldValues: { yes: 'نعم', no: 'لا', unknown: 'غير معروف' },
    unknownMeta: {
      title: 'Meta أعادت خطأ غير مصنّف بعد — افحص الرد الخام أدناه ({count})',
      body: 'كل عينة فشل صنّفها النظام كـ «خطأ غير مصنّف». افحص قسم «العيّنات الخام من Meta» في الأسفل — يحوي ردّ Meta الكامل لكل محاولة (request + response + code + subcode + type + message). أرسل لقطة منها للدعم لإضافة الكود إلى المُصنِّف.',
      copyTitle: 'نسخ السطر التقني الكامل',
      copy: 'نسخ',
    },
    rawMeta: {
      title: 'العيّنات الخام من Meta ({count})',
      copyAllTitle: 'نسخ كل العيّنات كـ JSON',
      copyAll: 'نسخ الكل',
      intro: 'كل عينة تحوي الطلب والاستجابة الكاملين — مفيد للتأكد من template.name و language.code وعدد المتغيّرات قبل تقديم تذكرة للدعم. عند اختلاف هيكل القالب عن البايلود تظهر شارة «اختلاف القالب» مع التفاصيل.',
      templateMismatch: 'اختلاف القالب ({count})',
      payloadDiffTitle: 'اختلاف بين القالب المعتمد والبايلود المُرسَل',
      copySampleTitle: 'نسخ السطر التقني الكامل لهذه العينة',
      copySample: 'نسخ الخطأ الخام',
      requestPayload: 'حِمولة الطلب (مُقنّعة):',
      responsePayload: 'حِمولة الاستجابة:',
    },
    delivery: {
      title: 'توصيل الحملة (من Meta status webhook)',
      deliveredOf: '{delivered}/{total} وصلت',
      missingWamid: '{n} صف مُعلَّم',
      missingWamidSuffix: '"تم الإرسال" بدون provider_message_id — لا يجوز اعتبارها مُرسلة فعلاً. راجع لوج الإرسال.',
      sampleTitle: 'عينة آخر إرسالات:',
      noWamid: '(بدون wamid)',
      stages: {
        accepted_by_provider: 'قبلتها Meta',
        delivered: 'وصلت للعميل',
        read: 'قرأها العميل',
        failed_after_accept: 'فشلت بعد القبول',
        unknown_delivery: 'لم تصل بعد',
      },
    },
    retryHealth: {
      title: 'صحة المحاولات (Retry Health)',
      stormHeadline: 'تم رصد retry storm — حدّ المحاولات تجاوز {limit}',
      ceilingHeadline: '{count} صف وصل إلى الحد الأقصى للمحاولات',
      zombieHeadline: '{count} صف عالق في sending',
      okHeadline: 'حماية المحاولات نشطة وكل الصفوف ضمن الحدود الآمنة',
      maxAttempts: 'أقصى محاولات',
      rowsAtCeiling: 'صفوف بلغت الحد',
      zombieRows: 'صفوف عالقة (sending)',
      maxSendAttempts: 'MAX_SEND_ATTEMPTS',
      stormNote: 'تم إيقاف الصفوف المتأثرة تلقائياً (error_code=retry_storm). راجع لوغات Railway للبحث عن campaign_send_retry_storm.',
      zombieNote: 'ستعيدها watchdog إلى queued تلقائياً عند إطلاق الإرسال التالي (timeout = {seconds}s).',
    },
    statusBreakdown: {
      title: 'توزيع حالات صفوف الإرسال ({total})',
      exoticTitle: 'قيم حالة غير قانونية مرصودة:',
      rows: STATUS_BREAKDOWN_AR,
    },
    sampleRows: {
      title: 'أول {count} صفوف من سجل الإرسال',
      colId: '#',
      colPhone: 'رقم العميل',
      colStatus: 'الحالة',
      colSkip: 'سبب التخطّي',
      colError: 'رمز الخطأ',
      colAttempts: 'محاولات',
      colUpdated: 'آخر تعديل',
    },
    dispatchErrors: {
      title: 'تفاصيل فشل الإرسال ({failed} من {total})',
    },
    report: REPORT_AR,
  },
  runtime: {
    schedulerOn: 'مفعّل',
    schedulerOff: 'معطّل',
    noConnection: 'لا اتصال',
    templateMissing: 'القالب غير موجود',
    excludeReasons: EXCLUDE_REASONS_AR,
    errorCodes: ERROR_CODES_AR,
  },
}
