import type { Translations } from './types'

const en: Translations = {
  meta: {
    code:  'en',
    label: 'English',
    dir:   'ltr',
  },

  nav: {
    groups: {
      main:  'Main',
      ai:    'AI',
      store: 'Store',
    },
    items: {
      overview:      'Overview',
      conversations: 'Conversations',
      orders:        'Orders',
      coupons:       'Coupons',
      campaigns:     'Campaigns',
      analyticsAI:   'Analytics & AI Logs',
      integrations:  'Integrations',
      settings:      'Settings',
    },
    storeBadge: {
      plan: 'Growth Plan',
    },
    logoTagline: 'Store Intelligence',
  },

  topbar: {
    searchPlaceholder: 'Search…',
    notifications:     'Notifications',
    admin:             'Admin',
  },

  pages: {
    overview: {
      title:    'Overview',
      subtitle: 'Store performance at a glance',
    },
    conversations: {
      title:    'Conversations',
      subtitle: 'WhatsApp chats & Nahla AI responses',
    },
    orders: {
      title:    'Orders',
      subtitle: 'AI-created orders and payment links',
    },
    coupons: {
      title:    'Coupons',
      subtitle: 'Discount codes, VIP tiers, and auto-generation rules',
    },
    campaigns: {
      title:    'Campaigns',
      subtitle: 'WhatsApp broadcasts, abandoned cart recovery, and VIP targeting',
    },
    analytics: {
      title:    'Analytics & AI',
      subtitle: 'Revenue, conversion & top products',
    },
    integrations: {
      title:    'Integrations',
      subtitle: 'Salla, Zid & WhatsApp API connections',
    },
    settings: {
      title:    'Settings',
      subtitle: 'Store settings, AI permissions & billing',
    },
  },

  actions: {
    export:      'Export',
    newCoupon:   'New Coupon',
    newCampaign: 'New Campaign',
    viewAll:     'View all',
    save:        'Save Changes',
    saved:       'Saved!',
    cancel:      'Cancel',
    search:      'Search',
    filter:      'Filter',
  },
}

export default en
