/**
 * Customers page UI labels — static chrome only.
 */
export interface CustomersPageLabels {
  title: string
  subtitle: string
  currency: string
  yes: string
  no: string
  actions: {
    cleanNames: string
    cleanNamesTitle: string
    importCustomers: string
    addCustomer: string
    refresh: string
    deleteSelected: string
    deleteAll: string
  }
  cards: {
    total: string
    vip: string
    atRisk: string
    active: string
  }
  segments: {
    title: string
    subtitle: string
    segmentInfoAria: string
    closeDefinition: string
    currentCount: string
    crmStatuses: string
    rfmBuckets: string
    showingFilter: string
    unsubscribedNoticeTitle: string
    unsubscribedNoticeBody: string
    unsubscribedHint: string
  }
  searchPlaceholder: string
  filters: {
    manualSegmentTitle: string
    showAll: string
    noManualTag: string
    manualOnly: string
    marketingTitle: string
    marketingAll: string
    marketingIn: string
    marketingOut: string
  }
  table: {
    name: string
    phone: string
    email: string
    status: string
    smartSegment: string
    orders: string
    spend: string
    lastOrder: string
    source: string
    selectAll: string
    deselectAll: string
    deleteCustomer: string
    noName: string
    noNameTitle: string
    editedBadge: string
    unsubscribed: string
    pendingUnsub: string
    quickEditName: string
    editNameAria: string
    manualEditHint: string
    namePlaceholder: string
    saveTitle: string
    cancelTitle: string
    empty: string
    pageOf: string
    prev: string
    next: string
  }
  inlineEdit: {
    nameTooLong: string
    nameCleared: string
    nameUpdated: string
    updateFailed: string
  }
  addModal: {
    title: string
    nameLabel: string
    phoneLabel: string
    emailLabel: string
    nameRequired: string
    addFailed: string
    cancel: string
    submit: string
  }
  deleteModal: {
    deleteAllTitle: string
    deleteSelectedTitle: string
    irreversible: string
    deleteAllBody: string
    deleteSelectedBody: string
    bulletNoRestore: string
    bulletDataGone: string
    bulletCampaigns: string
    confirmPrompt: string
    confirmPlaceholder: string
    confirmWord: string
    cancel: string
    deleting: string
    confirmDelete: string
    singleConfirm: string
    deleteFailed: string
  }
  draftDiscardConfirm: string
  drawer: {
    title: string
    smartClassification: string
    unsubscribedTitle: string
    unsubscribedBody: string
    unsubscribedNote: string
    pendingUnsubTitle: string
    pendingUnsubBody: string
    pendingUnsubNote: string
    orders: string
    spend: string
    avgOrder: string
    churnRisk: string
    rfmSegment: string
    rfmScore: string
    firstOrder: string
    lastOrder: string
    firstSeen: string
    lastRecalc: string
    returning: string
    deleteCustomer: string
  }
  manualSegments: {
    title: string
    subtitle: string
    manuallyAdded: string
    manuallyExcluded: string
    inSegmentTitle: string
    outSegmentTitle: string
    overrideHint: string
    addToSegment: string
    excludeFromSegment: string
    resetToAuto: string
    quickAddPlaceholder: string
    add: string
    optOutTitle: string
    optOutBody: string
    testListTitle: string
    testListBody: string
    updateSegmentFailed: string
    updatePrefFailed: string
    updateTestFailed: string
  }
  nameCleanup: {
    title: string
    subtitle: string
    scanning: string
    appliedSummary: string
    skippedSuffix: string
    totalCustomers: string
    scanned: string
    needsCleanup: string
    ofTotal: string
    highConfidence: string
    needsReview: string
    tooManyResults: string
    savingDraft: string
    saved: string
    saveFailed: string
    draftSaved: string
    draftSkipped: string
    rescanTitle: string
    rescan: string
    saveContinue: string
    discardDraftTitle: string
    discardDraft: string
    byReason: string
    categories: Record<
      'all' | 'source_label_name' | 'location_label_name' | 'placeholder_name' | 'suspicious_suffix' | 'generic_bad_name' | 'other',
      string
    >
    filters: Record<'all' | 'pending' | 'edited' | 'high' | 'low' | 'opted_out', string>
    emptyAfterApply: string
    emptyClean: string
    deselectVisible: string
    selectAllVisible: string
    selectedOf: string
    ofTotalItems: string
    clickRowHint: string
    refreshPreview: string
    refreshPreviewTitle: string
    noRowsInFilter: string
    deselect: string
    selectCustomer: string
    categoryBadges: Record<
      'source_label_name' | 'location_label_name' | 'placeholder_name' | 'suspicious_suffix' | 'generic_bad_name' | 'other',
      string
    >
    excludedFromCampaigns: string
    modified: string
    skipped: string
    highConf: string
    review: string
    noWords: string
    restoreWord: string
    removeWord: string
    resultLabel: string
    willClearName: string
    resetSuggestion: string
    skipRow: string
    skip: string
    clearEntireName: string
    reEnableMarketing: string
    excludeFromCampaigns: string
    reEnableMarketingShort: string
    excludeShort: string
    helpPrimary: string
    helpSecondary: string
    close: string
    applyHighOnly: string
    applyHighTitle: string
    applySelected: string
    applySelectedCount: string
    applySelectedHint: string
    applySelectedTitle: string
    loadPreviewFailed: string
    saveDraftFailed: string
    actionFailed: string
    discardFailed: string
    applyFailed: string
    manualEditReason: string
  }
  errors: {
    loadFailed: string
  }
  loading: string
}

