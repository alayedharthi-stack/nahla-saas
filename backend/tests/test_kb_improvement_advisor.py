"""
backend/tests/test_kb_improvement_advisor.py
────────────────────────────────────────────
KB-Improve V1 (Knowledge Improvement Suggestions, May 2026 #24).

Covers the 10 scenarios listed in the feature spec:

 1. Tenant without payment_method → suggests a payment policy.
 2. Tenant without shipping kind  → suggests a shipping policy.
 3. Behavioral text inside a commerce row → suggests moving it to
    assistant_behavior.
 4. Two same-kind rows with overlapping bodies → suggests a merge.
 5. Bank-transfer row without any attached media → suggests a barcode.
 6. Platform-connected tenant with a price-claim body → that suggestion
    is dropped (no price hints land in the KB).
 7. Completely empty tenant → returns foundational suggestions without
    crashing.
 8. Well-covered KB → returns few or zero suggestions.
 9. Suggestion list is hard-capped at 5 even if more findings exist.
10. ``polish_with_gpt`` is a no-op when ``OPENAI_API_KEY`` is unset.

The advisor accepts plain objects (it only reads .id / .kind / .title /
.body / .is_active / .media_links) so these tests use lightweight
``_Row`` stubs and stay independent of SQLAlchemy + the live DB.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import pytest

# Ensure ``backend/`` is on the path under pytest's repo-root collection.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ───────────────────────────────────────────────────────────────────


class _FakeMedia:
    """A media row attached to a knowledge section."""
    def __init__(self, *, is_active: bool = True) -> None:
        self.is_active = is_active


class _FakeLink:
    def __init__(self, media: _FakeMedia) -> None:
        self.media = media


class _Row:
    """Minimal stand-in for ``MerchantKnowledgeSection``."""
    def __init__(
        self,
        id: int,
        kind: str,
        title: str = "",
        body: str = "",
        *,
        is_active: bool = True,
        media_links: Optional[List[_FakeLink]] = None,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.is_active = is_active
        self.media_links = media_links or []


def _baseline_kb() -> List[_Row]:
    """Reusable "well-covered" KB so the empty-suggestion tests can
    layer one missing piece at a time."""
    return [
        _Row(1, "payment_method", "طرق الدفع",
             "نقبل مدى، فيزا، آبل باي، والتحويل البنكي. للتحويل البنكي "
             "أرسل لنا رسالة وسنرسل البيانات."),
        _Row(2, "shipping_zones", "مناطق الشحن",
             "نشحن إلى جميع مدن المملكة عبر سمسا وأرامكس خلال 2-4 أيام عمل."),
        _Row(3, "return_policy", "الاسترجاع",
             "نقبل الاسترجاع خلال 14 يوماً من تاريخ الاستلام إذا كان "
             "المنتج بحالته الأصلية."),
        _Row(4, "working_hours", "أوقات العمل",
             "السبت إلى الخميس 9 صباحاً - 9 مساءً."),
        _Row(5, "escalation_rules", "التحويل",
             "حوّل لموظف بشري عند الشكوى أو تأخر الشحنة."),
        _Row(6, "response_tone", "النبرة", "لهجة سعودية ودية مختصرة."),
        _Row(7, "forbidden_phrases", "ممنوع",
             "لا تستخدم: «حبيبي»، «قلبي»، «يا غالي»."),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Missing payment policy → suggestion
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_payment_method_yields_suggestion() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    # Start from baseline, drop the payment row.
    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]

    suggestions = audit(rows, platform_connected=False, products=[])
    payment = [s for s in suggestions if s.target_kind == "payment_method"]
    assert len(payment) == 1
    assert payment[0].type == "missing_required_knowledge"
    assert payment[0].severity == "high"
    assert "سياسة دفع" in payment[0].title or "الدفع" in payment[0].title
    # The body must include a placeholder (no fabricated bank numbers).
    assert "[" in payment[0].proposed_body and "]" in payment[0].proposed_body


# ─────────────────────────────────────────────────────────────────────────────
# 2. Missing shipping policy → suggestion
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_shipping_yields_suggestion() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "shipping_zones"]
    suggestions = audit(rows, platform_connected=False, products=[])
    shipping = [s for s in suggestions if s.target_kind == "shipping_zones"]
    assert len(shipping) == 1
    assert shipping[0].type == "missing_required_knowledge"
    assert shipping[0].severity == "high"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Behavioral text inside commerce row → suggests move
# ─────────────────────────────────────────────────────────────────────────────


def test_behavior_in_commerce_section_yields_contamination_suggestion() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = _baseline_kb()
    # Force a contamination: a "shipping" row carrying a behavior rule.
    rows.append(_Row(
        99, "shipping_zones", "ملاحظة شحن",
        "نشحن إلى الرياض. ولا تقل حبيبي أو قلبي للعملاء أثناء التواصل.",
    ))

    suggestions = audit(rows, platform_connected=False, products=[])
    contam = [s for s in suggestions
              if s.type == "semantic_contamination"
              and 99 in s.related_section_ids]
    assert len(contam) == 1
    assert contam[0].severity == "high"
    assert contam[0].target_kind in {"forbidden_phrases", "response_tone",
                                      "assistant_identity", "owner_identity",
                                      "compliance_rules", "escalation_rules",
                                      "allowed_style", "emoji_policy"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Duplicate rows → suggests merge
# ─────────────────────────────────────────────────────────────────────────────


def test_duplicate_sections_yield_merge_suggestion() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = _baseline_kb()
    rows.extend([
        _Row(50, "shipping_carrier", "شركات الشحن",
             "نتعامل مع شركة سمسا وأرامكس داخل المملكة فقط."),
        _Row(51, "shipping_carrier", "شركاء الشحن",
             "نتعامل مع سمسا وأرامكس داخل المملكة فقط لا غير."),
    ])
    suggestions = audit(rows, platform_connected=False, products=[], max_suggestions=10)
    dups = [s for s in suggestions if s.type == "duplicate_merge"]
    assert dups, "expected a duplicate_merge suggestion"
    assert set(dups[0].related_section_ids) == {50, 51}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Bank-transfer row without media → suggests barcode media
# ─────────────────────────────────────────────────────────────────────────────


def test_bank_transfer_without_media_suggests_barcode() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = _baseline_kb()
    rows.append(_Row(
        77, "bank_transfer", "التحويل البنكي",
        "نقبل التحويل البنكي عبر الراجحي والأهلي. سنرسل الآيبان عند الطلب.",
        media_links=[],
    ))
    suggestions = audit(rows, platform_connected=False, products=[], max_suggestions=10)
    barcode = [s for s in suggestions
               if s.type == "missing_media" and 77 in s.related_section_ids]
    assert len(barcode) == 1
    assert barcode[0].requires_media is True
    assert barcode[0].severity == "high"


def test_bank_transfer_with_media_no_suggestion() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = _baseline_kb()
    rows.append(_Row(
        77, "bank_transfer", "التحويل البنكي",
        "نقبل التحويل البنكي عبر الراجحي.",
        media_links=[_FakeLink(_FakeMedia(is_active=True))],
    ))
    suggestions = audit(rows, platform_connected=False, products=[], max_suggestions=10)
    barcode = [s for s in suggestions
               if s.type == "missing_media" and 77 in s.related_section_ids]
    assert barcode == []


# ─────────────────────────────────────────────────────────────────────────────
# 6. Platform-connected tenant with price-claim body → suggestion dropped
# ─────────────────────────────────────────────────────────────────────────────


def test_platform_connected_drops_price_claim_suggestions() -> None:
    """The platform-conflict guard must scrub any suggestion whose
    ``proposed_body`` contains a price/stock hint when a platform
    (Salla / Zid / Shopify) is connected.

    We don't construct a "real" finding with a price body here because
    the auditor doesn't generate one; instead we monkeypatch a pass to
    confirm the filter scrubs an injected one.
    """
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        audit,
    )
    import modules.ai.knowledge.improvement_advisor as advisor

    original = advisor._pass_missing_required

    def _polluted(views, idx_start):
        out = original(views, idx_start)
        out.append(ImprovementFinding(
            id=f"sug-{idx_start + len(out)}",
            type="missing_required_knowledge",
            severity="high",
            title="سعر عام",
            reason="-",
            expected_impact="-",
            target_kind="payment_method",
            proposed_body="السعر الموحد 200 ريال لكل بوكس.",
            requires_media=False,
            confidence=0.9,
        ))
        return out

    advisor._pass_missing_required = _polluted
    try:
        suggestions_connected = audit(
            _baseline_kb(), platform_connected=True, products=[],
            max_suggestions=10,
        )
        suggestions_disconnected = audit(
            _baseline_kb(), platform_connected=False, products=[],
            max_suggestions=10,
        )
    finally:
        advisor._pass_missing_required = original

    # The polluted suggestion only survives the disconnected run.
    assert any("200 ريال" in s.proposed_body for s in suggestions_disconnected)
    assert not any("200 ريال" in s.proposed_body for s in suggestions_connected)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Empty tenant → foundational suggestions without crashing
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_tenant_yields_foundational_suggestions() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    suggestions = audit([], platform_connected=False, products=[])
    # All five high-severity missing-required types should be visible.
    types = {s.type for s in suggestions}
    assert "missing_required_knowledge" in types
    # Cap is enforced (no exception, no overflow).
    assert len(suggestions) <= 5
    # No suggestion references a non-existent section.
    for s in suggestions:
        assert s.related_section_ids == []


# ─────────────────────────────────────────────────────────────────────────────
# 8. Well-covered KB → no high-severity suggestions
# ─────────────────────────────────────────────────────────────────────────────


def test_well_covered_kb_returns_no_high_severity() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    suggestions = audit(_baseline_kb(), platform_connected=False, products=[])
    # We allow up to a single low/medium tip (e.g. compliance hints),
    # but high-severity missing-required findings should be empty.
    high = [s for s in suggestions
            if s.severity == "high" and s.type == "missing_required_knowledge"]
    assert high == []


# ─────────────────────────────────────────────────────────────────────────────
# 9. Suggestion list is capped at 5
# ─────────────────────────────────────────────────────────────────────────────


def test_suggestion_cap_enforced_at_five() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    # Empty KB will hit ALL missing-required + behavior + contamination
    # passes — we ensure the cap kicks in.
    suggestions = audit([], platform_connected=False, products=[])
    assert len(suggestions) <= 5


def test_ranking_keeps_highest_severity_first() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    suggestions = audit([], platform_connected=False, products=[])
    severities = [s.severity for s in suggestions]
    # High items must come before any medium/low item.
    seen_lower = False
    for sev in severities:
        if sev != "high":
            seen_lower = True
        elif seen_lower:
            pytest.fail(f"high severity after non-high: {severities}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. polish_with_gpt no-ops without API key
# ─────────────────────────────────────────────────────────────────────────────


def test_polisher_noop_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.knowledge.improvement_advisor import (
        audit,
        polish_with_gpt,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    findings = audit([], platform_connected=False, products=[])
    polished = polish_with_gpt(findings, tenant_id=33)
    # Same objects, same content — polisher returned originals.
    assert len(polished) == len(findings)
    for orig, post in zip(findings, polished):
        assert orig.id == post.id
        assert orig.title == post.title
        assert orig.proposed_body == post.proposed_body


def test_polisher_overlays_text_fields_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the polisher returns refined copy, only the four text
    fields are overlaid — never severity / type / confidence."""
    import modules.ai.knowledge.improvement_advisor as advisor
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        polish_with_gpt,
    )

    findings = [ImprovementFinding(
        id="sug-1", type="missing_required_knowledge", severity="high",
        title="orig title", reason="orig reason",
        expected_impact="orig impact", target_kind="payment_method",
        proposed_body="orig body", requires_media=False, confidence=0.9,
    )]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    class _Resp:
        def raise_for_status(self): return None
        def json(self):
            import json
            return {"choices": [{"message": {"content": json.dumps({
                "suggestions": [{
                    "id": "sug-1",
                    "title": "POLISHED",
                    "reason": "polished reason",
                    "expected_impact": "polished impact",
                    "proposed_body": "polished body",
                }],
            }, ensure_ascii=False)}}]}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def post(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)

    polished = polish_with_gpt(findings, tenant_id=1)
    assert polished[0].title == "POLISHED"
    assert polished[0].proposed_body == "polished body"
    # Untouched fields preserved.
    assert polished[0].severity == "high"
    assert polished[0].confidence == 0.9
    assert polished[0].type == "missing_required_knowledge"
    assert polished[0].target_kind == "payment_method"


