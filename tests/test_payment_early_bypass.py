"""Regression tests for the EARLY payment-asset bypass at the WhatsApp
webhook entry point.

The merchant reported that even after commit 6f57631c (which routes
``ASK_PAYMENT_INFO`` → LLM_REPLY and adds the late-stage media
override), the bank-transfer barcode still wasn't reaching customers
when the conversation had been previously paused or escalated:

    * ``should_skip_ai`` returned True (because of a prior
      ``ai_pause_guard`` event) and the webhook ``return``-ed before
      the brain could run.
    * The conversation-mode resolver returned
      ``MODE_SUPPORT_ESCALATION`` and the hard-coded handoff
      acknowledgement fired ("وصلت رسالتك. تم تحويل المحادثة لفريق
      المتجر…") before reaching the brain.

The fix in this commit is an EARLY bypass: BEFORE the pause guard,
BEFORE the mode resolver, BEFORE every other branch — if the inbound
text is a payment-info request and a high-relevance active media
asset exists, we send the asset and return. Tests below lock the key
properties of that bypass.

These run at the contract layer (no live HTTP calls) by patching the
webhook helpers so we can verify the *call sequence* and the
*conditions under which the bypass fires*.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# 1. Source-level contract checks — the bypass MUST sit before pause
#    guard / mode resolver / brain in the webhook source order.
# ─────────────────────────────────────────────────────────────────────────

def _read_webhook() -> str:
    return (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
        encoding="utf-8",
    )


def test_early_bypass_block_exists():
    src = _read_webhook()
    assert "PAYMENT-ASSET EARLY BYPASS" in src, (
        "early bypass block missing — pause guard / mode resolver will "
        "still short-circuit payment requests on paused/escalated convos"
    )


def test_early_bypass_runs_before_pause_guard():
    src = _read_webhook()
    bypass_idx = src.find("PAYMENT-ASSET EARLY BYPASS")
    pause_idx = src.find("AI loop / cost guard")
    assert bypass_idx != -1 and pause_idx != -1
    assert bypass_idx < pause_idx, (
        "early bypass must run BEFORE the AI pause guard — otherwise "
        "should_skip_ai returns True for paused convos and the bypass "
        "never executes"
    )


def test_early_bypass_runs_before_mode_resolver():
    src = _read_webhook()
    bypass_idx = src.find("PAYMENT-ASSET EARLY BYPASS")
    # The mode resolver / support-escalation branch is downstream of the
    # pause guard, so checking against the support-escalation marker is
    # an additional layer of confidence.
    mode_idx = src.find("Human handoff / support escalation")
    assert bypass_idx != -1 and mode_idx != -1
    assert bypass_idx < mode_idx, (
        "early bypass must run BEFORE MODE_SUPPORT_ESCALATION branch — "
        "otherwise the handoff acknowledgement fires first"
    )


def test_early_bypass_short_circuits_with_return():
    """The bypass must call ``return`` after sending the asset so none
    of the downstream branches (brain, legacy, fallback handoff) fire."""
    src = _read_webhook()
    # Find the bypass block and confirm a ``return`` lives inside it.
    start = src.find("PAYMENT-ASSET EARLY BYPASS")
    assert start != -1
    # Window: from bypass start to the next major comment (the AI loop guard).
    end = src.find("AI loop / cost guard", start)
    assert end != -1
    block = src[start:end]
    assert "return" in block, (
        "bypass block must end with ``return`` so paused convos don't "
        "fall through to the support-escalation acknowledgement"
    )
    # And the bypass must call _send_media_message (asset dispatch).
    assert "_send_media_message" in block
    # And it must call validate_media_for_send (pre-send safety gate).
    assert "validate_media" in block.lower()
    # And it must check is_payment_query (intent gate).
    assert "is_payment_query" in block


def test_early_bypass_does_not_clear_handoff_flags():
    """If the merchant manually took over the conversation, they keep
    ownership for everything except this specific question. The bypass
    must NOT set ``convo.is_human_handoff = False`` or otherwise
    silently un-pause the conversation."""
    src = _read_webhook()
    start = src.find("PAYMENT-ASSET EARLY BYPASS")
    end = src.find("AI loop / cost guard", start)
    block = src[start:end]
    assert "is_human_handoff = False" not in block, (
        "early bypass must not silently un-pause merchant-owned convos"
    )
    assert "paused_by_human = False" not in block


# ─────────────────────────────────────────────────────────────────────────
# 2. Late-stage owner-fallback detection — phrasings GPT actually used
#    in production must trigger the text replacement.
# ─────────────────────────────────────────────────────────────────────────

def test_late_stage_fallback_detection_covers_actual_gpt_phrasings():
    """The merchant's screenshot shows GPT replying with
    ``"أعتذر إني ما أقدر أوفرها لك مباشرة — بس الفريق راح يتواصل معك"``.
    The previous detection list missed this phrasing. Lock the new
    phrases in source so future refactors can't shrink the list."""
    src = _read_webhook()
    required_markers = [
        "ما أقدر أوفرها",          # "ما أقدر أوفرها لك"
        "أعتذر إني ما أقدر",       # "أعتذر إني ما أقدر"
        "راح يتواصل معك",          # "الفريق راح يتواصل معك"
        "وصل طلبك للفريق",         # "وصل طلبك لفريق المتجر"
        "وصلت رسالتك",             # generic safe-reply leak
        "تواصلي مع المتجر",        # feminine variant
        "أحوّلك للفريق",           # active escalation phrasing
    ]
    for m in required_markers:
        assert m in src, (
            f"late-stage owner-fallback detection missing phrase {m!r} — "
            f"GPT will say this in production and the override won't "
            f"replace it with the warm payment intro"
        )


