"""
tests/test_conversation_flow_fixes.py
─────────────────────────────────────
Regression tests for the conversation-flow fixes:

  Fix A — phone-format-tolerant state lookup (no more "lost state every turn")
  Fix B — committed sales stages (deciding/ordering/checkout) cannot be
          downgraded by greeting/search side-effects
  Fix C — DecisionEngine refuses ACTION_GREET while in a committed stage
  Fix D — DecisionEngine routes INTENT_GREETING / INTENT_GENERAL
          back into the order flow when the customer is mid-checkout
  Fix E — Classifier always runs slot extraction during the order flow
  Fix F — Country-aware address requirements (SA: short code OR maps URL,
          INTL: country + free-form address line)
  Fix G — Deterministic Arabic ordering-slot extractor recognises
          plain names, Saudi cities, Maps URLs and short codes

These all map directly to the live-WhatsApp symptoms reported by the
operator (greeting repeating, state loss, free-text data ignored,
SA-customers asked for too much address detail).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Fix A: phone-format-tolerant Customer lookup ──────────────────────────────


class _FakeCustomer:
    def __init__(self, *, id: int, tenant_id: int, phone: str, normalized_phone: str):
        self.id = id
        self.tenant_id = tenant_id
        self.phone = phone
        self.normalized_phone = normalized_phone


class _FakeQuery:
    """Tiny stand-in for SQLAlchemy's chained query API."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._filters: list = []

    def filter(self, *conditions):
        # Each condition is a SQLAlchemy BinaryExpression; we won't try to
        # interpret it. Instead we let the test build a custom matcher by
        # remembering all calls and applying them to the in-memory rows.
        self._filters.append(conditions)
        return self

    def order_by(self, *_):
        return self

    def first(self):
        # Conditions look like: Customer.tenant_id == 7
        # We unwrap each into (column_name, value) by inspecting .left/.right.
        def _matches(row, predicate) -> bool:
            try:
                column = predicate.left.key
                value  = predicate.right.value
            except AttributeError:
                return True
            return getattr(row, column, None) == value

        results = list(self._rows)
        for group in self._filters:
            results = [r for r in results if all(_matches(r, p) for p in group)]
        return results[0] if results else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def test_state_store_finds_customer_when_webhook_phone_lacks_plus(monkeypatch):
    """
    The webhook hands us "966555555555" but the row was stored as
    "+966555555555". The old lookup missed → fresh state every turn.
    """
    from database import models  # noqa: F401 — ensure the symbol exists for filter inspection
    from modules.ai.brain.state import store as state_store_module

    customer = _FakeCustomer(
        id=42,
        tenant_id=7,
        phone="+966555555555",
        normalized_phone="+966555555555",
    )

    # Patch the local Customer import inside _find_customer so our fake
    # rows are actually queried.
    monkeypatch.setattr(state_store_module, "logger", MagicMock())
    fake_db = _FakeSession([customer])

    # The webhook-style identifier (digits only, no plus)
    found, matched_value, matched_column, tried = state_store_module._find_customer(
        fake_db, tenant_id=7, phone="966555555555"
    )
    assert found is customer
    assert matched_column in ("normalized_phone", "phone")
    assert "966555555555" in tried or "+966555555555" in tried

    # Direct E.164 also resolves
    found2, *_ = state_store_module._find_customer(
        fake_db, tenant_id=7, phone="+966555555555"
    )
    assert found2 is customer

    # Local Saudi format also resolves to the same row through normalize.
    # We don't strictly require this, but the helper should not crash.
    _ = state_store_module._find_customer(fake_db, tenant_id=7, phone="0555555555")


