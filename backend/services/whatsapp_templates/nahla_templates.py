"""
services/whatsapp_templates/nahla_templates.py
──────────────────────────────────────────────
مكتبة قوالب نحلة الرسمية — متوافقة مع سياسات WhatsApp/Meta

كل قالب:
  - مكتوب بعربية احترافية
  - يستخدم {{1}}/{{2}}/… (النمط القياسي لـ Meta)
  - يحتوي أفضل الأزرار لكل حالة:
      URL        → رابط المتجر / الطلب / التتبع
      COPY_CODE  → نسخ كود الخصم بلمسة
      QUICK_REPLY→ تأكيد / إلغاء بلمسة
  - مرتبط بـ smart_trigger لمحرك الأتمتة

قيود Meta للأزرار:
  - أزرار CTA (URL / COPY_CODE) لا تختلط مع QUICK_REPLY
  - حد أقصى 3 أزرار لكل قالب
  - COPY_CODE يُقرن مع URL (كلاهما من نوع CTA)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# ── Filter tags (تصنيفات الفلترة في الواجهة) ─────────────────────────
FILTER_TAGS = {
    "all":       "الكل",
    "marketing": "التسويق",
    "orders":    "الطلبات",
    "shipping":  "الشحن",
    "recovery":  "الاسترجاع",
    "discounts": "الخصومات",
    "welcome":   "الترحيب",
}


# ── كتالوج الخدمات (الغرض التجاري لكل عائلة قوالب) ────────────────────
# يُعرض في واجهة التاجر كبطاقة تعريفية أعلى معاينة القالب.
# كل service_key يُربط بعدة قوالب تنتمي لنفس الخدمة التجارية.
SERVICE_CATALOG: Dict[str, Dict[str, str]] = {
    "cart_recovery": {
        "name_ar":        "استرجاع السلات المتروكة",
        "description_ar": "تذكير العملاء الذين أضافوا منتجات لسلتهم دون إكمال الطلب لاسترجاع المبيعات",
        "icon":           "🛒",
        "color":          "amber",
    },
    "order_confirmation": {
        "name_ar":        "تأكيد الطلب",
        "description_ar": "إشعار العميل بتأكيد واستلام طلبه مع ملخص التفاصيل",
        "icon":           "📦",
        "color":          "blue",
    },
    "cod_confirmation": {
        "name_ar":        "تأكيد الدفع عند الاستلام",
        "description_ar": "التحقق من جدية العميل في طلبات الدفع عند الاستلام لتقليل الطلبات الوهمية",
        "icon":           "💰",
        "color":          "emerald",
    },
    "shipping_tracking": {
        "name_ar":        "الشحن وتتبع الطلب",
        "description_ar": "إبقاء العميل على اطلاع بحالة شحن طلبه ومواعيد التوصيل",
        "icon":           "🚚",
        "color":          "violet",
    },
    "post_delivery": {
        "name_ar":        "ما بعد التسليم",
        "description_ar": "تعزيز تجربة العميل بعد استلام الطلب وطلب تقييمه للمنتج",
        "icon":           "⭐",
        "color":          "yellow",
    },
    "predictive_reorder": {
        "name_ar":        "إعادة الطلب التنبؤية",
        "description_ar": "تذكير العملاء بإعادة شراء منتجات استهلاكية عند توقع نفادها",
        "icon":           "🔄",
        "color":          "teal",
    },
    "marketing_campaigns": {
        "name_ar":        "الحملات التسويقية",
        "description_ar": "إرسال عروض ترويجية وأكواد خصم وإعلانات المنتجات الجديدة",
        "icon":           "📢",
        "color":          "pink",
    },
    "welcome_onboarding": {
        "name_ar":        "الترحيب بالعملاء",
        "description_ar": "ترحيب بالعملاء الجدد عند أول تواصل أو تسجيل في المتجر",
        "icon":           "👋",
        "color":          "sky",
    },
    "customer_support": {
        "name_ar":        "خدمة العملاء",
        "description_ar": "متابعة العملاء بعد حل مشكلاتهم والتأكد من رضاهم",
        "icon":           "💬",
        "color":          "slate",
    },
    "customer_retention": {
        "name_ar":        "استرجاع العملاء غير النشطين",
        "description_ar": "تحفيز العملاء الذين لم يشتروا منذ فترة على العودة للتسوق",
        "icon":           "💛",
        "color":          "orange",
    },
    "payment_reminder": {
        "name_ar":        "تذكير بالدفع",
        "description_ar": "تذكير العملاء بإكمال دفع الطلبات المعلقة",
        "icon":           "💳",
        "color":          "rose",
    },
    "customer_engagement": {
        "name_ar":        "تفاعل العملاء",
        "description_ar": "متابعة العملاء المهتمين بمنتجات معينة لتشجيعهم على الشراء",
        "icon":           "💡",
        "color":          "cyan",
    },
    "vip_rewards": {
        "name_ar":        "مكافآت العملاء المميزين",
        "description_ar": "عروض حصرية ومكافآت لعملاء VIP المميزين",
        "icon":           "👑",
        "color":          "purple",
    },
    "back_in_stock": {
        "name_ar":        "إشعار عودة المنتج للمخزون",
        "description_ar": "إشعار العملاء المهتمين عند عودة منتج كان نافداً إلى المخزون",
        "icon":           "📦",
        "color":          "indigo",
    },
    "seasonal_offers": {
        "name_ar":        "عروض المناسبات الموسمية",
        "description_ar": "عروض ترويجية لمناسبات السنة (اليوم الوطني، رمضان، العيد، الجمعة البيضاء...)",
        "icon":           "🎊",
        "color":          "pink",
    },
    "salary_payday_offers": {
        "name_ar":        "عروض يوم الراتب",
        "description_ar": "عرض شهري في يوم الراتب لزيادة المبيعات في موسم القوة الشرائية",
        "icon":           "💵",
        "color":          "emerald",
    },
}


# ── مكتبة القوالب الرئيسية ────────────────────────────────────────────
NAHLA_TEMPLATES: List[Dict[str, Any]] = [

    # ══════════════════════════════════════════════════════════════════
    # 1. تذكير السلة المتروكة — ABANDONED CART
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "abandoned_cart_reminder",
        "service_key":    "cart_recovery",
        "name_ar":        "تذكير السلة المتروكة — المرحلة الأولى",
        "description_ar": "تُرسل تلقائياً بعد 30 دقيقة من ترك العميل المنتجات في السلة دون إكمال الطلب",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: بعد 30 دقيقة من ترك السلة",
        "step_number":         1,
        "has_coupon":          False,
        "trigger_delay_hours": 0.5,
        # BODY gets only the customer name; the URL button's {{1}} is fed
        # independently from the event payload (cart_url / checkout_url).
        # The base URL is merchant-agnostic: at import time example.com is
        # swapped with the merchant's real domain, and at send time the
        # engine passes the full path as the dynamic suffix — so this
        # single template works for /cart/, /checkout/, or any other path
        # structure across all merchants.
        "body_slots":   ["customer_name"],
        "button_slots": ["cart_url"],
        "slots":          ["customer_name", "cart_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 🛒\n\n"
                    "لاحظنا أنك أضفت منتجات إلى سلتك لكنك لم تكمل الطلب بعد.\n\n"
                    "سلتك محفوظة وتنتظرك — أكمل طلبك الآن قبل نفاذ المخزون!"
                ),
                "example": {"body_text": [["أحمد"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "أكمل طلبك",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/cart/abc123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 2. استكمال الطلب — COMPLETE ORDER
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "complete_your_order",
        "service_key":    "cart_recovery",
        "name_ar":        "تذكير السلة المتروكة — المرحلة الثانية",
        "description_ar": "تذكير ثانٍ للعميل بإكمال طلبه (يُرسل بعد 6 ساعات من التذكير الأول)",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: بعد 6 ساعات من ترك السلة",
        "step_number":         2,
        "has_coupon":          False,
        "trigger_delay_hours": 6,
        "body_slots":   ["customer_name"],
        "button_slots": ["cart_url"],
        "slots":          ["customer_name", "cart_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 👋\n\n"
                    "طلبك ما زال بانتظارك.\n\n"
                    "إذا واجهتَ أي صعوبة في الدفع أو الشحن، ردّ على هذه الرسالة وسنساعدك فوراً.\n\n"
                    "أو أكمل طلبك مباشرةً من هنا:"
                ),
                "example": {"body_text": [["سارة"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "إتمام الطلب",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/cart/abc123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 3. كود خصم للعودة — COMEBACK DISCOUNT  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "comeback_discount",
        "service_key":    "customer_retention",
        "name_ar":        "كود خصم للعودة",
        "description_ar": "تُرسل للعملاء غير النشطين (لم يشتروا منذ 30+ يوم) مع كود خصم حصري",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "discounts"],
        "smart_trigger":  "customer_inactive",
        "smart_label":    "يُرسل تلقائياً: العميل غير نشط 30 يوم",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "اشتقنا لك يا {{1}} 💛\n\n"
                    "مضى وقت منذ آخر زيارة لمتجر {{2}}!\n\n"
                    "جهّزنا لك كود خصم حصري للعودة — انسخه بلمسة واحدة:"
                ),
                "example": {"body_text": [["خالد", "متجر التقنية"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["BACK20"]},
                    {
                        "type": "URL", "text": "تسوق الآن",
                        "url": "https://example.com/",
                        "example": ["https://example.com/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 4. شكر بعد الشراء — POST PURCHASE THANKS
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "post_purchase_thanks",
        "service_key":    "order_confirmation",
        "name_ar":        "شكر بعد الشراء",
        "description_ar": "تُرسل فور تأكيد الطلب لتعزيز تجربة العميل وبناء الثقة",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_confirmed",
        "smart_label":    "يُرسل تلقائياً: عند تأكيد الطلب",
        "slots":          ["customer_name", "order_id", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "شكراً لطلبك يا {{1}} ❤️\n\n"
                    "طلبك رقم #{{2}} من متجر {{3}} تم استلامه بنجاح وهو الآن قيد المعالجة.\n\n"
                    "سنُرسل لك تحديثاً فور شحن طلبك."
                ),
                "example": {"body_text": [["أميرة", "12345", "متجر الأزياء"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تتبع طلبي",
                        "url": "https://example.com/track/{{1}}",
                        "example": ["https://example.com/track/12345"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 5. تحديث الشحن — SHIPPING UPDATE
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "shipping_update",
        "service_key":    "shipping_tracking",
        "name_ar":        "تحديث الشحن",
        "description_ar": "تُرسل عند شحن الطلب لإبقاء العميل على اطلاع بحالة طلبه",
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders"],
        "smart_trigger":  "order_shipped",
        "smart_label":    "يُرسل تلقائياً: عند شحن الطلب",
        "slots":          ["customer_name", "order_id"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "خبر سار يا {{1}} 🚚\n\n"
                    "طلبك رقم #{{2}} تم شحنه وهو في طريقه إليك!\n\n"
                    "يمكنك متابعة حالة الشحن لحظةً بلحظة من هنا:"
                ),
                "example": {"body_text": [["محمد", "12345"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تتبع الشحنة",
                        "url": "https://example.com/track/{{1}}",
                        "example": ["https://example.com/track/12345"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 6. تم التوصيل — ORDER DELIVERED
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "order_delivered",
        "service_key":    "post_delivery",
        "name_ar":        "تم التوصيل",
        "description_ar": "تُرسل عند وصول الطلب وتدعو العميل لتقييم تجربته",
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders"],
        "smart_trigger":  "order_delivered",
        "smart_label":    "يُرسل تلقائياً: عند تسليم الطلب",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "وصل طلبك يا {{1}} 🎉\n\n"
                    "نأمل أن يكون طلبك من {{2}} قد وصل بحالة ممتازة!\n\n"
                    "رأيك يهمّنا — شاركنا تقييمك وساعد عملاء آخرين على اتخاذ قراراتهم:"
                ),
                "example": {"body_text": [["ليلى", "متجر المنزل"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "اترك تقييمك",
                        "url": "https://example.com/review/{{1}}",
                        "example": ["https://example.com/review/order123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 7. طلب تقييم المنتج — REVIEW REQUEST
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "review_request",
        "service_key":    "post_delivery",
        "name_ar":        "طلب تقييم المنتج",
        "description_ar": "تُرسل بعد 3 أيام من استلام الطلب لطلب تقييم المنتج",
        "category":       "MARKETING",
        "filter_tags":    ["marketing"],
        "smart_trigger":  "order_delivered",
        "smart_label":    "يُرسل تلقائياً: 3 أيام بعد التسليم",
        "slots":          ["customer_name", "product_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "كيف كانت تجربتك مع {{2}}؟ 🌟\n\n"
                    "مرحباً {{1}}، نأمل أن تكون راضياً تماماً عن منتجك!\n\n"
                    "تقييمك يساعد آلاف العملاء على اتخاذ قرارات أفضل:"
                ),
                "example": {"body_text": [["عبدالله", "سماعات لاسلكية"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "قيّم المنتج",
                        "url": "https://example.com/review/{{1}}",
                        "example": ["https://example.com/review/product123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 8. عرض خاص — SPECIAL OFFER  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "special_offer",
        "service_key":    "marketing_campaigns",
        "name_ar":        "عرض خاص للعملاء",
        "description_ar": "عرض ترويجي مع كود خصم قابل للنسخ بلمسة واحدة",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  None,
        "smart_label":    "يُرسل يدوياً أو عبر الحملات",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "🎁 عرض خاص لك يا {{1}}!\n\n"
                    "متجر {{2}} يُقدم لك خصماً حصرياً لفترة محدودة.\n\n"
                    "انسخ الكود بلمسة واحدة واستمتع بالتوفير:"
                ),
                "example": {"body_text": [["فاطمة", "متجر الإلكترونيات"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["SAVE25"]},
                    {
                        "type": "URL", "text": "تسوق الآن",
                        "url": "https://example.com/",
                        "example": ["https://example.com/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 9. رسالة ترحيب — WELCOME MESSAGE
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "welcome_message",
        "service_key":    "welcome_onboarding",
        "name_ar":        "رسالة ترحيب",
        "description_ar": "تُرسل لكل عميل جديد عند تسجيله أو أول تواصل مع المتجر",
        "category":       "MARKETING",
        "filter_tags":    ["welcome", "marketing"],
        "smart_trigger":  "new_customer",
        "smart_label":    "يُرسل تلقائياً: عند انضمام عميل جديد",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أهلاً وسهلاً بك يا {{1}} 🌟\n\n"
                    "يسعدنا انضمامك إلى عائلة متجر {{2}}!\n\n"
                    "نحن هنا لخدمتك على مدار الساعة — تصفّح منتجاتنا واكتشف ما يناسبك:"
                ),
                "example": {"body_text": [["ريم", "متجر الأزياء العصرية"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تصفح المتجر",
                        "url": "https://example.com/",
                        "example": ["https://example.com/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 10. متابعة عميل مهتم — INTERESTED CUSTOMER FOLLOWUP
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "interested_followup",
        "service_key":    "customer_engagement",
        "name_ar":        "متابعة عميل مهتم",
        "description_ar": "تُرسل للعملاء الذين تصفحوا المنتجات أو سألوا عنها دون شراء",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "marketing"],
        "smart_trigger":  "product_interest",
        "smart_label":    "يُرسل تلقائياً: بعد الاستفسار عن منتج",
        "slots":          ["customer_name", "product_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 👋\n\n"
                    "لاحظنا اهتمامك بـ {{2}}.\n\n"
                    "هل لديك أي سؤال يمكننا مساعدتك فيه؟ أو هل تريد معرفة إذا كان متوفراً بمقاسك / لونك المفضل؟"
                ),
                "example": {"body_text": [["نورة", "حقيبة جلدية بنية"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "نعم، أريد مساعدة"},
                    {"type": "QUICK_REPLY", "text": "لا، شكراً"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 11. تأكيد الطلب — ORDER CONFIRMED
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "order_confirmed",
        "service_key":    "order_confirmation",
        "name_ar":        "تأكيد الطلب",
        "description_ar": "إشعار رسمي بتأكيد الطلب يتضمن رقم الطلب وتفاصيله",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_confirmed",
        "smart_label":    "يُرسل تلقائياً: عند تأكيد الطلب",
        "slots":          ["customer_name", "order_id", "order_total"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "تم تأكيد طلبك ✅\n\n"
                    "مرحباً {{1}}، تم استلام طلبك رقم #{{2}} بقيمة {{3}} ريال.\n\n"
                    "سنبدأ تجهيزه فوراً وسنرسل لك تحديثات الشحن قريباً."
                ),
                "example": {"body_text": [["بندر", "98765", "450"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تفاصيل الطلب",
                        "url": "https://example.com/orders/{{1}}",
                        "example": ["https://example.com/orders/98765"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 12. تأكيد الدفع عند الاستلام — COD CONFIRMATION ← QUICK_REPLY x2
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "cod_confirmation",
        "service_key":    "cod_confirmation",
        "name_ar":        "تأكيد الدفع عند الاستلام",
        "description_ar": "يطلب من العميل تأكيد طلب الدفع عند الاستلام بلمسة واحدة",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_cod_pending",
        "smart_label":    "يُرسل تلقائياً: لطلبات الدفع عند الاستلام",
        "slots":          ["customer_name", "order_id", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 👋\n\n"
                    "لديك طلب رقم #{{2}} من متجر {{3}} بنظام الدفع عند الاستلام.\n\n"
                    "هل تريد تأكيد هذا الطلب؟"
                ),
                "example": {"body_text": [["سلطان", "55123", "متجر الرياضة"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "تأكيد الطلب"},
                    {"type": "QUICK_REPLY", "text": "إلغاء الطلب"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 13. تذكير بالدفع — PAYMENT REMINDER
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "payment_reminder",
        "service_key":    "payment_reminder",
        "name_ar":        "تذكير بإكمال الدفع",
        "description_ar": "تُرسل للطلبات التي لم يُكتمل دفعها بعد مرور وقت محدد",
        "category":       "UTILITY",
        "filter_tags":    ["orders", "recovery"],
        "smart_trigger":  "order_payment_pending",
        "smart_label":    "يُرسل تلقائياً: عند تأخر الدفع",
        "slots":          ["customer_name", "order_id"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 💳\n\n"
                    "طلبك رقم #{{2}} لا يزال بانتظار إكمال الدفع.\n\n"
                    "أكمل الدفع الآن لضمان توفر المنتجات لك:"
                ),
                "example": {"body_text": [["حسن", "33456"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "إكمال الدفع",
                        "url": "https://example.com/pay/{{1}}",
                        "example": ["https://example.com/pay/33456"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 14. عرض VIP حصري — VIP EXCLUSIVE  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "vip_exclusive",
        "service_key":    "vip_rewards",
        "name_ar":        "عرض VIP حصري",
        "description_ar": "مكافأة حصرية لعملاء VIP المميزين بكود خصم قابل للنسخ",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  "vip_customer_upgrade",
        "smart_label":    "يُرسل تلقائياً: عند ترقية العميل لـ VIP",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أنت من عملائنا المميزين يا {{1}} 👑\n\n"
                    "شكراً لولائك لمتجر {{2}}!\n\n"
                    "هذا كود خاص جداً — مخصص لك وحدك — انسخه واستمتع بخصمك الحصري:"
                ),
                "example": {"body_text": [["وليد", "متجر الساعات"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["VIP30"]},
                    {
                        "type": "URL", "text": "تسوق الآن",
                        "url": "https://example.com/",
                        "example": ["https://example.com/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 15. منتجات جديدة — NEW ARRIVALS
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "new_arrivals",
        "service_key":    "marketing_campaigns",
        "name_ar":        "منتجات جديدة وصلت",
        "description_ar": "إشعار للعملاء بوصول منتجات جديدة تناسب اهتماماتهم",
        "category":       "MARKETING",
        "filter_tags":    ["marketing"],
        "smart_trigger":  "new_product_alert",
        "smart_label":    "يُرسل تلقائياً: عند إضافة منتجات جديدة",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "وصل الجديد يا {{1}} 🆕✨\n\n"
                    "متجر {{2}} يُطلق مجموعة جديدة تم اختيارها بعناية!\n\n"
                    "كن أول من يكتشف الوصولات الجديدة:"
                ),
                "example": {"body_text": [["دانة", "متجر الأزياء"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "اكتشف الجديد",
                        "url": "https://example.com/new/",
                        "example": ["https://example.com/new/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 16. سلة متروكة + كود خصم (24 ساعة) — ABANDONED CART 24H COUPON
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "abandoned_cart_24h_coupon",
        "service_key":    "cart_recovery",
        "name_ar":        "تذكير السلة المتروكة — المرحلة الثالثة مع خصم",
        "description_ar": "تُرسل بعد 24 ساعة من ترك السلة مع كود خصم حصري لتشجيع العميل على إكمال الطلب",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart", "discounts"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: بعد 24 ساعة من ترك السلة",
        "step_number":         3,
        "has_coupon":          True,
        "trigger_delay_hours": 24,
        "body_slots":   ["customer_name"],
        "button_slots": ["coupon_code", "cart_url"],   # coupon_code dynamic at send time
        "slots":          ["customer_name", "coupon_code", "cart_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أهلاً {{1}} 💛\n\n"
                    "سلتك لا تزال بانتظارك!\n\n"
                    "جهّزنا لك كود خصم حصري كهدية — انسخه وأكمل طلبك الآن قبل انتهاء العرض:"
                ),
                "example": {"body_text": [["أحمد"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    # COPY_CODE: example shown to Meta for review only —
                    # real coupon code is injected dynamically at send time
                    {"type": "COPY_CODE", "example": ["CART15"]},
                    {
                        "type": "URL", "text": "أكمل طلبك بالخصم",
                        # {{1}} is replaced with the customer's cart URL at send time
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/cart/abc123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 16b. سلة متروكة — تذكير أخير بعد 3 أيام — ABANDONED CART 3-DAY
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "abandoned_cart_3day_final",
        "service_key":    "cart_recovery",
        "name_ar":        "تذكير السلة المتروكة — المرحلة الرابعة (تذكير أخير)",
        "description_ar": "تذكير أخير يُرسل بعد 3 أيام من ترك السلة لتشجيع العميل على الشراء قبل حذف السلة",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: بعد 3 أيام من ترك السلة",
        "step_number":         4,
        "has_coupon":          False,
        "trigger_delay_hours": 72,
        "body_slots":   ["customer_name"],
        "button_slots": ["cart_url"],
        "slots":          ["customer_name", "cart_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "تنبيه {{1}} 🔔\n\n"
                    "هذا آخر تذكير — سلتك ستُحذف قريباً.\n\n"
                    "المنتجات التي اخترتها ما زالت متاحة الآن، لكن لا نضمن بقاءها.\n\n"
                    "أكمل طلبك الآن ولا تفوّت الفرصة:"
                ),
                "example": {"body_text": [["فهد"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "أكمل طلبك الآن",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/cart/abc123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 17. ملخص الطلب — ORDER SUMMARY
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "order_summary",
        "service_key":    "order_confirmation",
        "name_ar":        "ملخص الطلب",
        "description_ar": "تُرسل فور إنشاء الطلب بملخص شامل يتضمن رقم الطلب والمبلغ",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_created",
        "smart_label":    "يُرسل تلقائياً: عند إنشاء الطلب",
        "body_slots":   ["customer_name", "order_id", "order_total"],
        "button_slots": ["tracking_url"],
        "slots":          ["customer_name", "order_id", "order_total", "tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "تم استلام طلبك يا {{1}} 📦\n\n"
                    "رقم الطلب: #{{2}}\n"
                    "المبلغ الإجمالي: {{3}} ريال\n\n"
                    "سنبدأ تجهيز طلبك فوراً ونُعلمك بكل جديد."
                ),
                "example": {"body_text": [["سارة", "45678", "350"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "عرض تفاصيل الطلب",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/orders/45678"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 18. تأكيد COD قبل الشحن — COD REMINDER BEFORE SHIPPING
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "cod_reminder_before_shipping",
        "service_key":    "cod_confirmation",
        "name_ar":        "تأكيد COD قبل الشحن",
        "description_ar": "تُرسل قبل شحن طلب الدفع عند الاستلام للتأكد من جدية العميل وتقليل الطلبات الوهمية",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "cod_confirmation_pending",
        "smart_label":    "يُرسل تلقائياً: قبل شحن طلب COD",
        "slots":          ["customer_name", "order_id", "order_total"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 📋\n\n"
                    "طلبك رقم #{{2}} بقيمة {{3}} ريال جاهز للشحن.\n\n"
                    "بما أن الطلب بنظام الدفع عند الاستلام، نحتاج تأكيدك قبل الشحن:"
                ),
                "example": {"body_text": [["خالد", "67890", "280"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
                    {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 19. الطلب في الطريق — ORDER OUT FOR DELIVERY
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "order_out_for_delivery",
        "service_key":    "shipping_tracking",
        "name_ar":        "الطلب في الطريق",
        "description_ar": "تُرسل عند خروج الطلب للتوصيل ليستعد العميل لاستلامه",
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders"],
        "smart_trigger":  "order_out_for_delivery",
        "smart_label":    "يُرسل تلقائياً: عند خروج الطلب للتوصيل",
        "body_slots":   ["customer_name", "order_id"],
        "button_slots": ["tracking_url"],
        "slots":          ["customer_name", "order_id", "tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أهلاً {{1}} 🚗\n\n"
                    "طلبك رقم #{{2}} خرج للتوصيل وسيصل إليك قريباً!\n\n"
                    "يرجى التأكد من وجودك في العنوان المحدد لاستلام طلبك."
                ),
                "example": {"body_text": [["نورة", "78901"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تتبع التوصيل",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/track/78901"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 20. تذكير إعادة الطلب الذكي — PREDICTIVE REORDER REMINDER
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "predictive_reorder_reminder",
        "service_key":    "predictive_reorder",
        "name_ar":        "تذكير إعادة الطلب الذكي",
        "description_ar": "تُرسل عندما يتوقع النظام أن المنتج أوشك على النفاد لدى العميل (عسل، قهوة، مكملات...)",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "recovery"],
        "smart_trigger":  "reorder_prediction",
        "smart_label":    "يُرسل تلقائياً: عند توقع نفاد المنتج",
        "body_slots":   ["customer_name", "product_name"],
        "button_slots": ["reorder_url"],
        "slots":          ["customer_name", "product_name", "reorder_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 🍯\n\n"
                    "نتوقع أن {{2}} لديك أوشك على النفاد!\n\n"
                    "اطلب الآن ليصلك قبل ما يخلص:"
                ),
                "example": {"body_text": [["فهد", "عسل السدر الطبيعي"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "أعد الطلب",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/products/honey-123"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 21. رابط إعادة طلب سريع — REORDER QUICK LINK
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "reorder_quick_link",
        "service_key":    "predictive_reorder",
        "name_ar":        "رابط إعادة طلب سريع",
        "description_ar": "رسالة مختصرة برابط مباشر لإعادة طلب منتج سبق شراؤه",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "recovery"],
        "smart_trigger":  "reorder_prediction",
        "smart_label":    "يُرسل تلقائياً: رابط سريع لإعادة الطلب",
        "body_slots":   ["customer_name", "product_name"],
        "button_slots": ["reorder_url"],
        "slots":          ["customer_name", "product_name", "reorder_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 👋\n\n"
                    "حان وقت تجديد {{2}}!\n\n"
                    "اطلب بلمسة واحدة من هنا:"
                ),
                "example": {"body_text": [["دانة", "القهوة المختصة"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "اطلب الآن",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/reorder/coffee-456"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 22. حملة تسويقية عامة — MARKETING CAMPAIGN
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "marketing_campaign",
        "service_key":    "marketing_campaigns",
        "name_ar":        "حملة تسويقية عامة",
        "description_ar": "قالب مرن للحملات التسويقية والعروض الموسمية لجميع المتاجر",
        "category":       "MARKETING",
        "filter_tags":    ["marketing"],
        "smart_trigger":  None,
        "smart_label":    "يُرسل يدوياً أو عبر الحملات",
        "slots":          ["customer_name", "store_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 🎉\n\n"
                    "{{2}} عنده مفاجأة لك!\n\n"
                    "عروض حصرية لفترة محدودة — لا تفوّت الفرصة:"
                ),
                "example": {"body_text": [["عبدالله", "متجر الإلكترونيات"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "تسوق العروض",
                        "url": "https://example.com/",
                        "example": ["https://example.com/"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 23. عودة المنتج للمخزون — BACK IN STOCK ALERT
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "back_in_stock_alert",
        "service_key":    "back_in_stock",
        "name_ar":        "إشعار عودة المنتج للمخزون",
        "description_ar": "تُرسل تلقائياً للعملاء الذين سجّلوا اهتماماً بمنتج نافد عند توفره في المخزون من جديد",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "orders"],
        "smart_trigger":  "product_back_in_stock",
        "smart_label":    "يُرسل تلقائياً: عند توفر منتج كان نافداً",
        "body_slots":   ["customer_name", "product_name"],
        "button_slots": ["product_url"],
        "slots":          ["customer_name", "product_name", "product_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "بشرى لك يا {{1}} 🎉\n\n"
                    "{{2}} الذي كنت مهتماً به متوفر الآن في المخزون!\n\n"
                    "اطلبه قبل ما يخلص مرة ثانية:"
                ),
                "example": {"body_text": [["نوره", "حقيبة الجلد البنية"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "اطلب الآن",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/products/leather-bag-789"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 24. عرض موسمي بمناسبة — SEASONAL OFFER  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "seasonal_offer_template",
        "service_key":    "seasonal_offers",
        "name_ar":        "عرض موسمي بمناسبة",
        "description_ar": "عرض ترويجي لمناسبات السنة (اليوم الوطني، رمضان، العيد، الجمعة البيضاء...) مع كود خصم قابل للنسخ",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  "seasonal_event_due",
        "smart_label":    "يُرسل تلقائياً: قبل المناسبات الموسمية",
        "body_slots":   ["customer_name", "occasion_name", "discount_pct"],
        "button_slots": ["coupon_code", "store_url"],
        "slots":          ["customer_name", "occasion_name", "discount_pct", "coupon_code", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 🎊\n\n"
                    "بمناسبة {{2}} جهّزنا لك خصم {{3}}% حصري!\n\n"
                    "انسخ الكود واستمتع بالعرض قبل انتهائه:"
                ),
                "example": {"body_text": [["ريم", "اليوم الوطني السعودي", "20"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["KSA20"]},
                    {
                        "type": "URL", "text": "تسوّق العرض",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/seasonal"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 25. عرض يوم الراتب — SALARY PAYDAY OFFER  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "salary_payday_offer_template",
        "service_key":    "salary_payday_offers",
        "name_ar":        "عرض يوم الراتب",
        "description_ar": "عرض شهري يُرسل قبيل يوم الراتب لاستثمار موسم القوة الشرائية مع كود خصم قابل للنسخ",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  "salary_payday_due",
        "smart_label":    "يُرسل تلقائياً: قبيل يوم الراتب الشهري",
        "body_slots":   ["customer_name", "discount_pct"],
        "button_slots": ["coupon_code", "store_url"],
        "slots":          ["customer_name", "discount_pct", "coupon_code", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 💵\n\n"
                    "وصل الراتب وعندنا لك خصم {{2}}% للاحتفال!\n\n"
                    "انسخ الكود وتسوّق ما تحتاجه قبل انتهاء العرض:"
                ),
                "example": {"body_text": [["محمد", "10"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["PAYDAY10"]},
                    {
                        "type": "URL", "text": "تسوّق الآن",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/payday"],
                    },
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 26. متابعة خدمة العملاء — SUPPORT FOLLOWUP
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "support_followup",
        "service_key":    "customer_support",
        "name_ar":        "متابعة خدمة العملاء",
        "description_ar": "تُرسل بعد حل مشكلة العميل للتأكد من رضاه عن الخدمة",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "support_resolved",
        "smart_label":    "يُرسل تلقائياً: بعد حل مشكلة الدعم",
        "slots":          ["customer_name"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 💙\n\n"
                    "نأمل أن تكون مشكلتك قد حُلّت بالكامل.\n\n"
                    "هل هناك أي شيء آخر يمكننا مساعدتك فيه؟"
                ),
                "example": {"body_text": [["سعود"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "نعم، تم الحل ✅"},
                    {"type": "QUICK_REPLY", "text": "لا، أحتاج مساعدة"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # Meta review — English demo templates (product-owned, not automation)
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "meta_review_cart_recovery",
        "service_key":    "cart_recovery",
        "service_name_override": "Abandoned cart recovery",
        "service_description_override": (
            "Remind customers who left items in their cart to complete checkout"
        ),
        "name_ar":        "English Demo · Cart Recovery",
        "description_ar": (
            "Meta review demo — reminds customers to complete a cart they left behind"
        ),
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "english_demo"],
        "button_slots":   ["cart_url"],
        "slots":          ["cart_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "You left items in your cart 🛒\n\n"
                    "Complete your order before the products run out."
                ),
            },
            {"type": "FOOTER", "text": "Nahla — your store assistant"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "Complete order",
                        "url": "https://example.com/{{1}}",
                        "example": ["https://example.com/cart/abc123"],
                    },
                ],
            },
        ],
    },
    {
        "key":            "meta_review_order_confirmation",
        "service_key":    "order_confirmation",
        "service_name_override": "Order confirmation",
        "service_description_override": (
            "Notify customers when their order is confirmed and being prepared"
        ),
        "name_ar":        "English Demo · Order Confirmation",
        "description_ar": (
            "Meta review demo — confirms the order and sets shipping expectations"
        ),
        "category":       "UTILITY",
        "filter_tags":    ["orders", "english_demo"],
        "slots":          [],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Your order has been confirmed successfully ✅\n\n"
                    "We are preparing it for shipping."
                ),
            },
            {"type": "FOOTER", "text": "Nahla — your store assistant"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "View order",
                        "url": "https://example.com/orders/{{1}}",
                        "example": ["https://example.com/orders/98765"],
                    },
                ],
            },
        ],
    },
    {
        "key":            "meta_review_delivery_update",
        "service_key":    "shipping_tracking",
        "service_name_override": "Shipping & tracking",
        "service_description_override": (
            "Keep customers updated while their order is on the way"
        ),
        "name_ar":        "English Demo · Delivery Update",
        "description_ar": (
            "Meta review demo — shipping update with a track-shipment link"
        ),
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders", "english_demo"],
        "button_slots":   ["tracking_url"],
        "slots":          ["tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Your order is on the way 🚚\n\n"
                    "Track your shipment using the link below."
                ),
            },
            {"type": "FOOTER", "text": "Nahla — your store assistant"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL", "text": "Track shipment",
                        "url": "https://example.com/track/{{1}}",
                        "example": ["https://example.com/track/12345"],
                    },
                ],
            },
        ],
    },
]


# ── Smart Template Engine Mapping ─────────────────────────────────────
# ربط المحفّز بالقالب المناسب (للطيار الآلي)
SMART_TRIGGER_MAP: Dict[str, List[str]] = {
    trigger: [t["key"] for t in NAHLA_TEMPLATES if t.get("smart_trigger") == trigger]
    for trigger in set(t.get("smart_trigger") for t in NAHLA_TEMPLATES if t.get("smart_trigger"))
}


# ── Helper functions ──────────────────────────────────────────────────

def get_all_templates() -> List[Dict[str, Any]]:
    return NAHLA_TEMPLATES


def get_template_by_key(key: str) -> Optional[Dict[str, Any]]:
    return next((t for t in NAHLA_TEMPLATES if t["key"] == key), None)


def filter_templates(
    category: Optional[str] = None,
    tag: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    result = NAHLA_TEMPLATES
    if category and category.upper() != "ALL":
        result = [t for t in result if t["category"] == category.upper()]
    if tag and tag != "all":
        result = [t for t in result if tag in t.get("filter_tags", [])]
    if search:
        q = search.lower()
        result = [
            t for t in result
            if q in t["name_ar"].lower() or q in t.get("description_ar", "").lower()
        ]
    return result


def template_preview(tpl: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lightweight preview dict for the library listing."""
    body_component = next(
        (c for c in tpl["components"] if c["type"] == "BODY"), {}
    )
    buttons_component = next(
        (c for c in tpl["components"] if c["type"] == "BUTTONS"), {}
    )
    footer_component = next(
        (c for c in tpl["components"] if c["type"] == "FOOTER"), {}
    )
    service_key = tpl.get("service_key", "")
    service = SERVICE_CATALOG.get(service_key, {})
    return {
        "key":          tpl["key"],
        "name_ar":      tpl["name_ar"],
        "description_ar": tpl.get("description_ar", ""),
        "category":     tpl["category"],
        "filter_tags":  tpl.get("filter_tags", []),
        "smart_trigger": tpl.get("smart_trigger"),
        "smart_label":  tpl.get("smart_label"),
        "preview_body": body_component.get("text", ""),
        "preview_footer": footer_component.get("text", ""),
        "buttons": buttons_component.get("buttons", []),
        "slot_count": len(tpl.get("slots", [])),
        "slots": tpl.get("slots", []),
        "service_key":            service_key,
        "service_name_ar":        tpl.get("service_name_override") or service.get("name_ar", ""),
        "service_description_ar": tpl.get("service_description_override") or service.get("description_ar", ""),
        "service_icon":           service.get("icon", ""),
        "service_color":          service.get("color", "amber"),
        "step_number":            tpl.get("step_number"),
        "has_coupon":             tpl.get("has_coupon", False),
        "trigger_delay_hours":    tpl.get("trigger_delay_hours"),
    }
