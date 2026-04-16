"""
tests/test_default_automation_features.py
─────────────────────────────────────────
Coverage for the three core revenue automations Nahla ships out of the box:

  1. cart_abandoned        → abandoned_cart_recovery_{ar,en}
  2. customer_inactive     → win_back_{ar,en}
  3. vip_customer_upgrade  → vip_reward_{ar,en}

These tests assert the user-facing contract:

  • Each feature has both an Arabic and an English template in the library.
  • Each template uses Meta-safe numeric placeholders (`{{1}}`, `{{2}}`, …)
    paired with a fixed named-slot list — merchants can edit the text but
    cannot rename or remove a slot.
  • The seeded SmartAutomation rows point at the new template names and
    carry `auto_coupon: true` for the steps that should pull a coupon
    (cart reminder #3 + VIP reward + winback).
  • The placeholder integrity validator blocks slot deletion on edit.
  • The engine's `_build_template_vars` resolves named slots correctly,
    including coupon injection when `coupon_extras` is provided.
  • The AI rewrite helper preserves every placeholder verbatim even if the
    LLM tries to rewrite the surrounding text.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.template_library import (  # noqa: E402
    ALLOWED_VARIABLE_SLOTS,
    DEFAULT_AUTOMATION_TEMPLATES,
    feature_for_template,
    iter_template_seeds,
    numeric_var_map_for,
    required_slots_for,
)
from core.automation_engine import _build_template_vars  # noqa: E402
from core.automations_seed import SEED_AUTOMATIONS  # noqa: E402


# ── 1. Library shape ──────────────────────────────────────────────────────────

CORE_FEATURES = ("cart_abandoned", "customer_inactive", "vip_customer_upgrade")


@pytest.mark.parametrize("feature_key", CORE_FEATURES)
def test_each_core_feature_has_ar_and_en_templates(feature_key: str) -> None:
    """All three core revenue automations must ship in both AR and EN."""
    spec = DEFAULT_AUTOMATION_TEMPLATES[feature_key]
    languages = set(spec["languages"].keys())
    assert {"ar", "en"} <= languages, (
        f"feature '{feature_key}' is missing one of ar/en — got {languages}"
    )


@pytest.mark.parametrize("feature_key", CORE_FEATURES)
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_template_uses_only_numeric_placeholders(feature_key: str, lang: str) -> None:
    """
    Meta WhatsApp Cloud only renders `{{1}}, {{2}}, …`. Named placeholders
    like `{{customer_name}}` would ship as literal text and the template
    submission would fail review. The library must keep the numeric form.
    """
    spec = DEFAULT_AUTOMATION_TEMPLATES[feature_key]["languages"][lang]
    bodies: List[str] = [
        c.get("text", "") for c in spec["components"] if c.get("text")
    ]
    placeholders = set()
    for body in bodies:
        placeholders.update(re.findall(r"\{\{[^{}]+\}\}", body))

    for ph in placeholders:
        inner = ph.strip("{}").strip()
        assert inner.isdigit(), (
            f"{feature_key}/{lang}: placeholder {ph!r} is not numeric — Meta "
            "submission requires `{{1}}, {{2}}, …` not named slots."
        )


@pytest.mark.parametrize("feature_key", CORE_FEATURES)
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_slot_count_matches_placeholder_count(feature_key: str, lang: str) -> None:
    """
    Every numeric placeholder in the body must have a corresponding entry in
    the `slots` list. This is the contract the engine reads to know which
    real value to inject for `{{N}}`.
    """
    spec = DEFAULT_AUTOMATION_TEMPLATES[feature_key]["languages"][lang]
    placeholders = set()
    for comp in spec["components"]:
        text = comp.get("text", "") or ""
        placeholders.update(re.findall(r"\{\{(\d+)\}\}", text))

    expected_indexes = {str(i + 1) for i in range(len(spec["slots"]))}
    assert placeholders == expected_indexes, (
        f"{feature_key}/{lang}: placeholders={sorted(placeholders)} "
        f"but slots imply {sorted(expected_indexes)}"
    )


@pytest.mark.parametrize("feature_key", CORE_FEATURES)
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_all_slots_are_in_allowlist(feature_key: str, lang: str) -> None:
    """Library templates may only reference whitelisted named slots."""
    spec = DEFAULT_AUTOMATION_TEMPLATES[feature_key]["languages"][lang]
    for slot in spec["slots"]:
        assert slot in ALLOWED_VARIABLE_SLOTS, (
            f"{feature_key}/{lang}: slot '{slot}' is not in ALLOWED_VARIABLE_SLOTS — "
            "either add it to the allowlist (and teach _resolve_slot_value how to "
            "fill it) or drop it from the template."
        )


def test_iter_template_seeds_returns_one_per_feature_per_language() -> None:
    """
    The seeder helper should produce one row per (feature, language). The
    number of features grows over time (cart_abandoned, customer_inactive,
    vip_customer_upgrade, product_back_in_stock, ...) so we assert symmetry
    across AR/EN rather than a fixed count — that's the real invariant.
    """
    seeds_ar = iter_template_seeds("ar")
    seeds_en = iter_template_seeds("en")
    assert len(seeds_ar) == len(seeds_en), (
        "AR and EN libraries must have the same set of features. "
        f"Got AR={len(seeds_ar)}, EN={len(seeds_en)}"
    )
    # Every CORE_FEATURES entry must be present in both languages — adding
    # new features should never silently drop the originals.
    ar_names = {s["name"] for s in seeds_ar}
    en_names = {s["name"] for s in seeds_en}
    assert "abandoned_cart_recovery_ar" in ar_names
    assert "abandoned_cart_recovery_en" in en_names
    assert "win_back_ar" in ar_names
    assert "win_back_en" in en_names
    assert "vip_reward_ar" in ar_names
    assert "vip_reward_en" in en_names
    assert ar_names.isdisjoint(en_names), (
        "AR and EN templates must have distinct names so Meta sees them as "
        f"separate templates. Overlap: {ar_names & en_names}"
    )


def test_numeric_var_map_for_known_template() -> None:
    """The library's var_map must round-trip correctly."""
    var_map = numeric_var_map_for("abandoned_cart_recovery_ar")
    assert var_map == {
        "{{1}}": "customer_name",
        "{{2}}": "store_name",
        "{{3}}": "checkout_url",
    }


