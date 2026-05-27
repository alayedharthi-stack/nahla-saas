/**
 * Master translation shape.
 * Every key here must be present in both ar.ts and en.ts.
 */
export interface Translations {
  /** Language metadata */
  meta: {
    code: string
    label: string
    dir: 'rtl' | 'ltr'
  }

  /** Sidebar navigation */
  nav: {
    groups: {
      main:          string
      ai:            string
      store:         string
      adminPlatform: string
      adminSettings: string
    }
    items: {
      overview:         string
      conversations:    string
      orders:           string
      customers:        string
      autopilot:        string
      promotions:       string
      coupons:          string
      campaigns:        string
      templates:        string
      automations:      string
      intelligence:     string
      knowledgeBase:    string
      analyticsAI:      string
      salesAgent:       string
      handoffQueue:     string
      integrations:     string
      storeIntegration: string
      whatsappConnect:  string
      whatsappCatalog:  string
      manualSetup:      string
      widgets:          string
      systemStatus:     string
      deliveryQuality:  string
      billing:          string
      settings:         string
      security:         string
    }
    adminItems: {
      dashboard:       string
      tenants:         string
      revenue:         string
      aiUsage:         string
      features:        string
      troubleshooting: string
      coexistence:     string
      team:            string
      system:          string
      tools:           string
      catalog:         string
      aiQuality:       string
      settings:        string
      security:        string
    }
    adminTagline:  string
    adminOwner:    string
    storeBadge:    { plan: string }
    logoTagline:   string
  }

  /** Topbar */
  topbar: {
    searchPlaceholder: string
    notifications:     string
    admin:             string
  }

  /** User role labels */
  roles: {
    platformOwner: string
    owner:         string
    staff:         string
    merchant:      string
    support:       string
    defaultOwner:  string
    defaultMerchant: string
    /**
     * Platform-admin display name shown in the header / account
     * widgets when the caller is a Nahla platform owner. Used instead
     * of the merchant store name so the owner UI never advertises a
     * specific tenant's brand as the owner's identity.
     */
    nahlaAdmin:    string
  }

  /** Page titles & subtitles */
  pages: {
    overview:         { title: string; subtitle: string }
    conversations:    { title: string; subtitle: string }
    orders:           { title: string; subtitle: string }
    customers:        { title: string; subtitle: string }
    coupons:          { title: string; subtitle: string }
    promotions:       { title: string; subtitle: string }
    campaigns:        { title: string; subtitle: string }
    templates:        { title: string; subtitle: string }
    automations:      { title: string; subtitle: string }
    smartAutomations: { title: string; subtitle: string }
    intelligence:     { title: string; subtitle: string }
    knowledgeBase:    { title: string; subtitle: string }
    analytics:        { title: string; subtitle: string }
    integrations:     { title: string; subtitle: string }
    settings:         { title: string; subtitle: string }
    billing:          { title: string; subtitle: string }
    widgets:          { title: string; subtitle: string }
    systemStatus:     { title: string; subtitle: string }
    storeIntegration: { title: string; subtitle: string }
    whatsappConnect:  { title: string; subtitle: string }
  }

  /** Admin-only page titles */
  adminPages: {
    dashboard:       { title: string; subtitle: string }
    tenants:         { title: string; subtitle: string }
    merchants:       { title: string; subtitle: string }
    revenue:         { title: string; subtitle: string }
    aiUsage:         { title: string; subtitle: string }
    features:        { title: string; subtitle: string }
    troubleshooting: { title: string; subtitle: string }
    team:            { title: string; subtitle: string }
    system:          { title: string; subtitle: string }
    coexistence:     { title: string; subtitle: string }
    tools:           { title: string; subtitle: string }
    aiQuality:       { title: string; subtitle: string }
  }

  /** Common reusable action labels */
  actions: {
    export:        string
    newCoupon:     string
    newCampaign:   string
    newTemplate:   string
    syncTemplates: string
    viewAll:       string
    save:          string
    saved:         string
    cancel:        string
    search:        string
    filter:        string
  }

