/**
 * Single source of truth for official Nahlah company / contact information.
 * Used across public pages for Meta Business Verification consistency.
 */
export const COMPANY_INFO = {
  product: {
    nameEn: 'Nahlah AI',
    nameAr: 'نحلة AI',
  },
  legal: {
    nameEn: 'Nahlah Ai Establishment',
    nameAr: 'مؤسسة نحلة أي آي',
  },
  nationalUnifiedNumber: '7050202485',
  website: {
    url: 'https://nahlah.ai',
    display: 'nahlah.ai',
  },
  email: 'info@nahlah.ai',
  phone: {
    display: '+966 55 590 6901',
    href: 'tel:+966555906901',
  },
  address: {
    ar: 'الحلقة الغربية 1، حي الحلقة الغربية، الطائف 26563، المملكة العربية السعودية.',
    enLines: [
      'Al Halaqa Western 1,',
      'Al Halqah Al Gharbia District,',
      'At Taif 26563,',
      'Kingdom of Saudi Arabia.',
    ] as const,
  },
  legalStatement: {
    en: 'Nahlah AI is a technology platform owned and operated by Nahlah Ai Establishment, Saudi Arabia.',
    ar: 'نحلة AI منصة تقنية مملوكة ومشغلة من مؤسسة نحلة أي آي، المملكة العربية السعودية.',
  },
} as const
