#!/usr/bin/env python3
"""
Seed goal_based_recommendation KB entries for a merchant tenant.

Admin/API path only — no dashboard UI required.

Usage:
    cd backend
    python scripts/seed_goal_kb.py --tenant-id <id> [--dry-run]

Acceptance operators may pass ``--tenant-id 33`` for the real-channel merchant.
"""
from __future__ import annotations

import argparse
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
_repo = os.path.dirname(_backend)
for _p in (_backend, os.path.join(_repo, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GOAL_KB_KIND = "goal_based_recommendation"

# Merchant expertise lives HERE — not in platform modules.
SEED_ENTRIES = [
    {
        "title": "خصوبة وحيوية",
        "goal_tags": ["fertility_vitality"],
        "products": [
            {"product_id": 107, "ref": "غذاء ملكات النحل", "role": "primary"},
            {"product_id": 109, "ref": "عسل طلح نجد", "role": "primary"},
            {"product_id": 118, "ref": "حبوب لقاح", "role": "complement"},
        ],
        "usage_guidance": [
            "ملعقة صباحًا ومساءً",
            "غذاء الملكات حسب الإرشادات على العبوة",
            "يفضل الاستمرار فترة منتظمة",
        ],
        "soft_claims": [
            "كثير من العملاء يستخدمونه ضمن روتينهم اليومي",
            "قد يناسب من يبحث عن دعم عام للحيوية",
        ],
        "followup_questions": [
            "هل الهدف استخدام يومي أم برنامج أطول؟",
        ],
        "compliance": [
            "لا يُقدّم كعلاج طبي",
            "يفضل استشارة الطبيب للحالات الصحية",
        ],
    },
    {
        "title": "طاقة يومية",
        "goal_tags": ["energy_daily"],
        "products": [
            {"product_id": 109, "ref": "عسل طلح نجد", "role": "primary"},
            {"product_id": 107, "ref": "غذاء ملكات النحل", "role": "complement"},
        ],
        "usage_guidance": ["ملعقة صباحًا على الريق"],
        "soft_claims": ["كثير من العملاء يفضلونه للطاقة اليومية"],
        "followup_questions": ["هل تفضله للصباح أو قبل النشاط؟"],
        "compliance": ["ليس بديلاً عن الراحة أو العلاج الطبي"],
    },
    {
        "title": "دعم المناعة",
        "goal_tags": ["immunity_support"],
        "products": [
            {"product_id": 116, "ref": "عسل السدر", "role": "primary"},
            {"product_id": 106, "ref": "بروبوليس", "role": "complement"},
            {"product_id": 118, "ref": "حبوب لقاح", "role": "complement"},
        ],
        "usage_guidance": ["جرعة يومية منتظمة"],
        "soft_claims": ["قد يناسب ضمن نمط حياة متوازن"],
        "followup_questions": [],
        "compliance": ["لا ادعاءات علاجية"],
    },
    {
        "title": "عافية يومية",
        "goal_tags": ["daily_wellness"],
        "products": [
            {"product_id": 109, "ref": "عسل طلح نجد", "role": "primary"},
            {"product_id": 116, "ref": "عسل السدر", "role": "optional"},
        ],
        "usage_guidance": ["ملعقة يوميًا"],
        "soft_claims": ["خيار خفيف للاستخدام اليومي"],
        "followup_questions": [],
        "compliance": [],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", type=int, required=True, help="Target tenant id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from core.database import SessionLocal  # noqa: PLC0415
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    db = SessionLocal()
    created = updated = 0
    try:
        for entry in SEED_ENTRIES:
            title = entry["title"]
            metadata = {
                "goal_tags": entry["goal_tags"],
                "products": entry["products"],
                "usage_guidance": entry.get("usage_guidance") or [],
                "soft_claims": entry.get("soft_claims") or [],
                "followup_questions": entry.get("followup_questions") or [],
                "compliance": entry.get("compliance") or [],
            }
            existing = (
                db.query(MerchantKnowledgeSection)
                .filter(
                    MerchantKnowledgeSection.tenant_id == args.tenant_id,
                    MerchantKnowledgeSection.kind == GOAL_KB_KIND,
                    MerchantKnowledgeSection.title == title,
                )
                .first()
            )
            if args.dry_run:
                print(f"DRY-RUN would upsert: {title} goals={entry['goal_tags']}")
                continue
            if existing:
                existing.metadata_json = metadata
                existing.is_active = True
                existing.body = entry.get("body") or title
                updated += 1
            else:
                db.add(
                    MerchantKnowledgeSection(
                        tenant_id=args.tenant_id,
                        kind=GOAL_KB_KIND,
                        title=title,
                        body=title,
                        metadata_json=metadata,
                        priority=50,
                        is_active=True,
                        source="manual",
                    )
                )
                created += 1
        if not args.dry_run:
            db.commit()
        print(
            f"tenant={args.tenant_id} kind={GOAL_KB_KIND} "
            f"created={created} updated={updated} dry_run={args.dry_run}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
