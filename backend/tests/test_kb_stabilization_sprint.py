"""
backend/tests/test_kb_stabilization_sprint.py
─────────────────────────────────────────────
KB-2 (Smart Store Knowledge Hub stabilization sprint, May 2026 #23).

These tests cover the contract pieces of the sprint that DON'T require a
live OpenAI key:

1. Taxonomy registry — new ``assistant_behavior`` group + ``BEHAVIORAL_KINDS``.
2. Classifier defaults — ``gpt-4.1`` + ``temperature=0`` + few-shot examples.
3. Classifier prompt — contains the behavior-vs-commerce separation rule.
4. Classifier observability — emits a structured ``[KB_CLASSIFIER]`` log
   line on every code path.
5. Overlay separation — ``build_structured_facts_block`` drops behavioral
   rows; ``build_behavioral_overlay_block`` collects them into its own
   bucket; ``build_tenant_overlay_split`` surfaces both.
6. High-priority layer — accepts ``merchant_behavior_extra`` and renders
   the [D] sub-block only when non-empty.
7. Repair advisor — flags mis-taxonomy, contamination, and duplicates.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

import pytest

# Ensure ``backend/`` is on the path so the same import statements that
# production uses (``from services...``, ``from modules.ai...``) resolve
# the same way under pytest's collection from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Shared lightweight stubs (kept tiny on purpose) ─────────────────────────


class _FakeSection:
    def __init__(
        self,
        *,
        id: int,
        kind: str,
        title: str = "",
        body: str = "",
        priority: int = 100,
        is_active: bool = True,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = priority
        self.is_active = is_active
        self.updated_at = datetime.now(timezone.utc)
        self.media_links: List[Any] = []
        self.product_links: List[Any] = []


class _FakeQuery:
    def __init__(self, rows: List[_FakeSection]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def order_by(self, *args: Any) -> "_FakeQuery":
        return self

    def all(self) -> List[_FakeSection]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: List[_FakeSection]) -> None:
        self._rows = rows

    def query(self, _model: Any) -> _FakeQuery:
        return _FakeQuery(self._rows)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Taxonomy registry
# ─────────────────────────────────────────────────────────────────────────────


def test_behavioral_group_and_kinds_registered() -> None:
    from services.knowledge_section_kinds import (
        BEHAVIORAL_KINDS,
        GROUP_LABELS_AR,
        all_kinds,
        is_behavioral_kind,
    )

    # Group 7 — "سلوك المساعد"
    assert 7 in GROUP_LABELS_AR
    assert "سلوك" in GROUP_LABELS_AR[7]

    # All 8 behavioral kinds must be registered.
    expected = {
        "forbidden_phrases", "allowed_style", "escalation_rules",
        "compliance_rules", "response_tone", "emoji_policy",
        "owner_identity", "assistant_identity",
    }
    assert expected == set(BEHAVIORAL_KINDS)

    # Each behavioral kind must live in group 7 and be marked behavioral.
    by_kind = {sk.kind: sk for sk in all_kinds()}
    for kind in BEHAVIORAL_KINDS:
        assert kind in by_kind, f"missing kind in registry: {kind}"
        assert by_kind[kind].group == 7
        assert is_behavioral_kind(kind) is True

    # Commerce kinds must NOT be flagged as behavioral.
    for kind in ("payment_method", "shipping_carrier", "store_story",
                 "warranty", "product_usage", "custom", "quick_update"):
        assert is_behavioral_kind(kind) is False


def test_is_behavioral_kind_safe_on_nones() -> None:
    from services.knowledge_section_kinds import is_behavioral_kind

    assert is_behavioral_kind(None) is False
    assert is_behavioral_kind("") is False
    assert is_behavioral_kind("not_a_real_kind") is False
    assert is_behavioral_kind("  Forbidden_Phrases  ") is True  # case + whitespace


# ─────────────────────────────────────────────────────────────────────────────
# 2. Classifier defaults
# ─────────────────────────────────────────────────────────────────────────────


def _reload_classifier_module():
    """Helper — reimport the classifier module so env var changes take effect."""
    import importlib

    import modules.ai.knowledge.classifier as kbc  # noqa: PLC0415
    return importlib.reload(kbc)


def test_classifier_default_model_is_gpt_4_1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip any override so we read the actual default.
    monkeypatch.delenv("NAHLA_KB_CLASSIFIER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    kbc = _reload_classifier_module()
    assert kbc._KB_MODEL == "gpt-4.1"


def test_classifier_respects_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_KB_CLASSIFIER_MODEL", "gpt-5-mini")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    kbc = _reload_classifier_module()
    assert kbc._KB_MODEL == "gpt-5-mini"

    # OPENAI_MODEL is only a fallback — NAHLA_KB_CLASSIFIER_MODEL wins.
    monkeypatch.setenv("OPENAI_MODEL", "ignored")
    kbc = _reload_classifier_module()
    assert kbc._KB_MODEL == "gpt-5-mini"

    # Without NAHLA_KB_..., OPENAI_MODEL takes over.
    monkeypatch.delenv("NAHLA_KB_CLASSIFIER_MODEL", raising=False)
    kbc = _reload_classifier_module()
    assert kbc._KB_MODEL == "ignored"


def test_classifier_temperature_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP body sent to OpenAI must use temperature=0.

    We intercept httpx.Client.post so we don't actually hit the network.
    """
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"proposed_ops":[],'
                                                       '"conflicts":[],"confidence":0.5}'}}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, _url, *, headers, json):
            captured["body"] = json
            return _FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    kbc = _reload_classifier_module()

    import httpx  # noqa: PLC0415
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    kbc._call_openai_chat(prompt="ignored", user_text="ignored")

    assert captured["body"]["temperature"] == 0
    assert captured["body"]["response_format"] == {"type": "json_object"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Classifier prompt content
# ─────────────────────────────────────────────────────────────────────────────


def test_classifier_prompt_includes_behavior_vs_commerce_rule() -> None:
    from modules.ai.knowledge.classifier import PROPOSAL_SCHEMA_NOTE

    # The separation rule must be in the prompt verbatim — the model
    # relies on it to route "لا تقل حبيبي" away from store_info.
    assert "assistant_behavior" in PROPOSAL_SCHEMA_NOTE
    assert "forbidden_phrases" in PROPOSAL_SCHEMA_NOTE
    assert "response_tone" in PROPOSAL_SCHEMA_NOTE
    assert "escalation_rules" in PROPOSAL_SCHEMA_NOTE
    assert "emoji_policy" in PROPOSAL_SCHEMA_NOTE
    # And one concrete worked example so the model sees the boundary.
    assert "لا تقل حبيبي" in PROPOSAL_SCHEMA_NOTE


def test_classifier_prompt_includes_fewshot_examples() -> None:
    from modules.ai.knowledge.classifier import (
        AttachedMedia,
        ExistingSection,
        PlatformSignal,
        _build_system_prompt,
        _FEW_SHOT_EXAMPLES,
    )

    # 5 examples by design (see _FEW_SHOT_EXAMPLES rationale comment).
    assert len(_FEW_SHOT_EXAMPLES) >= 5

    prompt = _build_system_prompt(
        existing_sections=[],
        attached_media=[],
        platform_signal=PlatformSignal(connected=True, platform="salla",
                                       warning="موصولة بسلة"),
        available_kinds=["bank_transfer", "forbidden_phrases", "response_tone",
                         "cold_shipping", "quick_update"],
    )

    # All five canonical input strings must appear in the rendered prompt.
    for needle in (
        "باركود الراجحي للتحويل البنكي",
        "لا تقل حبيبي أو قلبي للعملاء",
        "الشحن المبرد مهم بالصيف للعسل",
        "بوكس الأرباع نفد مؤقتاً",
        "استخدم لهجة خليجية خفيفة وإيموجي بسيط",
    ):
        assert needle in prompt, f"few-shot example missing from prompt: {needle}"


def test_classifier_prompt_hides_platform_only_example_when_disconnected() -> None:
    from modules.ai.knowledge.classifier import (
        PlatformSignal,
        _build_system_prompt,
    )

    prompt = _build_system_prompt(
        existing_sections=[],
        attached_media=[],
        platform_signal=PlatformSignal(connected=False, platform=None,
                                       warning=""),
        available_kinds=["quick_update"],
    )
    # The "بوكس الأرباع نفد" example is platform-conflict-only.
    assert "بوكس الأرباع نفد" not in prompt
    # But the behavioral example must always be present.
    assert "لا تقل حبيبي" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# 4. Classifier observability + fallback path
# ─────────────────────────────────────────────────────────────────────────────


def test_classifier_emits_kb_log_on_empty_input(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    from modules.ai.knowledge.classifier import (
        PlatformSignal,
        classify_quick_update,
    )

    caplog.set_level(logging.INFO, logger="nahla.ai.knowledge.classifier")
    result = classify_quick_update(
        raw_text="",
        attached_media=[],
        existing_sections=[],
        platform_signal=PlatformSignal(connected=False, platform=None, warning=""),
        available_kinds=["quick_update"],
        tenant_id=42,
    )
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "empty_input"
    assert any(
        "[KB_CLASSIFIER]" in rec.getMessage() and "tenant_id=42" in rec.getMessage()
        and "fallback_reason=empty_input" in rec.getMessage()
        for rec in caplog.records
    )


def test_classifier_no_api_key_path_logs_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    kbc = _reload_classifier_module()

    caplog.set_level(logging.INFO, logger="nahla.ai.knowledge.classifier")
    result = kbc.classify_quick_update(
        raw_text="باركود الراجحي للتحويل",
        attached_media=[],
        existing_sections=[],
        platform_signal=kbc.PlatformSignal(
            connected=True, platform="salla", warning="موصولة"),
        available_kinds=["bank_transfer", "quick_update"],
        tenant_id=7,
    )
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "no_api_key"
    # The deterministic fallback always emits exactly one quick_update op.
    assert any(op["kind"] == "quick_update" for op in result["proposed_ops"])
    # And the structured log line carries the fallback reason.
    log_lines = [r.getMessage() for r in caplog.records if "[KB_CLASSIFIER]" in r.getMessage()]
    assert any("fallback_reason=no_api_key" in m for m in log_lines)
    assert any("retry_count=0" in m for m in log_lines)


def test_classifier_retries_once_on_parse_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On a malformed first reply, the classifier retries once with a
    reminder prompt at temperature=0. A successful retry means we
    DON'T fall back."""
    import logging
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    kbc = _reload_classifier_module()

    calls = {"count": 0, "prompts": []}

    def _fake_call(*, prompt: str, user_text: str) -> str:
        calls["count"] += 1
        calls["prompts"].append(prompt)
        if calls["count"] == 1:
            return "Here is some chatter before the JSON {malformed"
        return ('{"proposed_ops":[{"op_id":"op-1","op":"create",'
                '"kind":"quick_update","title":"x","body":"y",'
                '"metadata":{},"target_section_id":null,'
                '"link_role":null,"media_id":null,"rationale":"r"}],'
                '"conflicts":[],"confidence":0.7}')

    monkeypatch.setattr(kbc, "_call_openai_chat", _fake_call)

    caplog.set_level(logging.INFO, logger="nahla.ai.knowledge.classifier")
    result = kbc.classify_quick_update(
        raw_text="text",
        attached_media=[],
        existing_sections=[],
        platform_signal=kbc.PlatformSignal(connected=False, platform=None, warning=""),
        available_kinds=["quick_update"],
        tenant_id=1,
    )
    assert calls["count"] == 2
    assert result["fallback_used"] is False
    # The retry prompt must carry the reminder text.
    assert "JSON" in calls["prompts"][1]
    # Log must record retry_count=1.
    log_lines = [r.getMessage() for r in caplog.records if "[KB_CLASSIFIER]" in r.getMessage()]
    assert any("retry_count=1" in m for m in log_lines)


def test_classifier_falls_back_when_retry_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    kbc = _reload_classifier_module()

    def _fake_call(*, prompt: str, user_text: str) -> str:
        return "not even close to JSON"

    monkeypatch.setattr(kbc, "_call_openai_chat", _fake_call)

    result = kbc.classify_quick_update(
        raw_text="text",
        attached_media=[],
        existing_sections=[],
        platform_signal=kbc.PlatformSignal(connected=False, platform=None, warning=""),
        available_kinds=["quick_update"],
        tenant_id=1,
    )
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "parse_error"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Overlay separation — behavioral rows
# ─────────────────────────────────────────────────────────────────────────────


def test_structured_facts_block_drops_behavioral_rows() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(id=1, kind="payment_method", title="الدفع",
                     body="مدى وفيزا والتحويل البنكي."),
        _FakeSection(id=2, kind="forbidden_phrases", title="ممنوع",
                     body="لا تقل حبيبي أو قلبي للعملاء."),
        _FakeSection(id=3, kind="response_tone", title="النبرة",
                     body="ردّ بلهجة خليجية مختصرة."),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=42)
    # Commerce row is rendered ...
    assert "مدى وفيزا" in block
    # ... but behavioral rows MUST NOT appear in the facts block.
    assert "حبيبي" not in block
    assert "لهجة خليجية مختصرة" not in block


def test_structured_facts_block_empty_when_only_behavioral_rows() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(id=1, kind="forbidden_phrases", title="ممنوع",
                     body="لا تقل حبيبي."),
    ]
    assert build_structured_facts_block(_FakeSession(rows), tenant_id=1) == ""