  /** Common UI strings used across many pages */
  common: {
    saving:          string
    loading:         string
    error:           string
    success:         string
    active:          string
    inactive:        string
    enabled:         string
    disabled:        string
    yes:             string
    no:              string
    confirm:         string
    back:            string
    close:           string
    copy:            string
    copied:          string
    refresh:         string
    test:            string
    testing:         string
    connect:         string
    disconnect:      string
    connected:       string
    notConnected:    string
    status:          string
    unknown:         string
    required:        string
    optional:        string
    delete:          string
    edit:            string
    create:          string
    update:          string
    name:            string
    email:           string
    phone:           string
    password:        string
    submit:          string
    tryAgain:        string
    noData:          string
    poweredBy:       string
  }

  /** Login page */
  login: {
    title:           string
    subtitle:        string
    emailLabel:      string
    emailPlaceholder:string
    passwordLabel:   string
    submitBtn:       string
    submitting:      string
    forgotPassword:  string
    noAccount:       string
    registerLink:    string
    invalidCreds:    string
    dev:             string
    devRole:         string
  }

  /** Register page */
  register: {
    title:            string
    subtitle:         string
    storeNameLabel:   string
    storeNamePh:      string
    emailLabel:       string
    phoneLabel:       string
    phonePh:          string
    passwordLabel:    string
    submitBtn:        string
    submitting:       string
    hasAccount:       string
    loginLink:        string
    terms:            string
  }

  /** Settings page */
  settings: {
    tabs: {
      whatsapp:      string
      ai:            string
      automation:    string
      aiSales:       string
      store:         string
      team:          string
      notifications: string
      security:      string
      widget:        string
      system:        string
    }
    whatsapp: {
      accountTitle:   string
      accountDesc:    string
      businessName:   string
      phoneNumber:    string
      phoneHint:      string
      phoneNumberId:  string
      phoneIdHint:    string
      accessToken:    string
      webhookTitle:   string
      webhookDesc:    string
      verifyToken:    string
      verifyHint:     string
      webhookUrl:     string
      webhookHint:    string
      webhookNote:    string
      buttonsTitle:   string
      buttonsDesc:    string
      storeBtnLabel:  string
      storeBtnUrl:    string
      ownerBtnLabel:  string
      ownerWhatsapp:  string
      autoReplyTitle: string
      autoReplyLabel: string
      autoReplyHint:  string
      transferLabel:  string
      transferHint:   string
      testBtn:        string
      testingBtn:     string
      testSuccess:    string
      testFail:       string
    }
    ai: {
      personalityTitle:  string
      personalityDesc:   string
      assistantName:     string
      replyTone:         string
      toneOptions: {
        friendly:    string
        formal:      string
        luxury:      string
        playful:     string
      }
      languageLabel:     string
      langOptions: {
        arabic:   string
        english:  string
        both:     string
      }
      maxMessages:       string
      maxMsgHint:        string
      greetingTitle:     string
      greetingDesc:      string
      greetingMsg:       string
      capabilitiesTitle: string
      capabilitiesDesc:  string
      capProductQ:       string
      capProductHint:    string
      capOrders:         string
      capOrdersHint:     string
      capCoupons:        string
      capCouponsHint:    string
      capUpsell:         string
      capUpsellHint:     string
      capHandoff:        string
      capHandoffHint:    string
      contextTitle:      string
      contextDesc:       string
      storePolicy:       string
      storePolicyPh:     string
      returnsPolicy:     string
      returnsPolicyPh:   string
      handoffMsg:        string
      handoffMsgPh:      string
    }
    store: {
      title:      string
      desc:       string
      nameLabel:  string
      domainLabel:string
      domainHint: string
      currencyLabel: string
      timezoneLabel: string
    }
    notifications: {
      title:             string
      desc:              string
      emailEnabled:      string
      emailHint:         string
      emailAddr:         string
      whatsappEnabled:   string
      whatsappHint:      string
      whatsappPhone:     string
      newOrder:          string
      newOrderHint:      string
      handoff:           string
      handoffHint:       string
      dailySummary:      string
      dailyHint:         string
    }
    saveBar: {
      save:    string
      saving:  string
      saved:   string
      error:   string
    }
  }

