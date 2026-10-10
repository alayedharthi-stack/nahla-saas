/**
 * Shopify connection — merchant dashboard copy (Arabic and English).
 *
 * Static UI labels only. Server and provider text is never displayed: every
 * refusal maps to one of the fixed ``messages`` keys (see
 * ``lib/shopifyConnection/model.ts`` → ``MessageKey``).
 *
 * The copy separates the store *authorization* (the only thing this release
 * does) from *product import*, which does not exist yet and is never implied.
 */
import type { MessageKey } from '../lib/shopifyConnection/model'

export interface ShopifyConnectionLabels {
  page: { title: string; subtitle: string }
  card: {
    title: string
    description: string
    accessLine: string
    loading: string
    loadFailed: string
    retryLoad: string
    unavailable: string
    readOnlyNote: string
  }
  steps: {
    title: string
    authorization: string
    authorizationDone: string
    authorizationAttention: string
    authorizationTodo: string
    productImport: string
    productImportNotStarted: string
  }
  form: {
    label: string
    placeholder: string
    hint: string
    willConnect: string
    connect: string
    starting: string
    redirecting: string
  }
  connection: {
    authorized: string
    verifying: string
    reconnectRequired: string
    uninstalled: string
    disconnected: string
    unknown: string
    connectedAt: string
    disconnectedAt: string
    scopes: string
    importNote: string
    reconnect: string
    disconnect: string
    disconnecting: string
    disconnectedDone: string
  }
  disconnectConfirm: {
    title: string
    message: string
    confirm: string
    cancel: string
  }
  complete: {
    completing: string
    connected: string
    connectedShop: string
    nextStep: string
    nothing: string
    invalid: string
    expired: string
    unconfirmed: string
    uncertain: string
    sessionChanged: string
    cancelled: string
    featureOff: string
    retry: string
    cancel: string
    checkStatus: string
    checking: string
    statusFailed: string
    statusActive: string
    statusNone: string
    backToIntegrations: string
  }
  messages: Record<MessageKey, string>
}

