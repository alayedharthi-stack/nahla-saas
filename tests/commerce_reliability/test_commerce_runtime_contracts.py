"""Pure contract tests of the dormant commerce runtime foundation (no database)."""
from __future__ import annotations

import datetime as _dt

import pytest

from core.commerce_runtime import contracts as c

NOW = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.timezone.utc)
LATER = NOW + _dt.timedelta(seconds=30)
EARLIER = NOW - _dt.timedelta(seconds=30)
TOKEN = c.OwnershipToken(owner_id="worker-a", fence=3, epoch=1, tenant_id=7, namespace="live", conversation_id=42)


def _classify(**overrides):
    kwargs = dict(
        current_owner="worker-a", current_fence=3, current_epoch=1, current_expires_at=LATER,
        current_revision=7, token=TOKEN, db_now=NOW, expected_revision=None,
    )
    kwargs.update(overrides)
    return c.classify_rejection(**kwargs)


def test_valid_token_is_not_rejected() -> None:
    assert _classify() is None
    assert _classify(expected_revision=7) is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"current_fence": 4}, c.RejectReason.SUPERSEDED_FENCE),
        ({"current_fence": 2}, c.RejectReason.INVALID_FENCE),
        ({"current_epoch": 2}, c.RejectReason.OBSOLETE_EPOCH),
        ({"current_owner": "worker-b"}, c.RejectReason.STALE_OWNER),
        ({"current_owner": None, "current_expires_at": None}, c.RejectReason.STALE_OWNER),
        ({"current_expires_at": EARLIER}, c.RejectReason.EXPIRED_LEASE),
        ({"current_expires_at": NOW}, c.RejectReason.EXPIRED_LEASE),
        ({"expected_revision": 6}, c.RejectReason.STALE_REVISION),
    ],
)
def test_each_guard_has_an_exact_reason(overrides, reason) -> None:
    assert _classify(**overrides) is reason


def test_precedence_reports_the_earliest_invalidating_fact() -> None:
    # Superseded fence wins over an epoch change, an owner change and expiry.
    assert _classify(current_fence=9, current_epoch=5, current_owner="x", current_expires_at=EARLIER) \
        is c.RejectReason.SUPERSEDED_FENCE
    # Epoch wins over owner and expiry when the fence is current.
    assert _classify(current_epoch=2, current_owner=None, current_expires_at=None) is c.RejectReason.OBSOLETE_EPOCH
    # Owner wins over expiry.
    assert _classify(current_owner="worker-b", current_expires_at=EARLIER) is c.RejectReason.STALE_OWNER
    # Expiry wins over revision.
    assert _classify(current_expires_at=EARLIER, expected_revision=1) is c.RejectReason.EXPIRED_LEASE


@pytest.mark.parametrize("value", ["", " ", "a b", "tab\there", "nul\x00", "x" * (c.MAX_REF_LENGTH + 1), 5, None])
def test_references_are_bounded_printable_and_whitespace_free(value) -> None:
    with pytest.raises(c.ValidationError):
        c.validate_ref(value, field="conversation_ref", max_length=c.MAX_REF_LENGTH)


def test_references_accept_unicode_and_exact_bound() -> None:
    assert c.validate_ref("conv:عميل-١٢٣", field="r", max_length=c.MAX_REF_LENGTH) == "conv:عميل-١٢٣"
    assert len(c.validate_ref("x" * c.MAX_REF_LENGTH, field="r", max_length=c.MAX_REF_LENGTH)) == c.MAX_REF_LENGTH


def test_payload_bounds_and_shape() -> None:
    assert c.validate_payload(None, field="p", max_bytes=64) == {}
    assert c.validate_payload({"b": 1, "a": [1, 2]}, field="p", max_bytes=64) == {"a": [1, 2], "b": 1}
    with pytest.raises(c.ValidationError):
        c.validate_payload(["not", "an", "object"], field="p", max_bytes=64)
    with pytest.raises(c.ValidationError):
        c.validate_payload({1: "non-string key"}, field="p", max_bytes=64)
    with pytest.raises(c.ValidationError):
        c.validate_payload({"nan": float("nan")}, field="p", max_bytes=64)
    with pytest.raises(c.ValidationError):
        c.validate_payload({"obj": object()}, field="p", max_bytes=64)
    with pytest.raises(c.ValidationError):
        c.validate_payload({"text": "x" * c.MAX_PAYLOAD_BYTES}, field="p", max_bytes=c.MAX_PAYLOAD_BYTES)