  /** Overview page */
  overview: {
    aiSalesLabel:   string
    aiOrdersLabel:  string
    salesBot:       string
    kpiRevenue:     string
    kpiConversations: string
    kpiOrders:      string
    kpiAiRate:      string
    recentConvTitle:string
    recentOrdTitle: string
    aiBadge:        string
    humanBadge:     string
    statusPaid:     string
    statusPending:  string
    statusFailed:   string
    statusCancelled:string
    sourceAI:       string
    sourceManual:   string
  }

  /** WhatsApp Connect page */
  whatsappConnect: {
    title:           string
    subtitle:        string
    status: {
      not_connected: string
      connected:     string
      pending:       string
      error:         string
      disconnected:  string
      needs_reauth:  string
    }
    statusHint:      string
    connectBtn:      string
    reconnectBtn:    string
    disconnectBtn:   string
    howTitle:        string
    howStep1:        string
    howStep2:        string
    howStep3:        string
    howStep4:        string
    howStep5:        string
    prereqTitle:     string
    prereq1:         string
    prereq2:         string
    /** Wave 1A-i — main page shell, disconnect modal, connected state */
    page: {
      headerTitle:    string
      headerSubtitle: string
      modes: {
        manual:        string
        manualBadge:   string
        embedded:      string
        otp:           string
        coexistence:     string
      }
      modeHints: {
        manual:   string
        embedded: string
      }
    }
    connLabels: {
      viaMeta:      string
      coexistence:  string
      business:     string
      manualPrefix: string
    }
    metaBanner: {
      success: string
      failure: string
    }
    disconnect: {
      title:       string
      subtitle:    string
      consequence1: string
      consequence2: string
      consequence3: string
      cancel:      string
      confirm:     string
      confirming:  string
      managedByTeam: string
      opsOnlyError: string
      failedError: string
    }
    connected: {
      verifying:          string
      linkedUnverified:   string
      softWarning:        string
      verified:           string
      broken:             string
      linkedAt:           string
      reason:             string
      note:               string
      softWarningDetail:  string
      featureAutoReply:     string
      featureAiReady:       string
      featureCampaigns:     string
      recheckLive:          string
      rechecking:           string
      dashboard:            string
      disconnect:           string
      checkHasRecord:       string
      checkStatusOk:        string
      checkWabaId:          string
      checkPhoneId:         string
      checkToken:           string
      checkProvider:        string
    }
    manual: {
      title:                 string
      badge:                 string
      subtitle:              string
      noticeTitle:           string
      noticeBody:            string
      phoneNumberIdHint:     string
      wabaHint:              string
      tokenHint:             string
      digitsOnly:            string
      resolveWabaNeedCreds:  string
      wabaResolveFailed:     string
      wabaResolveError:      string
      validatePhoneIdRequired: string
      validatePhoneIdDigits:   string
      validateWabaRequired:    string
      validateWabaDigits:      string
      validateTokenRequired:   string
      resolveWabaTitle:        string
      resolving:               string
      resolved:                string
      discover:                string
      wabaAutoResolved:        string
      connectError:            string
      readinessTitle:          string
      credSaved:               string
      credSavedOk:             string
      credSavedFail:           string
      phoneRegistered:         string
      phoneRegisteredOk:       string
      phoneRegisteredFailPrefix: string
      phoneRegisteredPending:  string
      webhookSub:              string
      webhookSubOk:            string
      webhookSubFailPrefix:    string
      webhookSubPending:       string
      inboundReady:            string
      inboundReadyOk:          string
      inboundReadyPartial:     string
      continueAnyway:          string
      retry:                   string
      helpPrefix:              string
      helpLink:                string
      connecting:              string
      connectBtn:              string
    }
    coexistence: {
      connectedTitle:          string
      connectedBody:           string
      tipKeepApp:              string
      tipDontDelete:           string
      tipOpenPeriodically:     string
      statusRequestSubmitted:  string
      statusPendingActivation: string
      statusActionRequired:    string
      defaultPendingMessage:   string
      requestTimeLabel:        string
      formTitle:               string
      formSubtitle:            string
      benefitSameNumber:       string
      benefitAiReplies:        string
      benefitActivationTime:   string
      phonePlaceholder:        string
      displayNamePlaceholder:  string
      notesPlaceholder:        string
      phoneRequired:           string
      submitFailed:            string
      submitting:              string
      submitBtn:               string
    }
    embedded: {
      loadConfigFailed:        string
      activateFailed:          string
      syncStatusFailed:        string
      exchangeFailed:          string
      bspNotEnabled:           string
      directNotEnabled:        string
      sdkNotReady:             string
      linkCancelled:           string
      preparingVerify:         string
      selectPhoneFailed:       string
      phoneNameRequired:       string
      phoneInvalid:            string
      displayNameRequired:     string
      sendingOtp:              string
      addPhoneFailed:          string
      otpRequired:             string
      otpInvalid:              string
      refreshFailed:           string
      successTitle:            string
      selectPhoneTitle:        string
      addNewTitle:             string
      addNewHint:              string
      addNewBtn:               string
      noPhones:                string
      verified:                string
      unverified:              string
      addPhoneTitle:           string
      addPhoneSubtitle:        string
      phoneLabel:              string
      phoneExampleHint:        string
      businessNameLabel:       string
      businessNamePlaceholder: string
      businessNameHint:        string
      back:                    string
      sendOtp:                 string
      preparingCodeTitle:      string
      preparingCodeSubtitle:   string
      requestingCodeDefault:   string
      requestingCodeTip:       string
      verifyTitle:             string
      verifySubtitle:          string
      confirm:                 string
      syncingTitle:            string
      syncingSubtitle:         string
      syncingDefault:          string
      refreshNow:              string
      backToPhones:            string
      disabledTitle:           string
      disabledSubtitle:        string
      disabledReasonFallback:  string
      disabledExplainTitle:    string
      disabledExplainBody:     string
      disabledFooter:          string
      initTitle:               string
      initSubtitle:            string
      step1:                   string
      step2:                   string
      step3:                   string
      step4:                   string
      initHint:                string
      loading:                 string
      connectBtn:              string
      initFooter:              string
    }
    direct: {
      stepIdentity:            string
      stepVerify:              string
      stepProfile:             string
      stepDone:                string
      step1Title:              string
      step1Subtitle:           string
      phoneLabel:              string
      phoneHint:               string
      phoneNormalizedOk:       string
      phoneFormatHint:         string
      displayNameLabel:        string
      displayNameHint:         string
      displayNamePlaceholder:  string
      displayNameWarning:      string
      otpMethodLabel:          string
      otpMethodSms:            string
      otpMethodVoice:          string
      sending:                 string
      sendOtpBtn:              string
      requirementsTitle:       string
      requirement1:            string
      requirement2:            string
      requirement3:            string
      errPhoneRequired:        string
      errDisplayNameRequired:  string
      errPhoneInvalid:         string
      errRateLimitSuffix:      string
      errSendOtpFailed:        string
      resumeOtpSent:           string
      step2Title:              string
      otpFieldLabel:           string
      verifying:               string
      confirmPhoneBtn:         string
      metaVerifiedPrompt:      string
      refreshStatusBusy:       string
      refreshStatusBtn:        string
      refreshSuccess:          string
      changePhone:             string
      resendLabel:             string
      resendCooldownUnit:      string
      resendBtn:               string
      errOtpIncomplete:        string
      errVerifiedPendingMeta:  string
      step3Title:              string
      step3Subtitle:           string
      verifiedBanner:          string
      verticalLabel:           string
      aboutLabel:              string
      aboutHint:               string
      aboutPlaceholder:        string
      addressLabel:            string
      addressHint:             string
      addressPlaceholder:      string
      emailLabel:              string
      emailHint:               string
      websiteLabel:            string
      saving:                  string
      saveBtn:                 string
      skipBtn:                 string
      verticals: {
        RETAIL:                    string
        APPAREL:                   string
        BEAUTY_SPA_SALON:          string
        FOOD_AND_GROCERY:          string
        RESTAURANT:                string
        HEALTH_AND_MEDICAL:        string
        EDUCATION:                 string
        HOTEL_AND_LODGING:         string
        TRAVEL_AND_TRANSPORTATION: string
        AUTOMOTIVE:                string
        ENTERTAINMENT:             string
        PROFESSIONAL_SERVICES:     string
        NONPROFIT:                 string
        OTHER:                     string
      }
    }
    errors: {
      unexpected:       string
      meta131000:       string
      corsFetch:        string
      sessionExpired:   string
      sanitizeFallback: string
    }
  }

