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
    """A media row attached to a knowledge section.

    KB-Improve V1.2 (May 2026 #29): the auditor's missing-barcode
    pass now looks at ``media_key`` / ``link_role`` / ``title`` to
    decide whether the attached media counts as "the barcode the
    AI would send". Tests can opt in via the new fields — older
    tests that just want "some media present" keep working since
    the stub stays backward compatible.
    """
    def __init__(
        self, *, is_active: bool = True,
        media_key: str = "", title: str = "",
    ) -> None:
        self.is_active = is_active
        self.media_key = media_key
        self.title = title


class _FakeLink:
    def __init__(
        self, media: _FakeMedia, *, link_role: str = "primary",
    ) -> None:
        self.media = media
        self.link_role = link_role


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

    # Baseline minus the bank-shaped payment_method row, so #77 is
    # the ONLY bank-shaped section in the KB. With multiple bank
    # rows the new group logic emits a merge suggestion instead —
    # tested separately in test_bank_transfer_dedup_*.
    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
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
    """When a bank_transfer section already carries a barcode-shaped
    media (link_role='barcode' OR a registry payment media_key), the
    auditor must not nag the merchant to add one again.
    """
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
    rows.append(_Row(
        77, "bank_transfer", "التحويل البنكي",
        "نقبل التحويل البنكي عبر الراجحي.",
        media_links=[_FakeLink(
            _FakeMedia(
                is_active=True,
                media_key="payment_rajhi_barcode",
                title="باركود الراجحي",
            ),
            link_role="barcode",
        )],
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

    def _polluted(views, idx_start, signals=None):
        out = original(views, idx_start, signals=signals)
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
        category="MISSING_INFORMATION",
        problem="x",
    )
    fp_b = compute_fingerprint(
        type_="missing_required_knowledge",
        target_kind="payment_method",
        title="  أضف  سياسة  دفع  واضحة  ",  # whitespace mutation
        category="MISSING_INFORMATION",
        problem="x",
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


# ─────────────────────────────────────────────────────────────────────────────
# KB-Improve V1.2 (May 2026 #29): missing-barcode dedup + merge.
# ─────────────────────────────────────────────────────────────────────────────


def test_bank_transfer_dedup_five_rows_collapse_to_one_merge() -> None:
    """Five duplicate bank_transfer rows without barcodes used to
    surface as 5 separate "أضف باركود" findings (production bug
    #29). The new group logic must collapse them into a SINGLE
    merge suggestion that references all five sections.
    """
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
    ids = [201, 202, 203, 204, 205]
    for i, sid in enumerate(ids):
        rows.append(_Row(
            sid, "bank_transfer", f"التحويل البنكي {i + 1}",
            "نقبل التحويل البنكي عبر الراجحي. سنرسل الآيبان عند الطلب.",
            media_links=[],
        ))

    suggestions = audit(rows, platform_connected=False, products=[], max_suggestions=10)
    barcode_related = [
        s for s in suggestions
        if (s.type in ("missing_media", "duplicate_merge")
            and s.target_kind == "bank_transfer")
    ]
    # Exactly ONE suggestion across the whole bank_transfer group.
    assert len(barcode_related) == 1, (
        f"expected 1 merged barcode suggestion, got {len(barcode_related)}: "
        f"{[s.title for s in barcode_related]}"
    )
    sug = barcode_related[0]
    assert sug.type == "duplicate_merge"
    assert sug.requires_media is True
    assert set(sug.related_section_ids) == set(ids)


def test_bank_transfer_with_any_sibling_barcode_suppresses_all_suggestions() -> None:
    """Spec point 3: "إذا أي قسم bank_transfer لديه media role='barcode'
    أو media_key يحتوي rajhi/barcode/qr، لا تقترح نفس الاقتراح."

    One of three sibling bank_transfer rows has the Rajhi barcode
    attached — the auditor must emit zero missing-barcode suggestions
    for the whole group.
    """
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [r for r in _baseline_kb() if r.kind != "payment_method"]
    rows.append(_Row(
        301, "bank_transfer", "التحويل البنكي - الراجحي",
        "نقبل التحويل البنكي عبر الراجحي.",
        media_links=[_FakeLink(
            _FakeMedia(
                is_active=True,
                media_key="payment_rajhi_barcode",
                title="باركود الراجحي",
            ),
            link_role="barcode",
        )],
    ))
    rows.append(_Row(302, "bank_transfer", "تحويل ٢", "تحويل بنكي.", media_links=[]))
    rows.append(_Row(303, "bank_transfer", "تحويل ٣", "تحويل بنكي.", media_links=[]))

    suggestions = audit(rows, platform_connected=False, products=[], max_suggestions=10)
    # The CRITICAL invariant: zero suggestions whose rationale is
    # "add a payment barcode". A generic same-body duplicate_merge
    # (rows 302↔303 have identical bodies in this fixture) is fine
    # — that's a different concern and the merchant should be told
    # about it.
    barcode_purpose = [
        s for s in suggestions
        if s.target_kind == "bank_transfer"
        and "purpose:add_payment_barcode" in (s.rationale_keys or [])
    ]
    assert barcode_purpose == [], (
        f"expected no barcode-add suggestions when a sibling carries "
        f"the barcode, got: {[s.title for s in barcode_purpose]}"
    )


def test_purpose_dedup_drops_same_type_target_purpose_repeats() -> None:
    """Belt-and-braces: even if two passes accidentally emit findings
    with the same ``(type, target_kind, purpose:*)`` triple, the final
    dedup pass keeps only the highest-confidence one.
    """
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        _dedup_by_purpose,
    )

    low = ImprovementFinding(
        id="sug-1", type="missing_media", severity="high",
        title="A", reason="r", expected_impact="i",
        target_kind="bank_transfer", proposed_body="b",
        requires_media=True, confidence=0.60,
        related_section_ids=[10],
        rationale_keys=["purpose:add_payment_barcode"],
    )
    high = ImprovementFinding(
        id="sug-2", type="missing_media", severity="high",
        title="B", reason="r", expected_impact="i",
        target_kind="bank_transfer", proposed_body="b",
        requires_media=True, confidence=0.85,
        related_section_ids=[11],
        rationale_keys=["purpose:add_payment_barcode"],
    )
    other = ImprovementFinding(
        id="sug-3", type="missing_required_knowledge", severity="high",
        title="Other", reason="r", expected_impact="i",
        target_kind="shipping_zones", proposed_body="b",
        requires_media=False, confidence=0.9,
        related_section_ids=[],
        rationale_keys=["purpose:add_shipping_policy"],
    )
    kept = _dedup_by_purpose([low, high, other])
    kept_ids = {f.id for f in kept}
    assert "sug-2" in kept_ids  # higher confidence wins
    assert "sug-1" not in kept_ids
    assert "sug-3" in kept_ids  # different purpose stays


# ─────────────────────────────────────────────────────────────────────────────
# May 2026 #29 problem 1: payment regex + generic barcode triggers.
# Customer asks "كيف أحول لكم؟" (generic, no bank name, no "باركود"
# noun) → the legacy is_payment_query AND the modern registry
# generic-payment matcher must both fire so the safety nets can
# attach the merchant's single uploaded barcode.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "كيف أحول لكم؟",
        "كيف احول لكم",
        "ابي أحول لكم",
        "ودي أحول لكم",
        "كيف أدفع لكم؟",
        "كيف الدفع",
        "كيف التحويل",
        "وش طريقة الدفع؟",
        "وش طريقة التحويل؟",
        "طريقة الدفع",
        "طريقة التحويل",
    ],
)
def test_is_payment_query_matches_generic_transfer_phrasing(phrase: str) -> None:
    """The legacy hard-override path uses ``is_payment_query`` as
    its gate. Pre-fix the regex required either a bank name, the
    word "باركود", "آيبان", or "تحويل بنكي". Customers say "كيف
    أحول لكم؟" without any of those — we must catch it too."""
    from core.ai_libraries import is_payment_query

    assert is_payment_query(phrase), (
        f"is_payment_query should match: {phrase!r}"
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "كيف أحول لكم؟",
        "كيف احول لكم",
        "ابي أحول لكم",
        "كيف أدفع لكم؟",
        "كيف الدفع",
        "كيف التحويل",
        "طريقة الدفع",
        "وش طريقة التحويل",
    ],
)
def test_generic_payment_barcode_query_catches_transfer_intent(
    phrase: str,
) -> None:
    """``resolve_generic_payment_barcode`` only fires when this
    matcher returns True. Adding the "كيف أحول لكم" family of
    phrasings lets the tenant-aware single-asset fallback attach
    the merchant's barcode for the generic payment intent."""
    from services.media_key_registry import is_generic_payment_barcode_query

    assert is_generic_payment_barcode_query(phrase), (
        f"is_generic_payment_barcode_query should match: {phrase!r}"
    )


