/**
 * Master translation shape.
 * Every key here must be present in both ar.ts and en.ts.
 *
 * Scope intentionally limited to layout + common UI.
 * Page-level content translations are added per-page as the product grows.
 */
export interface Translations {
  /** Language metadata */
  meta: {
    code: string          // 'ar' | 'en'
    label: string         // 'العربية' | 'English'
    dir: 'rtl' | 'ltr'
  }

  /** Sidebar navigation */
  nav: {
    groups: {
      main:  string  // e.g. 'الرئيسية' | 'Main'
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
    storeBadge: {
      plan: string
    }
    logoTagline: string
  }

  /** Topbar */
  topbar: {
    searchPlaceholder: string
    notifications:     string
    admin:             string
  }

  /** Page titles & subtitles (used in PageHeader + Layout pageMeta) */
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
}

/** Supported language codes */
export type Lang = 'ar' | 'en'