# ─────────────────────────────────────────────────────────────────────────────
# Bonus: observability emit
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# KB-Improve V1.1 — fingerprint + suppression
# ─────────────────────────────────────────────────────────────────────────────


def test_fingerprint_stable_across_runs() -> None:
    """The same KB content must produce the same fingerprints on two
    independent ``audit()`` calls — that's the whole basis for
    cross-session suppression to work."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
    fps_first  = sorted(s.fingerprint for s in audit(rows, products=[]))
    fps_second = sorted(s.fingerprint for s in audit(rows, products=[]))
    assert fps_first == fps_second
    # And fingerprints are non-empty 16-char hex.
    for fp in fps_first:
        assert len(fp) == 16
        int(fp, 16)  # raises if non-hex


def test_fingerprint_differs_between_distinct_suggestions() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    # Empty KB → multiple distinct findings; their fps must collide
    # only for actually-equal suggestions.
    suggestions = audit([], platform_connected=False, products=[])
    fps = [s.fingerprint for s in suggestions]
    assert len(fps) == len(set(fps)), (
        f"distinct suggestions share a fingerprint: {fps}"
    )


def test_fingerprint_survives_polish_paraphrase() -> None:
    """A cosmetic title polish must NOT change the fingerprint —
    otherwise a merchant who dismissed the original would see the
    polished version reappear on the next run."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        compute_fingerprint,
    )

    fp_a = compute_fingerprint(
        type_="missing_required_knowledge",
        target_kind="payment_method",
        title="أضف سياسة دفع واضحة",
    )
    fp_b = compute_fingerprint(
        type_="missing_required_knowledge",
        target_kind="payment_method",
        title="  أضف  سياسة  دفع  واضحة  ",  # whitespace mutation
    )
    assert fp_a == fp_b

    # And the ImprovementFinding post-init re-derives the same fp:
    finding = ImprovementFinding(
        id="sug-1", type="missing_required_knowledge", severity="high",
        title="أضف سياسة دفع واضحة", reason="x", expected_impact="y",
        target_kind="payment_method", proposed_body="b",
        requires_media=False, confidence=0.9,
    )
    assert finding.fingerprint == fp_a


