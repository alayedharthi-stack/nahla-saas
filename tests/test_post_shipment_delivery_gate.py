"""
tests/test_post_shipment_delivery_gate.py
─────────────────────────────────────────
Locks the post-shipment delivery-confirmation gate (May 2026 #12).

Production reproducer — merchant screenshot
────────────────────────────────────────────
Customer receives a tracking-link / shipment notice from the merchant.
Hours later the customer sends:

    "وصل الله يوصل في عمرك بوهشام اليوم اخذته"

(blessing + "I received it today, I took it")

The bot replied:

    "وصل الإيصال، وسيتم متابعة الطلب وتجهيز الشحن للهفوف بإذن الله 🚚"

That is ENTIRELY wrong: the customer was confirming the PACKAGE
arrived, not claiming a money transfer. Two layers misfired:
  1. ``payment_intent.maybe_handle_payment_claim`` could fire on
     overlapping vocabulary ("وصل" prefix) when an active funnel
     was still flagged ``awaiting_payment_receipt``.
  2. ``order_flow.context_aware_dedup_fallback`` re-prompted for the
     receipt because the stale ``awaiting_payment_receipt=True`` flag
     never got cleared after the merchant manually sent shipping.

Surgical fix — no new intents, templates, or layers
───────────────────────────────────────────────────
* New helpers in ``core/payment_intent``:
    - ``looks_like_delivery_confirmation(text)`` — soft delivery
      tokens, *no* payment vocabulary allowed.
    - ``is_post_shipment_context(db, tenant_id, phone)`` — scans the
      last ~12 outbound message events for shipment markers.
    - ``is_post_shipment_delivery_confirmation(db, …, inbound_text)``
      — convenience combiner.
* ``maybe_handle_payment_claim`` returns ``None`` when the gate
  fires — brain handles it normally.
* ``context_aware_dedup_fallback`` accepts ``inbound_text`` and
  suppresses the awaiting-receipt re-prompt under the same gate.

Invariants under test
─────────────────────
1. Soft delivery confirmations + recent shipment outbound → gate fires.
2. Explicit transfer / receipt phrases → gate stays inert (real
   payment claims still short-circuit).
3. Unrelated text → gate inert.
4. Missing shipment outbound history → gate inert (no false-positive).
5. ``maybe_handle_payment_claim`` skips gracefully on the gate hit.
6. ``context_aware_dedup_fallback`` falls through to the default
   fallback when the gate fires AND ``awaiting_payment_receipt`` is
   set; still re-prompts for receipt when the gate is inert.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Fakes ───────────────────────────────────────────────────────────────


def _msg_event(*, body: str, direction: str = "out"):
    """Build a row-like object the helper can read via tuple index."""
    return (body, direction)


def _wire_db(*, conv_id: int | None = 17, msg_rows: List[Any] | None = None):
    """Mock SQLAlchemy session.

    The MessageEvent query chain
    ``query(_Msg.body, _Msg.direction) → filter → filter → order_by
    → limit → all`` returns ``msg_rows``. Conversation lookup is
    handled by patching ``order_flow._find_conversation_by_phone``
    in the calling test (so we sidestep the JOIN with Customer).
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.order_by.\
        return_value.limit.return_value.all.return_value = list(msg_rows or [])
    db._fake_conv_id = conv_id
    return db


def _patch_conv_lookup(monkeypatch, conv_id: int | None = 17):
    """Patch ``order_flow._find_conversation_by_phone`` to return a
    stub conversation row with the given id (or None)."""
    import core.order_flow as _of
    if conv_id is None:
        monkeypatch.setattr(
            _of, "_find_conversation_by_phone",
            lambda *a, **kw: None,
        )
    else:
        stub = MagicMock()
        stub.id = conv_id
        monkeypatch.setattr(
            _of, "_find_conversation_by_phone",
            lambda *a, **kw: stub,
        )


# ── 1. ``looks_like_delivery_confirmation`` ─────────────────────────────


