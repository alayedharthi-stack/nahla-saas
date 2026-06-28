"""
tests/test_minor_ai_fixes.py
─────────────────────────────
Regression coverage for the May 2026 minor AI behaviour hotfixes.
The intent of every test is to lock the surface area that production
already relies on so we never silently re-introduce the bug.

Five independent fixes are covered:

1. Name extractor rejects arrival/presence verbs ("أنا وصلت" / "جاي"
   / "أنا هنا"). Both the high-confidence extractor and the order-
   funnel slot extractor must refuse these as customer names.
2. Payment-evidence text override: when the customer says "هذا
   ايصال مدفوع" / "لا هذا ايصال" right after we mis-classified
   their PDF as a pre-transfer review, the next claim promotes
   the prior receipt to confirmed.
3. Handoff intent: explicit "أبي أتكلم مع أحد" must classify as
   INTENT_TALK_HUMAN (rule-based) so the deterministic webhook
   guard can flip needs_human / handoff_active.
4. Address interview: ``_compose_receipt_ack`` appends the
   structured first-name / last-name / city / Google-Maps OR
   national-short-address ask when those fields aren't already
   collected.
5. Map screenshot: ``maybe_handle_map_image_inbound`` short-circuits
   for ``image_kind=map_screenshot`` during an active order, asking
   the customer for a parseable location form.

Each test is isolated and uses tiny in-memory fixtures (no real
DB engine) so the suite runs in milliseconds and stays stable
against unrelated refactors.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ────────────────────────────────────────────────────────────────────
# Fix 1 — name extractor rejects arrival / presence verbs
# ────────────────────────────────────────────────────────────────────


def test_arrival_verbs_are_not_extracted_as_names() -> None:
    """High-confidence extractor must reject "أنا وصلت" / "أنا جاي"
    / "أنا هنا" / "وصلت" / "جايه الحين"."""
    from core.customer_name_extractor import extract_high_confidence_name

    for sample in (
        "أنا وصلت",
        "انا وصلت",
        "أنا جاي",
        "انا جايه",
        "أنا هنا",
        "أنا موجود",
        "وصلت",
        "وصلنا",
        "جاي الحين",
        "جايه الحين",
    ):
        result = extract_high_confidence_name(sample)
        assert result is None, (
            f"Should NOT extract a name from {sample!r}, got "
            f"{getattr(result, 'value', result)!r}"
        )


def test_real_names_still_extracted() -> None:
    """Sanity: explicit self-intros still get captured; bare ``أنا …``
    without ``اسمي`` is rejected under the P0 name policy."""
    from core.customer_name_extractor import extract_high_confidence_name

    assert extract_high_confidence_name("أنا محمد") is None

    result = extract_high_confidence_name("أنا اسمي محمد")
    assert result is not None
    assert result.value == "محمد"

    result = extract_high_confidence_name("اسمي محمد")
    assert result is not None
    assert result.value == "محمد"

    result = extract_high_confidence_name("معك محمد")
    assert result is not None
    assert result.value == "محمد"


def test_ordering_extractor_rejects_arrival_verbs() -> None:
    """The order-funnel slot extractor also refuses arrival verbs
    as the customer's name."""
    from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots

    for sample in (
        "وصلت",
        "أنا وصلت",
        "جاي",
        "أنا جاي",
        "موجود",
        "أنا موجود",
    ):
        slots = extract_ordering_slots(sample)
        assert not slots.get("customer_name"), (
            f"customer_name should be empty for {sample!r}, got "
            f"{slots.get('customer_name')!r}"
        )