def test_state_save_calls_flag_modified_for_jsonb_persistence(monkeypatch):
    """
    Production logs (2026-04-20) showed turn_save reporting result=ok but
    the next turn_load reading state_source=fresh / no_brain_state_in_metadata.
    Root cause: Conversation.extra_metadata is JSONB without
    MutableDict.as_mutable(), so dict reassignment can be silently dropped
    when an earlier autoflush in the same request snapshotted the column.

    The save MUST call sqlalchemy.orm.attributes.flag_modified() to force
    the column dirty regardless of snapshot history. This test asserts
    that save actually invokes flag_modified for the extra_metadata column.
    """
    from modules.ai.brain.state import store as state_store_module
    from modules.ai.brain.types import MerchantConversationState

    # Capture flag_modified invocations
    calls = []
    real_flag = state_store_module.__dict__.get("flag_modified")  # may not exist yet

    import sqlalchemy.orm.attributes as sa_attrs

    def _spy(instance, key):
        calls.append((type(instance).__name__, key))

    monkeypatch.setattr(sa_attrs, "flag_modified", _spy, raising=True)

    # Build a fake conversation row + customer + session that records writes
    class _FakeConv:
        def __init__(self):
            self.id = 9
            self.tenant_id = 1
            self.customer_id = 66
            self.extra_metadata = {"customer_phone": "+966555906901"}

    class _FakeCustomer2:
        id = 66
        tenant_id = 1
        phone = "+966555906901"
        normalized_phone = "+966555906901"

    fake_conv = _FakeConv()
    fake_customer = _FakeCustomer2()

    class _FakeSession2:
        def __init__(self):
            self.commits = 0

        def query(self, model):
            name = getattr(model, "__name__", "")
            rows = [fake_customer] if name == "Customer" else [fake_conv]
            return _FakeQuery(rows)

        def commit(self):
            self.commits += 1

        def rollback(self):
            pass

        def refresh(self, _row, attribute_names=None):
            # Simulate the post-commit verify step succeeding.
            return None

    db = _FakeSession2()
    store = state_store_module.DefaultStateStore()

    state = MerchantConversationState()
    state.stage = "ordering"
    state.turn = 2
    state.greeted = True

    store.save(db, tenant_id=1, customer_phone="+966555906901", state=state)

    # Verify the brain_state was written to the dict
    assert "brain_state" in (fake_conv.extra_metadata or {})
    # Verify flag_modified was called for extra_metadata
    assert ("_FakeConv", "extra_metadata") in calls, (
        f"flag_modified was not called; calls={calls!r}"
    )


def test_mask_phone_keeps_prefix_and_last_four():
    from modules.ai.brain.state.store import _mask_phone

    masked = _mask_phone("+966555123456")
    assert masked.startswith("+966")
    assert masked.endswith("3456")
    assert "X" in masked
    assert "5551" not in masked        # middle digits must be hidden

    masked2 = _mask_phone("966555123456")
    assert masked2.endswith("3456")
    assert "X" in masked2

    assert _mask_phone("") == "<empty>"
    assert _mask_phone("12345") != "12345"


# ── Fix B: transition refuses to downgrade committed sales stages ─────────────


def _make_state(stage: str, *, greeted: bool = True, product=None):
    from modules.ai.brain.types import MerchantConversationState
    state = MerchantConversationState()
    state.stage = stage
    state.greeted = greeted
    state.current_product_focus = product
    return state


def test_transition_does_not_downgrade_ordering_to_discovery_on_greet():
    from modules.ai.brain.decision.actions import ACTION_GREET
    from modules.ai.brain.state.store import DefaultStateStore
    from modules.ai.brain.state.stages import STAGE_ORDERING
    from modules.ai.brain.types import Decision, INTENT_GREETING, Intent

    store = DefaultStateStore()
    state = _make_state(STAGE_ORDERING, product={"id": 1, "title": "فستان"})

    new_state = store.transition(
        state=state,
        intent=Intent(name=INTENT_GREETING, confidence=0.9),
        decision=Decision(action=ACTION_GREET, reason="rules-only greeting"),
    )

    # Greeted flag is updated, but the funnel stage MUST be preserved.
    assert new_state.greeted is True
    assert new_state.stage == STAGE_ORDERING