class TestLooksLikeDeliveryConfirmation:
    def test_soft_delivery_phrases_match(self):
        from core.payment_intent import looks_like_delivery_confirmation
        for t in (
            "وصل الله يوصل في عمرك بوهشام اليوم اخذته",
            "استلمته اليوم، شكراً",
            "وصلني قبل قليل",
            "اخذت الطلب الحين",
            "تسلمت الطلب",
            "وصل بسلامه ومشكورين",
        ):
            assert looks_like_delivery_confirmation(t), (
                f"expected delivery-confirmation match for: {t!r}"
            )

    def test_explicit_payment_phrases_disqualify(self):
        from core.payment_intent import looks_like_delivery_confirmation
        # "وصل التحويل" / "وصل الإيصال" must NOT register as delivery.
        for t in (
            "وصل التحويل",
            "وصل الإيصال شكراً",
            "تم التحويل بنجاح",
            "حولت لك المبلغ",
            "ارسلت لك الايصال",
            "هذا ايصال مدفوع",
        ):
            assert not looks_like_delivery_confirmation(t), (
                f"explicit payment phrase MUST NOT register as delivery: {t!r}"
            )

    def test_unrelated_text_does_not_match(self):
        from core.payment_intent import looks_like_delivery_confirmation
        for t in (
            "السلام عليكم",
            "بكم سعر العسل؟",
            "ابغى اطلب",
            "",
            None,
        ):
            assert not looks_like_delivery_confirmation(t)

    def test_long_message_short_circuits(self):
        """The gate is for soft, conversational confirmations — a
        500-char message is something else and not our concern."""
        from core.payment_intent import looks_like_delivery_confirmation
        long_text = "وصل اليوم " + ("بلابلا " * 60)
        assert not looks_like_delivery_confirmation(long_text)


# ── 2. ``is_post_shipment_context`` ─────────────────────────────────────


class TestIsPostShipmentContext:
    def test_recent_outbound_with_shipment_markers_matches(self, monkeypatch):
        from core.payment_intent import is_post_shipment_context
        _patch_conv_lookup(monkeypatch, conv_id=17)
        rows = [
            _msg_event(body="مرحباً، الطلب رقم #1234 تم شحنه وهو في طريقه إليك", direction="out"),
            _msg_event(body="هلا", direction="in"),
        ]
        db = _wire_db(conv_id=17, msg_rows=rows)
        assert is_post_shipment_context(db, tenant_id=1, phone="+966500000001")

    def test_each_marker_triggers(self, monkeypatch):
        """At least one row matches when ANY of the documented
        shipment markers appears in an outbound."""
        from core.payment_intent import is_post_shipment_context
        _patch_conv_lookup(monkeypatch, conv_id=17)
        for marker_body in (
            "تم شحن الطلب",
            "خارج للتوصيل",
            "في طريقه إليك",
            "متابعة حالة الشحن",
            "رقم التتبع: TRK-9919",
            "shipped via Aramex",
            "out for delivery today",
        ):
            db = _wire_db(
                conv_id=17,
                msg_rows=[_msg_event(body=marker_body, direction="out")],
            )
            assert is_post_shipment_context(db, tenant_id=1, phone="+966500000001"), (
                f"expected shipment-context detection for outbound body: {marker_body!r}"
            )

    def test_inbound_shipment_words_do_not_match(self, monkeypatch):
        """Customer-side messages that happen to contain a shipping
        word must not satisfy the gate — only OUR outbound proves
        we already pushed a tracking notice."""
        from core.payment_intent import is_post_shipment_context
        _patch_conv_lookup(monkeypatch, conv_id=17)
        rows = [
            _msg_event(body="متى تشحن الطلب؟", direction="in"),
            _msg_event(body="مرحباً", direction="out"),
        ]
        db = _wire_db(conv_id=17, msg_rows=rows)
        assert not is_post_shipment_context(db, tenant_id=1, phone="+966500000001")

    def test_no_conversation_row_falls_through(self, monkeypatch):
        from core.payment_intent import is_post_shipment_context
        _patch_conv_lookup(monkeypatch, conv_id=None)
        db = _wire_db(conv_id=None, msg_rows=[])
        assert not is_post_shipment_context(db, tenant_id=1, phone="+966500000001")

    def test_db_failure_returns_false(self):
        from core.payment_intent import is_post_shipment_context
        db = MagicMock()
        db.query.side_effect = RuntimeError("simulated outage")
        assert not is_post_shipment_context(db, tenant_id=1, phone="+966500000001")


# ── 3. Combiner + production reproducer ─────────────────────────────────