def test_suppressed_fingerprints_filter_results() -> None:
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
    baseline = audit(rows, products=[])
    assert any(s.target_kind == "payment_method" for s in baseline)

    payment_fp = next(s.fingerprint for s in baseline
                      if s.target_kind == "payment_method")

    suppressed = audit(rows, products=[],
                       suppressed_fingerprints=[payment_fp])
    assert all(s.fingerprint != payment_fp for s in suppressed)


def test_min_confidence_floor_drops_low_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aggressive ``min_confidence`` floor must drop every finding,
    proving the gate is wired into ``audit()`` (and isn't just sort-
    order shuffling)."""
    from modules.ai.knowledge.improvement_advisor import audit

    suggestions = audit([], products=[], min_confidence=0.99)
    assert suggestions == []


def test_record_dismissal_writes_into_ai_settings() -> None:
    from modules.ai.knowledge.improvement_advisor import (
        active_dismissed_fingerprints,
        record_dismissal,
    )

    base = {"reply_tone": "friendly"}
    updated = record_dismissal(
        base, fingerprint="abc1234567890def",
        suggestion_type="missing_required_knowledge",
        target_kind="payment_method", ttl_days=7,
    )
    # Original dict not mutated.
    assert "kb_improvement_state" not in base
    state = updated["kb_improvement_state"]
    assert len(state["dismissed"]) == 1
    entry = state["dismissed"][0]
    assert entry["fp"] == "abc1234567890def"
    assert entry["target_kind"] == "payment_method"
    assert "ts" in entry and "expires_at" in entry

    # Re-dismissing the same fp must REPLACE the older entry (TTL reset),
    # not duplicate it.
    twice = record_dismissal(updated, fingerprint="abc1234567890def")
    assert len(twice["kb_improvement_state"]["dismissed"]) == 1

    # And ``active_dismissed_fingerprints`` returns it.
    fps = active_dismissed_fingerprints(twice)
    assert fps == {"abc1234567890def"}


def test_record_dismissal_prunes_expired_entries() -> None:
    from datetime import datetime, timedelta, timezone

    from modules.ai.knowledge.improvement_advisor import (
        active_dismissed_fingerprints,
        record_dismissal,
    )

    expired_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    expired_exp = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    base = {
        "kb_improvement_state": {
            "dismissed": [
                {"fp": "old-expired", "ts": expired_ts,
                 "expires_at": expired_exp,
                 "type": "missing_required_knowledge",
                 "target_kind": "shipping_zones"},
            ]
        }
    }

    # Adding a fresh dismissal should sweep the expired one.
    updated = record_dismissal(base, fingerprint="fresh-fp")
    fps = {e["fp"] for e in updated["kb_improvement_state"]["dismissed"]}
    assert fps == {"fresh-fp"}, fps

    # Read-side filter agrees.
    active = active_dismissed_fingerprints(updated)
    assert "old-expired" not in active
    assert "fresh-fp" in active


def test_active_dismissed_filters_expired_at_read_time() -> None:
    from datetime import datetime, timedelta, timezone

    from modules.ai.knowledge.improvement_advisor import (
        active_dismissed_fingerprints,
    )

    expired_exp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    live_exp    = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    state = {
        "kb_improvement_state": {
            "dismissed": [
                {"fp": "expired-fp",
                 "ts": expired_exp, "expires_at": expired_exp},
                {"fp": "live-fp",
                 "ts": "x", "expires_at": live_exp},
            ]
        }
    }
    assert active_dismissed_fingerprints(state) == {"live-fp"}


# ─────────────────────────────────────────────────────────────────────────────


def test_emit_improvement_log_writes_structured_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import time as _time
    from modules.ai.knowledge.improvement_advisor import (
        audit,
        emit_improvement_log,
    )

    caplog.set_level(logging.INFO, logger="nahla.ai.knowledge.improvement_advisor")
    findings = audit([], platform_connected=False, products=[])
    emit_improvement_log(
        tenant_id=33, suggestions=findings, started=_time.monotonic() - 0.05,
        model="gpt-4.1", fallback=False,
    )
    msg = next((r.getMessage() for r in caplog.records
                if "[KB_IMPROVEMENT_SUGGESTIONS]" in r.getMessage()), None)
    assert msg is not None
    assert "tenant_id=33" in msg
    assert "suggestions_count=" in msg
    assert "high_severity_count=" in msg
    assert "missing_required_count=" in msg
    assert "model=gpt-4.1" in msg
