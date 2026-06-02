/**
 * Landing page pricing section — Arabic and English copy.
 * Outcome-focused feature lists for Starter / Growth / Scale plans.
 */

export interface LandingPlanPricingLabels {
  name: string
  nameDisplay: string
  price: number
  launchPrice: number
  tagline: string
  idealFor: string
  features: string[]
  ctaLabel: string
}

export interface LandingPricingLabels {
  popularBadge: string
  securePayment: string
  defaultCta: string
  perMonth: string
  currency: string
  plans: {
    starter: LandingPlanPricingLabels
    growth: LandingPlanPricingLabels
    scale: LandingPlanPricingLabels
  }
}

export const landingPricingAr: LandingPricingLabels = {
  popularBadge: 'الأكثر شعبية',
  securePayment: 'دفع آمن — لا تُطلب بطاقة للتجربة',
  defaultCta: 'ابدأ مجاناً 14 يوم',
  perMonth: 'ريال / شهرياً',
  currency: 'ريال',
  plans: {
    starter: {
      name: 'STARTER',
      nameDisplay: 'الأساسية',
      price: 899,
      launchPrice: 449,
      tagline: 'سعر الإطلاق — وفّر 450 ريال شهرياً',
      idealFor: 'ابدأ البيع بالذكاء الاصطناعي على واتساب أعمالك',
      features: [
        'واتساب الأعمال على الجوال + الذكاء + الحملات',
        'طيار آلي للمبيعات والردود وخدمة العملاء',
        'حملات واتساب غير محدودة',
        'استرجاع السلات المتروكة تلقائيًا',
        'إشعارات الطلبات التلقائية',
        'كوبونات خصم تلقائية',
        'حتى 5,000 محادثة شهريًا',
      ],
      ctaLabel: 'ابدأ مجاناً 14 يوم',
    },
    growth: {
      name: 'GROWTH',
      nameDisplay: 'النمو',
      price: 1699,
      launchPrice: 849,
      tagline: 'سعر الإطلاق — وفّر 850 ريال شهرياً',
      idealFor: 'للمتاجر التي تريد زيادة المبيعات والأتمتة',
      features: [
        'واتساب الأعمال على الجوال + الذكاء + الحملات',
        'حملات وتسلسلات واتساب متقدمة',
        'تأكيد الدفع عند الاستلام تلقائيًا',
        'إرسال روابط الدفع المباشرة للعملاء',
        'لوحة تحليلات ومبيعات بالذكاء الاصطناعي',
        'أتمتة متقدمة لزيادة المبيعات',
        'حتى 15,000 محادثة شهريًا',
      ],
      ctaLabel: 'جرّب الخطة الأكثر شيوعاً',
    },
    scale: {
      name: 'SCALE',
      nameDisplay: 'التوسع',
      price: 2999,
      launchPrice: 1499,
      tagline: 'سعر الإطلاق — وفّر 1,500 ريال شهرياً',
      idealFor: 'للعلامات التجارية والمتاجر سريعة النمو',
      features: [
        'واتساب الأعمال على الجوال + الذكاء + الحملات',
        'محادثات غير محدودة',
        'إنشاء الطلبات تلقائيًا من واتساب',
        'طيار آلي كامل للمبيعات وخدمة العملاء',
        'فرق عمل وصلاحيات متعددة',
        'API وربط مخصص للأنظمة',
        'تقارير وتحليلات متقدمة للشركات',
        'مزامنة المنتجات مع Meta وGoogle وYouTube قريبًا',
        'أولوية قصوى في الدعم والمعالجة',
      ],
      ctaLabel: 'تحدث مع فريق المبيعات',
    },
  },
}

export const landingPricingEn: LandingPricingLabels = {
  popularBadge: 'Most Popular',
  securePayment: 'Secure checkout — no card required for trial',
  defaultCta: 'Start 14-day free trial',
  perMonth: 'SAR / month',
  currency: 'SAR',
  plans: {
    starter: {
      name: 'STARTER',
      nameDisplay: 'Starter',
      price: 899,
      launchPrice: 449,
      tagline: 'Launch price — save 450 SAR/month',
      idealFor: 'Start selling with AI on your WhatsApp Business',
      features: [
        'WhatsApp Business on your phone + AI + campaigns',
        'AI autopilot for sales and customer replies',
        'Unlimited WhatsApp campaigns',
        'Automatic abandoned cart recovery',
        'Automatic order notifications',
        'Automatic discount coupons',
        'Up to 5,000 conversations per month',
      ],
      ctaLabel: 'Start 14-day free trial',
    },
    growth: {
      name: 'GROWTH',
      nameDisplay: 'Growth',
      price: 1699,
      launchPrice: 849,
      tagline: 'Launch price — save 850 SAR/month',
      idealFor: 'For stores that want more sales and automation',
      features: [
        'WhatsApp Business on your phone + AI + campaigns',
        'Advanced WhatsApp marketing flows',
        'Automatic COD confirmation',
        'Direct payment links',
        'AI sales and analytics dashboard',
        'Advanced sales automation',
        'Up to 15,000 conversations per month',
      ],
      ctaLabel: 'Try the most popular plan',
    },
    scale: {
      name: 'SCALE',
      nameDisplay: 'Scale',
      price: 2999,
      launchPrice: 1499,
      tagline: 'Launch price — save 1,500 SAR/month',
      idealFor: 'For brands and fast-growing stores',
      features: [
        'WhatsApp Business on your phone + AI + campaigns',
        'Unlimited conversations',
        'Automatic order creation from WhatsApp',
        'Full AI autopilot for sales and customer service',
        'Multi-user teams and permissions',
        'API and custom integrations',
        'Advanced business reports and analytics',
        'Product sync with Meta, Google and YouTube — coming soon',
        'Highest priority support',
      ],
      ctaLabel: 'Talk to sales',
    },
  },
}