def test_numeric_var_map_for_unknown_template_is_empty() -> None:
    assert numeric_var_map_for("not_a_real_template_name") == {}


# ── 2. Seeder wiring ──────────────────────────────────────────────────────────

def _seed_for(automation_type: str) -> Dict[str, Any]:
    for seed in SEED_AUTOMATIONS:
        if seed["automation_type"] == automation_type:
            return seed
    raise AssertionError(f"no seed for automation_type={automation_type!r}")


def test_cart_abandoned_seed_uses_library_template() -> None:
    seed = _seed_for("abandoned_cart")
    assert seed["config"]["template_name"] == "abandoned_cart_recovery_ar"
    assert seed["config"]["template_name_en"] == "abandoned_cart_recovery_en"


def test_cart_abandoned_step_three_has_auto_coupon() -> None:
    """
    The 24-hour reminder must request a real coupon from the pool, not a
    static placeholder. This is what wires step-3 to the coupon_generator.
    """
    seed = _seed_for("abandoned_cart")
    steps = seed["config"]["steps"]
    assert len(steps) == 3
    assert steps[0]["delay_minutes"] == 30   # Reminder 1
    assert steps[1]["delay_minutes"] == 360  # Reminder 2 (6h)
    assert steps[2]["delay_minutes"] == 1440 # Reminder 3 (24h)
    assert steps[2].get("auto_coupon") is True
    assert steps[2].get("message_type") == "coupon"