def test_behavioral_overlay_block_renders_grouped_subtypes() -> None:
    from modules.ai.prompts.tenant_overlay import build_behavioral_overlay_block

    rows = [
        _FakeSection(id=1, kind="forbidden_phrases", title="كلمات ممنوعة",
                     body="لا تقل حبيبي أو قلبي."),
        _FakeSection(id=2, kind="response_tone", title="نبرة",
                     body="لهجة خليجية مختصرة."),
        _FakeSection(id=3, kind="escalation_rules", title="تصعيد",
                     body="حوّل لموظف عند طلب شكوى."),
        # Non-behavioral rows must be ignored by this block.
        _FakeSection(id=4, kind="payment_method", title="الدفع",
                     body="مدى وفيزا."),
    ]
    block = build_behavioral_overlay_block(_FakeSession(rows), tenant_id=1)

    # All three behavioral subtypes rendered.
    assert "كلمات وعبارات ممنوعة" in block
    assert "نبرة الرد المطلوبة" in block
    assert "متى تحوّل لموظف بشري" in block
    # Bodies preserved.
    assert "حبيبي" in block
    assert "خليجية" in block
    # Commerce row text MUST NOT leak in.
    assert "مدى وفيزا" not in block


def test_behavioral_overlay_empty_without_behavioral_rows() -> None:
    from modules.ai.prompts.tenant_overlay import build_behavioral_overlay_block

    rows = [
        _FakeSection(id=1, kind="payment_method", title="الدفع",
                     body="مدى."),
    ]
    assert build_behavioral_overlay_block(_FakeSession(rows), tenant_id=1) == ""


