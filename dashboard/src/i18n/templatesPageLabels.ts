/**
 * WhatsApp Templates page — service catalog + default template metadata (static UI).
 */
export interface TemplateServiceLabels {
  name: string
  description: string
}

export interface TemplateDefaultMetaLabels {
  purposeLabel: string
  automationLabel: string
  varLabels: Record<string, string>
}

export interface TemplatesPageExtraLabels {
  urlPlaceholder: string
  footerDefault: string
  services: Record<string, TemplateServiceLabels>
  defaultTemplates: Record<string, TemplateDefaultMetaLabels>
}

export const SERVICE_COLOR: Record<string, string> = {
  cart_recovery:        'amber',
  order_confirmation:   'blue',
  cod_confirmation:     'emerald',
  shipping_tracking:    'violet',
  post_delivery:        'yellow',
  predictive_reorder:   'teal',
  marketing_campaigns:  'pink',
  welcome_onboarding:   'sky',
  customer_support:     'slate',
  customer_retention:   'orange',
  payment_reminder:     'rose',
  customer_engagement:  'cyan',
  vip_rewards:          'purple',
}

export const SERVICE_ICON: Record<string, string> = {
  cart_recovery:        '🛒',
  order_confirmation:   '📦',
  cod_confirmation:     '💰',
  shipping_tracking:    '🚚',
  post_delivery:        '⭐',
  predictive_reorder:   '🔄',
  marketing_campaigns:  '📢',
  welcome_onboarding:   '👋',
  customer_support:     '💬',
  customer_retention:   '💛',
  payment_reminder:     '💳',
  customer_engagement:  '💡',
  vip_rewards:          '👑',
}

export const templatesPageExtraEn: TemplatesPageExtraLabels = {
  urlPlaceholder: 'https://...',
  footerDefault: '🐝 Nahla — your store assistant',
  services: {
    cart_recovery: {
      name: 'Abandoned cart recovery',
      description: 'Remind customers who added items but did not complete checkout',
    },
    order_confirmation: {
      name: 'Order confirmation',
      description: 'Notify the customer that their order was received with a summary',
    },
    cod_confirmation: {
      name: 'Cash on delivery confirmation',
      description: 'Verify customer intent for cash-on-delivery orders',
    },
    shipping_tracking: {
      name: 'Shipping & tracking',
      description: 'Keep the customer updated on shipment status',
    },
    post_delivery: {
      name: 'Post-delivery follow-up',
      description: 'Improve experience after delivery and ask for a review',
    },
    predictive_reorder: {
      name: 'Predictive reorder',
      description: 'Remind customers to repurchase before they run out',
    },
    marketing_campaigns: {
      name: 'Marketing campaigns',
      description: 'Send promotions, discount codes, and new product announcements',
    },
    welcome_onboarding: {
      name: 'Customer welcome',
      description: 'Welcome new customers on first contact or store signup',
    },
    customer_support: {
      name: 'Customer support',
      description: 'Follow up after resolving issues and confirm satisfaction',
    },
    customer_retention: {
      name: 'Win back inactive customers',
      description: 'Re-engage customers who have not purchased in a while',
    },
    payment_reminder: {
      name: 'Payment reminder',
      description: 'Remind customers to complete pending payments',
    },
    customer_engagement: {
      name: 'Customer engagement',
      description: 'Follow up with customers interested in specific products',
    },
    vip_rewards: {
      name: 'VIP rewards',
      description: 'Exclusive offers and rewards for VIP customers',
    },
  },
  defaultTemplates: {
    order_status_update_ar: {
      purposeLabel: 'Order status update notification',
      automationLabel: 'Order notifications',
      varLabels: {
        '{{1}}': 'Customer name',
        '{{2}}': 'Order number',
        '{{3}}': 'Order status',
      },
    },
    cod_order_confirmation_ar: {
      purposeLabel: 'Cash order confirmation',
      automationLabel: 'Cash on delivery orders',
      varLabels: {
        '{{1}}': 'Customer name',
        '{{2}}': 'Product name',
        '{{3}}': 'Order amount',
      },
    },
    predictive_reorder_reminder_ar: {
      purposeLabel: 'Predictive reorder reminder',
      automationLabel: 'Predictive reorder',
      varLabels: {
        '{{1}}': 'Customer name',
        '{{2}}': 'Product name',
        '{{3}}': 'Reorder link',
      },
    },
  },
}

