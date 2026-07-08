import type { Lang } from '../../i18n/types'

export interface WhatsAppDemoMessage {
  id: number
  role: 'customer' | 'nahla'
  lines: string[]
  time: string
  hasButton?: boolean
  paymentMethods?: string
  helpLine?: string
}

export interface WhatsAppDemoCopy {
  brandLabel: string
  online: string
  today: string
  completeOrder: string
  typeMessage: string
  messages: WhatsAppDemoMessage[]
}

export const WHATSAPP_DEMO_COPY: Record<Lang, WhatsAppDemoCopy> = {
  ar: {
    brandLabel: 'نحلة 🐝',
    online: 'متصل الآن',
    today: 'اليوم',
    completeOrder: 'إتمام الطلب',
    typeMessage: 'اكتب رسالة…',
    messages: [
      {
        id: 1,
        role: 'customer',
        lines: ['السلام عليكم، هل هذا المنتج متوفر؟'],
        time: '10:32',
      },
      {
        id: 2,
        role: 'nahla',
        lines: [
          'وعليكم السلام 😊',
          'نعم المنتج متوفر حالياً.',
          'السعر: *129 ريال*',
          'هل ترغب أن أرسل لك رابط الشراء الآن؟',
        ],
        time: '10:32',
      },
      {
        id: 3,
        role: 'customer',
        lines: ['نعم أرسله لي'],
        time: '10:33',
      },
      {
        id: 4,
        role: 'nahla',
        lines: ['رائع 👍', 'يمكنك إتمام الطلب مباشرة من هنا:'],
        time: '10:33',
        hasButton: true,
        paymentMethods: 'بطاقة ائتمانية · Apple Pay · تحويل بنكي',
        helpLine: 'وإذا احتجت أي مساعدة أنا هنا لخدمتك. 🤝',
      },
    ],
  },
  en: {
    brandLabel: 'Nahla 🐝',
    online: 'Online',
    today: 'Today',
    completeOrder: 'Complete order',
    typeMessage: 'Type a message…',
    messages: [
      {
        id: 1,
        role: 'customer',
        lines: ['Hi, is this product available?'],
        time: '10:32',
      },
      {
        id: 2,
        role: 'nahla',
        lines: [
          'Hello 😊',
          'Yes, it is in stock.',
          'Price: *129 SAR*',
          'Would you like me to send you the checkout link?',
        ],
        time: '10:32',
      },
      {
        id: 3,
        role: 'customer',
        lines: ['Yes, please send it'],
        time: '10:33',
      },
      {
        id: 4,
        role: 'nahla',
        lines: ['Great 👍', 'You can complete your order here:'],
        time: '10:33',
        hasButton: true,
        paymentMethods: 'Card · Apple Pay · Bank transfer',
        helpLine: 'If you need any help, I am here for you. 🤝',
      },
    ],
  },
}