# ─────────────────────────────────────────────────────────────────────────
# 3. Helper-function correctness (already covered indirectly, but lock
#    the specific call shape used by the bypass).
# ─────────────────────────────────────────────────────────────────────────

def test_is_payment_query_catches_screenshot_phrasing():
    """The exact verbatim message from the merchant's screenshot must
    light up the payment-query detector. If this test ever fails, the
    bypass will silently miss the most common phrasing and the bug
    regresses."""
    from core.ai_libraries import is_payment_query

    assert is_payment_query("ارسل لي حساب الراجحي") is True
    # Also the variant the customer sent earlier in the same chat.
    assert is_payment_query("طيب موبايلي أو STC Pay؟") is False, (
        "payment-method-name-only questions must NOT trip the bypass; "
        "they're not asking for a barcode/account asset"
    )


def test_find_best_payment_asset_validates_via_validate_media_compatible_shape():
    """The bypass calls ``validate_media_for_send`` directly on the
    return value of ``find_best_payment_asset``. Lock the keys
    required by the validator so a future refactor of either side
    can't drift apart silently."""
    from core.ai_libraries import find_best_payment_asset, validate_media_for_send
    from unittest.mock import MagicMock

    class _Row:
        def __init__(self):
            self.id = 7
            self.tenant_id = 33
            self.title = "باركود التحويل البنكي الراجحي"
            self.tags = ["تحويل", "بنك", "راجحي"]
            self.usage_context = "أرسله إذا طلب العميل التحويل البنكي"
            self.media_type = "image"
            self.file_url = "https://example.com/barcode.png"
            self.mime_type = "image/png"
            self.storage_kind = "external"
            self.storage_path = ""
            self.file_size_bytes = 4096
            self.is_active = True
            self.priority = 1

    db = MagicMock()
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.all.return_value = [_Row()]
    db.query.return_value = chain

    asset = find_best_payment_asset(db, tenant_id=33, customer_message="ارسل حساب الراجحي")
    assert asset is not None

    # Round-trip through the validator (with db=None to skip the DB
    # re-fetch). Should return ok=True.
    ok, err, normalised = validate_media_for_send(
        asset, expected_tenant_id=33, db=None,
    )
    assert ok is True, (
        f"asset shape from find_best_payment_asset must validate cleanly; "
        f"validator said err={err!r}"
    )
    assert normalised is not None
    assert normalised["file_url"].startswith("https://")


# ─────────────────────────────────────────────────────────────────────────
# 4. Logging contract — the merchant explicitly asked for these log
#    fields so they can diagnose at runtime ("does the new code run?").
# ─────────────────────────────────────────────────────────────────────────

def test_payment_info_log_block_emits_required_fields():
    """The merchant requested specific debug fields; lock them into the
    webhook source so a future refactor can't accidentally drop the
    diagnostic that lets them confirm the bypass fired."""
    src = _read_webhook()
    # Early-gate log line (always fires on payment intent so the
    # merchant can verify the new code is deployed).
    assert "[PAYMENT_INFO] early-gate" in src
    # APPLIED line — fires when the bypass actually runs.
    assert "early-bypass APPLIED" in src
    # Required diagnostic fields.
    for field in (
        "intent_detected",
        "asset_found",
        "asset_id",
        "asset_score",
        "transfer_fallback_skipped",
        "hard_override",
    ):
        assert field in src, f"required log field {field!r} missing"