def test_overlay_split_produces_behavior_bucket() -> None:
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    rows = [
        _FakeSection(id=1, kind="warranty", title="الضمان",
                     body="14 يوماً."),
        _FakeSection(id=2, kind="forbidden_phrases", title="ممنوع",
                     body="لا تقل حبيبي."),
    ]
    buckets = build_tenant_overlay_split(
        {"manual_knowledge_base": ""},
        db=_FakeSession(rows),
        tenant_id=11,
    )
    # The new key exists ...
    assert "behavior" in buckets
    # ... carries the behavioral content ...
    assert "حبيبي" in buckets["behavior"]
    # ... and the facts bucket has commerce only.
    assert "14 يوماً" in buckets["facts"]
    assert "حبيبي" not in buckets["facts"]


# ─────────────────────────────────────────────────────────────────────────────
# 6. High-priority layer accepts merchant_behavior_extra
# ─────────────────────────────────────────────────────────────────────────────


def test_high_priority_layer_includes_merchant_behavior_when_provided() -> None:
    from modules.ai.prompts.high_priority_layer import build_high_priority_block

    extra = (
        "• كلمات وعبارات ممنوعة:\n"
        "  - لا تقل حبيبي أو قلبي للعملاء.\n\n"
        "• نبرة الرد المطلوبة:\n"
        "  - لهجة خليجية مختصرة."
    )
    block = build_high_priority_block(
        settings=None,
        store_name="آل عايد",
        merchant_behavior_extra=extra,
    )
    assert "[D] MERCHANT-SPECIFIC BEHAVIOR" in block
    assert "حبيبي" in block
    assert "خليجية مختصرة" in block


