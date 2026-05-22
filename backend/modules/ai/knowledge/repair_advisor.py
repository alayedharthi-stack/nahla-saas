"""
backend/modules/ai/knowledge/repair_advisor.py
─────────────────────────────────────────────
KB-2 — Knowledge-base repair advisor (preview-only).

This module powers the merchant-facing "اقتراح إعادة تنظيم قاعدة المعرفة"
button. It scans the current ``merchant_knowledge_sections`` rows for a
tenant and returns *suggestions* — never mutations — covering three
classes of structural issue:

1. **Mis-taxonomy**: a section whose body strongly signals a behavioral
   intent (forbidden phrase / tone / escalation / persona) but whose
   ``kind`` is a commerce kind (``store_info``, ``payment_method``,
   ``shipping_*``, …). This is the exact contamination KB-2 aims to
   prevent at write time — the advisor handles the migration of rows
   created before the new classifier landed.

2. **Duplicate**: two or more rows that share the same ``kind`` and
   have overlapping bodies (Jaccard-on-token similarity ≥ 0.6). The
   merchant likely typed the same fact twice via different routes
   (quick-update vs direct edit).

3. **Contamination**: a section whose body mixes commerce facts
   (price / stock / branch hours / SKUs / payment QR…) with behavioral
   text (tone rules / forbidden phrases). The advisor flags the row so
   the merchant can split it manually before approving the move.

The advisor is intentionally HEURISTIC — no LLM call. The merchant
sees a preview, approves what makes sense, and clicks "تطبيق" on the
individual suggestions (Phase B, not part of this sprint). Keeping the
advisor LLM-free means it runs in O(n²) over the tenant's sections
(usually n < 50) in a single request — no background job needed.

Why preview-only:
  * Auto-moving rows would surprise merchants who had reasons for the
    placement.
  * The classifier already prevents new contamination — this is
    cleanup, not policy.
  * The owner dashboard's AI quality monitor surfaces these suggestions
    aggregated across tenants so the platform team can spot patterns
    without auditing each tenant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ── Heuristic keyword sets ──────────────────────────────────────────────────
#
# Keeping these inline (not in a yaml/json) so the advisor stays a single
# importable unit. The lists are intentionally short and high-precision:
# false positives in this surface cost merchant trust faster than false
# negatives. New patterns should be added with a real production
# example in the comment above them.

# Strong markers that a piece of text is a BEHAVIORAL rule:
#   "لا تقل حبيبي للعملاء"
#   "استخدم لهجة خليجية مختصرة"
#   "حول لموظف إذا طلب شكوى"
_BEHAVIORAL_PATTERNS: List[re.Pattern[str]] = [
    re.compile(p) for p in (
        r"\bلا\s+تقل\b",
        r"\bممنوع\s+(?:تقول|الرد|كلمة)\b",
        r"\bتجن[ّب]?\s+(?:كلمة|قول)\b",
        r"\b(?:استخدم|اعتمد)\s+لهجة\b",
        r"\bنبرة\s+الرد\b",
        r"\bأسلوب\s+الرد\b",
        r"\b(?:حبيبي|قلبي|يا\s+غالي)\b",
        r"\bحوّل\s+(?:ل|إلى)\s*(?:موظف|بشري|الفريق)\b",
        r"\b(?:صعّد|escalat\w*)\b",
        r"\bإيموجي\b",
        r"\bممنوع\s+ادّعاء\b",
        r"\bممنوع\s+ادعاء\b",
        r"\b(?:شخصية|هوية)\s+(?:المساعد|الذكاء|البوت)\b",
    )
]

# Strong markers that a piece of text is a COMMERCE fact. We use these
# in two places: to flag mis-taxonomy when behavior text sits in a
# commerce kind, and to flag CONTAMINATION when behavioral patterns
# coexist with these in the same row.
_COMMERCE_PATTERNS: List[re.Pattern[str]] = [
    re.compile(p) for p in (
        r"\d+\s*(?:ريال|ر\.?س|SAR|sar|درهم|aed|usd|\$)",
        r"\b(?:تحويل|حوالة|آيبان|IBAN|الراجحي|الأهلي|البنك\s+الأهلي)\b",
        r"\b(?:شحن|توصيل|سمسا|أرامكس|دي\s*اتش\s*ال|redbox)\b",
        r"\b(?:متوفر|غير\s+متوفر|نفد|نفذ|in\s+stock|out\s+of\s+stock)\b",
        r"\bالأرباع\b|\bالأنصاف\b|\bبوكس\b|\bعلبة\b",
        r"\bالدفع\s+عند\s+الاستلام\b|\bCOD\b",
    )
]


def _has_behavioral_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _BEHAVIORAL_PATTERNS)


def _has_commerce_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMMERCE_PATTERNS)


# ── Suggestion shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepairSuggestion:
    """One row in the advisor's report.

    The ``id`` is stable across runs for the same finding so the
    dashboard can dedupe between page loads — it's a hash of
    (kind, section_ids, suggested_kind) — but we keep that an
    implementation detail of the caller.
    """
    kind: str                       # 'move' | 'duplicate' | 'contamination'
    severity: str                   # 'info' | 'warn' | 'critical'
    section_ids: Tuple[int, ...]
    title_preview: str
    body_preview: str
    current_kind: Optional[str]
    suggested_kind: Optional[str]
    reason_ar: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "section_ids": list(self.section_ids),
            "title_preview": self.title_preview,
            "body_preview": self.body_preview,
            "current_kind": self.current_kind,
            "suggested_kind": self.suggested_kind,
            "reason_ar": self.reason_ar,
        }


# ── Core helpers ────────────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Lowercase token set used for the Jaccard similarity comparison.

    Only ASCII word characters + the Arabic letter range (U+0600–U+06FF)
    are kept. Tokens shorter than 2 chars are dropped (they're almost
    always noise — particles, lone digits).
    """
    if not text:
        return set()
    raw = (_TOKEN_RE.findall(text.lower()) or [])
    return {tok for tok in raw if len(tok) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _suggest_behavioral_kind(text: str) -> str:
    """Map a free-form behavioral string to the best ``assistant_behavior``
    subtype. Falls back to ``forbidden_phrases`` when ambiguous — that
    kind is the broadest behavioral catch-all (it can carry tone hints
    too).

    The order of checks matters: more specific patterns first.
    """
    lower = text.lower()
    if re.search(r"\bإيموجي|emoji\b", lower):
        return "emoji_policy"
    if re.search(r"\bنبرة|لهجة|أسلوب\s+الرد\b", lower):
        return "response_tone"
    if re.search(r"\bحوّل|صعّد|الفريق|موظف\s+بشري\b", lower):
        return "escalation_rules"
    if re.search(r"\bممنوع\s+ادّعاء|ادعاء\s+علاجي|قانون|شرع\b", lower):
        return "compliance_rules"
    if re.search(r"\b(?:هوية|شخصية)\s+(?:المساعد|الذكاء|البوت)\b", lower):
        return "assistant_identity"
    if re.search(r"\bصاحب\s+المتجر|اسم\s+المالك\b", lower):
        return "owner_identity"
    if re.search(r"\b(?:أسلوب|طريقة)\s+(?:الكلام|الحديث)\b", lower):
        return "allowed_style"
    return "forbidden_phrases"


# ── Public API ──────────────────────────────────────────────────────────────


def analyze_sections(rows: Sequence[Any]) -> List[RepairSuggestion]:
    """Scan section rows and return repair suggestions.

    Accepts ORM rows OR plain objects with ``.id``, ``.kind``, ``.title``,
    ``.body`` attributes (so unit tests can pass lightweight stubs).

    The advisor is deterministic — running it twice on the same input
    produces the same suggestions in the same order. That property is
    important: the dashboard relies on it for the "X suggestions found"
    badge to stay stable between refreshes.
    """
    try:
        from services.knowledge_section_kinds import (  # noqa: PLC0415
            BEHAVIORAL_KINDS,
        )
    except Exception:  # noqa: BLE001 — keep advisor importable in tests
        BEHAVIORAL_KINDS = frozenset()

    suggestions: List[RepairSuggestion] = []

    # ── Pass 1: mis-taxonomy + contamination ────────────────────────────
    for r in rows:
        kind = (getattr(r, "kind", "") or "").strip().lower()
        title = (getattr(r, "title", None) or "").strip()
        body = (getattr(r, "body", None) or "").strip()
        joined = f"{title}\n{body}".strip()

        behavior_hit = _has_behavioral_signal(joined)
        commerce_hit = _has_commerce_signal(joined)
        is_behavioral_kind = kind in BEHAVIORAL_KINDS

        # Order of checks (most specific first):
        #   1. Mixed content in a COMMERCE row → contamination(critical)
        #      — this is the worst case because Claude pulls commerce
        #      rows during retrieval and would drag the behavior text
        #      along with it.
        #   2. Mixed content in a BEHAVIORAL row → contamination(warn)
        #      — still wrong, but the wrong-direction leak is into
        #      the high-priority layer (less harmful than into facts).
        #   3. Pure behavioral content in a commerce row → move(warn)
        #      — clean migration target available.
        if behavior_hit and commerce_hit and not is_behavioral_kind:
            suggestions.append(RepairSuggestion(
                kind="contamination",
                severity="critical",
                section_ids=(int(r.id),),
                title_preview=title[:80],
                body_preview=body[:160].replace("\n", " "),
                current_kind=kind,
                suggested_kind=_suggest_behavioral_kind(joined),
                reason_ar=(
                    "هذا القسم يحتوي قواعد سلوكية + بيانات تجارية في نفس "
                    "السطر. الذكاء يستحضر القسم وقت سؤال العميل عن الدفع/"
                    "الشحن فيستحضر معه القواعد السلوكية. يُفضّل التقسيم."
                ),
            ))
        elif behavior_hit and commerce_hit and is_behavioral_kind:
            suggestions.append(RepairSuggestion(
                kind="contamination",
                severity="warn",
                section_ids=(int(r.id),),
                title_preview=title[:80],
                body_preview=body[:160].replace("\n", " "),
                current_kind=kind,
                suggested_kind=None,
                reason_ar=(
                    "هذا القسم يخلط قواعد سلوكية مع معلومات تجارية "
                    "(أسعار/شحن/توفر). يُفضّل تقسيمه إلى قسمين منفصلين."
                ),
            ))
        elif behavior_hit and not is_behavioral_kind:
            # Pure behavioral content sitting in a commerce / store_info /
            # custom row — exactly the contamination KB-2 prevents at
            # write time. Suggest moving to the best-fitting subtype.
            suggested = _suggest_behavioral_kind(joined)
            suggestions.append(RepairSuggestion(
                kind="move",
                severity="warn",
                section_ids=(int(r.id),),
                title_preview=title[:80],
                body_preview=body[:160].replace("\n", " "),
                current_kind=kind,
                suggested_kind=suggested,
                reason_ar=(
                    "هذا القسم مكتوب فيه قواعد سلوكية للمساعد (نبرة، كلمات "
                    "ممنوعة، تحويل…) لكنه مصنّف ضمن المعرفة التجارية. "
                    f"الأنسب نقله إلى «{suggested}» داخل مجموعة سلوك المساعد."
                ),
            ))

    # ── Pass 2: duplicates (O(n²) but n is small) ────────────────────────
    rows_list = list(rows)
    seen_pairs: set[Tuple[int, int]] = set()
    for i, a in enumerate(rows_list):
        a_kind = (getattr(a, "kind", "") or "").strip().lower()
        a_tokens = _tokens(f"{getattr(a, 'title', '') or ''} "
                           f"{getattr(a, 'body', '') or ''}")
        if not a_tokens:
            continue
        for b in rows_list[i + 1:]:
            b_kind = (getattr(b, "kind", "") or "").strip().lower()
            if a_kind != b_kind:
                continue
            b_tokens = _tokens(f"{getattr(b, 'title', '') or ''} "
                               f"{getattr(b, 'body', '') or ''}")
            if not b_tokens:
                continue
            sim = _jaccard(a_tokens, b_tokens)
            if sim < 0.6:
                continue
            ids = (int(a.id), int(b.id))
            ids_sorted = (min(ids), max(ids))
            if ids_sorted in seen_pairs:
                continue
            seen_pairs.add(ids_sorted)
            suggestions.append(RepairSuggestion(
                kind="duplicate",
                severity="info",
                section_ids=ids_sorted,
                title_preview=(getattr(a, "title", "") or "")[:80],
                body_preview=(getattr(a, "body", "") or "")[:160]
                              .replace("\n", " "),
                current_kind=a_kind,
                suggested_kind=None,
                reason_ar=(
                    f"قسمان من نفس النوع «{a_kind}» تشابه نصيهما بنسبة "
                    f"{round(sim * 100)}%. يُفضّل دمجهما أو حذف الأقدم."
                ),
            ))

    return suggestions


def summarize(suggestions: Sequence[RepairSuggestion]) -> Dict[str, int]:
    """Aggregate counts the dashboard renders as the top-of-page badge."""
    counts = {"total": 0, "move": 0, "duplicate": 0, "contamination": 0,
              "critical": 0, "warn": 0, "info": 0}
    for s in suggestions:
        counts["total"] += 1
        counts[s.kind] = counts.get(s.kind, 0) + 1
        counts[s.severity] = counts.get(s.severity, 0) + 1
    return counts
