"""
core/automations_seed.py
────────────────────────
Shared automation seeding logic.
Extracted from routers/automations.py to break cross-router coupling
(routers/intelligence.py used to import directly from routers/automations.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from models import SmartAutomation

SEED_AUTOMATIONS: List[Dict[str, Any]] = [
    {
        "automation_type": "cart_recovery",
        "name":   "استرجاع السلات المتروكة",
        "enabled": True,
        "config": {
            "delay_minutes": 60,
            "message_template": "مرحباً {customer_name}، لاحظنا أنك تركت بعض المنتجات في سلتك. هل تريد إكمال طلبك؟",
        },
    },
    {
        "automation_type": "reorder_reminder",
        "name":   "تذكير بإعادة الطلب",
        "enabled": True,
        "config": {
            "days_since_last_order": 21,
            "message_template": "مرحباً {customer_name}، مرت {days} أيام على آخر طلب. هل تريد إعادة الطلب؟",
        },
    },
    {
        "automation_type": "welcome_message",
        "name":   "رسالة ترحيب",
        "enabled": True,
        "config": {
            "message_template": "أهلاً {customer_name}، يسعدنا تواصلك مع {store_name}. كيف يمكننا مساعدتك؟",
        },
    },
]


def seed_automations_if_empty(db: Session, tenant_id: int) -> None:
    """Seed default automations for a tenant if none exist yet."""
    count = db.query(SmartAutomation).filter(
        SmartAutomation.tenant_id == tenant_id
    ).count()
    if count == 0:
        for seed in SEED_AUTOMATIONS:
            auto = SmartAutomation(
                tenant_id=tenant_id,
                automation_type=seed["automation_type"],
                name=seed["name"],
                enabled=seed["enabled"],
                config=seed["config"],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(auto)
        db.flush()