export const templatesPageExtraAr: TemplatesPageExtraLabels = {
  urlPlaceholder: 'https://...',
  footerDefault: '🐝 نحلة — مساعد متجرك',
  services: {
    cart_recovery: {
      name: 'استرجاع السلات المتروكة',
      description: 'تذكير العملاء الذين أضافوا منتجات لسلتهم دون إكمال الطلب',
    },
    order_confirmation: {
      name: 'تأكيد الطلب',
      description: 'إشعار العميل بتأكيد واستلام طلبه مع ملخص التفاصيل',
    },
    cod_confirmation: {
      name: 'تأكيد الدفع عند الاستلام',
      description: 'التحقق من جدية العميل في طلبات الدفع عند الاستلام',
    },
    shipping_tracking: {
      name: 'الشحن وتتبع الطلب',
      description: 'إبقاء العميل على اطلاع بحالة شحن طلبه',
    },
    post_delivery: {
      name: 'ما بعد التسليم',
      description: 'تعزيز تجربة العميل بعد استلام الطلب وطلب تقييمه',
    },
    predictive_reorder: {
      name: 'إعادة الطلب التنبؤية',
      description: 'تذكير العملاء بإعادة شراء منتجات عند توقع نفادها',
    },
    marketing_campaigns: {
      name: 'الحملات التسويقية',
      description: 'إرسال عروض ترويجية وأكواد خصم وإعلانات المنتجات الجديدة',
    },
    welcome_onboarding: {
      name: 'الترحيب بالعملاء',
      description: 'ترحيب بالعملاء الجدد عند أول تواصل أو تسجيل في المتجر',
    },
    customer_support: {
      name: 'خدمة العملاء',
      description: 'متابعة العملاء بعد حل مشكلاتهم والتأكد من رضاهم',
    },
    customer_retention: {
      name: 'استرجاع العملاء غير النشطين',
      description: 'تحفيز العملاء الذين لم يشتروا منذ فترة على العودة',
    },
    payment_reminder: {
      name: 'تذكير بالدفع',
      description: 'تذكير العملاء بإكمال دفع الطلبات المعلقة',
    },
    customer_engagement: {
      name: 'تفاعل العملاء',
      description: 'متابعة العملاء المهتمين بمنتجات معينة',
    },
    vip_rewards: {
      name: 'مكافآت العملاء المميزين',
      description: 'عروض حصرية ومكافآت لعملاء VIP',
    },
  },
  defaultTemplates: {
    order_status_update_ar: {
      purposeLabel: 'إشعار تحديث حالة الطلب',
      automationLabel: 'إشعارات الطلبات',
      varLabels: {
        '{{1}}': 'اسم العميل',
        '{{2}}': 'رقم الطلب',
        '{{3}}': 'حالة الطلب',
      },
    },
    cod_order_confirmation_ar: {
      purposeLabel: 'تأكيد الطلب النقدي',
      automationLabel: 'الطلبات بالدفع عند الاستلام',
      varLabels: {
        '{{1}}': 'اسم العميل',
        '{{2}}': 'اسم المنتج',
        '{{3}}': 'مبلغ الطلب',
      },
    },
    predictive_reorder_reminder_ar: {
      purposeLabel: 'تذكير إعادة الطلب التنبؤي',
      automationLabel: 'predictive_reorder',
      varLabels: {
        '{{1}}': 'اسم العميل',
        '{{2}}': 'اسم المنتج',
        '{{3}}': 'رابط إعادة الطلب',
      },
    },
  },
}

export function serviceInfoForKey(
  serviceKey: string | null | undefined,
  page: TemplatesPageExtraLabels,
): { name: string; description: string; icon: string; color: string } | null {
  if (!serviceKey || !page.services[serviceKey]) return null
  const svc = page.services[serviceKey]
  return {
    name: svc.name,
    description: svc.description,
    icon: SERVICE_ICON[serviceKey] ?? '📋',
    color: SERVICE_COLOR[serviceKey] ?? 'amber',
  }
}
