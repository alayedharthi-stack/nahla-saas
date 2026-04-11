"""
Template Policy Validator
──────────────────────────
Runs pre-submission compliance checks on a template draft before it is sent
to Meta. Prevents clutter, duplicate templates, and policy violations.

Returns a ValidationResult with:
  passed      — True if safe to proceed
  action      — 'submit' | 'merge' | 'review' | 'block'
  issues      — list of human-readable issue strings
  merge_with  — template id to merge with (when action='merge')
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ValidationResult:
    passed: bool
    action: str                        # submit | merge | review | block
    issues: List[str] = field(default_factory=list)
    merge_with_id: Optional[int] = None
    merge_with_name: Optional[str] = None


# ── Public API ─────────────────────────────────────────────────────────────────

def validate_draft(
    draft: Dict[str, Any],
    existing_templates: List[Any],   # list of WhatsAppTemplate ORM objects
) -> ValidationResult:
    """
    Run all policy checks against the draft dict and existing tenant templates.

    draft keys expected: name, category, objective, components, variables, language
    """
    issues: List[str] = []

    # 1. Duplicate name check
    duplicate = _find_duplicate_name(draft, existing_templates)
    if duplicate:
        return ValidationResult(
            passed=False,
            action="block",
            issues=[f"قالب باسم '{draft['name']}' موجود مسبقاً (#{duplicate.id})."],
        )

    # 2. Same-objective duplicate check
    same_obj = _find_same_objective(draft, existing_templates)
    if same_obj:
        approved_dupe = same_obj[0]
        if approved_dupe.status == "APPROVED":
            return ValidationResult(
                passed=False,
                action="merge",
                issues=[
                    f"قالب معتمد لنفس الهدف '{draft.get('objective')}' موجود بالفعل: "
                    f"'{approved_dupe.name}' (#{approved_dupe.id}). "
                    "يُنصح بدمجه بدلاً من إنشاء قالب جديد."
                ],
                merge_with_id=approved_dupe.id,
                merge_with_name=approved_dupe.name,
            )
        # Pending duplicate — flag for review
        issues.append(
            f"قالب بنفس الهدف في انتظار الموافقة: '{approved_dupe.name}' (#{approved_dupe.id})."
        )

    # 3. Body text quality checks
    body_text = _extract_body(draft)
    body_issues = _check_body_quality(body_text)
    issues.extend(body_issues)

    # 4. Variable count check
    var_issues = _check_variable_count(draft)
    issues.extend(var_issues)

    # 5. Category sanity check
    cat_issues = _check_category(draft)
    issues.extend(cat_issues)

    if any(i.startswith("🚫") for i in issues):
        return ValidationResult(passed=False, action="block", issues=issues)

    if issues:
        return ValidationResult(passed=True, action="review", issues=issues)

    return ValidationResult(passed=True, action="submit", issues=[])


# ── Checks ─────────────────────────────────────────────────────────────────────

def _find_duplicate_name(
    draft: Dict[str, Any],
    existing: List[Any],
) -> Optional[Any]:
    name = draft.get("name", "").lower()
    for t in existing:
        if t.name.lower() == name:
            return t
    return None


def _find_same_objective(
    draft: Dict[str, Any],
    existing: List[Any],
) -> List[Any]:
    obj = draft.get("objective")
    if not obj:
        return []
    matches = [
        t for t in existing
        if t.objective == obj and t.status not in ("REJECTED", "DISABLED")
    ]
    # Sort: APPROVED first
    matches.sort(key=lambda t: 0 if t.status == "APPROVED" else 1)
    return matches


def _extract_body(draft: Dict[str, Any]) -> str:
    for comp in draft.get("components", []):
        if comp.get("type") == "BODY":
            return comp.get("text", "")
    return ""


def _check_body_quality(body: str) -> List[str]:
    issues: List[str] = []
    if not body:
        issues.append("🚫 نص الرسالة فارغ.")
        return issues

    if len(body) < 20:
        issues.append("⚠️ نص الرسالة قصير جداً (أقل من 20 حرفاً).")

    if len(body) > 1024:
        issues.append("🚫 نص الرسالة يتجاوز الحد المسموح به من Meta (1024 حرفاً).")

    # Spam signal: excessive ALL CAPS
    upper_ratio = sum(1 for c in body if c.isupper()) / max(len(body), 1)
    if upper_ratio > 0.5:
        issues.append("⚠️ النص يحتوي على نسبة عالية من الأحرف الكبيرة — قد يُرفض كـ spam.")

    # Repetitive exclamation
    if body.count("!") > 5:
        issues.append("⚠️ كثرة علامات التعجب قد تُقلّل من جودة القالب.")

    # Contains URL hardcoded (should use variable instead)
    if re.search(r"https?://", body):
        if "{{" not in body:
            issues.append("⚠️ الرسالة تحتوي على رابط ثابت — يُفضّل استخدام متغير {{n}} بدلاً منه.")

    return issues


def _check_variable_count(draft: Dict[str, Any]) -> List[str]:
    variables = draft.get("variables", {})
    count = len(variables)
    if count > 10:
        return [f"🚫 عدد المتغيرات ({count}) يتجاوز الحد المسموح به (10)."]
    if count == 0:
        return ["⚠️ لا توجد متغيرات في القالب — قد يبدو غير مخصص للعميل."]
    return []


def _check_category(draft: Dict[str, Any]) -> List[str]:
    category = draft.get("category", "")
    objective = draft.get("objective", "")

    # Utility objectives should not be MARKETING
    utility_objectives = {"order_followup", "transactional_update", "quote_followup"}
    if objective in utility_objectives and category == "MARKETING":
        return [
            f"⚠️ الهدف '{objective}' يُصنَّف عادةً كـ UTILITY — "
            "استخدام MARKETING قد يزيد احتمالية الرفض من Meta."
        ]

    valid_categories = {"MARKETING", "UTILITY", "AUTHENTICATION"}
    if category not in valid_categories:
        return [f"🚫 فئة غير صالحة: '{category}'. القيم المقبولة: MARKETING, UTILITY, AUTHENTICATION."]

    return []