def test_transition_does_not_downgrade_checkout_on_search():
    from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS
    from modules.ai.brain.state.store import DefaultStateStore
    from modules.ai.brain.state.stages import STAGE_CHECKOUT
    from modules.ai.brain.types import Decision, INTENT_ASK_PRODUCT, Intent

    store = DefaultStateStore()
    state = _make_state(STAGE_CHECKOUT, product={"id": 1, "title": "فستان"})
    state.checkout_url = "https://pay.example.com/abc"

    new_state = store.transition(
        state=state,
        intent=Intent(name=INTENT_ASK_PRODUCT, confidence=0.7),
        decision=Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "حذاء"}),
    )

    assert new_state.stage == STAGE_CHECKOUT
    assert new_state.checkout_url == "https://pay.example.com/abc"


def test_transition_still_promotes_to_ordering_on_propose_draft():
    from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
    from modules.ai.brain.state.store import DefaultStateStore
    from modules.ai.brain.state.stages import STAGE_DISCOVERY, STAGE_ORDERING
    from modules.ai.brain.types import Decision, INTENT_START_ORDER, Intent

    store = DefaultStateStore()
    state = _make_state(STAGE_DISCOVERY, product=None)

    new_state = store.transition(
        state=state,
        intent=Intent(name=INTENT_START_ORDER, confidence=0.9),
        decision=Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={"product": {"id": 99, "title": "فستان"}},
        ),
    )

    assert new_state.stage == STAGE_ORDERING
    assert new_state.current_product_focus == {"id": 99, "title": "فستان"}


# ── Fix C + D: greeting lock + continuation ───────────────────────────────────


def _facts():
    from modules.ai.brain.types import CommerceFacts
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
        has_coupons=False,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
    )


def _ctx(state, intent_name: str, slots: dict | None = None):
    from modules.ai.brain.types import BrainContext, Intent
    intent = Intent(name=intent_name, confidence=0.9, slots=slots or {})
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        customer_id=42,
        message=intent.raw_message or "test",
        history=[],
        profile={},
        intent=intent,
        state=state,
        facts=_facts(),
    )


def test_decision_engine_refuses_to_greet_during_ordering():
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_PROPOSE_DRAFT_ORDER
    from modules.ai.brain.state.stages import STAGE_ORDERING
    from modules.ai.brain.types import INTENT_GREETING

    engine = DefaultDecisionEngine()
    state = _make_state(STAGE_ORDERING, product={"id": 1, "title": "فستان", "price": 189})

    decision = engine.decide(_ctx(state, INTENT_GREETING))

    # MUST NOT greet — should keep collecting checkout details instead.
    assert decision.action != ACTION_GREET
    assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


def test_decision_engine_refuses_to_greet_during_checkout():
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.actions import ACTION_GREET
    from modules.ai.brain.state.stages import STAGE_CHECKOUT
    from modules.ai.brain.types import INTENT_GREETING

    engine = DefaultDecisionEngine()
    state = _make_state(STAGE_CHECKOUT, product={"id": 1, "title": "فستان"})
    state.checkout_url = "https://pay.example.com/abc"

    decision = engine.decide(_ctx(state, INTENT_GREETING))
    assert decision.action != ACTION_GREET


def test_decision_engine_greets_on_first_turn_in_discovery():
    """Sanity check — greeting still works for genuine first-time hellos."""
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.actions import ACTION_GREET
    from modules.ai.brain.state.stages import STAGE_DISCOVERY
    from modules.ai.brain.types import INTENT_GREETING

    engine = DefaultDecisionEngine()
    state = _make_state(STAGE_DISCOVERY, greeted=False, product=None)
    decision = engine.decide(_ctx(state, INTENT_GREETING))
    assert decision.action == ACTION_GREET