@pytest.mark.parametrize(
    "phrase",
    [
        # Mentions a specific bank → defer to find_key_for_query.
        "كيف أحول للراجحي",
        # Unrelated questions.
        "كيف الأسعار؟",
        "ابي عسل السمر",
        "السلام عليكم",
    ],
)
def test_generic_payment_barcode_query_no_false_positive(
    phrase: str,
) -> None:
    from services.media_key_registry import is_generic_payment_barcode_query

    assert not is_generic_payment_barcode_query(phrase), (
        f"is_generic_payment_barcode_query should NOT match: {phrase!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# KB-Improve V1.3 (May 2026 #36) — content cluster pass.
# The dashboard previously rendered the same suggestion 5 times when
# 5 different sections produced byte-identical advice (same title,
# reason, proposed_body). The cluster pass collapses those into a
# single card with aggregated ``related_section_ids``.
# ─────────────────────────────────────────────────────────────────────────────


def test_cluster_collapses_byte_identical_findings() -> None:
    """Five compliance-risk findings with identical text but
    different section_ids must collapse into ONE card whose
    ``related_section_ids`` is the union of the originals."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    base_kwargs = dict(
        type="compliance",
        severity="high",
        title="راجع صياغة قسم «product_benefit» — قد تبدو ادعاء علاجي",
        reason="النص يحتوي عبارات قد تُفهم كادعاءات علاجية مباشرة.",
        expected_impact="صياغة آمنة قانونياً.",
        target_kind="compliance_rules",
        proposed_body="اعتمد صياغات عامة بدلاً من «يعالج».",
        requires_media=False,
        confidence=0.75,
    )
    findings = [
        ImprovementFinding(
            id=f"sug-{i}",
            related_section_ids=[100 + i],
            rationale_keys=[f"section:{100 + i}"],
            **base_kwargs,
        )
        for i in range(1, 6)
    ]

    survivors, collapsed = _cluster_findings(findings)
    assert len(survivors) == 1, (
        "byte-identical findings must collapse to one cluster card"
    )
    assert collapsed == 4
    merged = survivors[0]
    assert sorted(merged.related_section_ids) == [101, 102, 103, 104, 105], (
        "all sibling section ids must roll up into the surviving card"
    )
    # The fingerprint is recomputed for the merged ids so dismissals
    # hide the cluster, not just the anchor row.
    assert "section:101" in merged.rationale_keys
    assert "section:105" in merged.rationale_keys


def test_cluster_keeps_distinct_findings_separate() -> None:
    """Findings whose content differs (e.g. weak_section bodies are
    different per row) must NOT be merged. The cluster pass operates
    on full content, not just (type, target_kind)."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    a = ImprovementFinding(
        id="sug-1", type="weak_section", severity="medium",
        title="حسّن قسم «shipping_zones»",
        reason="القسم الحالي قصير جداً (12 حرف).",
        expected_impact="ردود أكثر اكتمالاً.",
        target_kind="shipping_zones",
        proposed_body="نشحن للرياض.\n\nأضف هنا تفاصيل أكثر",
        requires_media=False, confidence=0.65,
        related_section_ids=[1],
    )
    b = ImprovementFinding(
        id="sug-2", type="weak_section", severity="medium",
        title="حسّن قسم «shipping_zones»",
        reason="القسم الحالي قصير جداً (15 حرف).",
        expected_impact="ردود أكثر اكتمالاً.",
        target_kind="shipping_zones",
        proposed_body="نشحن للجبيل.\n\nأضف هنا تفاصيل أكثر",   # different body
        requires_media=False, confidence=0.65,
        related_section_ids=[2],
    )
    survivors, collapsed = _cluster_findings([a, b])
    assert len(survivors) == 2
    assert collapsed == 0