export type CustomersPageLabelsType = CustomersPageLabels

export const customersPageEn: CustomersPageLabels = {
  title: 'Customers',
  subtitle: 'Manage and segment your customers',
  currency: 'SAR',
  yes: 'Yes',
  no: 'No',
  actions: {
    cleanNames: 'Clean customer names',
    cleanNamesTitle: "Remove generic words ('customer', phone numbers…) from the name field — current store only",
    importCustomers: 'Import customers',
    addCustomer: 'Add customer',
    refresh: 'Refresh',
    deleteSelected: 'Delete selected',
    deleteAll: 'Delete all',
  },
  cards: {
    total: 'Total customers',
    vip: 'VIP customers',
    atRisk: 'At risk of churn',
    active: 'Active customers',
  },
  segments: {
    title: 'Customer segments',
    subtitle: 'Includes both smart and manual classification',
    segmentInfoAria: 'Segment definition for {label}',
    closeDefinition: 'Close definition',
    currentCount: 'Current count',
    crmStatuses: 'CRM statuses',
    rfmBuckets: 'RFM',
    showingFilter: 'Showing {count} customers in «{label}»',
    unsubscribedNoticeTitle: 'These customers unsubscribed from communications',
    unsubscribedNoticeBody: 'Automatically excluded from all campaigns, autopilot, and AI.',
    unsubscribedHint: 'To win them back, reach out personally. They return automatically when they send any message.',
  },
  searchPlaceholder: 'Search by name or phone…',
  filters: {
    manualSegmentTitle: 'Filter by manually tagged customers only',
    showAll: 'Show all',
    noManualTag: '— No manual tag —',
    manualOnly: 'Manual only: {label}',
    marketingTitle: 'Filter by manual marketing exclusion',
    marketingAll: 'All customers',
    marketingIn: 'Eligible for campaigns',
    marketingOut: 'Excluded from campaigns',
  },
  table: {
    name: 'Name',
    phone: 'Phone',
    email: 'Email',
    status: 'Status',
    smartSegment: 'Smart segment',
    orders: 'Orders',
    spend: 'Spend',
    lastOrder: 'Last order',
    source: 'Source',
    selectAll: 'Select all',
    deselectAll: 'Deselect all',
    deleteCustomer: 'Delete customer',
    noName: 'No name',
    noNameTitle: 'This customer has no name — use the pencil to add one',
    editedBadge: 'Edited',
    unsubscribed: 'Unsubscribed',
    pendingUnsub: 'Pending unsubscribe',
    quickEditName: 'Quick name edit',
    editNameAria: 'Edit customer name',
    manualEditHint: 'Manually edited — auto cleanup will not change this',
    namePlaceholder: 'Customer name',
    saveTitle: 'Save (Enter)',
    cancelTitle: 'Cancel (Esc)',
    empty: 'No customers',
    pageOf: 'Page {page} of {pages} ({total} customers)',
    prev: 'Previous',
    next: 'Next',
  },
  inlineEdit: {
    nameTooLong: 'Name is too long (max {max} characters)',
    nameCleared: 'Customer name cleared (shows as "No name")',
    nameUpdated: 'Customer name updated',
    updateFailed: 'Could not update customer name. Try again.',
  },
  addModal: {
    title: 'Add new customer',
    nameLabel: 'Name *',
    phoneLabel: 'WhatsApp number *',
    emailLabel: 'Email',
    nameRequired: 'Name and WhatsApp number are required',
    addFailed: 'Error adding customer',
    cancel: 'Cancel',
    submit: 'Add',
  },
  deleteModal: {
    deleteAllTitle: 'Delete all customers',
    deleteSelectedTitle: 'Delete {count} customers',
    irreversible: 'This action cannot be undone',
    deleteAllBody: 'All {total} customers will be permanently deleted.',
    deleteSelectedBody: '{count} selected customers will be permanently deleted.',
    bulletNoRestore: 'You will not be able to restore this data',
    bulletDataGone: 'All customer information will be deleted',
    bulletCampaigns: 'Linked campaigns and automations may be affected',
    confirmPrompt: 'To confirm, type',
    confirmPlaceholder: 'Type: {word}',
    confirmWord: 'DELETE',
    cancel: 'Cancel',
    deleting: 'Deleting…',
    confirmDelete: 'Delete permanently',
    singleConfirm: 'Are you sure you want to delete this customer?',
    deleteFailed: 'Error deleting customers',
  },
  draftDiscardConfirm: 'All saved draft edits will be discarded. Are you sure?',
  drawer: {
    title: 'Customer details',
    smartClassification: 'Smart classification',
    unsubscribedTitle: 'Unsubscribed',
    unsubscribedBody: 'Excluded from campaigns, autopilot, and AI',
    unsubscribedNote: 'Returns automatically when they send any message',
    pendingUnsubTitle: 'Pending unsubscribe confirmation',
    pendingUnsubBody: 'Unsubscribe requested — confirmation message sent with two buttons',
    pendingUnsubNote: 'Automation and AI pause until they tap confirm or cancel',
    orders: 'Orders',
    spend: 'Spend ({currency})',
    avgOrder: 'Avg. order ({currency})',
    churnRisk: 'Churn risk',
    rfmSegment: 'RFM segment',
    rfmScore: 'RFM score',
    firstOrder: 'First order',
    lastOrder: 'Last order',
    firstSeen: 'First seen',
    lastRecalc: 'Last recalculated',
    returning: 'Returning customer',
    deleteCustomer: 'Delete customer',
  },
  manualSegments: {
    title: 'This customer\'s segments',
    subtitle: 'Smart classification with manual overrides',
    manuallyAdded: 'Manually added',
    manuallyExcluded: 'Manually excluded',
    inSegmentTitle: 'Customer is currently in this segment.',
    outSegmentTitle: 'Customer is currently outside this segment.',
    overrideHint: 'Manually overridden — tap ↻ to return to smart classification',
    addToSegment: 'Add to this segment',
    excludeFromSegment: 'Exclude from this segment',
    resetToAuto: 'Return to smart classification',
    quickAddPlaceholder: 'Quick add to segment…',
    add: 'Add',
    optOutTitle: 'Exclude from marketing campaigns',
    optOutBody: 'Will not enter any manual marketing campaign. Does not affect order messages, service automations, or the 24-hour window.',
    testListTitle: 'Add to campaign test list',
    testListBody: 'Small internal group to test campaigns before full launch. Does not create a visible merchant tag.',
    updateSegmentFailed: 'Could not update classification',
    updatePrefFailed: 'Could not update preference',
    updateTestFailed: 'Could not update test list',
  },
  nameCleanup: {
    title: 'Clean customer names',
    subtitle: 'Preview suggested names before applying — current store customers only',
    scanning: 'Scanning customer names…',
    appliedSummary: 'Applied {applied} change(s)',
    skippedSuffix: ' — skipped {skipped} (already cleaned or missing)',
    totalCustomers: 'Total customers',
    scanned: 'Scanned {count} customers',
    needsCleanup: 'Names need cleanup',
    ofTotal: 'of {total}',
    highConfidence: 'High confidence',
    needsReview: 'Needs review',
    tooManyResults: 'Too many results — showing first {max} names only (total {match}). Apply this batch then reopen the tool to continue, or use "Apply high-confidence only" to process all high-confidence names at once (all customers, not just visible).',
    savingDraft: 'Saving draft…',
    saved: 'Saved',
    saveFailed: 'Save failed — will retry',
    draftSaved: 'Draft saved: {edited}',
    draftSkipped: '+ {skipped} skipped',
    rescanTitle: 'Rescan entire customer database',
    rescan: 'Rescan',
    saveContinue: 'Save & continue later',
    discardDraftTitle: 'Delete all saved draft edits',
    discardDraft: 'Discard draft',
    byReason: 'By reason:',
    categories: {
      all: 'All',
      source_label_name: 'Marketing source',
      location_label_name: 'City / location',
      placeholder_name: 'No name / generic',
      suspicious_suffix: 'Extra word',
      generic_bad_name: 'Not a real name',
      other: 'Other',
    },
    filters: {
      all: 'All',
      pending: 'Not cleaned',
      edited: 'Manually edited',
      high: 'Auto clean (high confidence)',
      low: 'Needs review',
      opted_out: 'Excluded from campaigns',
    },
    emptyAfterApply: 'No other names need cleanup. Close the window or tap "Rescan" to scan again.',
    emptyClean: 'All customer names in this store look clean — nothing to change.',
    deselectVisible: 'Deselect visible',
    selectAllVisible: 'Select all on this page{count}',
    selectedOf: '{selected} / {visible} selected',
    ofTotalItems: '(of {total})',
    clickRowHint: '• Click a row to select the customer',
    refreshPreview: 'Refresh preview ({count})',
    refreshPreviewTitle: 'Some rows no longer match this filter after edits. Refresh preview without losing saved edits.',
    noRowsInFilter: 'No names in this filter.',
    deselect: 'Deselect',
    selectCustomer: 'Select customer',
    categoryBadges: {
      source_label_name: 'Source',
      location_label_name: 'City',
      placeholder_name: 'No name',
      suspicious_suffix: 'Extra word',
      generic_bad_name: 'Not real',
      other: 'Cleanup',
    },
    excludedFromCampaigns: 'Excluded from campaigns',
    modified: 'Modified',
    skipped: 'Skipped',
    highConf: 'High confidence',
    review: 'Review',
    noWords: 'No words',
    restoreWord: 'Restore this word',
    removeWord: 'Remove this word',
    resultLabel: 'Result:',
    willClearName: 'Name will be cleared',
    resetSuggestion: 'Reset to suggestion',
    skipRow: 'Skip this row — hidden in future review sessions',
    skip: 'Skip',
    clearEntireName: 'Clear entire name',
    reEnableMarketing: 'Re-enable marketing for this customer',
    excludeFromCampaigns: 'Exclude from campaigns (inbound messages unaffected)',
    reEnableMarketingShort: 'Re-enable marketing',
    excludeShort: 'Exclude from campaigns',
    helpPrimary: 'Click any word to remove it (shown struck through) or restore it — or use "Clear entire name" to wipe the name entirely and let campaigns use the default phrase.',
    helpSecondary: 'After applying, campaigns use the saved name directly — if empty, the default phrase is used. All changes are logged in an internal audit trail.',
    close: 'Close',
    applyHighOnly: 'Apply high-confidence only',
    applyHighTitle: 'Apply all high-confidence suggestions without individual review',
    applySelected: 'Apply selected',
    applySelectedCount: 'Apply selected ({count})',
    applySelectedHint: 'Select rows to apply cleanup first',
    applySelectedTitle: 'Cleanup will be applied to {count} customers',
    loadPreviewFailed: 'Could not load preview',
    saveDraftFailed: 'Could not save draft',
    actionFailed: 'Could not complete action',
    discardFailed: 'Could not discard draft',
    applyFailed: 'Could not apply cleanup',
    manualEditReason: 'Manual edit — {reason}',
  },
  errors: {
    loadFailed: 'Could not load customers',
  },
  loading: 'Loading…',
}