def test_decision_engine_does_not_re_greet_already_greeted_customer():
    """
    Once we have greeted, INTENT_GENERAL should NOT trigger another
    greeting template — that's the visible "ترحيب يتكرر" symptom.
    """
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.actions import ACTION_GREET
    from modules.ai.brain.state.stages import STAGE_DISCOVERY
    from modules.ai.brain.types import INTENT_GENERAL

    engine = DefaultDecisionEngine()
    state = _make_state(STAGE_DISCOVERY, greeted=True, product=None)
    decision = engine.decide(_ctx(state, INTENT_GENERAL))
    assert decision.action != ACTION_GREET


def test_decision_engine_treats_name_message_as_continuation():
    """
    During ordering, a free-text message that the LLM/heuristic reads as
    a customer name (slot key `customer_name`) MUST be routed back to the
    DraftOrderHandler, not to the greeting template.
    """
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
    from modules.ai.brain.state.stages import STAGE_ORDERING
    from modules.ai.brain.types import INTENT_GENERAL

    engine = DefaultDecisionEngine()
    state = _make_state(STAGE_ORDERING, product={"id": 1, "title": "فستان"})
    decision = engine.decide(
        _ctx(state, INTENT_GENERAL, slots={"customer_name": "تركي الحارثي"}),
    )

    assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


# ── Fix E: classifier runs slot extraction during the order flow ──────────────


def test_classifier_runs_slot_extraction_during_ordering(monkeypatch):
    """
    A high-confidence rules match (e.g. INTENT_GREETING for "هلا") should
    NOT short-circuit slot extraction while the customer is in the order
    flow — otherwise we lose the customer_name / city slot the message
    might also carry.
    """
    from modules.ai.brain.intent.classifier import DefaultIntentClassifier
    from modules.ai.brain.state.stages import STAGE_ORDERING

    state = _make_state(STAGE_ORDERING, product={"id": 1, "title": "فستان"})

    classifier = DefaultIntentClassifier()
    intent = asyncio.run(classifier.classify("تركي الحارثي\nالطائف", history=[], state=state))

    assert intent.slots.get("city") == "الطائف"
    assert intent.slots.get("customer_first_name") == "تركي"
    assert intent.slots.get("customer_last_name") == "الحارثي"


def test_classifier_keeps_rules_only_when_not_in_order_flow():
    """Outside the order flow, the rules-only fast path is preserved."""
    from modules.ai.brain.intent.classifier import DefaultIntentClassifier
    from modules.ai.brain.state.stages import STAGE_DISCOVERY
    from modules.ai.brain.types import INTENT_GREETING

    classifier = DefaultIntentClassifier()
    state = _make_state(STAGE_DISCOVERY, greeted=False, product=None)
    intent = asyncio.run(classifier.classify("السلام عليكم", history=[], state=state))

    assert intent.name == INTENT_GREETING
    assert intent.extraction_method == "rules"


# ── Fix G: deterministic ordering-slot extractor ──────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("تركي الحارثي", {"customer_first_name": "تركي", "customer_last_name": "الحارثي"}),
        ("الطائف", {"city": "الطائف"}),
        ("الرياض", {"city": "الرياض"}),
        ("جده", {"city": "جدة"}),
        (
            "تركي الحارثي\nالطائف",
            {
                "customer_first_name": "تركي",
                "customer_last_name": "الحارثي",
                "city": "الطائف",
            },
        ),
        (
            "https://maps.app.goo.gl/abc123",
            {"google_maps_url": "https://maps.app.goo.gl/abc123"},
        ),
    ],
)
def test_ordering_extractor_finds_expected_slots(message, expected):
    from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots

    slots = extract_ordering_slots(message)
    for key, value in expected.items():
        assert slots.get(key) == value, f"missing {key}={value!r} in {slots!r}"


@pytest.mark.parametrize(
    "noise",
    ["نعم", "شكراً", "اوكي", "ابغى افضل سعر", "وش الحال", "12345"],
)
def test_ordering_extractor_does_not_invent_names_from_noise(noise):
    from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots

    slots = extract_ordering_slots(noise)
    assert "customer_first_name" not in slots
    assert "customer_last_name" not in slots


