import type { Translations } from './types'

/**
 * Arabic translations — primary language for Nahla SaaS.
 * Terms are chosen for a Saudi/Gulf e-commerce context, not literal translation.
 */
const ar: Translations = {
  meta: {
    code:  'ar',
    label: 'العربية',
    dir:   'rtl',
  },

  nav: {
    groups: {
      main:  'الرئيسية',
      ai:    'الذكاء الاصطناعي',
      store: 'المتجر',
    },
    items: {
      overview:      'نظرة عامة',
      conversations: 'المحادثات',
      orders:        'الطلبات',
      coupons:       'الكوبونات',
      campaigns:     'الحملات',
      analyticsAI:   'التحليلات وسجلات الذكاء',
      integrations:  'التكاملات',
      settings:      'الإعدادات',
    },
    storeBadge: {
      plan: 'خطة النمو',
    },
    logoTagline: 'ذكاء المتجر',
  },

  topbar: {
    searchPlaceholder: 'ابحث...',
    notifications:     'الإشعارات',
    admin:             'المدير',
  },

  pages: {
    overview: {
      title:    'نظرة عامة',
      subtitle: 'أداء متجرك دفعة واحدة',
    },
    conversations: {
      title:    'المحادثات',
      subtitle: 'محادثات واتساب وردود نهلة الذكية',
    },
    orders: {
      title:    'الطلبات',
      subtitle: 'الطلبات المنشأة بالذكاء الاصطناعي وروابط الدفع',
    },
    coupons: {
      title:    'الكوبونات',
      subtitle: 'أكواد الخصم وشرائح VIP وقواعد الإنشاء التلقائي',
    },
    campaigns: {
      title:    'الحملات',
      subtitle: 'حملات واتساب واسترداد العربات المتروكة واستهداف VIP',
    },
    analytics: {
      title:    'التحليلات والذكاء',
      subtitle: 'الإيرادات والتحويل والمنتجات الأكثر مبيعاً',
    },
    integrations: {
      title:    'التكاملات',
      subtitle: 'ربط سلة وزد وواتساب بنهلة',
    },
    settings: {
      title:    'الإعدادات',
      subtitle: 'إعدادات المتجر وصلاحيات الذكاء الاصطناعي',
    },
  },

  actions: {
    export:      'تصدير',
    newCoupon:   'كوبون جديد',
    newCampaign: 'حملة جديدة',
    viewAll:     'عرض الكل',
    save:        'حفظ التغييرات',
    saved:       'تم الحفظ!',
    cancel:      'إلغاء',
    search:      'بحث',
    filter:      'تصفية',
  },
}

export default ar
