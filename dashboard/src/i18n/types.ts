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
      main:  string
      ai:    string
      store: string
    }
    items: {
      overview:      string
      conversations: string
      orders:        string
      coupons:       string
      campaigns:     string
      templates:     string
      automations:   string
      intelligence:  string
      analyticsAI:   string
      integrations:  string
      settings:      string
    }
    storeBadge: { plan: string }
    logoTagline: string
  }

  /** Topbar */
  topbar: {
    searchPlaceholder: string
    notifications:     string
    admin:             string
  }

  /** Page titles & subtitles */
  pages: {
    overview:      { title: string; subtitle: string }
    conversations: { title: string; subtitle: string }
    orders:        { title: string; subtitle: string }
    coupons:       { title: string; subtitle: string }
    campaigns:     { title: string; subtitle: string }
    templates:     { title: string; subtitle: string }
    automations:   { title: string; subtitle: string }
    intelligence:  { title: string; subtitle: string }
    analytics:     { title: string; subtitle: string }
    integrations:  { title: string; subtitle: string }
    settings:      { title: string; subtitle: string }
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
}

/** Supported language codes */
export type Lang = 'ar' | 'en'