export const shopifyConnectionAr: ShopifyConnectionLabels = {
  page: { title: 'ربط متجر Shopify', subtitle: 'إكمال تفويض المتجر' },
  card: {
    title: 'Shopify',
    description: 'اربط متجرك على Shopify بنحلة بصلاحية قراءة المنتجات فقط.',
    accessLine: 'الصلاحية المطلوبة: {scopes} (قراءة فقط). لا وصول إلى العملاء أو الطلبات أو المدفوعات.',
    loading: 'جارٍ التحقق من حالة الربط…',
    loadFailed: 'تعذّر تحميل حالة ربط Shopify.',
    retryLoad: 'إعادة المحاولة',
    unavailable: 'ربط Shopify غير متاح حالياً.',
    readOnlyNote: 'أنت في جلسة دعم: يمكنك رؤية الحالة فقط. الربط وفصله يتمّان من حساب التاجر نفسه.',
  },
  steps: {
    title: 'خطوات الإعداد',
    authorization: 'تفويض المتجر',
    authorizationDone: 'مكتمل',
    authorizationAttention: 'يحتاج إلى متابعة',
    authorizationTodo: 'لم يبدأ',
    productImport: 'استيراد المنتجات',
    productImportNotStarted: 'لم يبدأ — غير متاح في هذه المرحلة',
  },
  form: {
    label: 'عنوان متجرك على Shopify',
    placeholder: 'my-store.myshopify.com',
    hint: 'اكتب اسم المتجر أو عنوان ‎.myshopify.com‎ (وليس النطاق المخصص).',
    willConnect: 'سيتم الربط مع: {shop}',
    connect: 'المتابعة إلى Shopify',
    starting: 'جارٍ التحضير…',
    redirecting: 'جارٍ الانتقال إلى Shopify…',
  },
  connection: {
    authorized: 'مفوَّض',
    verifying: 'قيد التحقق — أوقفت نحلة استخدام الصلاحية مؤقتاً',
    reconnectRequired: 'يحتاج إلى إعادة الربط',
    uninstalled: 'أُزيل التطبيق من المتجر',
    disconnected: 'مفصول',
    unknown: 'حالة غير معروفة',
    connectedAt: 'تاريخ التفويض: {at}',
    disconnectedAt: 'تاريخ الفصل: {at}',
    scopes: 'الصلاحيات الممنوحة: {scopes}',
    importNote: 'التفويض وحده لا يستورد المنتجات؛ لم يبدأ أي استيراد أو مزامنة.',
    reconnect: 'إعادة الربط',
    disconnect: 'فصل',
    disconnecting: 'جارٍ الفصل…',
    disconnectedDone: 'تم فصل المتجر في نحلة.',
  },
  disconnectConfirm: {
    title: 'فصل متجر Shopify؟',
    message: 'ستحذف نحلة رموز الوصول الخاصة بالمتجر {shop}. هذا لا يزيل التطبيق من لوحة Shopify؛ يمكنك إزالته من هناك. يمكنك إعادة الربط لاحقاً.',
    confirm: 'فصل',
    cancel: 'إلغاء',
  },
  complete: {
    completing: 'جارٍ إكمال ربط متجر Shopify…',
    connected: 'تم تفويض متجر Shopify.',
    connectedShop: 'المتجر: {shop}',
    nextStep: 'التفويض مكتمل. استيراد المنتجات لم يبدأ وغير متاح في هذه المرحلة.',
    nothing: 'لا يوجد طلب ربط لإكماله في هذه الصفحة. ابدأ الربط من صفحة التكاملات.',
    invalid: 'رابط العودة من Shopify غير صالح. ابدأ الربط من جديد.',
    expired: 'انتهت صلاحية طلب الربط. ابدأ الربط من جديد.',
    unconfirmed: 'لم نتمكن من تأكيد نتيجة الربط من هذا الرابط. تحقّق من الحالة الحالية.',
    uncertain: 'لم نتمكن من تأكيد نتيجة الربط. تحقّق من الحالة قبل المحاولة مرة أخرى.',
    sessionChanged: 'تغيّرت جلستك منذ بدء الربط. ابدأ الربط من جديد من صفحة التكاملات.',
    cancelled: 'أُلغي إكمال الربط. تحقّق من الحالة قبل البدء من جديد.',
    featureOff: 'ربط Shopify غير متاح حالياً.',
    retry: 'إعادة المحاولة',
    cancel: 'إلغاء',
    checkStatus: 'التحقق من الحالة',
    checking: 'جارٍ التحقق…',
    statusFailed: 'تعذّر قراءة الحالة. حاول مرة أخرى بعد قليل.',
    statusActive: 'متجر مفوَّض حالياً: {shop}',
    statusNone: 'لا يوجد متجر Shopify مفوَّض حالياً.',
    backToIntegrations: 'العودة إلى التكاملات',
  },
  messages: {
    sessionChanged: 'تغيّرت جلستك منذ بدء الربط. ابدأ من جديد.',
    merchantOnly: 'يتم ربط Shopify وفصله من حساب التاجر نفسه فقط.',
    expired: 'انتهت صلاحية طلب الربط. ابدأ من جديد.',
    alreadyUsed: 'استُخدم طلب الربط هذا من قبل. تحقّق من الحالة.',
    shopUnavailable: 'لا يمكن ربط هذا المتجر بحسابك. تواصل مع الدعم إن كنت تعتقد أن هذا خطأ.',
    inProgress: 'هناك محاولة ربط أخرى لهذا المتجر قيد التنفيذ. حاول بعد لحظات.',
    tooMany: 'هناك محاولات ربط كثيرة قيد الانتظار. انتظر بضع دقائق ثم حاول مجدداً.',
    superseded: 'حلّ تغيير أحدث محل هذه المحاولة. ابدأ من جديد.',
    shopInvalid: 'اكتب عنوان متجر صحيحاً بصيغة my-store.myshopify.com.',
    notConfirmed: 'لم يؤكد Shopify الربط. ابدأ من جديد.',
    scopeInvalid: 'لم تتطابق الصلاحيات الممنوحة مع صلاحية القراءة المطلوبة. ابدأ من جديد.',
    needsSupport: 'تعذّر التحقق من هوية المتجر. تواصل مع الدعم.',
    unavailable: 'ربط Shopify غير متاح حالياً.',
    uncertain: 'لم نتمكن من تأكيد النتيجة. تحقّق من الحالة قبل المحاولة مرة أخرى.',
    unexpectedResponse: 'تعذّر بدء الربط بأمان. حاول مرة أخرى لاحقاً.',
    generic: 'حدث خطأ. تحقّق من الحالة قبل المحاولة مرة أخرى.',
  },
}