  /** Billing page */
  billing: {
    title:           string
    subtitle:        string
    currentPlan:     string
    noPlan:          string
    choosePlan:      string
    plans: {
      starter:       string
      growth:        string
      enterprise:    string
    }
    perMonth:        string
    subscribe:       string
    upgradeBtn:      string
    cancelPlan:      string
    renewsOn:        string
    features: {
      conversations: string
      aiReplies:     string
      campaigns:     string
      analytics:     string
      support:       string
      whiteLabel:    string
    }
  }

  /** WhatsApp Templates page strings */
  templatesMgmt: {
    filterAll:         string
    filterApproved:    string
    filterPending:     string
    filterRejected:    string
    filterBlocked:     string
    filterPaused:      string
    colName:           string
    colLang:           string
    colCategory:       string
    colStatus:         string
    colVariables:      string
    colUpdated:        string
    statApproved:      string
    statPending:       string
    statDraft:         string
    submitBtn:         string
    submittingBtn:     string
    saveDraft:         string
    savingDraft:       string
    libraryBtn:        string
    pendingNote:       string
    draftNote:         string
    importNote:        string
    statusApproved:    string
    statusPending:     string
    statusRejected:    string
    statusDraft:       string
    statusBlocked:     string
    statusPaused:      string
    categoryMarketing: string
    categoryUtility:   string
    categoryAuth:      string
    varCount:          string
    compatible:        string
    awaitingMeta:      string
    needsReview:       string
    defaultBadge:      string
    noTemplates:       string
    createFirst:       string
    loadingTemplates:  string
    metaPolicy:        string
    metaPolicyText:    string
    tooltipPreview:    string
    tooltipEdit:       string
    tooltipDelete:     string
    disabled:          string
    archived:          string
    limitExceeded:     string
    /** WaPreview bubble chrome (copy-code fallback, read receipt) */
    previewCopyCodeFallback: string
    previewReadReceipt:      string
    create: {
      title: string
      stepProgressMiddle: string
      stepProgressOf:     string
      steps: {
        info:    string
        content: string
        buttons: string
        review:  string
      }
      step1: {
        intro: string
        nameLabel: string
        nameHint: string
        namePlaceholder: string
        languageLabel: string
        langArabic: string
        langEnglish: string
        langEnglishUS: string
        categoryLabel: string
        categoryOptionMarketing: string
        categoryOptionUtility: string
        categoryOptionAuth: string
        categoryNoticeBeforeMarketing: string
        marketingTerm: string
        categoryNoticeAfterMarketing: string
        utilityTerm: string
        categoryNoticeAfterUtility: string
        authTerm: string
        categoryNoticeAfterAuth: string
      }
      step2: {
        intro: string
        introSuffix: string
        headerLabel: string
        bodyLabel: string
        footerLabel: string
        charCountSuffix: string
      }
      step3: {
        intro: string
        addUrl: string
        addPhone: string
        addCopyCode: string
        noButtons: string
        btnTypeUrl: string
        btnTypePhone: string
        btnTypeCopyCode: string
        buttonTextPlaceholder: string
        copyCodeStrong: string
        copyCodeBody: string
      }
      step4: {
        reviewNoticeBefore: string
        reviewNoticeStrong: string
        reviewNoticeAfter: string
        summaryName: string
        summaryLanguage: string
        summaryCategory: string
        summaryVariables: string
        summaryButtons: string
        varUnit: string
        btnUnit: string
        noButtonsSummary: string
        previewLabel: string
      }
      nav: {
        prev: string
        next: string
      }
      errors: {
        createFailed: string
      }
    }
    edit: {
      title: string
      draftNoticeBefore: string
      draftNoticeStrong: string
      draftNoticeAfter: string
      buttonsLabel: string
      btnTypeUrl: string
      btnTypeCopyCode: string
      btnTypePhone: string
      btnTypeQuickReply: string
      urlInvalidWarningBefore: string
      urlInvalidWarningExample: string
      manualCouponStrong: string
      manualCouponBody: string
      dynamicCodeBody: string
      dynamicUrlStrong: string
      dynamicUrlAfter: string
      previewLabel: string
      save: string
      saving: string
      errors: {
        bodyRequired: string
        saveFailed: string
      }
    }
  }