def test_winback_seed_has_auto_coupon_and_library_template() -> None:
    seed = _seed_for("customer_winback")
    assert seed["config"]["auto_coupon"] is True
    assert seed["config"]["template_name"] == "win_back_ar"
    assert seed["config"]["template_name_en"] == "win_back_en"


def test_vip_seed_has_auto_coupon_and_library_template() -> None:
    seed = _seed_for("vip_upgrade")
    assert seed["config"]["auto_coupon"] is True
    assert seed["config"]["template_name"] == "vip_reward_ar"
    assert seed["config"]["template_name_en"] == "vip_reward_en"
    assert seed["config"]["min_spent_sar"] == 2000   # spec requirement


# ── 3. Variable resolution ────────────────────────────────────────────────────

class _StubEvent:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.created_at = None  # not used by _build_template_vars


class _StubCustomer:
    def __init__(self, name: str):
        self.name = name
        self.id = 99


def test_build_vars_resolves_library_template_by_name() -> None:
    """
    With no explicit `var_map` in config, the engine must look the template
    up in the library and resolve its named slots from event payload +
    helper kwargs.
    """
    vars_map = _build_template_vars(
        event=_StubEvent({"checkout_url": "https://shop/c/abc"}),
        customer=_StubCustomer("Sara"),
        config={},
        template_name="abandoned_cart_recovery_ar",
        store_name="MyStore",
    )
    assert vars_map == {
        "{{1}}": "Sara",
        "{{2}}": "MyStore",
        "{{3}}": "https://shop/c/abc",
    }


def test_build_vars_injects_coupon_extras_into_discount_code_slot() -> None:
    """
    When `_resolve_auto_coupon` produces a real code, the resolver must
    fill it into both `discount_code` and `vip_coupon` slots so cart and
    VIP templates both render correctly.
    """
    vars_map = _build_template_vars(
        event=_StubEvent({}),
        customer=_StubCustomer("Ali"),
        config={},
        template_name="win_back_ar",  # slots: customer_name, store_name, discount_code
        store_name="نحلة",
        coupon_extras={"discount_code": "NHA2X", "vip_coupon": "NHA2X"},
    )
    assert vars_map["{{3}}"] == "NHA2X"


def test_build_vars_falls_back_to_positional_for_unknown_template() -> None:
    """Merchant-authored templates with no library entry still render."""
    vars_map = _build_template_vars(
        event=_StubEvent({"checkout_url": "https://x"}),
        customer=_StubCustomer("Omar"),
        config={},
        template_name="some_merchant_template_v3",
    )
    # Falls back to {{1}}=customer_name, {{2}}=checkout_url
    assert vars_map["{{1}}"] == "Omar"
    assert vars_map["{{2}}"] == "https://x"


# ── 4. Placeholder integrity (variable lock) ──────────────────────────────────

def test_placeholder_integrity_blocks_slot_deletion() -> None:
    """
    The merchant must not be able to remove a `{{N}}` from a template via
    `PUT /templates/{id}`. This is the safeguard that keeps the named-slot
    contract intact across edits.
    """
    from routers.templates import _validate_placeholder_integrity

    old = [{"type": "BODY", "text": "Hi {{1}}, complete: {{2}}"}]
    new = [{"type": "BODY", "text": "Hi {{1}}, thanks!"}]   # dropped {{2}}

    with pytest.raises(HTTPException) as exc:
        _validate_placeholder_integrity(
            old_components=old, new_components=new,
        )
    assert exc.value.status_code == 422


def test_placeholder_integrity_allows_text_only_edit() -> None:
    """Editing surrounding text is allowed as long as all `{{N}}` survive."""
    from routers.templates import _validate_placeholder_integrity

    old = [{"type": "BODY", "text": "Hi {{1}}, complete: {{2}}"}]
    new = [{"type": "BODY", "text": "أهلاً {{1}}، أكمل من هنا: {{2}}"}]

    # Should not raise.
    _validate_placeholder_integrity(old_components=old, new_components=new)