class TestPostShipmentDeliveryConfirmationCombiner:
    def test_screenshot_reproducer_fires(self, monkeypatch):
        """The exact merchant screenshot:
        * Outbound: tracking link / "تم شحن"
        * Inbound: "وصل الله يوصل في عمرك بوهشام اليوم اخذته"
        Both halves match → combiner returns True.
        """
        from core.payment_intent import is_post_shipment_delivery_confirmation
        _patch_conv_lookup(monkeypatch, conv_id=17)
        rows = [
            _msg_event(
                body="طلبك رقم #4127 تم شحنه وهو في طريقه إليك. متابعة حالة الشحن: https://t.example/4127",
                direction="out",
            ),
        ]
        db = _wire_db(conv_id=17, msg_rows=rows)
        assert is_post_shipment_delivery_confirmation(
            db, tenant_id=1, phone="+966500000001",
            inbound_text="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
        )

    def test_explicit_payment_inbound_does_not_fire(self, monkeypatch):
        """Real receipt claims still flow through to the existing
        payment-claim short-circuit even when shipment context is
        present (defensive: we never want to disqualify a legitimate
        transfer claim)."""
        from core.payment_intent import is_post_shipment_delivery_confirmation
        _patch_conv_lookup(monkeypatch, conv_id=17)
        rows = [
            _msg_event(body="تم شحن الطلب رقم #4127", direction="out"),
        ]
        db = _wire_db(conv_id=17, msg_rows=rows)
        for inbound in (
            "وصل التحويل",
            "حولت لك المبلغ الآن",
            "ارسلت لك الايصال",
            "تم الدفع",
        ):
            assert not is_post_shipment_delivery_confirmation(
                db, tenant_id=1, phone="+966500000001",
                inbound_text=inbound,
            ), f"explicit payment inbound MUST NOT trip the gate: {inbound!r}"

    def test_no_shipment_context_does_not_fire(self, monkeypatch):
        """Without a recent shipment notice, even a perfect delivery
        phrase must NOT trip the gate. Otherwise we'd disqualify
        first-time customers who happen to say "وصلني الطلب" when no
        order is in flight."""
        from core.payment_intent import is_post_shipment_delivery_confirmation
        _patch_conv_lookup(monkeypatch, conv_id=17)
        db = _wire_db(conv_id=17, msg_rows=[])
        assert not is_post_shipment_delivery_confirmation(
            db, tenant_id=1, phone="+966500000001",
            inbound_text="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
        )


# ── 4. ``maybe_handle_payment_claim`` integration ───────────────────────


class TestPaymentClaimSkipsOnDeliveryGate:
    def test_payment_claim_returns_none_under_gate(self, monkeypatch):
        """When the inbound matches both detection layers, the
        payment-claim short-circuit MUST return None so the brain
        composes a natural delivery-acknowledgement reply."""
        from core import payment_intent

        # Inbound matches ``detect_payment_confirmation_text`` because
        # we deliberately use a borderline phrase that overlaps with
        # transfer wording, then we add the shipment outbound.
        rows = [_msg_event(body="تم شحن الطلب رقم #4127", direction="out")]
        db = _wire_db(conv_id=17, msg_rows=rows)
        _patch_conv_lookup(monkeypatch, conv_id=17)

        # Force ``detect_payment_confirmation_text`` to True so we
        # actually exercise the gate path (otherwise the function
        # returns None earlier on the detection short-circuit).
        monkeypatch.setattr(
            payment_intent, "detect_payment_confirmation_text",
            lambda _t: True,
        )

        result = payment_intent.maybe_handle_payment_claim(
            db, tenant_id=1, phone="+966500000001",
            inbound_text="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
            has_attached_media=False,
        )
        assert result is None, (
            "post-shipment delivery confirmation must suppress the "
            "payment-claim short-circuit"
        )

    def test_payment_claim_still_fires_for_real_transfer(self, monkeypatch):
        """Legacy behaviour (feature flag off): explicit transfer
        claims short-circuit with a hardcoded ACK even when shipment
        context is present.

        After Tenant 33 #48 (May 2026) the new default is to NOT
        short-circuit; this test disables the flag so the legacy
        path is still covered while it remains supported."""
        # Roll back to legacy hardcoded-ACK behaviour for this test.
        monkeypatch.setenv("PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED", "0")

        from core import payment_intent

        rows = [_msg_event(body="تم شحن الطلب", direction="out")]
        db = _wire_db(conv_id=17, msg_rows=rows)

        # Stub the brain-state load so the active-context gate passes.
        def _fake_loader(_db, *, tenant_id, phone):
            return None, {}
        def _fake_focus(_bs):
            return {
                "selected_product": "عسل سدر",
                "awaiting_payment_receipt": True,
                "payment_receipt_received": False,
                "order_status": "awaiting_receipt",
            }
        # Patch the order_flow imports done inside the function.
        import core.order_flow as _of
        monkeypatch.setattr(_of, "_load_brain_state", _fake_loader)
        monkeypatch.setattr(_of, "_focus_summary", _fake_focus)

        # Ensure the receipt-override path is a no-op so the legacy
        # claim ack runs.
        monkeypatch.setattr(
            payment_intent, "_maybe_promote_prior_evidence",
            lambda **_kw: None,
        )

        result = payment_intent.maybe_handle_payment_claim(
            db, tenant_id=1, phone="+966500000001",
            inbound_text="تم التحويل",
            has_attached_media=False,
        )
        assert result is not None, (
            "explicit transfer claim must still short-circuit even "
            "when a shipment notice was previously sent"
        )
        assert "reply_text" in result and "state_patch" in result


