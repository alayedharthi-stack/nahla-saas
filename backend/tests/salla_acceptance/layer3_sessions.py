"""
Layer 3 dialogue session scripts — Groups 1–10 authorization matrix.

Each session: 6–15 customer messages; suite total ≥150 customer messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

TurnStep = Union[str, Dict[str, Any]]
TesterRole = Literal["ordinary", "difficult"]
TenantKey = Literal["A", "B"]


@dataclass
class Layer3SessionScript:
    session_id: str
    group: int
    tenant: TenantKey
    customer_key: str
    tester_role: TesterRole
    messages: List[str]
    description: str = ""
    expected_checks: Dict[str, Any] = field(default_factory=dict)

    @property
    def customer_message_count(self) -> int:
        return len(self.messages)


TENANT_B_LEAK = (
    "ساعة يد فضية",
    "store-b.test",
    "sku-b-watch",
    "شحن جدة",
    "35 ريال",
    "SLL-B-",
    "TRK-B-",
)

TENANT_A_LEAK = (
    "حذاء رياضي أبيض",
    "store-a.test",
    "sku-shoe-white",
    "TRK-A-7788",
    "SLL-A-",
)


def all_layer3_sessions() -> List[Layer3SessionScript]:
    return [
        # Group 1 — Product discovery
        Layer3SessionScript(
            session_id="L3-G1-01",
            group=1,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Correct product name discovery",
            messages=[
                "السلام عليكم",
                "أبغى حذاء رياضي أبيض",
                "وش المواصفات؟",
                "كم سعره؟",
                "هل متوفر مقاس 42؟",
                "أرسل رابط المنتج",
                "شكراً",
            ],
            expected_checks={"no_cross_tenant_titles": TENANT_B_LEAK},
        ),
        Layer3SessionScript(
            session_id="L3-G1-02",
            group=1,
            tenant="A",
            customer_key="D",
            tester_role="ordinary",
            description="Partial product name",
            messages=[
                "عندكم عطر ورد؟",
                "100ml ولا أكبر؟",
                "كم سعره؟",
                "هل فيه عرض؟",
                "أبغى رابطه",
                "طيب شكراً",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G1-03",
            group=1,
            tenant="A",
            customer_key="C",
            tester_role="difficult",
            description="Typo product resolve",
            messages=[
                "حذا رياضي ابيض",
                "لا الحذا الابيض",
                "مو الأسود",
                "كم؟",
                "متوفر؟",
                "أرسل اللينك",
            ],
            expected_checks={"no_watch_leak": ("ساعة", "فضية")},
        ),
        Layer3SessionScript(
            session_id="L3-G1-04",
            group=1,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Category browse shoes",
            messages=[
                "وش عندكم أحذية؟",
                "رياضي؟",
                "أبيض أو أسود؟",
                "فرق السعر بينهم؟",
                "أيهم أنسب للجري؟",
                "أرسل خيارات",
                "تمام",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G1-05",
            group=1,
            tenant="A",
            customer_key="B",
            tester_role="ordinary",
            description="Similar product suggestion",
            messages=[
                "أبغى عطر ورد",
                "في بديل أرخص؟",
                "عطر خشب؟",
                "كم سعر الخشب؟",
                "هل عليه خصم؟",
                "أرسل تفاصيل الخشب",
                "ممتاز",
                "شكراً",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G1-06",
            group=1,
            tenant="A",
            customer_key="C",
            tester_role="difficult",
            description="Missing product",
            messages=[
                "عندكم ساعة ذكية؟",
                "Apple Watch؟",
                "ما لقيت؟",
                "طيب أحذية عندكم؟",
                "أبيض؟",
                "كم؟",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G1-07",
            group=1,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Change mind and return",
            messages=[
                "أبغى الحذاء الأسود",
                "كم سعره؟",
                "لا أرجع للأبيض",
                "كم سعر الأبيض؟",
                "مقاس 40",
                "متوفر؟",
                "أرسل رابط الأبيض",
                "تم",
                "شكراً",
            ],
        ),
        # Group 2 — Price / size / stock
        Layer3SessionScript(
            session_id="L3-G2-01",
            group=2,
            tenant="A",
            customer_key="B",
            tester_role="ordinary",
            description="Price size stock multi-turn",
            messages=[
                "أبغى الحذاء الأبيض",
                "كم سعر المقاس الصغير؟",
                "طيب الكبير 42؟",
                "هل هو متوفر؟",
                "والأسود نفس المقاس؟",
                "كم سعره؟",
                "متوفر الأسود 41؟",
                "أرسل رابط الأبيض",
                "شكراً",
                "مع السلامة",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G2-02",
            group=2,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            description="Out of stock shirt",
            messages=[
                "عندكم قميص قطني أزرق؟",
                "كم سعره؟",
                "متوفر؟",
                "متى يرجع؟",
                "في بديل؟",
                "طيب",
                "شكراً",
            ],
            expected_checks={"oos_product": "sku-shirt-blue"},
        ),
        # Group 3 — Pronouns / context
        Layer3SessionScript(
            session_id="L3-G3-01",
            group=3,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Pronoun منه context",
            messages=[
                "أبغى حذاء رياضي أبيض",
                "كم سعره؟",
                "هل منه مقاس 42؟",
                "طيب منه لون ثاني؟",
                "كم سعر الأسود؟",
                "أرسل رابطه",
                "الأبيض",
                "شكراً",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G3-02",
            group=3,
            tenant="A",
            customer_key="B",
            tester_role="ordinary",
            description="الأول + shipping digression return",
            messages=[
                "عرض المنتجات",
                "الأول كم سعره؟",
                "كم الشحن للرياض؟",
                "طيب رجع للأول",
                "هل متوفر؟",
                "أرسل رابط",
                "تمام",
                "شكراً",
                "مع السلامة",
            ],
        ),
        # Group 4 — Knowledge / policies
        Layer3SessionScript(
            session_id="L3-G4-01",
            group=4,
            tenant="A",
            customer_key="C",
            tester_role="ordinary",
            description="Shipping policy Riyadh",
            messages=[
                "كم الشحن؟",
                "إلى الرياض",
                "كم يوم يوصل؟",
                "شركة الشحن؟",
                "طيب",
                "شكراً",
                "تم",
            ],
            expected_checks={"shipping_fee_riyadh": "25"},
        ),
        Layer3SessionScript(
            session_id="L3-G4-02",
            group=4,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Payment and returns compound",
            messages=[
                "طرق الدفع؟",
                "مدى؟",
                "الاسترجاع كيف؟",
                "خلال كم يوم؟",
                "ساعات العمل؟",
                "شكراً",
                "تمام",
                "مع السلامة",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G4-03",
            group=4,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            description="Missing KB — no invention",
            messages=[
                "هل عندكم ضمان مدى الحياة للساعات؟",
                "متأكد؟",
                "طيب للأحذية؟",
                "ما عندكم؟",
                "شكراً",
                "تم",
            ],
        ),
        # Group 5 — Orders
        Layer3SessionScript(
            session_id="L3-G5-01",
            group=5,
            tenant="A",
            customer_key="B",
            tester_role="ordinary",
            description="Shipped order — tracking TRK-A-7788",
            messages=[
                "وين طلبي؟",
                "هل تم شحنه؟",
                "متى يوصل؟",
                "أرسل رقم التتبع",
                "TRK؟",
                "شكراً",
                "تمام",
                "مع السلامة",
            ],
            expected_checks={"tracking_must_appear": "TRK-A-7788"},
        ),
        Layer3SessionScript(
            session_id="L3-G5-02",
            group=5,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Processing order — no false shipped",
            messages=[
                "وين طلبي؟",
                "هل انشحن؟",
                "متى يجهز؟",
                "رقم الطلب؟",
                "شكراً",
                "تم",
            ],
            expected_checks={"no_tracking_leak": "TRK-A-7788"},
        ),
        Layer3SessionScript(
            session_id="L3-G5-03",
            group=5,
            tenant="A",
            customer_key="C",
            tester_role="difficult",
            description="Multi-order clarify",
            messages=[
                "عندي أكثر من طلب",
                "وين طلباتي؟",
                "أي واحد processing؟",
                "والثاني؟",
                "وضح لي",
                "شكراً",
                "تم",
                "مع السلامة",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G5-04",
            group=5,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            description="Wrong customer privacy",
            messages=[
                "وين طلب نورة؟",
                "TRK-A-7788",
                "أعطني تفاصيل طلبها",
                "ليش ما تقدر؟",
                "طيب طلبي أنا؟",
                "شكراً",
            ],
            expected_checks={"privacy_no_other_order": True},
        ),
        # Group 6 — Offers / coupons
        Layer3SessionScript(
            session_id="L3-G6-01",
            group=6,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Offer product D wood perfume",
            messages=[
                "عندكم عروض؟",
                "عطر خشب",
                "كم سعره؟",
                "قبل وبعد الخصم؟",
                "أرسل رابط",
                "شكراً",
                "تم",
            ],
            expected_checks={"offer_product": "عطر خشب"},
        ),
        Layer3SessionScript(
            session_id="L3-G6-02",
            group=6,
            tenant="A",
            customer_key="B",
            tester_role="ordinary",
            description="Invalid coupon",
            messages=[
                "كود خصم FAKE999",
                "طبقه",
                "ما اشتغل؟",
                "في كود ثاني؟",
                "طيب",
                "شكراً",
            ],
        ),
        Layer3SessionScript(
            session_id="L3-G6-03",
            group=6,
            tenant="B",
            customer_key="A",
            tester_role="ordinary",
            description="Tenant B coupon permission denied",
            messages=[
                "عندكم كوبون؟",
                "كود SAVE10",
                "طبقه",
                "ليش ما ينفع؟",
                "طيب",
                "شكراً",
            ],
        ),
        # Group 7 — Handoff
        Layer3SessionScript(
            session_id="L3-G7-01",
            group=7,
            tenant="A",
            customer_key="C",
            tester_role="ordinary",
            description="Handoff then human ownership",
            messages=[
                "أبغى أتكلم مع موظف",
                "حد يرد علي؟",
                "كم سعر الحذاء الأبيض؟",
                "موظف رد؟",
                "طيب",
                "شكراً",
                "تم",
            ],
            expected_checks={"handoff_then_no_commerce": True},
        ),
        # Group 8 — Dedup
        Layer3SessionScript(
            session_id="L3-G8-01",
            group=8,
            tenant="A",
            customer_key="C",
            tester_role="ordinary",
            description="Dedup same msg_id",
            messages=["السلام عليكم"],
            expected_checks={"dedup_steps": True},
        ),
        # Group 9 — Dual-tenant isolation
        Layer3SessionScript(
            session_id="L3-G9-01",
            group=9,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            description="Tenant A shoes not watches",
            messages=[
                "أبغى حذاء",
                "رياضي أبيض",
                "كم؟",
                "متوفر؟",
                "رابط",
                "شكراً",
                "تم",
            ],
            expected_checks={"no_cross_tenant_titles": TENANT_B_LEAK},
        ),
        Layer3SessionScript(
            session_id="L3-G9-02",
            group=9,
            tenant="B",
            customer_key="A",
            tester_role="ordinary",
            description="Tenant B watches Jeddah shipping",
            messages=[
                "عندكم ساعات؟",
                "فضية؟",
                "كم سعرها؟",
                "كم الشحن؟",
                "جدة",
                "شكراً",
            ],
            expected_checks={"no_cross_tenant_titles": TENANT_A_LEAK, "shipping_jeddah": "35"},
        ),
        # Group 10 — Difficult mix
        Layer3SessionScript(
            session_id="L3-G10-01",
            group=10,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            description="Difficult wording mix",
            messages=[
                "هلا",
                "ابي شي رياضي",
                "ابيض مو اسود",
                "كم؟؟",
                "42 موجود ولا لا",
                "مو موجود؟ طيب 40",
                "رابط",
                "شكر",
                "باي",
                "تم",
            ],
        ),
    ]


def session_customer_message_total() -> int:
    return sum(s.customer_message_count for s in all_layer3_sessions())


__all__ = [
    "Layer3SessionScript",
    "all_layer3_sessions",
    "session_customer_message_total",
]