def test_cluster_pass_through_when_all_unique() -> None:
    """Mixed findings of different types/targets must round-trip
    unchanged. The cluster pass must be a no-op when nothing
    duplicates."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    findings = [
        ImprovementFinding(
            id="sug-1", type="missing_required_knowledge", severity="high",
            title="A", reason="r1", expected_impact="i1",
            target_kind="payment_method", proposed_body="b1",
            requires_media=False, confidence=0.9,
        ),
        ImprovementFinding(
            id="sug-2", type="missing_required_knowledge", severity="high",
            title="B", reason="r2", expected_impact="i2",
            target_kind="shipping_zones", proposed_body="b2",
            requires_media=False, confidence=0.9,
        ),
    ]
    survivors, collapsed = _cluster_findings(findings)
    assert collapsed == 0
    assert [f.id for f in survivors] == ["sug-1", "sug-2"]


def test_audit_clusters_identical_compliance_findings_end_to_end() -> None:
    """End-to-end: when the auditor sees 4 product_benefit rows whose
    bodies all contain a medicinal-claim phrase, the previous code
    emitted 4 compliance findings with identical UI text but
    different fingerprints (each carried its own row id). The cluster
    pass must collapse those into a single card the dashboard
    renders ONCE.
    """
    from modules.ai.knowledge.improvement_advisor import audit

    # 4 rows, same kind, each tripping the medicinal-claim pattern
    # in the same way → byte-identical compliance findings.
    rows = [
        _Row(
            id=10 + i, kind="product_benefit",
            title="فوائد",
            body="هذا المنتج يعالج التهاب المعدة بإذن الله.",
        )
        for i in range(4)
    ]
    out = audit(rows, max_suggestions=10)
    compliance = [f for f in out if f.type == "compliance"]
    # Cluster collapses the four into one.
    assert len(compliance) == 1, (
        f"expected one merged compliance card, got: "
        f"{[(f.id, f.related_section_ids) for f in compliance]!r}"
    )
    assert sorted(compliance[0].related_section_ids) == [10, 11, 12, 13]


def test_audit_does_not_cluster_distinct_weak_sections_end_to_end() -> None:
    """Negative control: weak-section findings interpolate the row
    body into ``proposed_body``. Different bodies → different
    cluster keys → no collapse. We check this end-to-end so the
    cluster pass can't quietly over-merge in production."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=20, kind="shipping_zones",
            title="الشحن", body="نشحن للرياض.",
        ),
        _Row(
            id=21, kind="shipping_zones",
            title="الشحن", body="نشحن للجبيل وحفر الباطن.",
        ),
    ]
    out = audit(rows, max_suggestions=10)
    weak = [f for f in out if f.type == "weak_section"]
    assert len(weak) == 2, (
        "weak-section findings on rows with different bodies must stay "
        "as two cards"
    )


