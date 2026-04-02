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
      templates:     'قوالب واتساب',
      automations:   'التشغيل التلقائي',
      intelligence:  'نحلة الذكية',
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
      subtitle: 'محادثات واتساب وردود نحلة الذكية',
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
    templates: {
      title:    'قوالب واتساب',
      subtitle: 'إدارة القوالب المعتمدة من Meta وإنشاء قوالب جديدة',
    },
    automations: {
      title:    'التشغيل التلقائي الذكي',
      subtitle: 'أتمتة تسويقية مبنية على سلوك العملاء',
    },
    intelligence: {
      title:    'نحلة الذكية',
      subtitle: 'رؤى تنبؤية وتوصيات تسويقية مبنية على الذكاء الاصطناعي',
    },
    analytics: {
      title:    'التحليلات والذكاء',
      subtitle: 'الإيرادات والتحويل والمنتجات الأكثر مبيعاً',
    },
    integrations: {
      title:    'التكاملات',
      subtitle: 'ربط سلة وزد وواتساب بنحلة',
    },
    settings: {
      title:    'الإعدادات',
      subtitle: 'إعدادات المتجر وصلاحيات الذكاء الاصطناعي',
    },
  },

  actions: {
    export:        'تصدير',
    newCoupon:     'كوبون جديد',
    newCampaign:   'حملة جديدة',
    newTemplate:   'قالب جديد',
    syncTemplates: 'مزامنة من Meta',
    viewAll:       'عرض الكل',
    save:          'حفظ التغييرات',
    saved:         'تم الحفظ!',
    cancel:        'إلغاء',
    search:        'بحث',
    filter:        'تصفية',
  },
}

export default ar