  /** Merchants (admin) page */
  merchants: {
    title:        string
    subtitle:     string
    newMerchant:  string
    sendInvite:   string
    storeName:    string
    emailCol:     string
    tenantId:     string
    createdAt:    string
    statusCol:    string
    enterStore:   string
    toggleStatus: string
    deleteBtn:    string
    confirmDel:   string
    noMerchants:  string
    createFirst:  string
    inviteTitle:  string
    inviteDesc:   string
    invitePh:     string
    createLink:   string
    formTitle:    string
    storeNamePh:  string
    emailPh:      string
    passwordPh:   string
    phonePh:      string
    creating:     string
  }

  /** Security & 2FA — Phase 2A Sprint 1 (/settings/security) */
  security: {
    pageTitle:                string
    pageSubtitle:             string
    twoFactorTitle:           string
    twoFactorDesc:            string
    statusEnabled:            string
    statusDisabled:           string
    enrolledAt:               string
    lastUsedAt:               string
    recoveryRemaining:        string
    /** Enable / setup flow */
    enableBtn:                string
    setupStep1:               string
    setupStep1Desc:           string
    setupStep1AppGoogle:      string
    setupStep1AppMicrosoft:   string
    setupStep1AppAuthy:       string
    setupStep1AppStoreIOS:    string
    setupStep1AppStoreAndroid:string
    setupStep2:               string
    setupStep2Desc:           string
    setupStep3:               string
    setupStep3Desc:           string
    pickerHint:               string
    pickerBadgePrev:          string
    pickerBadgeNow:           string
    pickerBadgeNext:          string
    pickerExpired:            string
    pickerRefresh:            string
    pickerRefreshing:         string
    pickerOrType:             string
    pickerSecurityNote:       string
    scanQr:                   string
    cantScan:                 string
    manualSecretLabel:        string
    copySecret:               string
    secretCopied:             string
    otpLabel:                 string
    otpPlaceholder:           string
    verifyBtn:                string
    verifying:                string
    /** Recovery codes panel (shown ONCE) */
    recoveryTitle:            string
    recoveryDesc:             string
    recoveryWarning:          string
    copyAll:                  string
    downloadTxt:              string
    iSavedThem:               string
    codesCopied:              string
    /** Disable flow */
    disableBtn:               string
    disableTitle:             string
    disableDesc:              string
    currentPassword:          string
    otpOrRecovery:            string
    confirmDisable:           string
    cancel:                   string
    disabling:                string
    /** Generic errors / hints */
    errorGeneric:             string
    errorBadOtp:              string
    errorBadPassword:         string
    successEnabled:           string
    successDisabled:          string
  }
}

/** Supported language codes */
export type Lang = 'ar' | 'en'