def test_cluster_pass_logs_telemetry(caplog: pytest.LogCaptureFixture) -> None:
    """When the cluster pass collapses anything, a single
    structured ``[KB_IMPROVE_CLUSTER]`` INFO line is emitted so
    production triage can chart how often the dashboard would have
    shown duplicates pre-cluster."""
    import logging
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=30 + i, kind="product_benefit",
            title="فوائد",
            body="يشفي التهاب الحلق بإذن الله.",
        )
        for i in range(3)
    ]
    caplog.set_level(logging.INFO, logger="nahla.ai.knowledge.improvement_advisor")
    audit(rows, max_suggestions=10)
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[KB_IMPROVE_CLUSTER]" in ln and "collapsed=" in ln
        for ln in log_lines
    ), f"expected a [KB_IMPROVE_CLUSTER] line; got: {log_lines!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Topic-cluster regression — May 2026 #36 follow-up: production
# scenario where titles differed (because each title interpolated
# its row.title) but the proposed_body / reason were byte-identical.
# Pre-fix the cluster key included the title, so 4 cards survived.
# Post-fix the key is (type, target_kind, proposed_body) only and
# the surviving title is rewritten to "X أقسام".
# ─────────────────────────────────────────────────────────────────────────────


def test_cluster_collapses_compliance_with_divergent_titles() -> None:
    """Reproduces the exact production failure mode shown in the
    May 2026 #36 dashboard screenshot: four compliance findings
    that differ ONLY because each title contains a different
    section name. Pre-fix the cluster key hashed the title and
    they all survived. Post-fix the cluster pass groups them by
    advice (proposed_body) and emits one card whose title is
    rewritten with the count.
    """
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    section_names = [
        "العسل الصيفي الجبلي عند توفره",
        "الاستخدامات التقليدية",
        "العسل الجبلي إذا توفر",
        "عند سؤال العميل عن جرثومة المعدة",
    ]
    common_kwargs = dict(
        type="compliance",
        severity="high",
        reason="النص يحتوي عبارات قد تُفهم كادعاءات علاجية مباشرة.",
        expected_impact="صياغة قانونية آمنة وزيادة ثقة العملاء.",
        target_kind="compliance_rules",
        proposed_body=(
            "يرجى استخدام عبارات عامة مثل «يدخل في الأنظمة الغذائية "
            "الصحية»، وتجنب استخدام كلمات مثل «يعالج»، «يشفي»، "
            "«يقضي على»، أو أي وعد طبي مباشر."
        ),
        requires_media=False,
        confidence=0.75,
    )
    findings = [
        ImprovementFinding(
            id=f"sug-{i}",
            title=f"تحسين صياغة قسم «{name}» لتجنب العبارات العلاجية",
            related_section_ids=[200 + i],
            rationale_keys=[f"section:{200 + i}"],
            **common_kwargs,
        )
        for i, name in enumerate(section_names, start=1)
    ]

    survivors, collapsed = _cluster_findings(findings)

    assert len(survivors) == 1, (
        "topic-clustering must collapse same-advice findings "
        "regardless of per-row title interpolation"
    )
    assert collapsed == 3
    merged = survivors[0]
    assert sorted(merged.related_section_ids) == [201, 202, 203, 204]
    # Title rewritten because per-row titles differed across siblings.
    assert "4" in merged.title and "أقسام" in merged.title, (
        f"expected count-aware generic title, got {merged.title!r}"
    )
    # The advice text stays verbatim — only the title is generalised.
    assert "يدخل في الأنظمة الغذائية" in merged.proposed_body