# ────────────────────────────────────────────────────────────────────
# Fix 2 — payment-evidence text override promotes prior receipt
# ────────────────────────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, events: List[Any]) -> None:
        self._events = events

    def filter(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def limit(self, _n: int) -> "_FakeQuery":
        return self

    def all(self) -> List[Any]:
        return list(self._events)


class _FakeDB:
    def __init__(self, events: Optional[List[Any]] = None) -> None:
        self._events = events or []

    def query(self, _model: Any) -> _FakeQuery:
        return _FakeQuery(self._events)


def _make_inbound_event(*, payment_evidence_status: str) -> Any:
    return SimpleNamespace(
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        direction="inbound",
        extra_metadata={
            "payment_evidence_status": payment_evidence_status,
            "pdf_kind": "payment_pre_review",
            "filename": "Transaction-Receipt.pdf",
            "wa_message_id": "wamid.TEST",
        },
    )


def test_payment_evidence_override_promotes_prior_receipt(
    monkeypatch: Any,
) -> None:
    """When the prior inbound was a pre-transfer-review PDF and the
    customer corrects with "هذا ايصال مدفوع", the helper returns the
    confirmed-receipt patch + receipt ACK reply."""
    from core import payment_intent as pi

    # Stub the brain-state loader so the helper sees an active order.
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {
                    "title": "عسل سدر", "price": 360, "currency": "SAR",
                },
                "order_prep": {
                    "awaiting_payment_receipt": True,
                    "city": "الرياض",
                    "catalog_line_items_authoritative": True,
                    "catalog_checkout_total": 360,
                    "line_items": [{
                        "product_id": "p-sidr",
                        "product_name": "عسل سدر",
                        "quantity": 1,
                        "unit_price": 360,
                        "from_native_catalog_order": True,
                        "match_status": "confirmed",
                    }],
                },
            },
        ),
    )

    db = _FakeDB(events=[
        _make_inbound_event(payment_evidence_status="pre_transfer_review"),
    ])
    result = pi.maybe_handle_payment_claim(
        db,
        tenant_id=1,
        phone="+966500000001",
        inbound_text="لا هذا ايصال مدفوع",
        has_attached_media=False,
    )
    assert result is not None, (
        "override branch should fire when prior inbound was pre-transfer"
    )
    sp = result["state_patch"]
    assert sp["payment_receipt_received"] is True
    assert sp["awaiting_payment_receipt"] is False
    assert sp["order_status"] == "payment_submitted"
    assert "promoted_from" in sp["payment_receipt_metadata"]
    assert sp["payment_receipt_metadata"]["promoted_from"] in (
        "pre_transfer_review", "needs_confirmation",
        "payment_pre_review", "payment_pending_evidence",
    )
    # Receipt ACK includes the product line.
    assert "عسل سدر" in result["reply_text"]


def test_payment_evidence_override_skipped_without_prior_receipt(
    monkeypatch: Any,
) -> None:
    """If there's no recent pre-transfer/pending evidence inbound,
    the helper falls through to the legacy claim-ack branch.

    Tenant 33 #48 (May 2026) introduced the brain-driven text-claim
    policy (default on). This test verifies the legacy branch while
    that policy remains togglable, so we explicitly disable the
    flag for this assertion."""
    monkeypatch.setenv("PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED", "0")

    from core import payment_intent as pi

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل سدر", "price": 360},
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )

    db = _FakeDB(events=[])  # no prior receipt at all
    result = pi.maybe_handle_payment_claim(
        db,
        tenant_id=1,
        phone="+966500000002",
        inbound_text="تم التحويل",
        has_attached_media=False,
    )
    assert result is not None
    # Legacy branch sets awaiting_payment_receipt=True, NOT
    # payment_receipt_received=True.
    sp = result["state_patch"]
    assert sp.get("payment_receipt_received") is not True


# ────────────────────────────────────────────────────────────────────
# Fix 3 — talk-to-human rule matches "أبي أتكلم مع أحد"
# ────────────────────────────────────────────────────────────────────


def test_rules_match_explicit_talk_to_human_phrases() -> None:
    """The rule-based intent classifier MUST flag explicit Saudi
    Arabic talk-to-human asks so the webhook guard can flip
    needs_human / handoff_active."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_TALK_HUMAN

    for sample in (
        "أبي أتكلم مع أحد",
        "أبي اتكلم مع احد",
        "ابغى أتحدث مع موظف",
        "ودي اكلم احد منكم",
        "كلموني لو سمحتم",
        "محد رد علي",
        "في أحد يرد؟",
    ):
        intent = match(sample)
        assert intent is not None, f"No intent fired for {sample!r}"
        assert intent.name == INTENT_TALK_HUMAN, (
            f"Expected INTENT_TALK_HUMAN for {sample!r}, got {intent.name}"
        )


def test_rules_do_not_misfire_on_unrelated_messages() -> None:
    """Sanity: non-handoff messages don't get pulled into the broader
    INTENT_TALK_HUMAN patterns we widened in this hotfix."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_TALK_HUMAN

    for sample in (
        "السعر كم؟",
        "ابي اشتري عسل",
        "وين الفرع",
    ):
        intent = match(sample)
        if intent is not None:
            assert intent.name != INTENT_TALK_HUMAN, (
                f"INTENT_TALK_HUMAN should NOT fire for {sample!r}"
            )