export const shopifyConnectionEn: ShopifyConnectionLabels = {
  page: { title: 'Connect Shopify store', subtitle: 'Finish the store authorization' },
  card: {
    title: 'Shopify',
    description: 'Connect your Shopify store to Nahla with read-only product access.',
    accessLine: 'Requested access: {scopes} (read-only). No access to customers, orders or payments.',
    loading: 'Checking connection status…',
    loadFailed: 'Could not load the Shopify connection status.',
    retryLoad: 'Try again',
    unavailable: 'Shopify connection is not available right now.',
    readOnlyNote: 'You are in a support session: status is read-only. Connecting and disconnecting are done from the merchant’s own account.',
  },
  steps: {
    title: 'Setup steps',
    authorization: 'Store authorization',
    authorizationDone: 'Complete',
    authorizationAttention: 'Needs attention',
    authorizationTodo: 'Not started',
    productImport: 'Product import',
    productImportNotStarted: 'Not started — not available at this stage',
  },
  form: {
    label: 'Your Shopify store address',
    placeholder: 'my-store.myshopify.com',
    hint: 'Enter the store name or its .myshopify.com address (not a custom domain).',
    willConnect: 'Will connect: {shop}',
    connect: 'Continue to Shopify',
    starting: 'Preparing…',
    redirecting: 'Opening Shopify…',
  },
  connection: {
    authorized: 'Authorized',
    verifying: 'Being verified — Nahla has paused using this access',
    reconnectRequired: 'Reconnect required',
    uninstalled: 'App removed from the store',
    disconnected: 'Disconnected',
    unknown: 'Unknown status',
    connectedAt: 'Authorized: {at}',
    disconnectedAt: 'Disconnected: {at}',
    scopes: 'Granted access: {scopes}',
    importNote: 'Authorization alone does not import products; no import or sync has started.',
    reconnect: 'Reconnect',
    disconnect: 'Disconnect',
    disconnecting: 'Disconnecting…',
    disconnectedDone: 'The store was disconnected in Nahla.',
  },
  disconnectConfirm: {
    title: 'Disconnect Shopify store?',
    message: 'Nahla will delete its access tokens for {shop}. This does not remove the app from your Shopify admin; you can remove it there. You can reconnect later.',
    confirm: 'Disconnect',
    cancel: 'Cancel',
  },
  complete: {
    completing: 'Finishing the Shopify store connection…',
    connected: 'Shopify store authorized.',
    connectedShop: 'Store: {shop}',
    nextStep: 'Authorization is complete. Product import has not started and is not available at this stage.',
    nothing: 'There is no connection to finish on this page. Start the connection from Integrations.',
    invalid: 'The return link from Shopify is not valid. Start the connection again.',
    expired: 'The connection request expired. Start the connection again.',
    unconfirmed: 'This link cannot confirm the connection result. Check the current status.',
    uncertain: 'We could not confirm the connection result. Check the status before trying again.',
    sessionChanged: 'Your session changed since the connection started. Start again from Integrations.',
    cancelled: 'Finishing the connection was cancelled. Check the status before starting again.',
    featureOff: 'Shopify connection is not available right now.',
    retry: 'Try again',
    cancel: 'Cancel',
    checkStatus: 'Check status',
    checking: 'Checking…',
    statusFailed: 'Could not read the status. Try again shortly.',
    statusActive: 'Currently authorized store: {shop}',
    statusNone: 'No Shopify store is currently authorized.',
    backToIntegrations: 'Back to Integrations',
  },
  messages: {
    sessionChanged: 'Your session changed since the connection started. Start again.',
    merchantOnly: 'Shopify can only be connected or disconnected from the merchant’s own account.',
    expired: 'The connection request expired. Start again.',
    alreadyUsed: 'This connection request was already used. Check the status.',
    shopUnavailable: 'This store cannot be connected to your account. Contact support if you think this is a mistake.',
    inProgress: 'Another connection attempt for this store is in progress. Try again in a moment.',
    tooMany: 'Too many connection attempts are pending. Wait a few minutes and try again.',
    superseded: 'A newer change replaced this attempt. Start again.',
    shopInvalid: 'Enter a valid store address like my-store.myshopify.com.',
    notConfirmed: 'Shopify did not confirm the connection. Start again.',
    scopeInvalid: 'The granted access did not match the requested read-only access. Start again.',
    needsSupport: 'The store identity could not be verified. Contact support.',
    unavailable: 'Shopify connection is not available right now.',
    uncertain: 'We could not confirm the result. Check the status before trying again.',
    unexpectedResponse: 'The connection could not be started safely. Try again later.',
    generic: 'Something went wrong. Check the status before trying again.',
  },
}