def test_high_priority_layer_omits_d_block_when_no_merchant_behavior() -> None:
    from modules.ai.prompts.high_priority_layer import build_high_priority_block

    block = build_high_priority_block(settings=None, store_name="آل عايد")
    assert "[D] MERCHANT-SPECIFIC BEHAVIOR" not in block
    # The baseline A/B/C blocks must still be present.
    assert "HIGH PRIORITY" in block
    assert "[A] STYLE" in block
    assert "[B] POLICY" in block
    assert "[C] FORBIDDEN" in block


# ─────────────────────────────────────────────────────────────────────────────
# 7. Repair advisor
# ─────────────────────────────────────────────────────────────────────────────


class _AdvSection:
    """Lightweight stand-in used by the advisor (it only reads .id,
    .kind, .title, .body) — kept separate from _FakeSection so the
    advisor unit tests stay decoupled from the overlay tests."""

    def __init__(self, id: int, kind: str, title: str, body: str) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body


def test_repair_advisor_flags_behavior_in_commerce_kind() -> None:
    from modules.ai.knowledge.repair_advisor import analyze_sections

    rows = [
        _AdvSection(1, "store_info", "ملاحظة",
                    "لا تقل حبيبي أو قلبي للعملاء."),
        _AdvSection(2, "payment_method", "الدفع",
                    "مدى وفيزا — تحويل بنكي عبر الراجحي 200 ريال."),
    ]
    suggestions = analyze_sections(rows)
    moves = [s for s in suggestions if s.kind == "move"]
    assert len(moves) == 1
    assert moves[0].section_ids == (1,)
    assert moves[0].current_kind == "store_info"
    # Most-fitting subtype for "لا تقل ..." is forbidden_phrases.
    assert moves[0].suggested_kind == "forbidden_phrases"