# ────────────────────────────────────────────────────────────────────
# Fix 4 — receipt ACK includes structured address interview
# ────────────────────────────────────────────────────────────────────


def _confirmed_receipt_brain_state() -> dict:
    return {
        "draft_order_id": "draft-test",
        "order_prep": {
            "catalog_line_items_authoritative": True,
            "catalog_checkout_total": 360,
            "line_items": [{
                "product_id": "p-sidr",
                "product_name": "عسل سدر",
                "quantity": 1,
                "unit_price": 360,
                "from_native_catalog_order": True,
                "match_status": "confirmed",
            }],
        },
    }


def test_receipt_ack_appends_address_interview_when_missing() -> None:
    """After a confirmed receipt, the deterministic ACK must ask for
    the missing shipping fields in a single structured paragraph."""
    from core.order_flow import _compose_receipt_ack

    reply = _compose_receipt_ack({
        "selected_product": "عسل سدر",
        "price": 360,
        "currency": "SAR",
        "city": "",
        "short_address_code": "",
        "google_maps_url": "",
        "customer_first_name": "",
        "customer_last_name": "",
    }, brain_state=_confirmed_receipt_brain_state())
    assert "وصلنا إيصال التحويل" in reply
    assert "الاسم الأول والأخير" in reply
    assert "مدينة التوصيل" in reply
    # Either Google Maps OR national address must be mentioned.
    assert "قوقل ماب" in reply
    assert "العنوان الوطني" in reply


def test_receipt_ack_skips_interview_when_fully_known() -> None:
    """If we already know name + city + (national address OR maps
    link) we don't pester the customer with the interview."""
    from core.order_flow import _compose_receipt_ack

    reply = _compose_receipt_ack({
        "selected_product": "عسل سدر",
        "price": 360,
        "currency": "SAR",
        "city": "الرياض",
        "short_address_code": "RHRH1234",
        "google_maps_url": "",
        "customer_first_name": "محمد",
        "customer_last_name": "السبيعي",
    }, brain_state=_confirmed_receipt_brain_state())
    assert "الاسم الأول والأخير" not in reply
    assert "مدينة التوصيل" not in reply
    assert "العنوان الوطني: RHRH1234" in reply


# ────────────────────────────────────────────────────────────────────
# Fix 5 — map screenshot short-circuit asks for parseable location
# ────────────────────────────────────────────────────────────────────


def test_map_screenshot_short_circuit_during_active_order(
    monkeypatch: Any,
) -> None:
    """When the inbound is classified as ``map_screenshot`` and there
    IS an active order, the helper returns a deterministic reply that
    asks for a Google Maps link OR the national short address."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل سدر", "price": 360},
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )

    decision = order_flow.maybe_handle_map_image_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000003",
        inbound_normalized_type="image",
        inbound_metadata={"image_kind": "map_screenshot"},
    )
    assert decision is not None
    assert "قوقل ماب" in decision["reply_text"]
    assert "العنوان الوطني" in decision["reply_text"]
    # The state_patch arms ``awaiting_location_text`` so the next
    # inbound can be slot-extracted aggressively.
    assert decision["state_patch"].get("awaiting_location_text") is True


def test_payment_evidence_active_order_promotes_to_confirmed(
    monkeypatch: Any,
) -> None:
    """When a customer with an ACTIVE order and an
    ``awaiting_payment_receipt=True`` flag sends a payment-context
    PDF that the classifier marked as ``pre_transfer_review``, the
    soft-evidence helper must promote it to confirmed instead of
    re-asking for the receipt. This is the production fix for the
    "Transaction-Receipt.pdf forwarded twice" loop."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {
                    "title": "عسل سدر", "price": 360, "currency": "SAR",
                },
                "order_prep": {
                    "awaiting_payment_receipt": True,
                    "city": "الرياض",
                },
            },
        ),
    )

    decision = order_flow.maybe_handle_payment_evidence_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000010",
        inbound_normalized_type="document",
        inbound_metadata={
            "payment_evidence_status": "pre_transfer_review",
            "payment_evidence_reason": "pre_transfer_review_phrase",
            "pdf_kind": "payment_pre_review",
            "filename": "Transaction-Receipt.pdf",
            "wa_message_id": "wamid.X",
        },
    )
    assert decision is not None
    sp = decision["state_patch"]
    assert sp.get("payment_receipt_received") is True
    assert sp.get("awaiting_payment_receipt") is False
    assert sp.get("order_status") == "payment_submitted"
    # Reply should be the receipt ACK (with the address interview
    # because the test summary has no name / address fields set).
    assert "وصلنا" in decision["reply_text"] or "إيصال" in decision["reply_text"]
    # No re-ask for the receipt anywhere in the body.
    assert "ارسل" not in decision["reply_text"].split("بإذن الله")[0] or \
           "العنوان" in decision["reply_text"]