def test_payload_size_is_measured_on_the_canonical_utf8_form() -> None:
    arabic = {"t": "تنورة"}  # 5 Arabic letters, 10 UTF-8 bytes
    assert c.validate_payload(arabic, field="p", max_bytes=len('{"t":"تنورة"}'.encode("utf-8"))) == arabic
    with pytest.raises(c.ValidationError):
        c.validate_payload(arabic, field="p", max_bytes=len('{"t":"تنورة"}'.encode("utf-8")) - 1)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", c.MAX_LEASE_SECONDS + 1])
def test_lease_seconds_bounds(value) -> None:
    with pytest.raises(c.ValidationError):
        c.validate_lease_seconds(value)


@pytest.mark.parametrize("value", [0, -3, True, "1", None])
def test_tenant_id_must_be_a_positive_integer(value) -> None:
    with pytest.raises(c.ValidationError):
        c.validate_tenant_id(value)


def test_closed_vocabularies_reject_unknown_values() -> None:
    assert c.validate_namespace("shadow") is c.Namespace.SHADOW
    assert c.validate_namespace(c.Namespace.LIVE) is c.Namespace.LIVE
    with pytest.raises(c.ValidationError):
        c.validate_namespace("production")
    assert c.validate_enum(c.TransportOutcome.UNKNOWN, c.TransportOutcome, field="t") == "unknown"
    with pytest.raises(c.ValidationError):
        c.validate_enum("delivered", c.TransportOutcome, field="t")
    with pytest.raises(c.ValidationError):
        c.validate_enum("done", c.ProcessingOutcome, field="p")


def _token(**overrides) -> c.OwnershipToken:
    fields = dict(owner_id="w", fence=1, epoch=0, tenant_id=7, namespace="live", conversation_id=42)
    fields.update(overrides)
    return c.OwnershipToken(**fields)


@pytest.mark.parametrize("overrides", [
    {"fence": -1}, {"fence": True}, {"epoch": -2}, {"tenant_id": 0}, {"tenant_id": "7"},
    {"namespace": "production"}, {"conversation_id": -1}, {"owner_id": ""},
])
def test_token_validation_rejects_bad_counters_or_scope(overrides) -> None:
    with pytest.raises(c.ValidationError):
        c.validate_token(_token(**overrides))


def test_token_validation_normalises_and_keeps_the_scope() -> None:
    with pytest.raises(c.ValidationError):
        c.validate_token(("w", 1, 0))
    assert c.validate_token(_token(namespace=c.Namespace.SHADOW)) == _token(namespace="shadow")


@pytest.mark.parametrize("target", [
    dict(tenant_id=8, namespace="live", conversation_id=42),    # other tenant
    dict(tenant_id=7, namespace="shadow", conversation_id=42),  # other namespace
    dict(tenant_id=7, namespace="live", conversation_id=43),    # other conversation
])
def test_token_scope_binding_refuses_any_other_scope(target) -> None:
    with pytest.raises(c.ScopeMismatch) as rejected:
        c.require_token_scope(_token(), **target)
    assert rejected.value.target == (target["tenant_id"], target["namespace"], target["conversation_id"])
    c.require_token_scope(_token(), tenant_id=7, namespace="live", conversation_id=42)  # exact scope passes


def test_transport_unknown_is_a_distinct_recorded_value_not_a_failure_alias() -> None:
    assert c.TransportOutcome.UNKNOWN.value == "unknown"
    assert {m.value for m in c.TransportOutcome} == {"accepted", "rejected_definitive", "unknown", "not_attempted"}
    assert {m.value for m in c.CustomerReach} == {"reached", "not_reached", "unknown", "not_applicable"}
    assert {m.value for m in c.ProcessingOutcome} == {"completed", "failed", "abandoned"}