# ── Fix F: country-aware address requirements ─────────────────────────────────


def _prep(**overrides):
    from modules.ai.brain.types import OrderPreparationState
    p = OrderPreparationState()
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def test_sa_flow_accepts_short_code_only():
    from modules.ai.brain.execution.orders import _missing_checkout_fields

    prep = _prep(
        customer_first_name="تركي",
        customer_last_name="الحارثي",
        city="الطائف",
        short_address_code="RIYD2342",
    )
    assert _missing_checkout_fields(prep, is_sa=True) == []


def test_sa_flow_accepts_maps_url_only():
    from modules.ai.brain.execution.orders import _missing_checkout_fields

    prep = _prep(
        customer_first_name="تركي",
        customer_last_name="الحارثي",
        city="الطائف",
        google_maps_url="https://maps.app.goo.gl/xyz",
    )
    assert _missing_checkout_fields(prep, is_sa=True) == []


def test_sa_flow_does_not_demand_district_or_postal():
    """
    Regression: the SA customer shouldn't be asked for street/district/postal
    when they already provided a Maps link or a national short address.
    """
    from modules.ai.brain.execution.orders import _missing_checkout_fields

    prep = _prep(
        customer_first_name="تركي",
        customer_last_name="الحارثي",
        city="الطائف",
        short_address_code="RIYD2342",
    )
    missing = _missing_checkout_fields(prep, is_sa=True)
    for forbidden in ("street", "district", "postal_code", "building_number"):
        assert forbidden not in missing


def test_intl_flow_demands_country_and_address_line():
    from modules.ai.brain.execution.orders import _missing_checkout_fields

    prep = _prep(
        customer_first_name="John",
        customer_last_name="Doe",
        city="Dubai",
    )
    missing = _missing_checkout_fields(prep, is_sa=False)
    assert "country" in missing
    assert "address_line" in missing


def test_intl_flow_complete_with_address_line_and_country():
    from modules.ai.brain.execution.orders import _missing_checkout_fields

    prep = _prep(
        customer_first_name="John",
        customer_last_name="Doe",
        country="UAE",
        city="Dubai",
        address_line="Burj Khalifa, Downtown, Apt 1234",
    )
    assert _missing_checkout_fields(prep, is_sa=False) == []


@pytest.mark.parametrize(
    "phone,expected",
    [
        ("+966555555555", True),
        ("966555555555", True),
        ("0555555555", True),
        ("+971501234567", False),  # UAE
        ("+15551234567", False),   # US
        ("",            True),     # unknown → SA-first default
    ],
)
def test_is_saudi_customer_classification(phone, expected):
    from modules.ai.brain.execution.orders import _is_saudi_customer
    assert _is_saudi_customer(phone) is expected


def test_intl_question_text_is_english_for_intl_flow():
    from modules.ai.brain.execution.orders import _checkout_question

    q_country = _checkout_question("country", is_sa=False)
    q_address = _checkout_question("address_line", is_sa=False)

    assert "country" in q_country.lower()
    assert "address" in q_address.lower()


# ── Fix H: StateManager.save MUST preserve brain_state ────────────────────────
# Regression for the "verified=True at save, but state_source=fresh on next
# turn" production bug. ``StateManager.save`` (called once per inbound webhook
# from whatsapp_webhook._handle_merchant_message before brain.process) used to
# overwrite the entire ``Conversation.extra_metadata`` JSONB column, silently
# wiping the ``brain_state`` key written by the MerchantBrain on the previous
# turn — which caused every customer message to look like the first turn and
# triggered an endless greeting loop.

class _FakeConvWithMeta:
    def __init__(self, conv_id: int, tenant_id: int, extra_metadata: dict):
        self.id = conv_id
        self.tenant_id = tenant_id
        self.status = "active"
        self.extra_metadata = extra_metadata