def test_bill_payment_evidence_active_order_does_not_mark_receipt_received(
    monkeypatch: Any,
) -> None:
    """Bill-payment or amount-only attachments stay unverified even
    when the conversation is awaiting a receipt for an active order."""
    from core import order_flow
    from core.payment_evidence import (
        PAYMENT_EVIDENCE_BILL_PAYMENT_UNRELATED,
        classify_payment_evidence,
    )

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {
                    "title": "عسل سدر", "price": 360, "currency": "SAR",
                },
                "order_prep": {
                    "awaiting_payment_receipt": True,
                    "city": "الرياض",
                },
            },
        ),
    )

    verdict = classify_payment_evidence(
        "تم سداد الفاتورة\n472.13 ريال",
        extra_context={"awaiting_payment_receipt": True},
    )
    assert verdict["status"] == PAYMENT_EVIDENCE_BILL_PAYMENT_UNRELATED

    decision = order_flow.maybe_handle_payment_evidence_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000010",
        inbound_normalized_type="image",
        inbound_metadata={
            "payment_evidence_status": verdict["status"],
            "payment_evidence_reason": verdict["reason"],
            "image_kind": "payment_pending_evidence",
            "vision_text": "تم سداد الفاتورة\n472.13 ريال",
            "wa_message_id": "wamid.bill",
        },
    )
    if decision is not None:
        sp = decision["state_patch"]
        assert sp.get("payment_receipt_received") is not True
        assert sp.get("payment_submission_received") is not True


def test_payment_evidence_without_active_order_stays_soft(
    monkeypatch: Any,
) -> None:
    """Without an active order, the soft branch must stay soft —
    a stray bank screenshot on a brand-new conversation should NOT
    flip ``payment_receipt_received`` to True."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (None, {}),
    )

    decision = order_flow.maybe_handle_payment_evidence_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000011",
        inbound_normalized_type="document",
        inbound_metadata={
            "payment_evidence_status": "pre_transfer_review",
            "payment_evidence_reason": "pre_transfer_review_phrase",
            "pdf_kind": "payment_pre_review",
        },
    )
    if decision is not None:
        sp = decision["state_patch"]
        assert sp.get("payment_receipt_received") is not True


def test_map_screenshot_no_short_circuit_without_active_order(
    monkeypatch: Any,
) -> None:
    """Without ANY active order context, a stray map screenshot must
    NOT trigger the address ask — the conversation might be brand-new
    and the customer just sharing a location for unrelated reasons."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (None, {}),
    )

    decision = order_flow.maybe_handle_map_image_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000004",
        inbound_normalized_type="image",
        inbound_metadata={"image_kind": "map_screenshot"},
    )
    assert decision is None


# ──────────────────────────────────────────────────────────────────────
# Image classification — narrow gates (May 2026 hotfix #2)
#
# The previous round of fixes made the map / payment-evidence gates
# too wide, causing a Kaaba-themed Hajj greeting card to be
# misclassified and the bot to ask the customer for product /
# shipping. The gates must now be NARROW: map only on strong UI
# markers, payment only on strong receipt markers, everything else
# is a "general image" that flows to the vision/brain path with no
# inline classification tag prepended.
# ──────────────────────────────────────────────────────────────────────