def test_cluster_does_not_rewrite_title_for_single_member_cluster() -> None:
    """A cluster with one finding must NOT have its title generalised.
    The count-aware rewrite only fires when N > 1 AND siblings'
    titles diverge — otherwise the per-row title is the most useful
    signal for the merchant."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    f = ImprovementFinding(
        id="sug-1",
        type="compliance",
        severity="high",
        title="راجع صياغة قسم «product_benefit» — قد تبدو ادعاء علاجي",
        reason="r",
        expected_impact="i",
        target_kind="compliance_rules",
        proposed_body="advice",
        requires_media=False,
        confidence=0.75,
        related_section_ids=[50],
    )
    survivors, collapsed = _cluster_findings([f])
    assert collapsed == 0
    assert survivors[0].title == f.title


def test_cluster_keeps_anchor_title_when_all_members_share_it() -> None:
    """If every member of a cluster has the IDENTICAL title (because
    they all reference the same row.title or because the row.title
    was empty), the anchor title is preserved — no need to
    generalise. Tests the ``titles_differed`` branch of the rewriter.
    """
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    base = dict(
        type="compliance", severity="high",
        title="راجع صياغة قسم «compliance_rules» — قد تبدو ادعاء علاجي",
        reason="r", expected_impact="i",
        target_kind="compliance_rules",
        proposed_body="canned advice",
        requires_media=False, confidence=0.75,
    )
    findings = [
        ImprovementFinding(
            id=f"sug-{i}",
            related_section_ids=[i],
            rationale_keys=[f"section:{i}"],
            **base,
        )
        for i in range(1, 4)
    ]
    survivors, collapsed = _cluster_findings(findings)
    assert collapsed == 2
    assert len(survivors) == 1
    assert survivors[0].title == base["title"]


def test_cluster_emits_per_cluster_debug_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each non-trivial cluster must emit a
    ``[KB_IMPROVE_CLUSTER_DEBUG]`` INFO line exposing the cluster
    key, type, target_kind, member count, ``titles_differed`` flag,
    and merged section ids — so production triage can answer "why
    didn't card X merge?" without reading source.
    """
    import logging
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding, _cluster_findings,
    )

    common = dict(
        type="compliance", severity="high",
        reason="r", expected_impact="i",
        target_kind="compliance_rules",
        proposed_body="advice body",
        requires_media=False, confidence=0.75,
    )
    findings = [
        ImprovementFinding(
            id=f"sug-{i}",
            title=f"unique title {i}",
            related_section_ids=[i],
            **common,
        )
        for i in range(1, 4)
    ]
    caplog.set_level(
        logging.INFO,
        logger="nahla.ai.knowledge.improvement_advisor",
    )
    _cluster_findings(findings)
    log_lines = [r.getMessage() for r in caplog.records]
    debug = [ln for ln in log_lines if "[KB_IMPROVE_CLUSTER_DEBUG]" in ln]
    assert debug, (
        f"expected at least one [KB_IMPROVE_CLUSTER_DEBUG] line; "
        f"got: {log_lines!r}"
    )
    assert any(
        "members=3" in ln and "titles_differed=True" in ln
        and "type=compliance" in ln
        for ln in debug
    ), f"telemetry payload missing expected fields: {debug!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Orphan staff names — May 2026 #36 follow-up: a section that