# ── 5. AI rewrite preserves placeholders ──────────────────────────────────────

def test_ai_rewrite_protect_and_restore_round_trip() -> None:
    """The sentinel masking helper must perfectly round-trip every `{{…}}`."""
    from routers.templates import _placeholder_protect, _placeholder_restore

    text = "Hi {{1}}, your code is {{3}} — {{2}}"
    masked, mapping = _placeholder_protect(text)

    # Sentinels are LLM-safe: ASCII, no braces, and they don't appear in
    # the original text (so a clueless rewrite still can't fabricate them).
    for sentinel in mapping:
        assert "{" not in sentinel and "}" not in sentinel
        assert sentinel not in text

    restored = _placeholder_restore(masked, mapping)
    assert restored == text


def test_ai_rewrite_helper_preserves_placeholders_when_llm_drops_them() -> None:
    """
    Simulate a misbehaving LLM that drops `{{2}}` from the rewrite. The
    helper must catch the missing placeholder, raise 422, and never return
    text that would render incorrectly downstream.
    """
    from routers.templates import _ai_rewrite_body_text

    original = "Hi {{1}}, here is your link: {{2}}"

    class _BadPayload:
        # The fake LLM returns the masked text but loses sentinel #2.
        # Because we mask before sending, the helper must detect that
        # __NHVAR2__ never came back and refuse to apply the rewrite.
        def __init__(self, text: str):
            self.reply_text = text

    def _fake_generate(**kwargs):
        # Strip "__NHVAR2__" from whatever the helper sent us.
        masked_message = kwargs.get("message", "")
        # The orchestrator gets the masked text in `message`. We rewrite by
        # appending a friendly prefix and DROPPING the second sentinel.
        rewritten = "Hello! " + masked_message.replace("__NHVAR2__", "")
        return _BadPayload(rewritten)

    with patch(
        "modules.ai.orchestrator.adapter.generate_ai_reply", side_effect=_fake_generate
    ):
        with pytest.raises(HTTPException) as exc:
            _ai_rewrite_body_text(
                body_text=original,
                mode="improve",
                language="en",
                tenant_id=1,
                store_name="Acme",
            )
    assert exc.value.status_code == 422
    assert "{{2}}" in str(exc.value.detail)


def test_ai_rewrite_helper_returns_rewrite_when_llm_preserves_sentinels() -> None:
    """
    Happy path: the LLM returns the masked text rewritten with sentinels
    intact. The helper restores them and returns clean text containing
    every original `{{N}}` placeholder.
    """
    from routers.templates import _ai_rewrite_body_text

    original = "Hi {{1}}, your link: {{2}}"

    class _GoodPayload:
        def __init__(self, text: str):
            self.reply_text = text

    def _fake_generate(**kwargs):
        masked_message = kwargs.get("message", "")
        # Rewrite: prefix + same masked body. Sentinels untouched.
        return _GoodPayload("Hello there! " + masked_message)

    with patch(
        "modules.ai.orchestrator.adapter.generate_ai_reply", side_effect=_fake_generate
    ):
        result = _ai_rewrite_body_text(
            body_text=original,
            mode="friendlier",
            language="en",
            tenant_id=1,
            store_name="Acme",
        )

    assert "{{1}}" in result
    assert "{{2}}" in result
    assert result.startswith("Hello there!")


# ── 6. Library lookup helpers ─────────────────────────────────────────────────

def test_feature_for_template_finds_known_template() -> None:
    feature = feature_for_template("vip_reward_en")
    assert feature is not None
    assert feature["feature_key"] == "vip_customer_upgrade"
    assert feature["category"] == "MARKETING"


def test_required_slots_for_known_template_matches_spec() -> None:
    assert required_slots_for("vip_reward_ar") == [
        "customer_name", "store_name", "vip_coupon",
    ]


def test_required_slots_for_unknown_template_is_empty() -> None:
    assert required_slots_for("not_a_real_template") == []