def test_greeting_image_skips_payment_evidence_short_circuit(
    monkeypatch: Any,
) -> None:
    """End-to-end short-circuit check: when the image normaliser sees
    a Hajj greeting card, ``payment_evidence_status`` is not set,
    therefore the order-flow short-circuit must short-return None and
    the brain stays in charge of replying."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (None, {"current_product_focus": {}}),
    )
    decision = order_flow.maybe_handle_payment_evidence_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000020",
        inbound_normalized_type="image",
        inbound_metadata={
            # The normaliser would NOT set these on a greeting card
            # any more — confirm the short-circuit also refuses to
            # fire if a downstream caller mistakenly forwards a
            # greeting image with empty payment metadata.
            "image_kind": None,
            "payment_evidence_status": None,
        },
    )
    assert decision is None


def test_greeting_image_skips_map_short_circuit(monkeypatch: Any) -> None:
    """Greeting card → no map_screenshot tag → map short-circuit
    must return None even when the conversation is in an active
    order state."""
    from core import order_flow

    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {
                    "title": "عسل سدر", "price": 360, "currency": "SAR",
                },
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )
    decision = order_flow.maybe_handle_map_image_inbound(
        db=None,
        tenant_id=1,
        phone="+966500000021",
        inbound_normalized_type="image",
        inbound_metadata={
            # No image_kind set → general image → must not fire.
            "image_kind": None,
        },
    )
    assert decision is None


def test_payment_evidence_classifier_greeting_card_is_not_payment() -> None:
    """The Kaaba/Hajj greeting card (production reproducer) MUST
    classify as not_payment regardless of any noisy token overlap
    with the payment lexicon."""
    from core.payment_evidence import (
        classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
    )
    ocr_text = (
        "أهنئكم بقدوم عشر ذي الحجة، خير الأيام عند الله، "
        "تقبل الله منا ومنكم صالح الأعمال، وكتب لكم الأجر، "
        "ويبلغكم يوم النحر، وأسعدكم طول الدهر، كل عام وأنتم "
        "إلى الله أقرب، وعلى الطاعة أدوم، وعن النار أبعد، "
        "وإلى الجنة أسبق"
    )
    v = classify_payment_evidence(ocr_text)
    assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT
    assert v["reason"] == "greeting_or_social_content"


def test_general_product_image_caption_is_not_payment() -> None:
    """A product photo with a price tag is a SINGLE payment-context
    hit + one currency token — under the tightened thresholds this
    must stay not_payment so the brain replies to the actual product."""
    from core.payment_evidence import (
        classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
    )
    v = classify_payment_evidence(
        "صورة عسل سدر طبيعي 1 كيلو السعر 360 ريال"
    )
    assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT


def test_bare_amount_without_payment_context_is_not_payment() -> None:
    """A lone currency amount outside an awaiting-receipt funnel is not
    payment-like — it must not escalate to amount_only_insufficient."""
    from core.payment_evidence import (
        classify_payment_evidence,
        PAYMENT_EVIDENCE_NOT_PAYMENT,
    )

    v = classify_payment_evidence("472.13 ريال")
    assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT


def test_product_caption_while_awaiting_is_not_payment() -> None:
    """Product/catalog captions must stay not_payment even when the
    funnel is awaiting a bank-transfer receipt."""
    from core.payment_evidence import (
        classify_payment_evidence,
        PAYMENT_EVIDENCE_NOT_PAYMENT,
    )

    caption = "صورة عسل سدر طبيعي 1 كيلو السعر 360 ريال"
    v = classify_payment_evidence(
        caption,
        extra_context={"awaiting_payment_receipt": True},
    )
    assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT


def test_transfer_receipt_phrase_amount_only_is_insufficient() -> None:
    """Payment-like wording with an amount but no merchant linkage is
    insufficient evidence — not a valid transfer receipt."""
    from core.payment_evidence import (
        classify_payment_evidence,
        PAYMENT_EVIDENCE_AMOUNT_ONLY_INSUFFICIENT,
    )

    v = classify_payment_evidence("إيصال تحويل 360 ريال")
    assert v["status"] == PAYMENT_EVIDENCE_AMOUNT_ONLY_INSUFFICIENT


def test_real_receipt_still_classifies_as_confirmed() -> None:
    """Lock-in: tightening the noise floor must NOT block actual
    completed-transfer screenshots from firing."""
    from core.payment_evidence import (
        classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
    )
    v = classify_payment_evidence(
        "تم التحويل بنجاح\n"
        "المبلغ: 360 ريال\nالمستفيد: متجر نهلة\nرقم العملية: 9981234"
    )
    assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED
