/**
 * Master translation shape.
 * Every key here must be present in both ar.ts and en.ts.
 */
import type { CustomersPageLabels } from './customersPageLabels'
import type { LandingPricingLabels } from './landingPricingLabels'
import type { CampaignsListLabels } from './campaignsListPageLabels'
import type { TemplatesPageExtraLabels } from './templatesPageLabels'

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
    destinations: {
      overview:  string
      inbox:     string
      products:  string
      orders:    string
      customers: string
      marketing: string
      automation: string
      templates: string
      channels:  string
      settings:  string
    }
    sections: {
      nahlaSmart: string
      advanced:   string
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
      operationsCenter: string
      salesChannels:    string
      analyticsAI:      string
      salesAgent:       string
      handoffQueue:     string
      integrations:     string
      storeIntegration: string
      whatsappConnect:  string
      whatsappCatalog:      string
      catalogIntelligence:  string
      manualSetup:          string
      widgets:          string
      nahlaTemplateLibrary: string
      orderUpdates:     string
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
    operationsCenter: { title: string; subtitle: string }
    salesChannels: {
      title: string
      subtitle: string
      tabs: {
        sales: string
        branches: string
        contacts: string
        routing: string
      }
      contactsTab: {
        description: string
        openBranches: string
      }
      routingTab: {
        rules: string[]
        note: string
      }
      loadError: string
      saveError: string
      saved: string
      save: string
      statusLabel: string
      available: string
      notAvailable: string
      onlineStore: {
        title: string
        enableLabel: string
        enableHint: string
        urlLabel: string
        urlHint: string
      }
      whatsapp: { title: string; description: string }
      showroom: {
        title: string
        description: string
        mapsHint: string
        branchesLink: string
      }
    }
    analytics:        { title: string; subtitle: string }
    integrations:     { title: string; subtitle: string }
    settings:         { title: string; subtitle: string }
    billing:          { title: string; subtitle: string }
    widgets:          { title: string; subtitle: string }
    inboxHub: {
      title: string
      subtitle: string
      cards: {
        conversations: { title: string; description: string }
        handoffQueue:  { title: string; description: string }
      }
    }
    productsHub: {
      title: string
      subtitle: string
      cards: {
        catalog:              { title: string; description: string }
        catalogIntelligence:  { title: string; description: string }
      }
    }
    channelsHub: {
      title: string
      subtitle: string
      cards: {
        integrations:     { title: string; description: string }
        storeIntegration: { title: string; description: string }
        whatsappConnect:  { title: string; description: string }
        manualSetup:      { title: string; description: string }
        salesChannels:    { title: string; description: string }
      }
    }
    ordersHub: {
      title: string
      subtitle: string
      cards: {
        orders:    { title: string; description: string }
        customers: { title: string; description: string }
      }
    }
    automationHub: {
      title: string
      subtitle: string
      cards: {
        smartAutomations: { title: string; description: string }
        autopilot:        { title: string; description: string }
      }
    }
    templatesHub: {
      title: string
      subtitle: string
      cards: {
        nahlaLibrary:      { title: string; description: string }
        whatsappTemplates: { title: string; description: string }
      }
    }
    settingsHub: {
      title: string
      subtitle: string
      sections: {
        core:       { title: string; description: string }
        nahlaSmart: { title: string; description: string }
        advanced:   { title: string; description: string }
      }
      cards: {
        overview:        { title: string; description: string }
        general:         { title: string; description: string }
        security:        { title: string; description: string }
        billing:         { title: string; description: string }
        orderUpdates:    { title: string; description: string }
        intelligence:    { title: string; description: string }
        knowledgeBase:   { title: string; description: string }
        systemStatus:    { title: string; description: string }
        deliveryQuality: { title: string; description: string }
        salesAgent:      { title: string; description: string }
        analytics:       { title: string; description: string }
      }
    }
    marketingHub: {
      title: string
      subtitle: string
      cards: {
        campaigns:  { title: string; description: string }
        promotions: { title: string; description: string }
        coupons:    { title: string; description: string }
        widgets:    { title: string; description: string }
      }
    }
    nahlaTemplateLibrary: {
      title: string
      subtitle: string
      sections: {
        ecommerce: {
          title: string
          description: string
          comingSoon: string
        }
        whatsapp: {
          title: string
          description: string
          linkLabel: string
        }
        orderUpdates: {
          title: string
          description: string
          editLink: string
          templates: {
            order_confirmation: { title: string; description: string }
            shipping_tracking:  { title: string; description: string }
          }
        }
      }
    }
    systemStatus:     { title: string; subtitle: string }
    storeIntegration: { title: string; subtitle: string }
    whatsappConnect:  { title: string; subtitle: string }
    catalogIntelligence: {
      title: string
      subtitle: string
      refresh: string
      tabs: { groups: string; settings: string; uncategorized: string }
      groupsTitle: string
      newGroupPlaceholder: string
      noGroups: string
      selectGroupHint: string
      catalogMatchPlaceholder: string
      saveGroup: string
      saved: string
      confirmDeleteGroup: string
      inactive: string
      productsInGroup: string
      alternatives: string
      alternativesFor: string
      bestSeller: string
      searchProductPlaceholder: string
      pickProduct: string
      pickAlternative: string
      addAlternative: string
      settingsTitle: string
      bestSellerMode: string
      defaultGroupSlug: string
      maxRelations: string
      saveSettings: string
      validationTitle: string
      validationSummary: string
      validationOk: string
      uncategorizedHint: string
      uncategorizedCount: string
      allCategorized: string
    }
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
    kpiConversationsToday: string
    kpiMessagesToday: string
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
    periodToday:        string
    periodLast7Days:    string
    periodThisMonth:    string
    sectionTitle:       string
    chartTitle:         string
    chartSubtitle:      string
    chartRevenueLabel:  string
    messagesSent:       string
    currency:           string
    viewPlans:          string
    noConversationsYet: string
    noOrdersYet:        string
    convStatusActive:   string
    convStatusHuman:    string
    convStatusClosed:   string
    chartDays: {
      mon: string
      tue: string
      wed: string
      thu: string
      fri: string
      sat: string
      sun: string
    }
    waUsage: {
      title: string
      periodUsageTitle: string
      todayConversations: string
      todayInPeriod: string
      periodUsageHint: string
      todayConversationsHint: string
      preRenewalNote: string
      emergencyStop: string
      campaignsStopped: string
      nearLimit90: string
      used70: string
      conversationsUnit: string
      details: string
      upgrade: string
      emergencyBanner: string
      campaignsBanner: string
      campaignsBannerNote: string
      nearLimitBanner: string
      upgradeLink: string
      upgradeNowLink: string
      metaTier: {
        title: string
        staleValue: string
        hint: string
        verifyInMeta: string
        source: string
        lastSynced: string
        numberQuality: string
        qualityExcellent: string
        qualityMedium: string
        qualityLow: string
        refreshNow: string
        refreshing: string
        refreshAriaLabel: string
      }
      sync: {
        never: string
        unavailable: string
        momentsAgo: string
        minutesAgo: string
        hoursAgo: string
      }
      diagnostics: {
        updatedFromProvider: string
        notFromProvider: string
        provider: string
        hideDetails: string
        technicalDetails: string
        tierHint: string
        errorPrefix: string
      }
    }
  }

  /** Orders page — static UI only */
  ordersPage: {
    cards: {
      totalOrders: string
      needsFollowUpNow: string
      pendingPayment: string
      completedToday: string
      whatsappOrdersToday: string
      whatsappRevenueToday: string
      todayRevenue: string
    }
    tabs: {
      all: string
      needsAction: string
      missingLocation: string
      store: string
      whatsapp: string
      pendingPayment: string
      paymentSubmitted: string
      paid: string
      abandoned: string
      completed: string
      cancelled: string
    }
    table: {
      order: string
      customer: string
      amount: string
      status: string
      source: string
      products: string
      date: string
    }
    status: {
      paid: string
      pending: string
      failed: string
      cancelled: string
    }
    source: {
      salla: string
      zid: string
      shopify: string
      whatsapp: string
      manual: string
    }
    badges: {
      createdByAI: string
      fromStore: string
      needsAction: string
      paymentLink: string
    }
    empty: string
    loadError: string
    retry: string
    showing: string
    currency: string
  }

  /** Conversations page — static UI only */
  conversationsPage: {
    title: string
    unreadCount: string
    searchPlaceholder: string
    emptyList: string
    emptyDetailTitle: string
    emptyDetailSubtitle: string
    loadMore: string
    loadingMore: string
    refreshNow: string
    noMessages: string
    filters: {
      all: string
      active: string
      human: string
      agentReq: string
      paused: string
      blocked: string
      paid: string
      unsubscribed: string
      campaignExcluded: string
      closed: string
    }
    badges: {
      customerMessage: string
      aiResponse: string
      system: string
      requestsStaff: string
      openConversation: string
      humanReply: string
      paymentConfirmed: string
      unsubscribed: string
      pendingUnsub: string
    }
    senderTypes: {
      ai: string
      campaign: string
      automation: string
      cod: string
      manual: string
      system: string
    }
    actions: {
      resumeAI: string
      pauseAI: string
      moreActions: string
      takeOver: string
      endSupervision: string
      excludeCampaigns: string
      excludedFromCampaigns: string
      removeExclusion: string
      removeExclusionShort: string
      blockNumber: string
      sendTemplate: string
      back: string
      close: string
      cancel: string
      exclude: string
      excluding: string
      reEnable: string
      reEnabling: string
    }
    banners: {
      humanSupervision: string
      aiPaused: string
      unsubscribed: string
      pendingUnsub: string
      excludeModalTitle: string
      excludeModalBody: string
      reEnableModalTitle: string
      reEnableModalBody: string
    }
    pauseReasons: {
      manual: string
      humanHandoff: string
      manualTakeover: string
      supportEscalation: string
      botLoop: string
      rateLimit: string
      internalNumber: string
    }
    delivery: {
      notSent: string
      pending: string
      failed: string
      awaitingWamid: string
      sent: string
    }
    replyPlaceholder: string
    aiHandlingHint: string
    unsubscribedFilterHint: string
    mobileFilters: {
      sheetTitle: string
      menuButtonLabel: string
    }
    scrollToBottom: string
    aiPausedBadge: string
    errors: {
      refreshFailed: string
      loadMoreFailed: string
      sendReplyFailed: string
      handoffFailed: string
      pauseFailed: string
      customerNotFound: string
      excludeFailed: string
      resumeFailed: string
      blockFailed: string
      unpauseFailed: string
    }
    toasts: {
      excludedFromCampaigns: string
      reEnabledFromCampaigns: string
      resumedToAI: string
    }
    editCustomerName: {
      title: string
      fieldLabel: string
      save: string
      cancel: string
      nameRequired: string
      nameTooLong: string
      toastSuccess: string
      toastError: string
    }
    confirm: {
      blockNumber: string
    }
    loadingMessages: string
    conversationTags: {
      staff_request: string
      human_active: string
      ai_paused: string
      blocked: string
      unsubscribed: string
      pending_unsub: string
      paid: string
      closed: string
      customer_message: string
      open: string
    }
    sendErrors: {
      default: { label: string; advice: string }
      out_of_24h_window: { label: string; advice: string }
      not_on_whatsapp: { label: string; advice: string }
      invalid_phone: { label: string; advice: string }
      user_not_opted_in: { label: string; advice: string }
      marketing_blocked: { label: string; advice: string }
      rate_limit: { label: string; advice: string }
      template_not_found: { label: string; advice: string }
      template_paused: { label: string; advice: string }
      policy_violation: { label: string; advice: string }
      auth_error: { label: string; advice: string }
      unknown: { label: string; advice: string }
    }
  }

  /** Customers page — static UI only; dynamic customer data stays as API values */
  customersPage: CustomersPageLabels
  landingPricing: LandingPricingLabels

  /** Analytics page */
  analyticsPage: {
    title: string
    subtitle: string
    cards: {
      revenue: string
      conversionRate: string
      orders: string
      conversations: string
    }
    revenueTrend: string
    last6Months: string
    todayRevenue: string
    loading: string
    revenueLabel: string
    convVsConv: string
    conversationsBar: string
    conversionsBar: string
    orderSources: string
    topProducts: string
    table: {
      rank: string
      product: string
      orders: string
      revenue: string
      trend: string
    }
    noProductData: string
    currency: string
  }

  /** Sales Agent / AI logs page */
  salesAgentPage: {
    title: string
    interactionsRecorded: string
    refresh: string
    searchPlaceholder: string
    loading: string
    noResults: string
    allIntents: string
    stats: {
      total: string
      ordersCreated: string
      paymentsSent: string
      handoffs: string
      avgConfidence: string
    }
    table: {
      time: string
      customer: string
      intent: string
      confidence: string
      order: string
    }
    detail: {
      customerMessage: string
      aiReply: string
      yes: string
    }
    flags: {
      catalogUsed: string
      catalogNotUsed: string
      orderCreated: string
      orderNotCreated: string
      paymentLinkSent: string
      paymentLinkNotSent: string
      handoff: string
      noHandoff: string
    }
    showing: string
    loadError: string
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
    simplified: {
      chooseMethodTitle:        string
      metaCardTitle:            string
      metaCardDescription:      string
      metaCardBadge:            string
      metaStepsTitle:           string
      metaSteps:                string[]
      metaApprovalNotice:       string
      metaExistingAccountHint:  string
      metaConnectBtn:           string
      manualSetupLink:          string
    }
    assisted: {
      formTitle:               string
      formSubtitle:            string
      benefitNoSecrets:        string
      benefitTeamSetup:        string
      benefitSecure:           string
      contactPhoneLabel:       string
      contactPhoneHint:        string
      contactPhonePlaceholder: string
      displayNameLabel:        string
      displayNameHint:         string
      displayNamePlaceholder:  string
      notesLabel:              string
      notesHint:               string
      notesPlaceholder:        string
      submitBtn:               string
      submitting:              string
      submitFailed:            string
      footerHint:              string
      statusRequestSubmitted:  string
      statusPendingActivation: string
      statusActionRequired:    string
      defaultPendingMessage:   string
      requestTimeLabel:        string
      contactInstructions:     string
      supportEmail:            string
      manualSetupLink:         string
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
    pricingDetailsTitle: string
    pricingDetailsBody:  string
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
        headerPlaceholder: string
        headerExamplePlaceholder: string
        bodyPlaceholder: string
        bodyExamplePlaceholder: string
        footerPlaceholder: string
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
      headerPlaceholder: string
      bodyPlaceholder: string
      footerPlaceholder: string
      errors: {
        bodyRequired: string
        saveFailed: string
      }
    }
    library: {
      title: string
      subtitle: string
      searchPlaceholder: string
      tags: {
        all:       string
        order_updates: string
        marketing: string
        orders:    string
        shipping:  string
        recovery:  string
        discounts: string
        welcome:   string
      }
      emptyState: string
      stepLabel: string
      delayAfter: string
      delayDays: string
      delayHours: string
      delayMinutes: string
      discountBadge: string
      discountWithCoupon: string
      servicePurposeLabel: string
      previewLabel: string
      variablesLabel: string
      importing: string
      importCustomize: string
      importCustomizeCta: string
      importedEditable: string
      importedDone: string
      copyCodeDynamicLabel: string
      dynamicSuffix: string
      errors: {
        importFailed: string
      }
    }
    preview: {
      title: string
      arabicNameLabel: string
      edit: string
      save: string
      notSet: string
      namePlaceholder: string
      metaNameLabel: string
      categoryLabel: string
      statusLabel: string
      setActive: string
      disable: string
      enable: string
      unlinkService: string
      whenUsedTitle: string
      outsideStrong: string
      outsideBody: string
      insideStrong: string
      insideBody: string
      compatibilityTitle: string
      supportedFeaturesPrefix: string
      varMappingTitle: string
      varMappingIntro: string
      mappingArrow: string
      varValuesLabel: string
      varValueFallback: string
      couponIncludedSuffix: string
      toasts: {
        savedName: string
        enabled: string
        disabled: string
        unlinked: string
        setActiveDone: string
        setActiveDeactivatedBefore: string
      }
    }
    row: {
      activeBadge: string
      fallbackBadge: string
      inactiveBadge: string
      withCouponSuffix: string
      linkedTo: string
      linkedToServiceTitlePrefix: string
      manualBadge: string
      manualCouponBadge: string
      manualTooltip: string
      autoBoundBadge: string
      autoBoundTooltip: string
      removeFromNahla: string
    }
    sync: {
      loading: string
      noSyncYet: string
      autoSyncEstimateDefault: string
      refreshTitle: string
      sourceScheduled: string
      sourceManual: string
      lastSyncTitle: string
      successDefault: string
      statSynced: string
      statBound: string
      statFailed: string
      statTotal: string
      relativeJustNow: string
      relativeMinute: string
      relativeMinutes: string
      relativeHour: string
      relativeHours: string
      relativeDay: string
      relativeDays: string
      errors: {
        no_waba_id: string
        no_valid_token: string
        bad_provider_payload: string
        no_provider_data: string
        db_lookup_failed: string
        db_commit_failed: string
        unexpected_failure: string
        read_failed: string
      }
    }
    delete: {
      title: string
      approvedWarning: string
      deletePermanent: string
      deletePermanentHint: string
      removeNahlaOnly: string
      removeNahlaOnlyHint: string
    }
    submitErrors: {
      fallback: string
      activateSubscription: string
      fixWhatsAppConnect: string
    }
    manualCoupon: {
      title: string
      description: string
    }
    page: TemplatesPageExtraLabels
  }

  /** Campaigns page — wizard + list (Wave 1C) */
  campaignsMgmt: {
    wizard: {
      title:              string
      stepProgressMiddle: string
      stepProgressOf:     string
      prev:               string
      next:               string
      steps: {
        goal:      string
        audience:  string
        template:  string
        variables: string
        preview:   string
        testSend:  string
        review:    string
        launch:    string
      }
    }
    step1: {
      loading:      string
      introBefore:  string
      introBold:    string
      introAfter:   string
    }
    step2: {
      loading:           string
      introBefore:       string
      introAfter:        string
      criteriaPrefix:    string
      testListTitle:     string
      testListBadge:     string
      testListDesc:      string
      testListTooltip:   string
      recommendedBadge:  string
      reachableCount:    string
      manualBadge:       string
      manualDesc:        string
      manualTooltip:     string
      excludeTitle:      string
      excludeDesc:       string
    }
    goals: Record<string, { description: string }>
    segments: Record<string, { description: string; criteria: string }>
    testRecipients: {
      label:        string
      description:  string
      criteria:     string
      manualSuffix: string
    }
    step3: {
      loading:            string
      intro:              string
      modeManualPill:     string
      modeAutoPill:       string
      modeManualTitle:    string
      modeAutoTitle:      string
      legendManualDesc:   string
      legendAutoDesc:     string
      groupAuto:          string
      groupManual:        string
      bestForCampaign:    string
      emptyDefault:       string
      emptyNoTemplates:   string
      emptyPending:       string
      emptyRejected:      string
      emptyDraft:         string
      closestTemplate:    string
      closestMeta:        string
      createCtaBefore:    string
      createCtaLink:       string
      createCtaAfter:     string
      /** Maps backend Arabic badge strings → display label (EN block only; AR uses keys as values). */
      badges: Record<string, string>
    }
    step4: {
      noVars:              string
      allAutoTitle:        string
      allAutoDesc:         string
      autoFilledHeader:    string
      manualIntroManual:   string
      manualIntroMixed:    string
      placeholderExample:  string
      dynamicValue:        string
      autoVars: Record<string, { label: string; source: string }>
      manualVarHints: Record<string, string>
    }
    step5: {
      intro:           string
      labelTemplate:   string
      labelLanguage:   string
      labelCategory:   string
    }
    step6: {
      warningBefore:         string
      warningStrong:         string
      warningAfter:          string
      phoneLabel:            string
      sendTest:              string
      mockDataNote:          string
      testSent:              string
      testSimulatedFallback: string
      testFailedFallback:    string
      unexpectedError:       string
    }
    step7: {
      intro:                    string
      summaryGoal:              string
      summarySegment:           string
      summaryTemplate:          string
      summaryLanguage:          string
      segmentCount:             string
      campaignNameLabel:        string
      campaignNamePlaceholder:  string
      scheduleLabel:            string
      scheduleImmediate:        string
      scheduleScheduled:        string
      scheduleDelayed:          string
      delayMinutes:             string
      delayHours:               string
      couponReminderTitle:      string
      couponReminderDesc:       string
      couponManualLabel:        string
      couponManualPlaceholder:  string
      couponManualTplHint:      string
      couponManualGoalHint:     string
      couponAutoLabel:          string
      couponAutoDesc:           string
      couponAutoConfirm:        string
      couponAutoOff:            string
      sendStrategy: {
        title:           string
        qualityBadge:    string
        tooSmall:        string
        batchSizeLabel:  string
        delayBetweenLabel: string
        planTitle:       string
        planSummary:     string
        batchRecipient:  string
        delayNone:       string
        delaySeconds:    string
        delayMinutes:    string
        delayHours:      string
        delayDays:       string
        delay15m:        string
        delay30m:        string
        delay1h:         string
        delay2h:         string
        delay4h:         string
        delay6h:         string
        delay12h:        string
        delay24h:        string
        immediateLabel:  string
        immediateDesc:   string
        adaptiveLabel:   string
        adaptiveDesc:    string
        batchedLabel:    string
        batchedDesc:     string
      }
    }
    step8: {
      readyTitle:           string
      readyDesc:            string
      protectionTitle:      string
      protectionIntro:      string
      protectionBullet1:    string
      protectionBullet2:    string
      protectionBullet3:    string
      protectionFooter:     string
      protectionDaysBadge:  string
      protectionSafeBadge:  string
      protectionIdempotentBadge: string
      summaryCampaignName:  string
      summaryTemplate:      string
      summarySchedule:      string
      summaryCoupon:        string
      scheduleImmediate:    string
      scheduleDelayed:      string
      couponAutoPerCustomer: string
      couponNone:           string
      couponAutoPercent:    string
      launchBtn:            string
      launching:            string
      launchCreateFailed:   string
      launchTimeout:        string
    }
    list: CampaignsListLabels
  }

  /** WhatsApp Catalog page — hub, studio, import, manual entry */
  catalogMgmt: {
    loading: string
    page: {
      title:          string
      productCount:   string
      intro:          string
      importFromMeta: string
      addManual:      string
      resync:         string
    }
    summary: {
      title:            string
      productCount:     string
      sourceLabel:      string
      lastUpdateLabel:  string
      statusLabel:      string
      statusReady:      string
      statusNotReady:   string
      statusEmpty:      string
      lastImportNever:  string
      moreActions:      string
    }
    channels: {
      title:              string
      whatsapp:           string
      ai:                 string
      campaigns:          string
      google:             string
      statusReady:        string
      statusAvailableAi:  string
      statusAvailableCampaigns: string
      statusNeedsAction:  string
      statusNotConnected: string
      statusComingSoon:   string
    }
    advanced: {
      title:                    string
      structureTitle:           string
      catalogStatusTitle:       string
      linkStatusTitle:          string
      bindingSettingsTitle:     string
      commerceDiagnosticsTitle: string
      catalogToolsTitle:        string
      manualProductsTitle:      string
      metaImportTitle:          string
      testSendTitle:            string
    }
    diagnostics: {
      title:                string
      readyTitle:           string
      notReadyTitle:        string
      metaLinked:           string
      metaNotLinked:        string
      productSource:        string
      sourceBreakdownTitle: string
      noProductsYet:        string
      coverageTitle:        string
      coverageDesc:         string
      coverageHint:         string
      channelTitle:         string
      channelConnected:     string
      channelNotConnected:  string
      importTitle:          string
      importNever:          string
      importLastAt:         string
      importStatusRunning:  string
      importStatusSuccess:  string
      importStatusDiscoveryOnly: string
      importDiscoveryOnlyHint: string
      importStatusFailed:   string
      importCounts:         string
      graphTokenSource:     string
      commerceReadyTitle:   string
      commerceNotReadyTitle: string
      missingRequirements:  string
      checkLabels: {
        whatsapp_connected:       string
        phone_number_id:          string
        meta_catalog_id:          string
        catalog_enabled:          string
        graph_token_available:    string
        products_with_retailer_id: string
      }
    }
    connectionStatus: {
      title:                  string
      whatsappLabel:          string
      notLinked:              string
      catalogIdLabel:         string
      retailerCoverageLabel:  string
    }
    studioSection: {
      title: string
      intro: string
    }
    channelBinding: {
      title:          string
      intro:          string
      catalogIdLabel: string
      catalogIdHint:  string
      catalogIdPh:    string
      enableTitle:    string
      enableDesc:     string
      enabled:        string
      disabled:       string
      save:           string
    }
    wabaLinkStatus: {
      loading:              string
      refresh:              string
      fetchFailed:          string
      linkedTitle:          string
      linkedDesc:           string
      linkedDisclaimer:     string
      linkedManualCheck:    string
      linkedBadge:          string
      catalogNameLabel:     string
      catalogIdLabel:       string
      wabaConnectedLabel:   string
      wabaConnectedValue:   string
      noneTitle:            string
      noneDesc:             string
      noneGuidance:         string
      linkComingSoon:       string
      linkCtaDisabled:      string
      useThisCatalog:       string
      useLinkedCatalog:     string
      currentlyInUse:       string
      switchConfirm:        string
      catalogAppliedSuccess: string
      catalogApplyFailed:   string
      singleCatalogRecommendation: string
      mismatchTitle:        string
      mismatchDesc:         string
      expectedCatalogLabel: string
      linkedCatalogsLabel:  string
      missingTitle:         string
      missingConnection:    string
      missingWaba:          string
      missingCatalogId:     string
      missingToken:         string
      metaErrorTitle:       string
      wabaInaccessibleTitle: string
      wabaInaccessibleDesc:  string
      wabaCatalogExistsNote: string
      wabaNotFoundTitle:     string
      wabaNotFoundDesc:      string
      showTechnicalDetails:  string
      hideTechnicalDetails:  string
    }
    tools: {
      title:         string
      coverageLabel: string
      coverageDesc:  string
      resyncBtn:     string
      reportTitle:   string
      scanned:       string
      assigned:      string
      alreadySet:    string
      synthetic:     string
      published:     string
      errors:        string
    }
    manual: {
      title:            string
      modalTitle:       string
      explainerStore:   string
      explainerNoStore: string
      addNew:           string
      productName:      string
      productNamePh:    string
      price:            string
      pricePh:          string
      currencyLabel:    string
      imageUrl:         string
      imageLabel:       string
      imageDropHint:    string
      imageChoose:      string
      imageRemove:      string
      imageRequired:    string
      imageUploading:   string
      imageUploadFailed:string
      imageTypeInvalid: string
      imageTooLarge:    string
      productUrl:       string
      metaRidLabel:     string
      metaRidHint:      string
      metaRidPh:        string
      description:      string
      descriptionPh:    string
      availability:     string
      inStock:          string
      outOfStock:       string
      sku:              string
      skuPh:            string
      additionalOptions:string
      priceRequired:    string
      priceInvalid:     string
      unsavedCloseTitle:string
      unsavedCloseMessage: string
      unsavedCloseConfirm: string
      cancel:           string
      save:             string
      saving:           string
      nameRequired:     string
    }
    metaImport: {
      title:          string
      intro:          string
      importBtn:      string
      hideDetail:     string
      showDetail:     string
      reportTitle:    string
      truncated:      string
      scanned:        string
      created:        string
      updated:        string
      skippedManual:  string
      reportErrors:   string
      pages:          string
      errors: {
        connection_not_found:      string
        catalog_id_missing:        string
        access_token_missing:      string
        meta_access_token_missing: string
        catalog_not_found:         string
        catalog_type_unsupported:  string
        meta_http_error:           string
        defaultUnexpected:         string
      }
    }
    importedProducts: {
      title:        string
      intro:        string
      count:        string
      loading:      string
      loadFailed:   string
      emptyTitle:   string
      emptyDesc:    string
      colProduct:   string
      colPrice:     string
      colRetailerId: string
      colSource:    string
      noImage:      string
      discountedPriceBadge: string
    }
    testSend: {
      title:                string
      intro:                string
      phonePlaceholder:     string
      titlePlaceholder:     string
      productIdPlaceholder: string
      sendBtn:              string
      resultTitle:          string
      productLabel:         string
      catalogLine:          string
      imageLine:            string
      ctaLine:              string
      succeeded:            string
      failed:               string
      notAttempted:         string
    }
    hub: {
      title:          string
      advancedTitle:  string
      intro:          string
      inputsLabel:    string
      outputsLabel:   string
      sourcesLabel:   string
      channelsLabel:  string
      nahlaCatalog:   string
      unifiedSource:  string
      productCount:   string
      sources: {
        salla:           string
        meta:            string
        manual:          string
        shopifyPlanned:  string
      }
      channels: {
        whatsapp:       string
        ai:             string
        campaigns:      string
        googlePlanned:  string
      }
      subtitles: {
        sallaUnlinked:          string
        sallaCount:             string
        metaImported:           string
        metaReadyToImport:      string
        metaNotLinked:          string
        manualCount:            string
        manualAlwaysAvailable:  string
        shopifyPlanned:         string
        whatsappReady:          string
        whatsappNeedsCatalogId: string
        whatsappConnectFirst:   string
        aiReadsCatalog:         string
        aiNeedsProducts:        string
        campaignsAvailable:     string
        campaignsNeedsProducts: string
        googlePlanned:          string
      }
      nodeStatus: {
        live:      string
        active:    string
        available: string
        unused:    string
        planned:   string
      }
    }
    sources: {
      salla:        string
      zid:          string
      meta:         string
      manual:       string
      nahla_native: string
      unknown:      string
      mixed:        string
    }
    messages: {
      resyncSuccess:         string
      resyncFailed:          string
      loadFailed:            string
      saveFailed:            string
      settingsAlreadySaved:  string
      settingsSaved:         string
      testFailed:            string
      catalogIdRequired:     string
      addProductSuccess:     string
      addProductFailed:      string
      unexpectedImport:      string
    }
    testResult: {
      catalogSucceeded: string
      catalogFailed:    string
      imageOk:          string
      imageFailed:      string
      ctaOk:            string
      ctaFailed:        string
    }
    studio: {
      filters: {
        searchPlaceholder: string
        allSources:        string
        imageAll:          string
        imageYes:          string
        imageNo:           string
        retailerIdAll:     string
        retailerIdYes:     string
        retailerIdNo:      string
        stockAll:          string
        stockYes:          string
        stockNo:           string
        visibilityAll:     string
        visibilityHidden:  string
        visibilityRemoved: string
        visibilityArchived:string
        visibilityEvery:   string
        clear:             string
        showing:           string
      }
      variantsSummary: {
        products:       string
        variants:       string
        whatsappReady:  string
        metaReady:      string
        googleReady:    string
        needsReview:    string
      }
      variantsDrawer: {
        noVariants:  string
        option:      string
        sku:         string
        price:       string
        stock:       string
        retailerId:  string
        status:      string
        missing:     string
        inStock:     string
        outOfStock:  string
      }
      grid: {
        loading:           string
        emptyTitle:        string
        emptyDesc:         string
        importFromMeta:    string
        addManual:         string
        colProduct:        string
        colSource:         string
        colPrice:          string
        colStock:          string
        colRetailerId:     string
        colReadiness:      string
        hideVariants:      string
        showVariants:      string
        noVariantsTooltip: string
        variantsBadge:     string
        missing:           string
        inStock:           string
        outOfStock:        string
      }
      readiness: {
        missingInChannels: string
        readyWithWarn:     string
        ready:             string
        readySimple:       string
        needsCompletion:   string
      }
      pagination: {
        pageOf: string
        prev:   string
        next:   string
      }
      channelBadge: {
        planned:       string
        missing:       string
        readyWarn:     string
        ready:         string
        readyWhatsappData: string
        readyDataOnly:     string
        futureChannel: string
      }
      readinessPanel: {
        issuesTitle: string
        moreIssues:  string
      }
      drawer: {
        loading:           string
        defaultTitle:      string
        loadingData:       string
        readinessTitle:    string
        productDataTitle:  string
        saleLabel:         string
        retailerIdLabel:   string
        storePage:         string
        autoSaveNote:      string
        autoSaveIdle:      string
        autoSavePending:   string
        autoSaveSaving:    string
        autoSaveSaved:     string
        autoSaveFailed:    string
        variantsTitle:     string
        variantsPhase2Note: string
        fields: {
          title:        string
          description:  string
          price:        string
          salePrice:    string
          currency:     string
          availability: string
          imageUrl:     string
          productUrl:   string
          brand:        string
          category:     string
          condition:    string
          gtin:         string
          mpn:          string
        }
        placeholders: {
          currency:     string
          availability: string
          condition:    string
        }
        hideBtn:           string
        restoreBtn:        string
        hideConfirm:       string
        hideSuccess:       string
        hideFailed:        string
        restoreSuccess:    string
        restoreFailed:     string
        statusRemovedMeta: string
        statusHidden:      string
        ownershipNahlaManaged:    string
        ownershipExternalManaged: string
        ownershipMetaReadonly:    string
        readOnlyNote:             string
        deleteBtn:                string
        deleteConfirm:            string
        deleteFailed:             string
        priceHelper:              string
        priceInvalid:             string
        metaSyncBtn:              string
        metaSyncDryRunNote:       string
        metaSyncBlockedExternal:  string
        metaSyncRunning:          string
        metaSyncFailed:           string
        metaSyncTitle:            string
        metaSyncFatalTitle:       string
        metaSyncWarningsTitle:    string
        metaSyncCatalogId:        string
        metaSyncRetailerId:       string
        metaSyncConfirmBtn:       string
        metaSyncConfirmModal:     string
        metaSyncConfirmRunning:   string
        metaSyncConfirmFailed:    string
        metaSyncConfirmSuccess:   string
        metaSyncStatusTitle:      string
        metaSyncPending:          string
        metaSyncSyncing:          string
        metaSyncBlocked:          string
        metaSyncStateFailed:      string
        metaSyncSynced:           string
        metaSyncWabaLinked:       string
        metaSyncWabaUncertain:    string
        metaSyncWhatsappNotVerified: string
        metaSyncVisibleWhatsapp:  string
        metaSyncRetryBtn:         string
        metaSyncRetryRunning:     string
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
