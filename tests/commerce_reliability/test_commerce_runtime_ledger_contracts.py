"""Pure contracts of the effect and delivery ledgers (no database).

Legal transition tables, the business-key derivation, the payload hash, the
WhatsApp-shaped send-response classifier, the terminal derivations and the
human-transfer evidence rule. Runs in the ordinary root suite.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc

NOW = _dt.datetime(2026, 9, 19, 12, 0, tzinfo=_dt.timezone.utc)


def _effect(**overrides) -> lc.EffectRecord:
    base = dict(
        effect_id=1, tenant_id=1, namespace="live", conversation_id=1, turn_id=1,
        action_type=lc.ActionType.HANDOFF_REQUEST.value, idempotency_key="handoff_request:conv:1",
        payload={"reason": "customer asked"}, payload_hash=lc.payload_hash({"reason": "customer asked"}),
        status=lc.EffectStatus.CONFIRMED.value, attempt_count=1, reserved_by="worker-a", reserved_fence=1,
        reserved_epoch=0, confirmed_result={"request_id": "hr-1"}, created_at=NOW, updated_at=NOW,
    )
    base.update(overrides)
    return lc.EffectRecord(**base)


# ── Transition tables ────────────────────────────────────────────────────────


def test_effect_transitions_are_closed_and_honest() -> None:
    allowed = {(s.value, t.value) for s, targets in lc.EFFECT_TRANSITIONS.items() for t in targets}
    assert allowed == {
        ("reserved", "dispatching"),
        ("dispatching", "confirmed"), ("dispatching", "rejected"), ("dispatching", "unknown"),
        ("unknown", "confirmed"), ("unknown", "rejected"),
    }
    # Confirmed and rejected are final; unknown never goes back to dispatching or reserved.
    for final in ("confirmed", "rejected"):
        assert all(not lc.effect_transition_allowed(final, target) for target in lc.EffectStatus)
    assert not lc.effect_transition_allowed("unknown", "dispatching")
    assert not lc.effect_transition_allowed("unknown", "reserved")
    assert not lc.effect_transition_allowed("reserved", "confirmed")   # nothing confirms without a dispatch


def test_delivery_transitions_permit_no_fallback_after_accepted_or_unknown() -> None:
    assert lc.delivery_transition_allowed("pending", "accepted")
    assert lc.delivery_transition_allowed("rejected", "pending")        # only through the bounded recovery
    assert lc.delivery_transition_allowed("unknown", "accepted")        # late evidence for the same attempt
    assert not lc.delivery_transition_allowed("unknown", "pending")     # never a new attempt
    assert not lc.delivery_transition_allowed("accepted", "pending")
    assert not lc.delivery_transition_allowed("accepted", "rejected")
    assert lc.MAX_DELIVERY_ATTEMPTS == 2 and lc.MAX_EFFECT_ATTEMPTS == 1


# ── Business identity ────────────────────────────────────────────────────────


def test_business_key_derives_from_business_identifiers_not_tool_call_ids() -> None:
    key = lc.derive_business_key(lc.ActionType.ORDER_CANCEL, "tenant-7", "SO-1001")
    assert key == "order_cancel:k1:tenant-7:SO-1001"
    assert lc.derive_business_key("order_cancel", "tenant-7", "SO-1001") == key   # retry reproduces it
    assert lc.derive_business_key(lc.ActionType.ORDER_CANCEL, "tenant-7", 1001) == "order_cancel:k1:tenant-7:1001"
    long_key = lc.derive_business_key(lc.ActionType.COUPON_APPLY, "x" * 120, "y" * 120)
    assert long_key.startswith("coupon_apply:k1#sha256:") and len(long_key) <= lc.MAX_IDEMPOTENCY_KEY_LENGTH
    with pytest.raises(c.ValidationError):
        lc.derive_business_key("not_an_action", "a")
    with pytest.raises(c.ValidationError):
        lc.derive_business_key(lc.ActionType.ORDER_CANCEL)
    with pytest.raises(c.ValidationError):
        lc.derive_business_key(lc.ActionType.ORDER_CANCEL, "has space")
    with pytest.raises(c.ValidationError):
        lc.derive_business_key(lc.ActionType.ORDER_CANCEL, None)


def test_business_key_encoding_is_unambiguous() -> None:
    derive = lc.derive_business_key
    assert lc.KEY_ENCODING_VERSION == "k1"
    # A delimiter inside a component is escaped, so the split point is part of the identity.
    assert derive("order_cancel", "a:b", "c") == "order_cancel:k1:a%3Ab:c"
    assert derive("order_cancel", "a", "b:c") == "order_cancel:k1:a:b%3Ac"
    assert derive("order_cancel", "a:b", "c") != derive("order_cancel", "a", "b:c")
    assert derive("order_cancel", "a:b:c") not in {derive("order_cancel", "a:b", "c"), derive("order_cancel", "a", "b:c")}
    # Escaped text and the raw character stay distinct; the escape character is itself escaped.
    assert derive("order_cancel", "a%3Ab") == "order_cancel:k1:a%253Ab" != derive("order_cancel", "a:b")
    assert derive("order_cancel", "50%") == "order_cancel:k1:50%25"
    # Different component counts never coincide.
    assert derive("order_cancel", "ab") != derive("order_cancel", "a", "b")
    assert derive("order_cancel", "a", "b") != derive("order_cancel", "a", "b", "c")
    assert derive("order_cancel", "a", "b") != derive("order_cancel", "a", "b", 0)
    # Empty components are refused explicitly rather than silently collapsed.
    with pytest.raises(c.ValidationError):
        derive("order_cancel", "")
    with pytest.raises(c.ValidationError):
        derive("order_cancel", "a", "")
    # Unicode components are carried as they are (printable, no whitespace).
    assert derive("order_cancel", "طلب-١٢٣", "متجر") == "order_cancel:k1:طلب-١٢٣:متجر"
    with pytest.raises(c.ValidationError):
        derive("order_cancel", "طلب ١٢٣")
    # Long keys hash the unambiguous encoding, deterministically, and stay within the bound.
    long_a = derive("coupon_apply", "x" * 100, "y" * 100)
    long_b = derive("coupon_apply", "x" * 100 + ":" + "y" * 20)      # one component, still over the bound
    with pytest.raises(c.ValidationError):
        derive("coupon_apply", "x" * (lc.MAX_IDEMPOTENCY_KEY_LENGTH + 1))   # a component is bounded too
    assert long_a == derive("coupon_apply", "x" * 100, "y" * 100)
    assert long_a != long_b
    assert long_a.startswith("coupon_apply:k1#sha256:") and len(long_a) <= lc.MAX_IDEMPOTENCY_KEY_LENGTH
    assert lc.validate_idempotency_key(long_a) == long_a
    # The plain form always has ':' after the version, so it can never look like the hashed form.
    assert derive("order_cancel", "#sha256", "abc") == "order_cancel:k1:#sha256:abc"
    # Identical components always reproduce the identical key.
    assert derive("payment_link_create", "SO-1", 2, "SAR") == derive("payment_link_create", "SO-1", 2, "SAR")


def test_payload_hash_is_canonical_and_intents_are_validated() -> None:
    assert lc.payload_hash({"b": 1, "a": [1, 2]}) == lc.payload_hash({"a": [1, 2], "b": 1})
    assert lc.payload_hash({"a": 1}) != lc.payload_hash({"a": 2})
    assert len(lc.payload_hash({})) == lc.PAYLOAD_HASH_LENGTH
    intent = lc.validate_effect_intent(lc.EffectIntent("order_cancel", "order_cancel:t:1", {"order": "SO-1"}))
    assert intent.action_type == "order_cancel" and intent.payload == {"order": "SO-1"}
    with pytest.raises(c.ValidationError):
        lc.validate_effect_intent(lc.EffectIntent("order_cancel", "", {}))
    with pytest.raises(c.ValidationError):
        lc.validate_effect_intent(lc.EffectIntent("tool_call", "k", {}))
    with pytest.raises(c.ValidationError):
        lc.validate_effect_intent(lc.EffectIntent("order_cancel", "k", {"blob": "x" * (c.MAX_PAYLOAD_BYTES + 1)}))
    with pytest.raises(c.ValidationError):
        lc.validate_effect_intent({"action_type": "order_cancel"})   # not an EffectIntent
    assert lc.validate_delivery_intent(lc.DeliveryIntent("rich", {"card": 1})).kind == "rich"
    with pytest.raises(c.ValidationError):
        lc.validate_delivery_intent(lc.DeliveryIntent("voice", {}))
    with pytest.raises(c.ValidationError):
        lc.validate_evidence({"blob": "x" * (lc.MAX_EVIDENCE_BYTES + 1)})


# ── WhatsApp-shaped classifier ───────────────────────────────────────────────


@pytest.mark.parametrize("response, expected", [
    (lc.SendResponse(200, {"messages": [{"id": "wamid.HBgL"}]}), ("accepted", "wamid.HBgL")),
    (lc.SendResponse(201, {"messages": [{"id": "wamid.HBgL"}]}), ("accepted", "wamid.HBgL")),
    (lc.SendResponse(200, {}), ("unknown", None)),                        # success without an id is not proof
    (lc.SendResponse(200, {"messages": []}), ("unknown", None)),
    (lc.SendResponse(200, {"messages": [{"id": ""}]}), ("unknown", None)),
    (lc.SendResponse(200, {"messages": [{"id": "has space"}]}), ("unknown", None)),
    (lc.SendResponse(200, {"messages": "wamid.HBgL"}), ("unknown", None)),
    (lc.SendResponse(None, {}, timed_out=True), ("unknown", None)),
    (lc.SendResponse(200, {"messages": [{"id": "wamid.HBgL"}]}, timed_out=True), ("unknown", None)),
    (lc.SendResponse(None, {}), ("unknown", None)),
    (lc.SendResponse(400, {"error": {"code": 131026}}), ("rejected", None)),
    (lc.SendResponse(401, {}), ("rejected", None)),
    (lc.SendResponse(429, {}), ("rejected", None)),
    (lc.SendResponse(500, {}), ("unknown", None)),
    (lc.SendResponse(503, {}), ("unknown", None)),
    (lc.SendResponse(302, {}), ("unknown", None)),
])
def test_send_response_classifier(response: lc.SendResponse, expected) -> None:
    kind, pmid = lc.classify_send_response(response)
    assert (kind.value, pmid) == expected


def test_classifier_rejects_non_responses() -> None:
    with pytest.raises(c.ValidationError):
        lc.classify_send_response({"http_status": 200})


# ── Terminal derivations ─────────────────────────────────────────────────────


def test_transport_and_reach_derivations_keep_acceptance_delivery_and_reading_distinct() -> None:
    assert lc.transport_outcome_for(None) == "not_attempted"
    assert lc.transport_outcome_for("accepted") == "accepted"
    assert lc.transport_outcome_for("rejected") == "rejected_definitive"
    assert lc.transport_outcome_for("unknown") == "unknown"
    assert lc.customer_reach_for(None, []) == "not_applicable"
    assert lc.customer_reach_for("accepted", []) == "unknown"                 # acceptance is not reach
    assert lc.customer_reach_for("accepted", ["delivered"]) == "reached"
    assert lc.customer_reach_for("accepted", ["read"]) == "reached"
    assert lc.customer_reach_for("accepted", ["failed"]) == "not_reached"
    assert lc.customer_reach_for("rejected", []) == "not_reached"
    assert lc.customer_reach_for("unknown", []) == "unknown"
    assert lc.customer_reach_for("pending", []) == "unknown"


def test_handoff_request_is_not_human_ownership_transfer() -> None:
    assert lc.human_transfer_established(_effect()) is False                             # request recorded only
    assert lc.human_transfer_established(_effect(confirmed_result={"needs_human": True})) is False
    assert lc.human_transfer_established(_effect(payload={"needs_human": True},
                                                 confirmed_result={"needs_human": True, "request_id": "x"})) is False
    assert lc.human_transfer_established(_effect(status="unknown", confirmed_result=None)) is False
    assert lc.human_transfer_established(_effect(action_type="order_cancel",
                                                 confirmed_result={"transfer": {"human_owner_ref": "agent:1",
                                                                                "accepted_at": "t"}})) is False
    assert lc.human_transfer_established(_effect(confirmed_result={"transfer": {"human_owner_ref": "agent:1"}})) is False
    assert lc.human_transfer_established(_effect(confirmed_result={
        "transfer": {"human_owner_ref": "agent:1", "accepted_at": "2026-09-19T12:00:00Z"},
    })) is True


def test_completion_guard_relation_registry_matches_the_ledger_schema() -> None:
    """The foundation's completion guard classifies the schema over every ledger
    relation: its registry must name exactly the tables the ledger models declare,
    and the partial-schema refusal carries the missing and present names."""
    from core.commerce_runtime import ledger_models as lm
    from core.commerce_runtime import repositories as r

    assert set(r.LEDGER_RELATIONS) == {t.name for t in lm.LEDGER_TABLES}
    assert len(r.LEDGER_RELATIONS) == len(lm.LEDGER_TABLES) == 6
    assert {r.LEDGER_EFFECTS_TABLE, r.LEDGER_SEQUENCES_TABLE} <= set(r.LEDGER_RELATIONS)
    err = lc.LedgerSchemaIncomplete(missing=(lm.EFFECTS_TABLE,), present=(lm.DELIVERY_SEQUENCES_TABLE,))
    assert isinstance(err, c.CommerceRuntimeError) and lc.LedgerSchemaIncomplete is c.LedgerSchemaIncomplete
    assert (err.missing, err.present) == ((lm.EFFECTS_TABLE,), (lm.DELIVERY_SEQUENCES_TABLE,))
    assert f"missing {lm.EFFECTS_TABLE}" in str(err) and f"present {lm.DELIVERY_SEQUENCES_TABLE}" in str(err)