# ── 5. ``context_aware_dedup_fallback`` integration ─────────────────────


class TestContextAwareDedupFallbackHonoursGate:
    def _patch_brain_state(self, monkeypatch, summary):
        import core.order_flow as _of
        monkeypatch.setattr(
            _of, "_load_brain_state",
            lambda _db, *, tenant_id, phone: (None, {}),
        )
        monkeypatch.setattr(
            _of, "_focus_summary",
            lambda _bs: summary,
        )

    def test_awaiting_receipt_reprompt_suppressed_under_gate(self, monkeypatch):
        from core.order_flow import context_aware_dedup_fallback

        self._patch_brain_state(monkeypatch, {
            "awaiting_payment_receipt": True,
            "payment_receipt_received": False,
            "selected_product": None,
            "order_status": "awaiting_receipt",
        })
        _patch_conv_lookup(monkeypatch, conv_id=17)

        rows = [_msg_event(body="تم شحن الطلب رقم #4127", direction="out")]
        db = _wire_db(conv_id=17, msg_rows=rows)

        out = context_aware_dedup_fallback(
            db, tenant_id=1, phone="+966500000001",
            history=[],
            default_fallback="تأمر بشيء أكمّل لك فيه؟",
            inbound_text="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
        )
        # The receipt re-prompt MUST NOT appear when the gate fires.
        assert "بانتظار إيصال التحويل" not in out, (
            "receipt re-prompt leaked despite delivery-confirmation gate"
        )
        # Falls through to the default fallback (or a deeper branch
        # like selected_product, but here there's no product set).
        assert out == "تأمر بشيء أكمّل لك فيه؟"

    def test_awaiting_receipt_reprompt_still_fires_without_gate(self, monkeypatch):
        """Inbound that does NOT look like delivery confirmation
        keeps the original behaviour — re-ask for the receipt."""
        from core.order_flow import context_aware_dedup_fallback

        self._patch_brain_state(monkeypatch, {
            "awaiting_payment_receipt": True,
            "payment_receipt_received": False,
            "selected_product": None,
            "order_status": "awaiting_receipt",
        })

        rows = [_msg_event(body="تم شحن الطلب رقم #4127", direction="out")]
        db = _wire_db(conv_id=17, msg_rows=rows)

        out = context_aware_dedup_fallback(
            db, tenant_id=1, phone="+966500000001",
            history=[],
            default_fallback="تأمر بشيء أكمّل لك فيه؟",
            inbound_text="بكم سعر التوصيل؟",
        )
        assert "بانتظار إيصال التحويل" in out, (
            "non-delivery inbound must still get the receipt re-prompt"
        )

    def test_awaiting_receipt_reprompt_when_inbound_text_omitted(self, monkeypatch):
        """Backwards-compat: callers that don't pass ``inbound_text``
        keep the legacy behaviour (re-prompt for the receipt)."""
        from core.order_flow import context_aware_dedup_fallback

        self._patch_brain_state(monkeypatch, {
            "awaiting_payment_receipt": True,
            "payment_receipt_received": False,
            "selected_product": None,
            "order_status": "awaiting_receipt",
        })

        out = context_aware_dedup_fallback(
            MagicMock(),
            tenant_id=1, phone="+966500000001",
            history=[],
            default_fallback="تأمر بشيء أكمّل لك فيه؟",
            # inbound_text intentionally omitted
        )
        assert "بانتظار إيصال التحويل" in out
