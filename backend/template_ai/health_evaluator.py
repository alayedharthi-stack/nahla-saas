"""
Template Health Evaluator
──────────────────────────
Scores every tenant template and generates merchant-facing recommendations.

Health score (0.0–1.0):
  1.0  — recently used, approved, unique objective
  0.5  — approved but unused
  0.0  — rejected, disabled, or duplicate

Recommendation types:
  delete      — template should be removed
  update      — wording should be improved
  merge       — duplicate objective exists, consolidate
  archive     — unused for a long time, no longer relevant
  none        — no action needed
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ── Thresholds (days) ──────────────────────────────────────────────────────────
UNUSED_WARNING_DAYS = 30
UNUSED_ARCHIVE_DAYS = 90
OLD_TEMPLATE_DAYS = 180


def evaluate_templates(templates: List[Any]) -> List[Dict[str, Any]]:
    """
    Score and generate recommendations for a list of WhatsAppTemplate ORM objects.

    Returns list of dicts:
    {
        template_id, health_score,
        recommendation_state,   # none | pending
        recommendation_note,
    }
    """
    results = []
    now = datetime.now(timezone.utc)

    # Build objective → templates map for duplicate detection
    obj_map: Dict[str, List[Any]] = {}
    for t in templates:
        if t.objective:
            obj_map.setdefault(t.objective, []).append(t)

    for tpl in templates:
        score, state, note = _score_template(tpl, now, obj_map)
        results.append({
            "template_id": tpl.id,
            "health_score": round(score, 2),
            "recommendation_state": state,
            "recommendation_note": note,
        })

    return results


def health_summary(templates: List[Any]) -> Dict[str, Any]:
    """
    Return a dashboard-level summary of template health across the tenant.
    """
    results = evaluate_templates(templates)
    total = len(results)
    healthy = sum(1 for r in results if r["health_score"] >= 0.7)
    needs_attention = sum(1 for r in results if r["recommendation_state"] == "pending")

    return {
        "total": total,
        "healthy": healthy,
        "needs_attention": needs_attention,
        "avg_health_score": round(
            sum(r["health_score"] for r in results) / max(total, 1), 2
        ),
        "details": results,
    }


# ── Scoring logic ──────────────────────────────────────────────────────────────

def _score_template(
    tpl: Any,
    now: datetime,
    obj_map: Dict[str, List[Any]],
) -> tuple[float, str, Optional[str]]:
    """Return (score, recommendation_state, recommendation_note)."""

    # Rejected / disabled — immediate low score
    if tpl.status in ("REJECTED", "DISABLED"):
        note = (
            "هذا القالب مرفوض من Meta. يُنصح بمراجعته وإعادة الصياغة أو حذفه."
            if tpl.status == "REJECTED"
            else "هذا القالب معطّل حالياً. تحقق من سبب التعطيل."
        )
        return 0.1, "pending", note

    # DRAFT — not submitted yet, medium score
    if tpl.status == "DRAFT":
        return 0.4, "none", None

    # PENDING — waiting for Meta, neutral
    if tpl.status == "PENDING":
        return 0.5, "none", None

    # APPROVED — evaluate quality
    score = 1.0
    state = "none"
    note: Optional[str] = None

    days_since_used = _days_since(tpl.last_used_at, now)
    days_since_created = _days_since(tpl.created_at, now)
    usage = tpl.usage_count or 0

    # ── Usage decay ───────────────────────────────────────────────────────────
    if usage == 0 and days_since_created > UNUSED_WARNING_DAYS:
        score -= 0.3
        if days_since_created > UNUSED_ARCHIVE_DAYS:
            score -= 0.2
            state = "pending"
            note = (
                f"هذا القالب لم يُستخدم منذ إنشائه قبل {days_since_created} يوماً. "
                "يُنصح بأرشفته أو حذفه."
            )
        else:
            state = "pending"
            note = f"هذا القالب لم يُستخدم بعد ({days_since_created} يوماً منذ الإنشاء)."

    elif usage > 0 and days_since_used is not None:
        if days_since_used > UNUSED_ARCHIVE_DAYS:
            score -= 0.35
            state = "pending"
            note = (
                f"آخر استخدام لهذا القالب كان منذ {days_since_used} يوماً. "
                "قد يكون لم يعد ذا صلة. يُنصح بمراجعته."
            )
        elif days_since_used > UNUSED_WARNING_DAYS:
            score -= 0.15

    # ── Duplicate objective ───────────────────────────────────────────────────
    if tpl.objective:
        duplicates = [
            t for t in obj_map.get(tpl.objective, [])
            if t.id != tpl.id and t.status == "APPROVED"
        ]
        if duplicates:
            score -= 0.25
            if state != "pending":
                state = "pending"
            other_names = ", ".join(f"'{d.name}'" for d in duplicates[:2])
            note = (
                f"يخدم هذا القالب نفس الهدف مثل: {other_names}. "
                "يُنصح بالدمج للحفاظ على قائمة قوالب نظيفة."
            )

    # ── Old template with no usage ─────────────────────────────────────────────
    if days_since_created > OLD_TEMPLATE_DAYS and usage < 5:
        score -= 0.15
        if state == "none":
            state = "pending"
            note = (
                f"هذا القالب قديم ({days_since_created} يوماً) ولم يُستخدم إلا {usage} مرة. "
                "قد يحتاج إلى تحديث في الصياغة."
            )

    score = max(0.0, min(1.0, score))
    return score, state, note


def _days_since(dt: Optional[datetime], now: datetime) -> Optional[int]:
    if dt is None:
        return None
    return (now - dt).days