# mentions a staff role/name without a phone next to it should
# emit a ``missing_staff_phone`` finding so the merchant fills
# the gap that the safety net's KB scan can't paper over.
# ─────────────────────────────────────────────────────────────────────────────


def test_orphan_staff_section_yields_missing_phone_finding() -> None:
    """A ``branches`` row that mentions "بائع المعرض" but has no
    Saudi phone must emit a ``missing_staff_phone`` finding."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=1, kind="branches",
            title="فرع الطائف",
            body=(
                "فرع الطائف - حي الحلقة الغربية. "
                "بائع المعرض: أمين. مواعيد العمل من 9 ص إلى 11 م."
            ),
        ),
    ]
    out = audit(rows, max_suggestions=10)
    types = [f.type for f in out]
    assert "missing_staff_phone" in types, (
        f"expected missing_staff_phone for orphan staff name; got types={types!r}"
    )
    finding = next(f for f in out if f.type == "missing_staff_phone")
    assert 1 in finding.related_section_ids
    assert "أمين" in finding.proposed_body or "0541690226" in finding.proposed_body


def test_staff_section_with_phone_does_not_emit_missing_finding() -> None:
    """Negative control: a row that already has a Saudi phone
    next to the staff name must NOT emit ``missing_staff_phone``."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=2, kind="branches",
            title="فرع الطائف",
            body="بائع المعرض: أمين - 0541690226",
        ),
    ]
    out = audit(rows, max_suggestions=10)
    types = [f.type for f in out]
    assert "missing_staff_phone" not in types, (
        f"phone was present, no missing_staff_phone expected; got types={types!r}"
    )


def test_orphan_staff_findings_collapse_into_one_card() -> None:
    """When TWO branch rows both have orphan staff names, the
    cluster pass merges them into one card whose
    ``related_section_ids`` covers both rows. The merchant sees
    ONE "أكمل أرقام التواصل" card, not two."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=10, kind="branches",
            title="فرع الطائف",
            body="بائع المعرض: أمين - يستقبلكم بإذن الله.",
        ),
        _Row(
            id=11, kind="branches",
            title="فرع الرياض",
            body="المحاسب: هشام - يقفل الحساب آخر اليوم.",
        ),
    ]
    out = audit(rows, max_suggestions=10)
    staff_findings = [f for f in out if f.type == "missing_staff_phone"]
    assert len(staff_findings) == 1, (
        "two orphan-staff rows must collapse via the cluster pass "
        f"(got {len(staff_findings)} findings: {staff_findings!r})"
    )
    assert sorted(staff_findings[0].related_section_ids) == [10, 11]


def test_faq_section_with_role_keyword_is_not_flagged() -> None:
    """FAQ rows often quote the customer turn ("هل عندكم بائع
    معرض؟") — flagging them would create noise. The audit pass
    only sweeps ``_STAFF_AUDIT_KINDS`` (branches / owner_identity /
    escalation_rules / store_story / quick_update / custom)."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=20, kind="faq",
            title="من البائع؟",
            body="بائع المعرض جاهز للرد على استفساراتكم.",
        ),
    ]
    out = audit(rows, max_suggestions=10)
    types = [f.type for f in out]
    assert "missing_staff_phone" not in types


