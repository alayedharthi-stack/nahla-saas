"""
Template AI Generator
─────────────────────
Generates WhatsApp-compliant template drafts from a merchant objective.
Output is store-agnostic and uses numbered variables ({{1}}, {{2}}, ...) for
all dynamic values — no store-specific content is hardcoded.

Supported objectives (extensible):
  abandoned_cart | reorder | winback | back_in_stock | price_drop |
  order_followup | quote_followup | promotion | transactional_update
"""
from __future__ import annotations

from typing import Any, Dict, List

# ── Objective catalogue ────────────────────────────────────────────────────────
# Each entry defines:
#   category   — Meta template category (MARKETING | UTILITY)
#   language   — default language
#   body       — Arabic body text with numbered placeholders
#   variables  — mapping of {{n}} → semantic field name (for var-map)
#   footer     — optional footer line
#   buttons    — optional CTA buttons list (Meta format)

_OBJECTIVE_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "abandoned_cart": {
        "category": "MARKETING",
        "body": "مرحباً {{1}} 👋\nلاحظنا أنك تركت بعض المنتجات في سلة التسوق.\nلا تفوّت الفرصة — سلتك في انتظارك! 🛒\n{{2}}",
        "variables": {"{{1}}": "customer_name", "{{2}}": "cart_url"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [{"type": "URL", "text": "أكمل الشراء", "url": "{{2}}"}],
    },
    "reorder": {
        "category": "MARKETING",
        "body": "مرحباً {{1}} 😊\nحان وقت تجديد طلبك من *{{2}}*!\nاطلب الآن وسنوصل إليك بأسرع وقت. ⚡\n{{3}}",
        "variables": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "reorder_url"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [{"type": "URL", "text": "أعد الطلب", "url": "{{3}}"}],
    },
    "winback": {
        "category": "MARKETING",
        "body": "نشتاق إليك {{1}} 💙\nمرّ وقت منذ آخر زيارة لنا.\nعُد الآن واستمتع بخصم {{2}}% على طلبك القادم بكود: *{{3}}*",
        "variables": {"{{1}}": "customer_name", "{{2}}": "discount_pct", "{{3}}": "coupon_code"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [],
    },
    "back_in_stock": {
        "category": "MARKETING",
        "body": "بشرى سارة {{1}} 🎉\nالمنتج الذي كنت تبحث عنه *{{2}}* متاح الآن!\nاطلبه قبل نفاد الكمية. 🔥\n{{3}}",
        "variables": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "product_url"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [{"type": "URL", "text": "اطلب الآن", "url": "{{3}}"}],
    },
    "price_drop": {
        "category": "MARKETING",
        "body": "انخفض السعر! 📉\nمرحباً {{1}}، منتجك المفضّل *{{2}}* أصبح بسعر أفضل الآن!\nلا تتردد في الطلب. 🛍️\n{{3}}",
        "variables": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "product_url"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [{"type": "URL", "text": "اشترِ الآن", "url": "{{3}}"}],
    },
    "order_followup": {
        "category": "UTILITY",
        "body": "مرحباً {{1}} 😊\nطلبك رقم *{{2}}* قيد المعالجة.\nسنُعلمك فور الشحن. شكراً لثقتك بنا! 🙏",
        "variables": {"{{1}}": "customer_name", "{{2}}": "order_id"},
        "footer": None,
        "buttons": [],
    },
    "quote_followup": {
        "category": "UTILITY",
        "body": "مرحباً {{1}} 👋\nنود متابعة عرض السعر الذي أرسلناه لك بتاريخ {{2}}.\nهل لديك أي استفسار؟ يسعدنا مساعدتك! 😊",
        "variables": {"{{1}}": "customer_name", "{{2}}": "quote_date"},
        "footer": None,
        "buttons": [{"type": "QUICK_REPLY", "text": "تواصل معنا"}],
    },
    "promotion": {
        "category": "MARKETING",
        "body": "عرض خاص لك {{1}} 🎁\nاستمتع بخصم {{2}}% على جميع المنتجات باستخدام كود: *{{3}}*\nالعرض محدود — لا تفوّته! ⏳",
        "variables": {"{{1}}": "customer_name", "{{2}}": "discount_pct", "{{3}}": "coupon_code"},
        "footer": "للإلغاء أرسل STOP",
        "buttons": [],
    },
    "transactional_update": {
        "category": "UTILITY",
        "body": "مرحباً {{1}}،\nتحديث بخصوص طلبك رقم *{{2}}*: {{3}}\nللاستفسار تواصل معنا في أي وقت. 🙏",
        "variables": {"{{1}}": "customer_name", "{{2}}": "order_id", "{{3}}": "status_message"},
        "footer": None,
        "buttons": [],
    },
}

SUPPORTED_OBJECTIVES = list(_OBJECTIVE_CATALOGUE.keys())


def generate_template_draft(
    objective: str,
    language: str = "ar",
    tenant_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Return a template draft dict ready for DB insertion and Meta submission.

    Parameters
    ----------
    objective       : one of SUPPORTED_OBJECTIVES
    language        : 'ar' | 'en' (currently only ar templates exist)
    tenant_context  : optional dict with store_name etc. (not embedded in body)

    Returns
    -------
    {
        name, language, category, objective,
        components, variables,
        source, ai_generation_metadata
    }
    Raises ValueError for unknown objectives.
    """
    if objective not in _OBJECTIVE_CATALOGUE:
        raise ValueError(
            f"Unknown objective '{objective}'. "
            f"Supported: {', '.join(SUPPORTED_OBJECTIVES)}"
        )

    spec = _OBJECTIVE_CATALOGUE[objective]
    template_name = _generate_name(objective, language)
    components = _build_components(spec)

    return {
        "name": template_name,
        "language": language,
        "category": spec["category"],
        "objective": objective,
        "components": components,
        "variables": spec["variables"],
        "source": "ai_generated",
        "status": "DRAFT",
        "ai_generation_metadata": {
            "objective": objective,
            "generated_by": "nahla_template_generator_v1",
            "variable_count": len(spec["variables"]),
            "category": spec["category"],
        },
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _generate_name(objective: str, language: str) -> str:
    """Produce a snake_case Meta-safe template name."""
    return f"nahla_{objective}_{language}"


def _build_components(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    components: List[Dict[str, Any]] = []

    components.append({
        "type": "BODY",
        "text": spec["body"],
    })

    if spec.get("footer"):
        components.append({
            "type": "FOOTER",
            "text": spec["footer"],
        })

    if spec.get("buttons"):
        components.append({
            "type": "BUTTONS",
            "buttons": spec["buttons"],
        })

    return components