class _FakeJSONBKey:
    def __init__(self, key: str):
        self._key = key
        self.astext = self  # so ``.astext == phone`` works in the filter clause

    def __eq__(self, other):  # noqa: D401 - chainable comparison sentinel
        return ("phone_eq", other)


class _FakeMetaColumn:
    def __getitem__(self, key):
        return _FakeJSONBKey(key)


class _FakeConversationModel:
    tenant_id = "tenant_id"
    extra_metadata = _FakeMetaColumn()

    @classmethod
    def __call__(cls, *args, **kwargs):
        return _FakeConvWithMeta(
            conv_id=kwargs.get("id", 999),
            tenant_id=kwargs.get("tenant_id", 0),
            extra_metadata=kwargs.get("extra_metadata", {}),
        )


class _FakeMetaQuery:
    def __init__(self, conv):
        self._conv = conv

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._conv


class _FakeMetaSession:
    def __init__(self, conv):
        self._conv = conv
        self.committed = False

    def query(self, *args, **kwargs):
        return _FakeMetaQuery(self._conv)

    def add(self, _obj):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        return None


def test_state_manager_save_preserves_brain_state(monkeypatch):
    """
    StateManager.save MUST merge with existing extra_metadata so that
    ``brain_state`` (owned by the MerchantBrain) survives across turns.
    """
    from core import conversation_engine as ce

    existing_brain_state = {
        "stage": "ordering",
        "turn": 2,
        "greeted": True,
        "current_product_focus": {"id": 42, "name": "فستان", "price": 189.0},
        "order_prep": {"customer_first_name": "تركي", "city": "الطائف"},
    }
    conv = _FakeConvWithMeta(
        conv_id=9,
        tenant_id=1,
        extra_metadata={
            "phone": "+966555906901",
            "stage": "active",
            "turn": 1,
            "brain_state": existing_brain_state,
            "customer_phone": "+966555906901",
        },
    )

    fake_session = _FakeMetaSession(conv)
    monkeypatch.setattr(ce, "Conversation", _FakeConversationModel, raising=False)

    state = ce.ConversationState(phone="+966555906901")
    state.tenant_id = 1
    state.turn = 2
    state.stage = "active"

    ce.StateManager.save(fake_session, state, tenant_id=1)

    assert fake_session.committed is True, "StateManager.save must commit"
    # Owned keys updated:
    assert conv.extra_metadata.get("turn") == 2
    assert conv.extra_metadata.get("stage") == "active"
    assert conv.extra_metadata.get("phone") == "+966555906901"
    # Unowned keys preserved — this is the regression guard:
    assert conv.extra_metadata.get("brain_state") == existing_brain_state, (
        "brain_state must NOT be wiped by StateManager.save — "
        "this is the production bug that caused endless greeting loops."
    )
    assert conv.extra_metadata.get("customer_phone") == "+966555906901"


def test_state_manager_save_does_not_drop_unrelated_keys(monkeypatch):
    """
    Defensive: any future key written to extra_metadata by another
    subsystem must also survive a StateManager.save call.
    """
    from core import conversation_engine as ce

    conv = _FakeConvWithMeta(
        conv_id=10,
        tenant_id=1,
        extra_metadata={
            "phone": "+966500000000",
            "brain_state": {"stage": "deciding"},
            "ai_handoff_token": "abc123",
            "experimental_flag": {"variant": "B"},
        },
    )
    fake_session = _FakeMetaSession(conv)
    monkeypatch.setattr(ce, "Conversation", _FakeConversationModel, raising=False)

    state = ce.ConversationState(phone="+966500000000")
    state.tenant_id = 1
    state.turn = 5
    state.stage = "active"
    ce.StateManager.save(fake_session, state, tenant_id=1)

    assert conv.extra_metadata["brain_state"] == {"stage": "deciding"}
    assert conv.extra_metadata["ai_handoff_token"] == "abc123"
    assert conv.extra_metadata["experimental_flag"] == {"variant": "B"}
    assert conv.extra_metadata["turn"] == 5
