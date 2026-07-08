import type { Lang } from '../../i18n/types'

export type InboxFilterId = 'all' | 'active' | 'human' | 'agent_req' | 'campaigns' | 'closed'

export type InboxBadgeKind =
  | 'ai'
  | 'human'
  | 'agent_req'
  | 'campaign'
  | 'autopilot'
  | 'cart'
  | 'closed'

export type InboxMessageKind =
  | 'ai'
  | 'human'
  | 'autopilot'
  | 'campaign'
  | 'cart'
  | 'customer'

export interface InboxDemoMessage {
  kind: InboxMessageKind
  text: string
  time: string
  buttons?: string[]
  read?: boolean
}

export interface InboxDemoConversation {
  id: string
  name: string
  avatarColor: string
  initials: string
  preview: string
  time: string
  unread?: number
  bucket: Exclude<InboxFilterId, 'all'>
  badge: InboxBadgeKind
  messages: InboxDemoMessage[]
}

export interface InboxDemoCopy {
  badgeLabels: Record<InboxBadgeKind, string>
  filterLabels: Record<InboxFilterId, string>
  tagLabels: Record<Exclude<InboxMessageKind, 'customer'>, string>
  searchPlaceholder: string
  emptyFilter: string
  back: string
  composer: string
  send: string
  conversations: InboxDemoConversation[]
}

