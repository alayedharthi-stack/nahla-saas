"""
tests/test_outbound_planner_scrub.py
────────────────────────────────────
Locks the contract for the June 2026 incident: an outbound reply
contained raw planner identifiers (``response_goal``,
``execute_pending_offer``, ``resolve_ambiguous_need``) embedded in
otherwise natural Arabic prose.

The fix is a surgical extension of the existing wire-layer guard
(``core.outbound_sanitizer.sanitize_outbound_payload``) — same
chokepoint that already runs in ``_post_wa`` for the May 2026
search-leak incident. No prompt changes, no brain-logic changes,
no new intent layers — output guard only.

Invariants under test
─────────────────────
1.  ``contains_planner_markers`` returns the matching name for each
    listed identifier and ``None`` for clean Arabic text.
2.  ``extract_natural_segment`` recovers the clean Arabic portion
    of a contaminated reply (paragraph-level filtering preferred,
    line-level fallback).
3.  ``sanitize_outbound_payload`` rewrites a ``text`` payload that
    leaked ``response_goal`` / ``execute_pending_offer`` /
    ``resolve_ambiguous_need`` to the recovered natural sentence.
4.  Buttons on interactive payloads are PRESERVED when we recover
    a natural segment (the buttons were authored for the same turn
    and are still meaningful).
5.  Clean replies pass through untouched.
6.  When no clean segment can be salvaged, the payload falls back
    to ``SAFE_FALLBACK_TEXT`` rather than leaking.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────────
# 1. Predicate
# ──────────────────────────────────────────────────────────────────────


class TestContainsPlannerMarkers:

    def test_detects_response_goal(self):
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers(
            "الـ response_goal يقول execute_pending_offer"
        ) is not None

    def test_detects_each_named_identifier(self):
        from core.outbound_sanitizer import contains_planner_markers
        for token in (
            "response_goal",
            "execute_pending_offer",
            "resolve_ambiguous_need",
        ):
            assert contains_planner_markers(f"بداية {token} نهاية") == token, (
                f"{token} not detected by contains_planner_markers"
            )

    def test_detects_action_constants(self):
        from core.outbound_sanitizer import contains_planner_markers
        for token in ("ACTION_LLM_REPLY", "ACTION_WEB_SEARCH", "ACTION_HANDOFF"):
            assert contains_planner_markers(f"خذي {token} منا") == "action_token"

    def test_detects_field_assignments(self):
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers("intent=greeting") == "intent_field"
        assert contains_planner_markers("decision: ACTION_LLM_REPLY") in (
            "decision_field", "action_token",
        )

    def test_detects_bare_english_diagnostic_words(self):
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers("هذا internal فقط") == "internal_word"
        assert contains_planner_markers("debug: stage=exploring") == "debug_word"

    def test_clean_arabic_passes(self):
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers(
            "أبشر 👍 تبي ربع السمر بـ 79 ريال ولا ربع الطلح بـ 126 ريال؟"
        ) is None

    def test_arabic_word_goal_is_not_mistaken(self):
        """``GOAL`` (uppercase) is the planner constant prefix.
        Customer-facing Arabic text may say ``goal`` in lowercase
        (rare) — current rule treats lowercase ``goal`` as safe
        because the regex requires ``GOAL_<UPPER>`` shape."""
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers("نعم هذا هدفنا (goal) الأساسي") is None

    def test_empty_and_none_safe(self):
        from core.outbound_sanitizer import contains_planner_markers
        assert contains_planner_markers("") is None
        assert contains_planner_markers(None) is None  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# 2. Natural-segment extractor
# ──────────────────────────────────────────────────────────────────────


class TestExtractNaturalSegment:

    def test_recovers_clean_paragraph_from_two_paragraph_leak(self):
        """Mirrors the EXACT shape of the June 2026 leak: a
        thinking-out-loud paragraph followed by a clean Arabic
        paragraph, separated by a blank line."""
        from core.outbound_sanitizer import (
            contains_planner_markers,
            extract_natural_segment,
        )
        leaked = (
            "العميل قال \"تمام\" بعد ما عرضت عليه خيارات (ربع السمر "
            "أو ربع الطلح). هذا تأكيد قصير لكن غير واضح أيهما يريد. "
            "الـ response_goal يقول execute_pending_offer + "
            "resolve_ambiguous_need. يجب أن أسأله أي خيار يبي بدون إطالة.\n"
            "\n"
            "أبشر 👍 تبي ربع السمر بـ 79 ريال ولا ربع الطلح بـ 126 ريال؟"
        )
        recovered = extract_natural_segment(leaked)
        assert recovered is not None
        assert "response_goal" not in recovered
        assert "execute_pending_offer" not in recovered
        assert "resolve_ambiguous_need" not in recovered
        assert "العميل قال" not in recovered  # planner narration dropped
        assert "أبشر" in recovered
        assert "ربع السمر" in recovered and "ربع الطلح" in recovered
        # Sanity: the recovered text is itself clean for the predicate.
        assert contains_planner_markers(recovered) is None

    def test_falls_back_to_line_level_when_no_blank_line_separator(self):
        from core.outbound_sanitizer import extract_natural_segment
        leaked = (
            "intent=ack decision=ACTION_LLM_REPLY\n"
            "أبشر، طلبك جاهز للشحن."
        )
        recovered = extract_natural_segment(leaked)
        assert recovered == "أبشر، طلبك جاهز للشحن."

    def test_returns_none_when_nothing_clean(self):
        from core.outbound_sanitizer import extract_natural_segment
        leaked = "response_goal=ack\nintent=greet\ndecision=ACTION_LLM_REPLY"
        assert extract_natural_segment(leaked) is None

    def test_returns_none_for_empty(self):
        from core.outbound_sanitizer import extract_natural_segment
        assert extract_natural_segment("") is None
        assert extract_natural_segment(None) is None  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# 3. End-to-end: sanitize_outbound_payload on the live leak shape
# ──────────────────────────────────────────────────────────────────────


class TestSanitizeOutboundPayload:

    # The exact reply the customer received (from the merchant
    # screenshot, June 2026 incident).
    LEAKED_REPLY = (
        "العميل قال \"تمام\" بعد ما عرضت عليه خيارات (ربع السمر "
        "أو ربع الطلح). هذا تأكيد قصير لكن غير واضح أيهما يريد. "
        "الـ response_goal يقول execute_pending_offer + "
        "resolve_ambiguous_need. يجب أن أسأله أي خيار يبي بدون إطالة.\n"
        "\n"
        "أبشر 👍 تبي ربع السمر بـ 79 ريال ولا ربع الطلح بـ 126 ريال؟"
    )
    EXPECTED_CLEAN = (
        "أبشر 👍 تبي ربع السمر بـ 79 ريال ولا ربع الطلح بـ 126 ريال؟"
    )

    def test_text_payload_is_rewritten_to_clean_segment(self):
        """The user-specified end-to-end test: feed the live leak
        into the sanitiser and assert the final body contains ONLY
        the natural Arabic sentence."""
        from core.outbound_sanitizer import sanitize_outbound_payload
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {"body": self.LEAKED_REPLY},
        }
        out, sanitised = sanitize_outbound_payload(
            payload, tenant_id=33, recipient="+966500000111",
        )
        assert sanitised is True
        body = out["text"]["body"]
        # Hard contract: NO internal/planner words survive.
        for forbidden in (
            "response_goal",
            "execute_pending_offer",
            "resolve_ambiguous_need",
            "العميل قال",
            "internal",
            "ACTION_",
            "intent=",
            "decision=",
        ):
            assert forbidden not in body, (
                f"sanitiser leaked {forbidden!r} into outbound body: {body!r}"
            )
        # Hard contract: the natural sentence IS what the customer sees.
        assert body == self.EXPECTED_CLEAN

    def test_clean_payload_passes_through_unchanged(self):
        from core.outbound_sanitizer import sanitize_outbound_payload
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {"body": self.EXPECTED_CLEAN},
        }
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=33)
        assert sanitised is False
        assert out["text"]["body"] == self.EXPECTED_CLEAN

    def test_interactive_payload_recovery_keeps_buttons(self):
        """When we RECOVER a natural segment (vs falling back to
        the generic apology), buttons authored for the same turn
        must be preserved — the buttons reflect the merchant's
        intent for the reply we are now safely sending."""
        from core.outbound_sanitizer import sanitize_outbound_payload
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": self.LEAKED_REPLY},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "samr",  "title": "ربع السمر"}},
                        {"type": "reply", "reply": {"id": "talh",  "title": "ربع الطلح"}},
                    ],
                },
            },
        }
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=33)
        assert sanitised is True
        assert out["interactive"]["body"]["text"] == self.EXPECTED_CLEAN
        # Buttons survived because we recovered a natural segment.
        buttons = out["interactive"]["action"]["buttons"]
        assert len(buttons) == 2
        assert buttons[0]["reply"]["title"] == "ربع السمر"
        assert buttons[1]["reply"]["title"] == "ربع الطلح"

    def test_unrecoverable_leak_falls_back_to_safe_text(self):
        """Reply that is ENTIRELY planner output (no clean Arabic
        segment to recover) must fall back to ``SAFE_FALLBACK_TEXT``
        rather than leaking."""
        from core.outbound_sanitizer import (
            SAFE_FALLBACK_TEXT,
            sanitize_outbound_payload,
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {
                "body": (
                    "intent=ack\n"
                    "decision=ACTION_LLM_REPLY\n"
                    "response_goal=execute_pending_offer"
                ),
            },
        }
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=33)
        assert sanitised is True
        assert out["text"]["body"] == SAFE_FALLBACK_TEXT

    def test_planner_check_runs_before_search_check(self):
        """Order matters: a reply that contains BOTH a planner
        identifier and a search-leak fingerprint should be handled
        by the planner branch (which preserves recoverable text)
        rather than the search branch (which would replace with the
        generic apology). Belt-and-suspenders contract."""
        from core.outbound_sanitizer import sanitize_outbound_payload
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {
                "body": (
                    "response_goal=execute_pending_offer\n"
                    "\n"
                    "أبشر، الطلب جاهز للشحن."
                ),
            },
        }
        out, sanitised = sanitize_outbound_payload(payload, tenant_id=33)
        assert sanitised is True
        assert out["text"]["body"] == "أبشر، الطلب جاهز للشحن."


# ──────────────────────────────────────────────────────────────────────
# 4. Wire-up: the sanitiser is still called inside _post_wa
# ──────────────────────────────────────────────────────────────────────


class TestSanitiserStillWired:
    """Locks the physical wire-up so a future refactor that removes
    the sanitiser call from ``_post_wa`` is forced to update this
    test (and notice the contract change).

    We read the file at its known path rather than via the
    imported module's ``__file__`` to avoid being affected by
    ``sys.path`` pollution from sibling tests (the backend ships a
    ``core/secrets.py`` that can shadow Python's stdlib ``secrets``
    on certain test orderings, breaking starlette's import chain)."""

    def test_post_wa_imports_sanitize_outbound_payload(self):
        webhook_path = REPO_ROOT / "backend" / "routers" / "whatsapp_webhook.py"
        assert webhook_path.exists(), (
            f"expected {webhook_path} to exist — file moved? "
            f"update this test if the router was relocated"
        )
        src = webhook_path.read_text(encoding="utf-8")
        assert "from core.outbound_sanitizer import sanitize_outbound_payload" in src, (
            "_post_wa no longer imports sanitize_outbound_payload — "
            "the final-line-of-defence outbound sanitiser has been "
            "disconnected from the WhatsApp send path"
        )
        assert "sanitize_outbound_payload(" in src
