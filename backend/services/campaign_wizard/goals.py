"""
campaign_wizard.goals
─────────────────────
Fixed, hand-curated taxonomy of campaign goals shown as Step 1 of the
new wizard. Kept as plain dataclasses (no DB table) because:

  * The list is short (7 items) and product-managed, not merchant-managed.
  * Treating it as data lets the recommender (`recommender.py`) score
    templates against a goal without round-tripping the DB.
  * Tests can import GOALS directly and assert on the keys.

If you add a goal here, also extend:
  * recommender.score_template — keyword/category boosts
  * frontend Campaigns.tsx Step1 grid (icon + label)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CampaignGoal:
    key: str
    label_ar: str
    label_en: str
    description_ar: str
    icon: str  # lucide-react icon name; consumed by the frontend grid
    # Meta WhatsApp template categories that fit this goal. Empty tuple
    # means "no hard filter — accept any approved category" (used by
    # `broadcast` and `custom`).
    allowed_meta_categories: Tuple[str, ...]
    # Default segment selected when the merchant picks this goal — pre-fills
    # Step 2 so the wizard feels predictive, not interrogative.
    default_segment_key: str
    # Free-text keywords (Arabic + English) that, when found in a template's
    # body, give the template a "best for this goal" boost in the recommender.
    keywords: Tuple[str, ...] = field(default_factory=tuple)


# Order here is the order the merchant sees in the UI.
GOALS: Tuple[CampaignGoal, ...] = (
    CampaignGoal(
        key="welcome",
        label_ar="ترحيب بالعملاء الجدد",
        label_en="Welcome new customers",
        description_ar="رسالة ترحيب أولى للعملاء الذين سجّلوا حديثاً",
        icon="HandHeart",
        allowed_meta_categories=("UTILITY", "MARKETING"),
        default_segment_key="new",
        keywords=("ترحيب", "مرحباً", "مرحبا", "أهلاً", "أهلا", "welcome", "hello"),
    ),
    CampaignGoal(
        key="promotion",
        label_ar="عرض ترويجي",
        label_en="Promotion",
        description_ar="عرض أو خصم محدد بوقت لجذب الشراء",
        icon="Tag",
        allowed_meta_categories=("MARKETING",),
        default_segment_key="all",
        keywords=("خصم", "عرض", "كوبون", "تخفيض", "%", "sale", "promo", "offer", "discount"),
    ),
    CampaignGoal(
        key="reactivation",
        label_ar="تنشيط العملاء الخاملين",
        label_en="Reactivation",
        description_ar="استعادة العملاء الذين توقّفوا عن الشراء أو التفاعل",
        icon="RefreshCw",
        allowed_meta_categories=("MARKETING",),
        default_segment_key="dormant",
        keywords=("نفتقدك", "افتقدناك", "عودة", "رجعنا لك", "winback", "comeback", "miss"),
    ),
    CampaignGoal(
        key="reorder",
        label_ar="إعادة شراء",
        label_en="Reorder",
        description_ar="حفز عملاءك المتكررين على إعادة شراء منتجاتهم المفضّلة",
        icon="Repeat",
        allowed_meta_categories=("MARKETING",),
        default_segment_key="repeat",
        keywords=("إعادة", "كرّر", "كرر", "نفد", "reorder", "again"),
    ),
    CampaignGoal(
        key="reminder",
        label_ar="تذكير",
        label_en="Reminder",
        description_ar="تذكير بإكمال الطلب، الدفع، أو موعد قادم",
        icon="Bell",
        allowed_meta_categories=("UTILITY",),
        default_segment_key="abandoned_cart",
        keywords=("تذكير", "لا تنسَ", "لا تنسى", "أكمل", "remind", "reminder", "complete"),
    ),
    CampaignGoal(
        key="broadcast",
        label_ar="حملة عامة",
        label_en="Broadcast",
        description_ar="إعلان واسع لجميع عملائك أو شريحة كبيرة",
        icon="Megaphone",
        allowed_meta_categories=(),  # accept any approved category
        default_segment_key="all",
        keywords=(),
    ),
    CampaignGoal(
        key="custom",
        label_ar="حملة مخصصة",
        label_en="Custom",
        description_ar="بدون توصيات — اختر القالب والشريحة بنفسك",
        icon="Settings2",
        allowed_meta_categories=(),
        default_segment_key="all",
        keywords=(),
    ),
)


_BY_KEY: Dict[str, CampaignGoal] = {g.key: g for g in GOALS}


def list_goals() -> List[Dict[str, object]]:
    """Return the public, JSON-serialisable representation used by the API."""
    return [
        {
            "key": g.key,
            "label_ar": g.label_ar,
            "label_en": g.label_en,
            "description_ar": g.description_ar,
            "icon": g.icon,
            "allowed_meta_categories": list(g.allowed_meta_categories),
            "default_segment_key": g.default_segment_key,
        }
        for g in GOALS
    ]


def get_goal(key: str) -> Optional[CampaignGoal]:
    """Lookup helper. Returns None if the key is unknown — callers must
    decide whether to 404 or fall back to `custom`."""
    return _BY_KEY.get((key or "").strip().lower())