export const INBOX_DEMO_COPY: Record<Lang, InboxDemoCopy> = {
  ar: {
    badgeLabels: {
      ai: 'الذكاء الاصطناعي',
      human: 'رد بشري',
      agent_req: 'يطلب موظف',
      campaign: 'حملة تسويقية',
      autopilot: 'الطيار الآلي',
      cart: 'استرجاع سلة',
      closed: 'مغلقة',
    },
    filterLabels: {
      all: 'الكل',
      active: 'نشطة',
      human: 'بشري',
      agent_req: 'يطلب موظف',
      campaigns: 'حملات',
      closed: 'مغلقة',
    },
    tagLabels: {
      ai: '🤖 الذكاء',
      human: '✋ ردّ بشري',
      autopilot: '✨ الطيار الآلي',
      campaign: '📣 حملة',
      cart: '🛒 استرجاع سلة',
    },
    searchPlaceholder: 'ابحث في المحادثات…',
    emptyFilter: 'لا توجد محادثات في هذا الفلتر',
    back: 'رجوع',
    composer: 'اكتب رسالة… أو اترك نحلة ترد تلقائياً ✨',
    send: 'إرسال',
    conversations: [
      {
        id: 'reem',
        name: 'ريم الحربي',
        initials: 'ر',
        avatarColor: 'from-rose-500 to-pink-600',
        preview: 'أبغى أتكلم مع موظف لو سمحت 🙏',
        time: 'الآن',
        unread: 2,
        bucket: 'agent_req',
        badge: 'agent_req',
        messages: [
          { kind: 'customer', time: '10:38', text: 'السلام عليكم، عندي استفسار خاص بطلب سابق' },
          { kind: 'ai', time: '10:38', read: true, text: 'وعليكم السلام أهلاً ريم 🌷 أكيد، اعطيني رقم الطلب وأخدمك مباشرة.' },
          { kind: 'customer', time: '10:41', text: 'أبغى أتكلم مع موظف لو سمحت 🙏' },
        ],
      },
      {
        id: 'sara',
        name: 'سارة الأحمدي',
        initials: 'س',
        avatarColor: 'from-amber-500 to-orange-500',
        preview: 'تمام، أرسلي لي الرابط 🌷',
        time: 'دقيقتين',
        unread: 1,
        bucket: 'active',
        badge: 'ai',
        messages: [
          { kind: 'customer', time: '10:30', text: 'أبغى أعرف عن كريم الترطيب اليومي، هل متوفر؟' },
          { kind: 'ai', time: '10:30', read: true, text: 'هلا سارة 🌷 نعم متوفر — العبوة 50مل بسعر 89 ريال، والشحن مجاني للطلبات فوق 150.' },
          { kind: 'ai', time: '10:31', read: true, text: 'أرسل لكِ رابط الشراء المباشر؟' },
          { kind: 'customer', time: '10:32', text: 'تمام، أرسلي لي الرابط 🌷' },
        ],
      },
      {
        id: 'noof',
        name: 'نوف العتيبي',
        initials: 'ن',
        avatarColor: 'from-emerald-500 to-teal-600',
        preview: 'تم تأكيد طلبك، شكراً لتواصلك 💚',
        time: '15 د',
        bucket: 'human',
        badge: 'human',
        messages: [
          { kind: 'customer', time: '09:55', text: 'فيه خصم على المجموعة الكاملة؟' },
          { kind: 'ai', time: '09:55', read: true, text: 'هلا نوف 🌷 خليني أتحقق وأرد عليكِ بالتفاصيل.' },
          { kind: 'human', time: '10:02', read: true, text: 'مرحباً نوف، أنا تركي من فريق المتجر. أكيد، نقدر نعطيكِ خصم 12٪ على المجموعة الكاملة هذي المرة.' },
          { kind: 'human', time: '10:25', read: true, text: 'تم تأكيد طلبك، شكراً لتواصلك 💚' },
        ],
      },
      {
        id: 'khalid',
        name: 'خالد المطيري',
        initials: 'خ',
        avatarColor: 'from-violet-500 to-purple-600',
        preview: 'عرض الجمعة البيضاء — خصم 25٪',
        time: 'ساعة',
        bucket: 'campaigns',
        badge: 'campaign',
        messages: [
          {
            kind: 'campaign',
            time: '09:40',
            read: true,
            text: 'مرحباً خالد 🌷 الجمعة البيضاء بدأت في متجر نحلة!\n\n— خصم 25٪ على كل المنتجات\n— شحن مجاني للطلبات +150 ريال\n— العرض ينتهي بعد 48 ساعة',
            buttons: ['تسوّق الآن', 'إلغاء الاشتراك'],
          },
          { kind: 'customer', time: '09:48', text: 'ممتاز، أنا متابع' },
        ],
      },
      {
        id: 'mohammed',
        name: 'محمد القحطاني',
        initials: 'م',
        avatarColor: 'from-sky-500 to-blue-600',
        preview: 'تم شحن طلبك #4127 — رقم البوليصة 8842…',
        time: '3 س',
        bucket: 'active',
        badge: 'autopilot',
        messages: [
          { kind: 'customer', time: 'أمس', text: 'بكم الشحن للرياض؟' },
          { kind: 'ai', time: 'أمس', read: true, text: 'أهلاً محمد 🌷 الشحن للرياض 15 ريال ويصلك خلال 2-4 أيام عمل.' },
          { kind: 'customer', time: 'أمس', text: 'تمام، خذ هذا طلبي' },
          { kind: 'autopilot', time: 'أمس', read: true, text: '✅ تم استلام طلبك #4127 وجاري تجهيزه.' },
          { kind: 'autopilot', time: '07:15', read: true, text: '🚚 تم شحن طلبك #4127 — رقم البوليصة 8842309561 (سمسا).' },
        ],
      },
      {
        id: 'fahd',
        name: 'فهد السويدي',
        initials: 'ف',
        avatarColor: 'from-orange-500 to-rose-600',
        preview: 'تذكير: سلتك في متجر نحلة لا تزال محفوظة…',
        time: '5 س',
        bucket: 'active',
        badge: 'cart',
        messages: [
          {
            kind: 'cart',
            time: '06:00',
            read: true,
            text: 'فهد 🌷 سلتك في متجر نحلة لا تزال محفوظة لك.\n\nأكمل طلبك خلال الساعتين القادمتين واحصل على خصم 10٪.',
            buttons: ['أكمل طلبي', 'استخدم الكوبون'],
          },
        ],
      },
      {
        id: 'abdullah',
        name: 'عبدالله الزهراني',
        initials: 'ع',
        avatarColor: 'from-slate-500 to-slate-700',
        preview: 'شكراً، تم استلام الطلب 👍',
        time: 'أمس',
        bucket: 'closed',
        badge: 'closed',
        messages: [
          { kind: 'autopilot', time: 'أمس', read: true, text: '📦 تم تسليم طلبك #4081. شكراً لاختيارك متجر نحلة 💛' },
          { kind: 'customer', time: 'أمس', text: 'شكراً، تم استلام الطلب 👍' },
        ],
      },
    ],
  },
  en: {
    badgeLabels: {
      ai: 'AI',
      human: 'Human reply',
      agent_req: 'Agent requested',
      campaign: 'Campaign',
      autopilot: 'Autopilot',
      cart: 'Cart recovery',
      closed: 'Closed',
    },
    filterLabels: {
      all: 'All',
      active: 'Active',
      human: 'Human',
      agent_req: 'Agent req.',
      campaigns: 'Campaigns',
      closed: 'Closed',
    },
    tagLabels: {
      ai: '🤖 AI',
      human: '✋ Human',
      autopilot: '✨ Autopilot',
      campaign: '📣 Campaign',
      cart: '🛒 Cart',
    },
    searchPlaceholder: 'Search conversations…',
    emptyFilter: 'No conversations in this filter',
    back: 'Back',
    composer: 'Type a message… or let Nahla reply automatically ✨',
    send: 'Send',
    conversations: [
      {
        id: 'reem',
        name: 'Reem Al-Harbi',
        initials: 'R',
        avatarColor: 'from-rose-500 to-pink-600',
        preview: 'I would like to speak with staff please 🙏',
        time: 'Now',
        unread: 2,
        bucket: 'agent_req',
        badge: 'agent_req',
        messages: [
          { kind: 'customer', time: '10:38', text: 'Hi, I have a question about a previous order' },
          { kind: 'ai', time: '10:38', read: true, text: 'Hello Reem 🌷 Of course — share your order number and I will help right away.' },
          { kind: 'customer', time: '10:41', text: 'I would like to speak with staff please 🙏' },
        ],
      },
      {
        id: 'sara',
        name: 'Sara Al-Ahmadi',
        initials: 'S',
        avatarColor: 'from-amber-500 to-orange-500',
        preview: 'Sure, send me the link 🌷',
        time: '2 min',
        unread: 1,
        bucket: 'active',
        badge: 'ai',
        messages: [
          { kind: 'customer', time: '10:30', text: 'Is the daily moisturizer available?' },
          { kind: 'ai', time: '10:30', read: true, text: 'Hi Sara 🌷 Yes — 50ml is 89 SAR and shipping is free on orders over 150 SAR.' },
          { kind: 'ai', time: '10:31', read: true, text: 'Shall I send you the checkout link?' },
          { kind: 'customer', time: '10:32', text: 'Sure, send me the link 🌷' },
        ],
      },
      {
        id: 'noof',
        name: 'Noof Al-Otaibi',
        initials: 'N',
        avatarColor: 'from-emerald-500 to-teal-600',
        preview: 'Your order is confirmed — thank you 💚',
        time: '15 min',
        bucket: 'human',
        badge: 'human',
        messages: [
          { kind: 'customer', time: '09:55', text: 'Is there a discount on the full bundle?' },
          { kind: 'ai', time: '09:55', read: true, text: 'Hi Noof 🌷 Let me check and get back to you with details.' },
          { kind: 'human', time: '10:02', read: true, text: 'Hi Noof, Turki from the store team here. We can offer 12% off the full bundle this time.' },
          { kind: 'human', time: '10:25', read: true, text: 'Your order is confirmed — thank you 💚' },
        ],
      },
      {
        id: 'khalid',
        name: 'Khalid Al-Mutairi',
        initials: 'K',
        avatarColor: 'from-violet-500 to-purple-600',
        preview: 'White Friday offer — 25% off',
        time: '1 hr',
        bucket: 'campaigns',
        badge: 'campaign',
        messages: [
          {
            kind: 'campaign',
            time: '09:40',
            read: true,
            text: 'Hi Khalid 🌷 White Friday is live at your store!\n\n— 25% off all products\n— Free shipping on orders over 150 SAR\n— Offer ends in 48 hours',
            buttons: ['Shop now', 'Unsubscribe'],
          },
          { kind: 'customer', time: '09:48', text: 'Great, I will check it out' },
        ],
      },
      {
        id: 'mohammed',
        name: 'Mohammed Al-Qahtani',
        initials: 'M',
        avatarColor: 'from-sky-500 to-blue-600',
        preview: 'Order #4127 shipped — tracking 8842…',
        time: '3 hr',
        bucket: 'active',
        badge: 'autopilot',
        messages: [
          { kind: 'customer', time: 'Yesterday', text: 'How much is shipping to Riyadh?' },
          { kind: 'ai', time: 'Yesterday', read: true, text: 'Hi Mohammed 🌷 Shipping to Riyadh is 15 SAR, delivery in 2–4 business days.' },
          { kind: 'customer', time: 'Yesterday', text: 'Sounds good — here is my order' },
          { kind: 'autopilot', time: 'Yesterday', read: true, text: '✅ Order #4127 received and is being prepared.' },
          { kind: 'autopilot', time: '07:15', read: true, text: '🚚 Order #4127 shipped — tracking 8842309561 (SMSA).' },
        ],
      },
      {
        id: 'fahd',
        name: 'Fahd Al-Suwaidi',
        initials: 'F',
        avatarColor: 'from-orange-500 to-rose-600',
        preview: 'Reminder: your cart is still saved…',
        time: '5 hr',
        bucket: 'active',
        badge: 'cart',
        messages: [
          {
            kind: 'cart',
            time: '06:00',
            read: true,
            text: 'Fahd 🌷 Your cart is still saved.\n\nComplete your order within the next two hours and get 10% off.',
            buttons: ['Complete order', 'Use coupon'],
          },
        ],
      },
      {
        id: 'abdullah',
        name: 'Abdullah Al-Zahrani',
        initials: 'A',
        avatarColor: 'from-slate-500 to-slate-700',
        preview: 'Thanks, order received 👍',
        time: 'Yesterday',
        bucket: 'closed',
        badge: 'closed',
        messages: [
          { kind: 'autopilot', time: 'Yesterday', read: true, text: '📦 Order #4081 delivered. Thank you for shopping with us 💛' },
          { kind: 'customer', time: 'Yesterday', text: 'Thanks, order received 👍' },
        ],
      },
    ],
  },
}
