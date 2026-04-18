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


# ── مكتبة القوالب الرئيسية ────────────────────────────────────────────
NAHLA_TEMPLATES: List[Dict[str, Any]] = [

    # ══════════════════════════════════════════════════════════════════
    # 1. تذكير السلة المتروكة — ABANDONED CART
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "abandoned_cart_reminder",
        "name_ar":        "تذكير السلة المتروكة",
        "description_ar": "تُرسل تلقائياً بعد ساعة من ترك العميل المنتجات في السلة دون إكمال الطلب",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: عند ترك السلة",
        "slots":          ["customer_name", "store_name", "checkout_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 🛒\n\n"
                    "لاحظنا أنك أضفت منتجات إلى سلتك في متجر {{2}} لكن لم تكمل الطلب بعد.\n\n"
                    "سلتك محفوظة وتنتظرك — أكمل طلبك الآن قبل نفاد المخزون!"
                ),
                "example": {"body_text": [["أحمد", "متجر الأناقة", "https://example.com/cart"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "أكمل طلبك 🛒", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 2. استكمال الطلب — COMPLETE ORDER
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "complete_your_order",
        "name_ar":        "استكمال الطلب",
        "description_ar": "تذكير ثانٍ للعميل بإكمال طلبه (يُرسل بعد 6 ساعات من التذكير الأول)",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "cart"],
        "smart_trigger":  "cart_abandoned",
        "smart_label":    "يُرسل تلقائياً: متابعة السلة المتروكة",
        "slots":          ["customer_name", "store_name", "checkout_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "{{1}} 👋\n\n"
                    "طلبك في متجر {{2}} ما زال بانتظارك.\n\n"
                    "إذا واجهتَ أي صعوبة في الدفع أو الشحن، ردّ على هذه الرسالة وسنساعدك فوراً.\n\n"
                    "أو أكمل طلبك مباشرةً من هنا:"
                ),
                "example": {"body_text": [["سارة", "متجر الجمال", "https://example.com/cart"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "إتمام الطلب", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 3. كود خصم للعودة — COMEBACK DISCOUNT  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "comeback_discount",
        "name_ar":        "كود خصم للعودة",
        "description_ar": "تُرسل للعملاء غير النشطين (لم يشتروا منذ 30+ يوم) مع كود خصم حصري",
        "category":       "MARKETING",
        "filter_tags":    ["recovery", "discounts"],
        "smart_trigger":  "customer_inactive",
        "smart_label":    "يُرسل تلقائياً: العميل غير نشط 30 يوم",
        "slots":          ["customer_name", "store_name", "discount_code", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "اشتقنا لك يا {{1}} 💛\n\n"
                    "مضى وقت منذ آخر زيارة لمتجر {{2}}!\n\n"
                    "جهّزنا لك كود خصم حصري للعودة — انسخه بلمسة واحدة:"
                ),
                "example": {"body_text": [["خالد", "متجر التقنية", "BACK20", "https://example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["BACK20"]},
                    {"type": "URL", "text": "تسوق الآن 🛍️", "url": "{{4}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 4. شكر بعد الشراء — POST PURCHASE THANKS
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "post_purchase_thanks",
        "name_ar":        "شكر بعد الشراء",
        "description_ar": "تُرسل فور تأكيد الطلب لتعزيز تجربة العميل وبناء الثقة",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_confirmed",
        "smart_label":    "يُرسل تلقائياً: عند تأكيد الطلب",
        "slots":          ["customer_name", "order_id", "store_name", "tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "شكراً لطلبك يا {{1}} ❤️\n\n"
                    "طلبك رقم #{{2}} من متجر {{3}} تم استلامه بنجاح وهو الآن قيد المعالجة.\n\n"
                    "سنُرسل لك تحديثاً فور شحن طلبك."
                ),
                "example": {"body_text": [["أميرة", "12345", "متجر الأزياء", "https://track.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "تتبع طلبي 📦", "url": "{{4}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 5. تحديث الشحن — SHIPPING UPDATE
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "shipping_update",
        "name_ar":        "تحديث الشحن",
        "description_ar": "تُرسل عند شحن الطلب لإبقاء العميل على اطلاع بحالة طلبه",
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders"],
        "smart_trigger":  "order_shipped",
        "smart_label":    "يُرسل تلقائياً: عند شحن الطلب",
        "slots":          ["customer_name", "order_id", "tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "خبر سار يا {{1}} 🚚\n\n"
                    "طلبك رقم #{{2}} تم شحنه وهو في طريقه إليك!\n\n"
                    "يمكنك متابعة حالة الشحن لحظةً بلحظة من هنا:"
                ),
                "example": {"body_text": [["محمد", "12345", "https://track.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "تتبع الشحنة 🗺️", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 6. تم التوصيل — ORDER DELIVERED
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "order_delivered",
        "name_ar":        "تم التوصيل",
        "description_ar": "تُرسل عند وصول الطلب وتدعو العميل لتقييم تجربته",
        "category":       "UTILITY",
        "filter_tags":    ["shipping", "orders"],
        "smart_trigger":  "order_delivered",
        "smart_label":    "يُرسل تلقائياً: عند تسليم الطلب",
        "slots":          ["customer_name", "store_name", "review_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "وصل طلبك يا {{1}} 🎉\n\n"
                    "نأمل أن يكون طلبك من {{2}} قد وصل بحالة ممتازة!\n\n"
                    "رأيك يهمّنا — شاركنا تقييمك وساعد عملاء آخرين على اتخاذ قراراتهم:"
                ),
                "example": {"body_text": [["ليلى", "متجر المنزل", "https://review.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "اترك تقييمك ⭐", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 7. طلب تقييم المنتج — REVIEW REQUEST
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "review_request",
        "name_ar":        "طلب تقييم المنتج",
        "description_ar": "تُرسل بعد 3 أيام من استلام الطلب لطلب تقييم المنتج",
        "category":       "MARKETING",
        "filter_tags":    ["marketing"],
        "smart_trigger":  "order_delivered",
        "smart_label":    "يُرسل تلقائياً: 3 أيام بعد التسليم",
        "slots":          ["customer_name", "product_name", "review_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "كيف كانت تجربتك مع {{2}}؟ 🌟\n\n"
                    "مرحباً {{1}}، نأمل أن تكون راضياً تماماً عن منتجك!\n\n"
                    "تقييمك يساعد آلاف العملاء على اتخاذ قرارات أفضل:"
                ),
                "example": {"body_text": [["عبدالله", "سماعات لاسلكية", "https://review.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "قيّم المنتج ⭐", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 8. عرض خاص — SPECIAL OFFER  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "special_offer",
        "name_ar":        "عرض خاص للعملاء",
        "description_ar": "عرض ترويجي مع كود خصم قابل للنسخ بلمسة واحدة",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  None,
        "smart_label":    "يُرسل يدوياً أو عبر الحملات",
        "slots":          ["customer_name", "store_name", "discount_code", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "🎁 عرض خاص لك يا {{1}}!\n\n"
                    "متجر {{2}} يُقدم لك خصماً حصرياً لفترة محدودة.\n\n"
                    "انسخ الكود بلمسة واحدة واستمتع بالتوفير:"
                ),
                "example": {"body_text": [["فاطمة", "متجر الإلكترونيات", "SAVE25", "https://example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["SAVE25"]},
                    {"type": "URL", "text": "تسوق الآن 🛍️", "url": "{{4}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 9. رسالة ترحيب — WELCOME MESSAGE
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "welcome_message",
        "name_ar":        "رسالة ترحيب",
        "description_ar": "تُرسل لكل عميل جديد عند تسجيله أو أول تواصل مع المتجر",
        "category":       "MARKETING",
        "filter_tags":    ["welcome", "marketing"],
        "smart_trigger":  "new_customer",
        "smart_label":    "يُرسل تلقائياً: عند انضمام عميل جديد",
        "slots":          ["customer_name", "store_name", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أهلاً وسهلاً بك يا {{1}} 🌟\n\n"
                    "يسعدنا انضمامك إلى عائلة متجر {{2}}!\n\n"
                    "نحن هنا لخدمتك على مدار الساعة — تصفّح منتجاتنا واكتشف ما يناسبك:"
                ),
                "example": {"body_text": [["ريم", "متجر الأزياء العصرية", "https://example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "تصفح المتجر 🏪", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 10. متابعة عميل مهتم — INTERESTED CUSTOMER FOLLOWUP
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "interested_followup",
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
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
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
        "name_ar":        "تأكيد الطلب",
        "description_ar": "إشعار رسمي بتأكيد الطلب يتضمن رقم الطلب وتفاصيله",
        "category":       "UTILITY",
        "filter_tags":    ["orders"],
        "smart_trigger":  "order_confirmed",
        "smart_label":    "يُرسل تلقائياً: عند تأكيد الطلب",
        "slots":          ["customer_name", "order_id", "order_total", "tracking_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "تم تأكيد طلبك ✅\n\n"
                    "مرحباً {{1}}، تم استلام طلبك رقم #{{2}} بقيمة {{3}} ريال.\n\n"
                    "سنبدأ تجهيزه فوراً وسترسل لك تحديثات الشحن قريباً."
                ),
                "example": {"body_text": [["بندر", "98765", "450", "https://track.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "تفاصيل الطلب 📋", "url": "{{4}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 12. تأكيد الدفع عند الاستلام — COD CONFIRMATION ← QUICK_REPLY x2
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "cod_confirmation",
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
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
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
    # 13. تذكير بالدفع — PAYMENT REMINDER
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "payment_reminder",
        "name_ar":        "تذكير بإكمال الدفع",
        "description_ar": "تُرسل للطلبات التي لم يُكتمل دفعها بعد مرور وقت محدد",
        "category":       "UTILITY",
        "filter_tags":    ["orders", "recovery"],
        "smart_trigger":  "order_payment_pending",
        "smart_label":    "يُرسل تلقائياً: عند تأخر الدفع",
        "slots":          ["customer_name", "order_id", "payment_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "مرحباً {{1}} 💳\n\n"
                    "طلبك رقم #{{2}} لا يزال بانتظار إكمال الدفع.\n\n"
                    "أكمل الدفع الآن لضمان توفر المنتجات لك:"
                ),
                "example": {"body_text": [["حسن", "33456", "https://pay.example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "إكمال الدفع 💳", "url": "{{3}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 14. عرض VIP حصري — VIP EXCLUSIVE  ← COPY_CODE + URL
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "vip_exclusive",
        "name_ar":        "عرض VIP حصري",
        "description_ar": "مكافأة حصرية لعملاء VIP المميزين بكود خصم قابل للنسخ",
        "category":       "MARKETING",
        "filter_tags":    ["marketing", "discounts"],
        "smart_trigger":  "vip_customer_upgrade",
        "smart_label":    "يُرسل تلقائياً: عند ترقية العميل لـ VIP",
        "slots":          ["customer_name", "store_name", "vip_coupon", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "أنت من عملائنا المميزين يا {{1}} 👑\n\n"
                    "شكراً لولائك لمتجر {{2}}!\n\n"
                    "هذا كود خاص جداً — مخصص لك وحدك — انسخه واستمتع بخصمك الحصري:"
                ),
                "example": {"body_text": [["وليد", "متجر الساعات", "VIP30", "https://example.com"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "COPY_CODE", "example": ["VIP30"]},
                    {"type": "URL", "text": "تسوق الآن 👑", "url": "{{4}}"},
                ],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════
    # 15. منتجات جديدة — NEW ARRIVALS
    # ══════════════════════════════════════════════════════════════════
    {
        "key":            "new_arrivals",
        "name_ar":        "منتجات جديدة وصلت",
        "description_ar": "إشعار للعملاء بوصول منتجات جديدة تناسب اهتماماتهم",
        "category":       "MARKETING",
        "filter_tags":    ["marketing"],
        "smart_trigger":  "new_product_alert",
        "smart_label":    "يُرسل تلقائياً: عند إضافة منتجات جديدة",
        "slots":          ["customer_name", "store_name", "store_url"],
        "components": [
            {
                "type": "BODY",
                "text": (
                    "وصل الجديد يا {{1}} 🆕✨\n\n"
                    "متجر {{2}} يُطلق مجموعة جديدة تم اختيارها بعناية!\n\n"
                    "كن أول من يكتشف الوصولات الجديدة:"
                ),
                "example": {"body_text": [["دانة", "متجر الأزياء", "https://example.com/new"]]},
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك 🐝"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "اكتشف الجديد 🆕", "url": "{{3}}"},
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
    }
