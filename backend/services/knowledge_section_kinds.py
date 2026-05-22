"""
backend/services/knowledge_section_kinds.py
───────────────────────────────────────────
Canonical registry of valid ``MerchantKnowledgeSection.kind`` values.

Why a registry (mirroring the pattern in ``media_key_registry``)?
──────────────────────────────────────────────────────────────────
The merchant-facing dashboard groups sections into six top-level
"buckets" (Quick Updates, Store Info, Sales Policies, Shipping,
Product Extras, Linked Media). The Phase-2 GPT classifier picks a
``kind`` from this exact list when classifying free-form text the
merchant typed in the Quick-Updates field. The Phase-4 prompt
overlay renders sections grouped by ``group`` for Claude.

Free-form ``kind`` would break all three flows. We accept arbitrary
values for the special ``custom`` kind only — that's the escape
hatch when the classifier is unsure.

The registry is intentionally small: it covers what every Saudi
e-commerce store in the Nahla pilot needs to express. Adding a new
kind is a one-line append + one Arabic label + a UI re-render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


# ── Types ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionKind:
    """One row in the section-kinds registry.

    Attributes
    ──────────
    kind          stable snake_case slug — what the DB column stores
                  and what the classifier emits.
    group         which of the six dashboard buckets this kind lives
                  in. Drives both the UI accordion grouping and the
                  prompt-overlay section ordering.
    label_ar      Arabic label shown in the dashboard.
    placeholder_ar  hint for the editor textarea.
    is_product_bound  ``True`` for kinds that are typically tied to
                  a specific catalog product (product_usage,
                  product_recipe, …). Phase 3 wires per-product
                  injection on these so they don't leak into every
                  conversation.
    """

    kind: str
    group: int                 # 1..6 — see ``GROUP_LABELS_AR``
    label_ar: str
    placeholder_ar: str
    is_product_bound: bool = False


# ── Group labels (the six dashboard buckets) ─────────────────────────────────

GROUP_LABELS_AR: Dict[int, str] = {
    1: "التحديثات السريعة",
    2: "معلومات المتجر",
    3: "سياسات البيع",
    4: "سياسات الشحن",
    5: "معلومات المنتجات الإضافية",
    6: "مكتبة الوسائط المرتبطة",
}


# ── Canonical registry ───────────────────────────────────────────────────────
#
# Order matters: the dashboard renders kinds within a group in this
# order, and the prompt overlay emits them in this order so Claude sees
# stable section titles run-after-run.

REGISTRY: List[SectionKind] = [
    # 1 — Quick updates (bucket reserved for raw merchant input that has
    # not been classified yet by the Phase-2 GPT pass).
    SectionKind(
        "quick_update", 1,
        "تحديث سريع",
        "اكتب أي معلومة جديدة وسيتم تصنيفها لاحقاً.",
    ),

    # 2 — Store info
    SectionKind(
        "store_story", 2,
        "قصة المتجر",
        "نبذة قصيرة عن المتجر، رسالته، ولماذا اخترتم منتجاتكم.",
    ),
    SectionKind(
        "reply_style", 2,
        "أسلوب الرد",
        "كيف تحب أن يرد الذكاء على عملائك؟ ودي، رسمي، مرح، مختصر…",
    ),
    SectionKind(
        "dialect", 2,
        "اللهجة",
        "اللهجة المفضلة (سعودي عام، نجدي، حجازي، فصحى…).",
    ),
    SectionKind(
        "working_hours", 2,
        "أوقات العمل",
        "أوقات الدوام، أيام الإجازات، رد الذكاء خارج الدوام.",
    ),
    SectionKind(
        "branches", 2,
        "الفروع",
        "أسماء وعناوين الفروع وروابط الخرائط.",
    ),

    # 3 — Sales policies
    SectionKind(
        "payment_method", 3,
        "طرق الدفع",
        "الطرق المتاحة: مدى، فيزا، Apple Pay، تابي، تمارا…",
    ),
    SectionKind(
        "bank_transfer", 3,
        "التحويل البنكي",
        "الحسابات البنكية، شروط التحويل، إثبات الإيداع.",
    ),
    SectionKind(
        "cod", 3,
        "الدفع عند الاستلام",
        "هل متاح؟ أي مدن؟ هل فيه رسوم إضافية؟",
    ),
    SectionKind(
        "return_policy", 3,
        "الاستبدال والاسترجاع",
        "المدة، الشروط، طريقة استلام البديل.",
    ),
    SectionKind(
        "warranty", 3,
        "الضمان",
        "مدة الضمان لكل فئة منتجات، ما يُغطى وما لا يُغطى.",
    ),

    # 4 — Shipping
    SectionKind(
        "shipping_carrier", 4,
        "شركات الشحن",
        "الشركات المعتمدة (سمسا، أرامكس، ريدبوكس…)، أوقات الالتقاط.",
    ),
    SectionKind(
        "shipping_zones", 4,
        "المناطق ومدد التوصيل",
        "المناطق المغطاة، مدة التوصيل المتوقعة لكل منطقة.",
    ),
    SectionKind(
        "cold_shipping", 4,
        "الشحن المبرد",
        "متى يستخدم؟ ما هي المدن المشمولة؟ هل فيه رسوم إضافية؟",
    ),
    SectionKind(
        "summer_note", 4,
        "ملاحظات الصيف",
        "إجراءات الصيف الخاصة (تغليف حراري، جدولة شحن…).",
    ),

    # 5 — Product extras (typically product-bound — Phase 3 wires this).
    SectionKind(
        "product_usage", 5,
        "طريقة الاستخدام",
        "الجرعة، التوقيت، الفئة المستهدفة، تحذيرات الاستخدام.",
        is_product_bound=True,
    ),
    SectionKind(
        "product_recipe", 5,
        "وصفة",
        "وصفات تستخدم المنتج، خطوات التحضير.",
        is_product_bound=True,
    ),
    SectionKind(
        "product_benefit", 5,
        "فوائد عامة",
        "فوائد عامة بدون ادعاءات علاجية.",
        is_product_bound=True,
    ),
    SectionKind(
        "product_storage", 5,
        "التخزين",
        "كيف يُحفظ المنتج؟ درجة الحرارة، مدة الصلاحية بعد الفتح.",
        is_product_bound=True,
    ),
    SectionKind(
        "product_compare", 5,
        "الفروقات بين المنتجات",
        "مقارنات سريعة بين المنتجات المتشابهة لمساعدة العميل في الاختيار.",
        is_product_bound=True,
    ),

    # Cross-cutting fallbacks
    SectionKind(
        "faq", 3,
        "سؤال شائع",
        "سؤال تكرر من العملاء وإجابته الجاهزة.",
    ),
    SectionKind(
        "custom", 2,
        "ملاحظة مخصصة",
        "أي معلومة لم تجد لها قسماً مناسباً.",
    ),
]


# ── Lookup helpers ───────────────────────────────────────────────────────────


_BY_KIND: Dict[str, SectionKind] = {sk.kind: sk for sk in REGISTRY}


def all_kinds() -> List[SectionKind]:
    return list(REGISTRY)


def get_kind(kind: str) -> Optional[SectionKind]:
    return _BY_KIND.get((kind or "").strip().lower())


def is_valid_kind(kind: Optional[str]) -> bool:
    if not kind:
        return False
    return kind.strip().lower() in _BY_KIND


def group_for(kind: str) -> int:
    """Return the dashboard bucket index (1..6) for ``kind``.

    Unknown kinds fall back to bucket 2 ("معلومات المتجر") — that
    keeps the UI from losing rows if the registry is extended on the
    backend before the dashboard catches up.
    """
    sk = get_kind(kind)
    return sk.group if sk else 2


def kinds_in_group(group: int) -> List[SectionKind]:
    return [sk for sk in REGISTRY if sk.group == group]


# Set of allowed link_role values (for ``MerchantKnowledgeMedia.link_role``).
# Keep in lockstep with the migration default + the dashboard dropdown.
ALLOWED_LINK_ROLES = (
    "primary",
    "evidence",
    "barcode",
    "tutorial_video",
    "recipe_video",
    "policy_pdf",
    "certificate",
    "map",
)


def is_valid_link_role(role: Optional[str]) -> bool:
    if not role:
        return False
    return role.strip().lower() in ALLOWED_LINK_ROLES
