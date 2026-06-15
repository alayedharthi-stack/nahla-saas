export type EscalationChainType =
  | 'general'
  | 'branch_arrival'
  | 'showroom_pickup'
  | 'orders'
  | 'complaints'

export const ESCALATION_CHAIN_TYPES: {
  id: EscalationChainType
  label: string
  available: boolean
}[] = [
  { id: 'general', label: 'عام', available: true },
  { id: 'branch_arrival', label: 'الوصول للفرع', available: false },
  { id: 'showroom_pickup', label: 'الاستلام من المعرض', available: false },
  { id: 'orders', label: 'الطلبات', available: false },
  { id: 'complaints', label: 'الشكاوى', available: false },
]

export const ESCALATION_EXAMPLE_LEVELS = [
  { level: 1, role: 'بائع المعرض' },
  { level: 2, role: 'خدمة العملاء' },
  { level: 3, role: 'الإدارة' },
]