export const customersPageAr: CustomersPageLabels = {
  title: 'العملاء',
  subtitle: 'إدارة وتصنيف العملاء',
  currency: 'ر.س',
  yes: 'نعم',
  no: 'لا',
  actions: {
    cleanNames: 'تنظيف أسماء العملاء',
    cleanNamesTitle: "إزالة الكلمات التجارية ('عميل'، 'customer'...) وأرقام الجوال من حقل الاسم — يعمل على المتجر الحالي فقط",
    importCustomers: 'استيراد العملاء',
    addCustomer: 'إضافة عميل',
    refresh: 'تحديث',
    deleteSelected: 'حذف المحدد',
    deleteAll: 'حذف الكل',
  },
  cards: {
    total: 'إجمالي العملاء',
    vip: 'عملاء VIP',
    atRisk: 'في خطر المغادرة',
    active: 'عملاء نشطون',
  },
  segments: {
    title: 'شرائح العملاء',
    subtitle: 'تشمل التصنيف الذكي والتصنيف اليدوي معاً',
    segmentInfoAria: 'تعريف شريحة {label}',
    closeDefinition: 'إغلاق التعريف',
    currentCount: 'عدد الحالي',
    crmStatuses: 'حالات CRM',
    rfmBuckets: 'RFM',
    showingFilter: 'عرض {count} عميل ضمن «{label}»',
    unsubscribedNoticeTitle: 'هؤلاء العملاء ألغوا الاشتراك في التواصل',
    unsubscribedNoticeBody: 'مستثنون تلقائياً من جميع الحملات والطيار الآلي والذكاء الاصطناعي.',
    unsubscribedHint: 'لاستعادتهم، تواصل معهم شخصياً. يعودون تلقائياً عند إرسال أي رسالة.',
  },
  searchPlaceholder: 'بحث بالاسم أو رقم الهاتف...',
  filters: {
    manualSegmentTitle: 'فلترة حسب العملاء المُصنَّفين يدوياً فقط',
    showAll: 'عرض الكل',
    noManualTag: '— بدون أي تصنيف يدوي —',
    manualOnly: 'يدوي فقط: {label}',
    marketingTitle: 'فلترة حسب الاستبعاد التسويقي اليدوي',
    marketingAll: 'كل العملاء',
    marketingIn: 'المؤهلون للحملات',
    marketingOut: 'مستبعدون من الحملات',
  },
  table: {
    name: 'الاسم',
    phone: 'الهاتف',
    email: 'البريد',
    status: 'الحالة',
    smartSegment: 'القطاع الذكي',
    orders: 'الطلبات',
    spend: 'الإنفاق',
    lastOrder: 'آخر طلب',
    source: 'المصدر',
    selectAll: 'تحديد الكل',
    deselectAll: 'إلغاء تحديد الكل',
    deleteCustomer: 'حذف العميل',
    noName: 'بدون اسم',
    noNameTitle: 'هذا العميل بلا اسم — استخدم القلم لإضافة اسم',
    editedBadge: 'محرّر',
    unsubscribed: 'ألغى الاشتراك',
    pendingUnsub: 'بانتظار تأكيد الإلغاء',
    quickEditName: 'تعديل سريع للاسم',
    editNameAria: 'تعديل اسم العميل',
    manualEditHint: 'الاسم محرّر يدوياً — لن يُعدّله التنظيف التلقائي',
    namePlaceholder: 'اسم العميل',
    saveTitle: 'حفظ (Enter)',
    cancelTitle: 'إلغاء (Esc)',
    empty: 'لا يوجد عملاء',
    pageOf: 'صفحة {page} من {pages} ({total} عميل)',
    prev: 'السابق',
    next: 'التالي',
  },
  inlineEdit: {
    nameTooLong: 'الاسم طويل جداً (الحد {max} حرفاً)',
    nameCleared: 'تم حذف اسم العميل (سيظهر "بدون اسم")',
    nameUpdated: 'تم تحديث اسم العميل',
    updateFailed: 'تعذّر تحديث اسم العميل. حاول مرة أخرى.',
  },
  addModal: {
    title: 'إضافة عميل جديد',
    nameLabel: 'الاسم *',
    phoneLabel: 'رقم الواتساب *',
    emailLabel: 'البريد الإلكتروني',
    nameRequired: 'الاسم ورقم الواتساب مطلوبان',
    addFailed: 'حدث خطأ أثناء إضافة العميل',
    cancel: 'إلغاء',
    submit: 'إضافة',
  },
  deleteModal: {
    deleteAllTitle: 'حذف جميع العملاء',
    deleteSelectedTitle: 'حذف {count} عميل',
    irreversible: 'هذا الإجراء لا يمكن التراجع عنه',
    deleteAllBody: 'سيتم حذف جميع العملاء ({total} عميل) بشكل دائم.',
    deleteSelectedBody: 'سيتم حذف {count} عميل محدد بشكل دائم.',
    bulletNoRestore: 'لن تتمكن من استعادة هذه البيانات',
    bulletDataGone: 'سيتم حذف جميع معلومات العملاء وبياناتهم',
    bulletCampaigns: 'قد تتأثر الحملات والأتمتة المرتبطة بهم',
    confirmPrompt: 'للتأكيد، اكتب كلمة',
    confirmPlaceholder: 'اكتب: {word}',
    confirmWord: 'احذف',
    cancel: 'إلغاء',
    deleting: 'جارٍ الحذف...',
    confirmDelete: 'حذف نهائي',
    singleConfirm: 'هل أنت متأكد من حذف هذا العميل؟',
    deleteFailed: 'حدث خطأ أثناء الحذف',
  },
  draftDiscardConfirm: 'سيتم تجاهل جميع التعديلات المحفوظة في المسودة. هل أنت متأكد؟',
  drawer: {
    title: 'تفاصيل العميل',
    smartClassification: 'تصنيف ذكي',
    unsubscribedTitle: 'ألغى الاشتراك',
    unsubscribedBody: 'مستثنى من الحملات والطيار الآلي والذكاء',
    unsubscribedNote: 'يعود تلقائياً عند إرساله أي رسالة',
    pendingUnsubTitle: 'بانتظار تأكيد إلغاء الاشتراك',
    pendingUnsubBody: 'طلب الإلغاء — أُرسلت له رسالة تأكيد بزرين',
    pendingUnsubNote: 'يتم إيقاف الأتمتة والذكاء حتى يضغط "نعم متأكد" أو "تراجع"',
    orders: 'الطلبات',
    spend: 'الإنفاق (ر.س)',
    avgOrder: 'متوسط الطلب (ر.س)',
    churnRisk: 'خطر المغادرة',
    rfmSegment: 'قطاع RFM',
    rfmScore: 'درجة RFM',
    firstOrder: 'أول طلب',
    lastOrder: 'آخر طلب',
    firstSeen: 'أول ظهور',
    lastRecalc: 'آخر إعادة حساب',
    returning: 'عميل متكرر',
    deleteCustomer: 'حذف العميل',
  },
  manualSegments: {
    title: 'شرائح هذا العميل',
    subtitle: 'التصنيف الذكي مع إمكانية التعديل',
    manuallyAdded: 'مضاف يدويًا',
    manuallyExcluded: 'مستبعد يدويًا',
    inSegmentTitle: 'العميل ضمن هذه الشريحة حالياً.',
    outSegmentTitle: 'العميل خارج هذه الشريحة حالياً.',
    overrideHint: 'تم تعديل التصنيف يدوياً — اضغط ↻ للعودة للتصنيف التلقائي.',
    addToSegment: 'إضافة لهذا التصنيف',
    excludeFromSegment: 'استبعاد من هذا التصنيف',
    resetToAuto: 'العودة للتصنيف التلقائي',
    quickAddPlaceholder: 'إضافة سريعة لشريحة…',
    add: 'إضافة',
    optOutTitle: 'استبعاد من الحملات التسويقية',
    optOutBody: 'لن يدخل في أي حملة تسويقية يدوية. لا يؤثر على رسائل الطلبات أو الأتمتات الخدمية أو نافذة 24 ساعة.',
    testListTitle: 'إضافة إلى قائمة اختبار الحملات',
    testListBody: 'مجموعة داخلية صغيرة لاختبار الحملة قبل الإطلاق الكامل. لا يُنشئ تصنيفاً ظاهراً للتاجر.',
    updateSegmentFailed: 'تعذر تحديث التصنيف',
    updatePrefFailed: 'تعذر تحديث التفضيل',
    updateTestFailed: 'تعذر تحديث قائمة الاختبار',
  },
  nameCleanup: {
    title: 'تنظيف أسماء العملاء',
    subtitle: 'معاينة الأسماء المُقترحة قبل التطبيق — يعمل فقط على عملاء المتجر الحالي',
    scanning: 'جاري فحص أسماء العملاء...',
    appliedSummary: 'تم تطبيق {applied} تغيير',
    skippedSuffix: ' — تخطّينا {skipped} (تم تنظيفها مسبقاً أو غير موجودة)',
    totalCustomers: 'إجمالي العملاء',
    scanned: 'فُحص {count} عميل',
    needsCleanup: 'أسماء تحتاج تنظيف',
    ofTotal: 'من أصل {total}',
    highConfidence: 'ثقة عالية',
    needsReview: 'تحتاج مراجعة',
    tooManyResults: 'النتائج الكثيرة جداً — نعرض أول {max} اسم فقط (المجموع {match}). طبّق هذه الدفعة ثم أعد فتح الأداة لإكمال البقية، أو استخدم "تطبيق ذوي الثقة العالية فقط" لتنفيذ كل الأسماء عالية الثقة دفعة واحدة (يعمل على جميع العملاء وليس على المعروضين فقط).',
    savingDraft: 'جاري حفظ المسودة...',
    saved: 'تم الحفظ',
    saveFailed: 'فشل الحفظ — سنحاول مجدداً',
    draftSaved: 'مسودة محفوظة: {edited}',
    draftSkipped: '+ {skipped} متخطى',
    rescanTitle: 'إعادة فحص قاعدة العملاء كاملة من جديد',
    rescan: 'إعادة الفحص',
    saveContinue: 'حفظ ومتابعة لاحقاً',
    discardDraftTitle: 'حذف جميع التعديلات المحفوظة في المسودة',
    discardDraft: 'تجاهل المسودة',
    byReason: 'حسب السبب:',
    categories: {
      all: 'الكل',
      source_label_name: 'مصدر تسويقي',
      location_label_name: 'مدينة / موقع',
      placeholder_name: 'بدون اسم / عام',
      suspicious_suffix: 'كلمة زائدة',
      generic_bad_name: 'اسم غير حقيقي',
      other: 'أخرى',
    },
    filters: {
      all: 'الكل',
      pending: 'غير منظف',
      edited: 'تم تعديله يدوياً',
      high: 'تنظيف تلقائي (ثقة عالية)',
      low: 'يحتاج مراجعة',
      opted_out: 'مستبعد من الحملات',
    },
    emptyAfterApply: 'لا توجد أسماء أخرى تحتاج تنظيفاً. يمكنك إغلاق النافذة أو الضغط على "إعادة الفحص" لإعادة المسح.',
    emptyClean: 'جميع أسماء العملاء في هذا المتجر تبدو نظيفة — لا يوجد ما يحتاج إلى تغيير.',
    deselectVisible: 'إلغاء تحديد المعروض',
    selectAllVisible: 'تحديد الكل في الصفحة الحالية{count}',
    selectedOf: '{selected} / {visible} محدد',
    ofTotalItems: '(من {total})',
    clickRowHint: '• انقر على الصف لتحديد العميل',
    refreshPreview: 'تحديث المعاينة ({count})',
    refreshPreviewTitle: 'بعض الصفوف خرجت من شروط هذا الفلتر بعد التعديل. اضغط لتحديث المعاينة وفلترة من جديد — دون أن يفقد التاجر تعديلاته المحفوظة.',
    noRowsInFilter: 'لا توجد أسماء ضمن هذا الفلتر.',
    deselect: 'إلغاء التحديد',
    selectCustomer: 'تحديد العميل',
    categoryBadges: {
      source_label_name: 'مصدر',
      location_label_name: 'مدينة',
      placeholder_name: 'بدون اسم',
      suspicious_suffix: 'كلمة زائدة',
      generic_bad_name: 'غير حقيقي',
      other: 'تنظيف',
    },
    excludedFromCampaigns: 'مستبعد من الحملات',
    modified: 'معدّل',
    skipped: 'متخطى',
    highConf: 'ثقة عالية',
    review: 'مراجعة',
    noWords: 'لا توجد كلمات',
    restoreWord: 'إعادة هذه الكلمة',
    removeWord: 'حذف هذه الكلمة',
    resultLabel: 'الناتج:',
    willClearName: 'سيُمسح الاسم',
    resetSuggestion: 'إعادة المقترح',
    skipRow: 'تخطّى هذا الصف — لن يظهر في جلسات المراجعة القادمة',
    skip: 'تخطّى',
    clearEntireName: 'مسح الاسم بالكامل',
    reEnableMarketing: 'إعادة تفعيل التسويق لهذا العميل',
    excludeFromCampaigns: 'استبعاد من الحملات (لا يتأثر استقبال رسائله)',
    reEnableMarketingShort: 'إعادة تفعيل التسويق',
    excludeShort: 'استبعاد من الحملات',
    helpPrimary: 'اضغط على أي كلمة لحذفها (تظهر مشطوبة) أو لإعادتها — أو استخدم "مسح الاسم بالكامل" لمسح الاسم كلياً وترك الحملات تستخدم العبارة الافتراضية.',
    helpSecondary: 'بعد التطبيق، تستخدم الحملات الاسم المحفوظ مباشرة — إذا أصبح الاسم فارغاً تُستخدم العبارة الافتراضية. كل التغييرات تُحفظ في سجل تدقيق داخلي قابل للمراجعة.',
    close: 'إغلاق',
    applyHighOnly: 'تطبيق ذوي الثقة العالية فقط',
    applyHighTitle: 'تطبيق كل المقترحات عالية الثقة دون مراجعة فردية',
    applySelected: 'تطبيق المحدد',
    applySelectedCount: 'تطبيق المحدد ({count})',
    applySelectedHint: 'حدد الصفوف التي تريد تطبيق التنظيف عليها أولاً',
    applySelectedTitle: 'سيتم تطبيق التنظيف على {count} عميل',
    loadPreviewFailed: 'تعذر تحميل المعاينة',
    saveDraftFailed: 'تعذر حفظ المسودة',
    actionFailed: 'تعذر تنفيذ الإجراء',
    discardFailed: 'تعذر تجاهل المسودة',
    applyFailed: 'تعذر تطبيق التنظيف',
    manualEditReason: 'تعديل يدوي — {reason}',
  },
  errors: {
    loadFailed: 'تعذر تحميل العملاء',
  },
  loading: 'جارٍ التحميل...',
}