def test_repair_advisor_flags_contamination_in_commerce_row() -> None:
    from modules.ai.knowledge.repair_advisor import analyze_sections

    rows = [
        _AdvSection(1, "payment_method", "الدفع",
                    "مدى وفيزا 200 ريال — لا تقل حبيبي للعملاء."),
    ]
    suggestions = analyze_sections(rows)
    contam = [s for s in suggestions if s.kind == "contamination"]
    assert len(contam) == 1
    assert contam[0].severity == "critical"
    assert contam[0].section_ids == (1,)


def test_repair_advisor_detects_duplicates() -> None:
    from modules.ai.knowledge.repair_advisor import analyze_sections

    rows = [
        _AdvSection(1, "shipping_carrier", "الشحن",
                    "نتعامل مع شركة سمسا وأرامكس فقط داخل المملكة."),
        _AdvSection(2, "shipping_carrier", "شركات الشحن",
                    "شركة سمسا وأرامكس داخل المملكة فقط."),
        # Different kind — must NOT collide with rows 1/2.
        _AdvSection(3, "payment_method", "الدفع",
                    "شركة سمسا وأرامكس فقط داخل المملكة."),
    ]
    suggestions = analyze_sections(rows)
    dups = [s for s in suggestions if s.kind == "duplicate"]
    assert len(dups) == 1
    assert dups[0].section_ids == (1, 2)


def test_repair_advisor_empty_when_clean() -> None:
    from modules.ai.knowledge.repair_advisor import analyze_sections, summarize

    rows = [
        _AdvSection(1, "payment_method", "الدفع", "مدى وفيزا."),
        _AdvSection(2, "forbidden_phrases", "ممنوع", "لا تقل حبيبي."),
    ]
    suggestions = analyze_sections(rows)
    assert suggestions == []
    summary = summarize(suggestions)
    assert summary["total"] == 0


def test_repair_advisor_summary_counts() -> None:
    from modules.ai.knowledge.repair_advisor import (
        RepairSuggestion,
        summarize,
    )

    s = [
        RepairSuggestion(kind="move", severity="warn", section_ids=(1,),
                         title_preview="", body_preview="",
                         current_kind="store_info",
                         suggested_kind="forbidden_phrases",
                         reason_ar=""),
        RepairSuggestion(kind="duplicate", severity="info", section_ids=(2, 3),
                         title_preview="", body_preview="",
                         current_kind="shipping_carrier",
                         suggested_kind=None, reason_ar=""),
        RepairSuggestion(kind="contamination", severity="critical",
                         section_ids=(4,), title_preview="", body_preview="",
                         current_kind="payment_method",
                         suggested_kind=None, reason_ar=""),
    ]
    summary = summarize(s)
    assert summary["total"] == 3
    assert summary["move"] == 1
    assert summary["duplicate"] == 1
    assert summary["contamination"] == 1
    assert summary["critical"] == 1
    assert summary["warn"] == 1
    assert summary["info"] == 1