# ─────────────────────────────────────────────────────────────────────────────
# Barcode-keyword expansion — May 2026 #36 follow-up: a row whose
# body mentions "باركود" but has no bank-name keywords (and no
# barcode media attached) used to slip through the missing-media
# pass. Now ``باركود`` / ``QR`` are bank-transfer keywords too.
# ─────────────────────────────────────────────────────────────────────────────


def test_barcode_only_body_without_media_triggers_missing_media() -> None:
    """A bank_transfer row whose body talks about a barcode but
    doesn't mention "تحويل" / "الراجحي" must still trigger the
    missing-barcode advisor."""
    from modules.ai.knowledge.improvement_advisor import audit

    rows = [
        _Row(
            id=30, kind="bank_transfer",
            title="الباركود",
            body=(
                "أرسل التحويل عبر الباركود المرفق ثم أرسل لنا "
                "صورة الإيصال."
            ),
        ),
    ]
    out = audit(rows, max_suggestions=10)
    media_findings = [f for f in out if f.type == "missing_media"]
    assert media_findings, (
        f"expected missing_media finding for barcode-only body; got: "
        f"{[(f.type, f.target_kind) for f in out]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral Expansion Advisor — preserve surface forms (May 2026 #39)
# ─────────────────────────────────────────────────────────────────────────────


def test_intent_surface_examples_yield_expansion_card_preserving_examples() -> None:
    """Surface forms are training/intent signals, not wiki prose."""
    from modules.ai.knowledge.improvement_advisor import audit

    examples = [
        "أنا قريب",
        "أنا بالطريق",
        "أنا عند البوابة",
        "وين المعرض",
        "أرسل اللوكيشن",
        "أنا جنبكم",
        "ما لقيت المدخل",
        "أنا بالمواقف",
        "أنا قدام المعرض",
    ]
    rows = _baseline_kb() + [
        _Row(900, "custom", "صيغ الوصول", "\n".join(f"- {e}" for e in examples)),
    ]

    out = audit(rows, platform_connected=False, products=[], max_suggestions=20)
    expansion = [f for f in out if f.type == "intent_surface_expansion"]
    assert len(expansion) == 1
    card = expansion[0]
    assert card.knowledge_mode == "artifact_trigger_examples"
    assert card.preserve_surface_forms is True
    assert card.intent == "ask_location_or_arrival_help"
    assert card.artifact_target == "maps_link_or_staff_contact"
    assert card.examples_to_preserve == examples
    assert "فهم تنوع تعبيرات العميل" not in card.title
    assert "فهم تنوع تعبيرات العميل" not in card.proposed_body
    payload = card.to_dict()
    assert payload["examples_to_preserve"] == examples
    assert payload["preserve_surface_forms"] is True


def test_surface_example_clustering_groups_without_deleting_examples() -> None:
    """Similarity means grouping only for surface-form memory."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        _cluster_findings,
    )

    a_examples = ["أنا قريب", "أنا عند البوابة", "وين المعرض"]
    b_examples = ["أرسل اللوكيشن", "ما لقيت المدخل", "أنا بالمواقف"]
    common = dict(
        type="intent_surface_expansion",
        severity="medium",
        title="وسّع أمثلة طلب الموقع",
        reason="r",
        expected_impact="i",
        target_kind="custom",
        proposed_body="body",
        requires_media=False,
        confidence=0.82,
        knowledge_mode="artifact_trigger_examples",
        preserve_surface_forms=True,
        intent="ask_location_or_arrival_help",
        artifact_target="maps_link_or_staff_contact",
        summary="summary",
    )
    findings = [
        ImprovementFinding(
            id="sug-1", related_section_ids=[1],
            examples_to_preserve=a_examples, **common,
        ),
        ImprovementFinding(
            id="sug-2", related_section_ids=[2],
            examples_to_preserve=b_examples, **common,
        ),
    ]

    survivors, collapsed = _cluster_findings(findings)
    assert collapsed == 1
    assert len(survivors) == 1
    merged = survivors[0]
    assert merged.preserve_surface_forms is True
    assert merged.related_section_ids == [1, 2]
    assert merged.examples_to_preserve == a_examples + b_examples
    assert "تنظيمياً فقط" in merged.reason


def test_surface_protected_findings_bypass_purpose_dedup() -> None:
    """Purpose dedup is for hygiene suggestions, not behavioral memory."""
    from modules.ai.knowledge.improvement_advisor import (
        ImprovementFinding,
        _dedup_by_purpose,
    )

    common = dict(
        type="intent_surface_expansion",
        severity="medium",
        title="وسّع أمثلة طلب الموقع",
        reason="r",
        expected_impact="i",
        target_kind="custom",
        proposed_body="body",
        requires_media=False,
        confidence=0.82,
        rationale_keys=["purpose:preserve_location_surface_forms"],
        knowledge_mode="artifact_trigger_examples",
        preserve_surface_forms=True,
        intent="ask_location_or_arrival_help",
        artifact_target="maps_link_or_staff_contact",
    )
    findings = [
        ImprovementFinding(id="sug-1", examples_to_preserve=["أنا قريب"], **common),
        ImprovementFinding(id="sug-2", examples_to_preserve=["أنا عند البوابة"], **common),
    ]
    out = _dedup_by_purpose(findings)
    assert [f.id for f in out] == ["sug-1", "sug-2"]


def test_payment_gap_boosted_by_conversation_signals() -> None:
    from modules.ai.knowledge.improvement_advisor import (
        CATEGORY_MISSING_INFORMATION,
        audit,
    )
    from modules.ai.knowledge.conversation_signal_scanner import (
        ConversationSignalSummary,
    )

    signals = ConversationSignalSummary(
        payment_questions=18,
        human_handoff_after_payment=4,
    )
    findings = audit([], conversation_signals=signals)
    payment = next(f for f in findings if f.target_kind == "payment_method")
    assert payment.category == CATEGORY_MISSING_INFORMATION
    assert "18 سؤال" in payment.source_reason
    assert payment.confidence >= 0.9
    assert "وسائل الدفع" in payment.reason


def test_location_gap_from_signals_when_no_branch_section() -> None:
    from modules.ai.knowledge.improvement_advisor import audit
    from modules.ai.knowledge.conversation_signal_scanner import (
        ConversationSignalSummary,
    )

    signals = ConversationSignalSummary(location_questions=11)
    findings = audit([], conversation_signals=signals)
    loc = next(f for f in findings if f.target_kind == "branches")
    assert "11 سؤال" in loc.source_reason
    assert loc.confidence >= 0.85


def test_price_contradiction_quarter_vs_kilo() -> None:
    from modules.ai.knowledge.improvement_advisor import (
        CATEGORY_CONTRADICTION,
        CatalogSlice,
        audit,
    )

    products = [
        CatalogSlice(id=1, title="عسل الطلح ربع كيلو", price_sar=120.0),
        CatalogSlice(id=2, title="عسل الطلح 1 kg", price_sar=200.0),
    ]
    findings = audit([], products=products)
    price_findings = [f for f in findings if f.category == CATEGORY_CONTRADICTION]
    assert len(price_findings) == 1
    assert "تضارب" in price_findings[0].title


def test_fingerprint_includes_category_and_problem() -> None:
    from modules.ai.knowledge.improvement_advisor import compute_fingerprint

    fp_a = compute_fingerprint(
        type_="missing_required_knowledge",
        target_kind="payment_method",
        title="أضف سياسة دفع",
        category="MISSING_INFORMATION",
        problem="تم رصد 10 أسئلة دفع",
    )
    fp_b = compute_fingerprint(
        type_="missing_required_knowledge",
        target_kind="payment_method",
        title="أضف سياسة دفع",
        category="MISSING_INFORMATION",
        problem="تم رصد 20 سؤال دفع",
    )
    assert fp_a != fp_b


def test_product_compare_sales_signal() -> None:
    from modules.ai.knowledge.improvement_advisor import (
        CATEGORY_SALES_OPPORTUNITY,
        audit,
    )
    from modules.ai.knowledge.conversation_signal_scanner import (
        ConversationSignalSummary,
    )

    signals = ConversationSignalSummary(product_compare_questions=8)
    findings = audit([], conversation_signals=signals)
    sales = [f for f in findings if f.category == CATEGORY_SALES_OPPORTUNITY]
    assert len(sales) == 1
    assert sales[0].target_kind == "product_compare"

