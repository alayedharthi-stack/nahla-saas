"""The pilot's single routing decision, proved fail-closed (no database, no model).

The runtime must be unreachable unless every condition is configured, and the
decision must be exclusive: whatever it returns, exactly one runtime owns the
turn. These cases drive the guard directly with a connection double, so what is
proved is the policy, not a particular deployment.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from core.commerce_runtime import pilot_guard as pg

TENANT = 4242
OTHER_TENANT = 9999
PHONE_ID = "1555000111"
OWNER_PHONE = "+966500000001"
STRANGER_PHONE = "+966500000999"
# A placeholder: these cases prove the guard requires *a* configured model and
# reports the one it was given. Which model the pilot runs on is an owner
# decision, and nothing in the repository may stand in for it.
MODEL = "model-configured-for-this-pilot"


class _Connection:
    def __init__(self, row_id: int, business_display_name: str = "Owner Test Store") -> None:
        self.id = row_id
        self.business_display_name = business_display_name


class _Query:
    def __init__(self, rows: List[_Connection]) -> None:
        self._rows = rows

    def filter(self, *_conditions: Any) -> "_Query":
        return self

    def first(self) -> Optional[_Connection]:
        return self._rows[0] if self._rows else None


class _Db:
    """A database double that answers the one ownership question the guard asks."""

    def __init__(self, rows: Optional[List[_Connection]] = None, *, explode: bool = False) -> None:
        self._rows = rows if rows is not None else [_Connection(17)]
        self._explode = explode
        self.queries = 0

    def query(self, _model: Any) -> _Query:
        self.queries += 1
        if self._explode:
            raise RuntimeError("database unavailable")
        return _Query(self._rows)


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(TENANT))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, OWNER_PHONE)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)
    return {}


def route(db: Any, **overrides: Any) -> pg.PilotDecision:
    kwargs: Dict[str, Any] = dict(
        tenant_id=TENANT, customer_phone=OWNER_PHONE, phone_number_id=PHONE_ID,
        inbound_text="عندكم حذاء رياضي؟", legacy_already_answered=False, ai_gate_skipped=False,
    )
    kwargs.update(overrides)
    return pg.evaluate_pilot_route(db, **kwargs)


# ── Fail closed ──────────────────────────────────────────────────────────────


def test_the_runtime_is_unreachable_until_it_is_switched_on(monkeypatch):
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(TENANT))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, OWNER_PHONE)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)
    db = _Db()
    decision = route(db)
    assert decision.permitted is False and decision.reason == pg.PILOT_DISABLED
    assert db.queries == 0                       # nothing is even looked up while it is off


def test_an_empty_tenant_allowlist_permits_nothing(monkeypatch):
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.delenv(pg.ENV_TENANT_ALLOWLIST, raising=False)
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, OWNER_PHONE)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)
    assert route(_Db()).reason == pg.TENANT_NOT_ALLOWLISTED


def test_an_allowlisted_tenant_is_never_enabled_wholesale(monkeypatch):
    """The recipient list is required as well; a tenant alone opens nothing."""
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(TENANT))
    monkeypatch.delenv(pg.ENV_RECIPIENT_ALLOWLIST, raising=False)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)
    assert route(_Db()).reason == pg.RECIPIENT_NOT_ALLOWLISTED


def test_another_tenant_is_refused_even_on_an_allowlisted_number(configured):
    assert route(_Db(), tenant_id=OTHER_TENANT).reason == pg.TENANT_NOT_ALLOWLISTED


def test_another_conversation_in_the_allowlisted_tenant_is_refused(configured):
    decision = route(_Db(), customer_phone=STRANGER_PHONE)
    assert decision.permitted is False and decision.reason == pg.RECIPIENT_NOT_ALLOWLISTED


@pytest.mark.parametrize("phone, reason", [
    ("", pg.RECIPIENT_MISSING),
    ("   ", pg.RECIPIENT_MISSING),
    ("not-a-phone", pg.RECIPIENT_UNNORMALIZABLE),
])
def test_an_unusable_recipient_is_refused_by_name(configured, phone, reason):
    assert route(_Db(), customer_phone=phone).reason == reason


def test_a_connection_that_is_not_this_tenant_s_is_refused(configured):
    """Ownership is proved against the database, not taken from the payload."""
    decision = route(_Db(rows=[]))
    assert decision.permitted is False and decision.reason == pg.CONNECTION_NOT_VERIFIED


def test_a_display_name_never_authorises_anything(configured, monkeypatch):
    """A store called like the owner's, on a tenant that is not allowlisted, is refused."""
    decision = route(_Db(rows=[_Connection(1, business_display_name="Owner Test Store")]),
                     tenant_id=OTHER_TENANT)
    assert decision.reason == pg.TENANT_NOT_ALLOWLISTED


def test_a_missing_phone_number_id_cannot_be_verified(configured):
    assert route(_Db(), phone_number_id="").reason == pg.CONNECTION_NOT_VERIFIED


def test_a_connection_lookup_that_fails_is_undecidable_not_unverified(configured):
    """A failed read is a fact about us, not about the tenant.

    ``connection_not_verified`` is a verified negative — the tenant does not
    own this number — and acceptance treats it as "not ours". A lookup that
    raised established nothing of the kind, so the guard says ``guard_error``
    and the caller that acknowledges inbounds refuses rather than drops.
    """
    decision = route(_Db(explode=True))
    assert decision.permitted is False and decision.reason == pg.GUARD_ERROR


def test_a_verified_row_the_caller_already_holds_is_not_looked_up_again(configured):
    """Acceptance resolved the connection once; a second lookup that fails must
    not turn that verified association into ``connection_not_verified``."""
    decision = route(_Db(explode=True), verified=(f"wa:{PHONE_ID}", "17"))
    assert decision.permitted is True
    assert decision.connection_ref == f"wa:{PHONE_ID}" and decision.connection_id == "17"


def test_an_unexpected_guard_error_refuses_rather_than_raising(configured, monkeypatch):
    monkeypatch.setattr(pg, "_normalize", lambda value: (_ for _ in ()).throw(RuntimeError("x")))
    decision = route(_Db())
    assert decision.permitted is False and decision.reason == pg.GUARD_ERROR


def test_a_turn_the_legacy_path_already_answered_is_not_taken(configured):
    assert route(_Db(), legacy_already_answered=True).reason == pg.LEGACY_ALREADY_ANSWERED


def test_a_turn_the_existing_ai_gates_skipped_is_not_taken(configured):
    """Pause, handoff, blocklist and the rest keep their meaning for the pilot."""
    assert route(_Db(), ai_gate_skipped=True).reason == pg.AI_GATE_SKIPPED


def test_an_empty_customer_turn_is_not_taken(configured):
    assert route(_Db(), inbound_text="   ").reason == pg.EMPTY_INBOUND


# ── The permitted case ───────────────────────────────────────────────────────


def test_a_fully_configured_owner_conversation_is_permitted_once(configured):
    decision = route(_Db(rows=[_Connection(17)]))
    assert decision.permitted is True and decision.reason == pg.PERMITTED
    assert decision.tenant_id == TENANT
    assert decision.recipient and decision.recipient.endswith("500000001")
    assert decision.connection_ref == f"wa:{PHONE_ID}"
    assert decision.connection_id == "17"
    assert decision.model == MODEL


def test_the_decision_is_exclusive_in_both_directions(configured):
    permitted = route(_Db())
    refused = route(_Db(), tenant_id=OTHER_TENANT)
    assert permitted.permitted is True and permitted.legacy_owns_turn is False
    assert refused.permitted is False and refused.legacy_owns_turn is True


def test_the_recipient_allowlist_is_matched_after_normalisation(configured, monkeypatch):
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "00966500000001 , 966500000002")
    assert route(_Db(), customer_phone="+966 50 000 0001").permitted is True
    assert route(_Db(), customer_phone="+966500000002").permitted is True
    assert route(_Db(), customer_phone="+966500000003").permitted is False


def test_an_unparsable_tenant_entry_is_dropped_rather_than_widening_the_list(configured, monkeypatch):
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, f"abc, -1, 0, {TENANT}")
    assert pg.tenant_allowlist() == frozenset({TENANT})
    assert route(_Db()).permitted is True


# ── The model is chosen, never inherited ─────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_pilot_with_no_configured_model_is_not_permitted(configured, monkeypatch, value):
    """Activation must name a model. Silence is refused, not resolved."""
    if value is None:
        monkeypatch.delenv(pg.ENV_MODEL, raising=False)
    else:
        monkeypatch.setenv(pg.ENV_MODEL, value)
    decision = route(_Db())
    assert decision.permitted is False and decision.reason == pg.MODEL_NOT_CONFIGURED
    assert decision.model is None


def test_the_model_is_read_only_from_its_own_variable(configured, monkeypatch):
    """Not from the legacy resolution, and not from the repository fallback."""
    monkeypatch.delenv(pg.ENV_MODEL, raising=False)
    monkeypatch.setenv("CLAUDE_MODEL", "some-other-model")
    assert route(_Db()).reason == pg.MODEL_NOT_CONFIGURED
    assert pg.pilot_model() == ""


def test_the_configured_model_is_carried_on_the_decision_verbatim(configured, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODEL, "  a-specific-model  ")
    decision = route(_Db())
    assert decision.permitted is True and decision.model == "a-specific-model"


def test_an_unconfigured_model_is_refused_before_the_connection_is_looked_up(configured,
                                                                             monkeypatch):
    """A misconfigured pilot costs nothing and touches no database."""
    monkeypatch.delenv(pg.ENV_MODEL, raising=False)
    db = _Db()
    assert route(db).reason == pg.MODEL_NOT_CONFIGURED
    assert db.queries == 0


def test_an_unconfigured_model_still_leaves_the_turn_with_the_legacy_path(configured, monkeypatch):
    monkeypatch.delenv(pg.ENV_MODEL, raising=False)
    assert route(_Db()).legacy_owns_turn is True


# ── Finite limits ────────────────────────────────────────────────────────────


def test_the_default_budget_is_finite_and_within_every_ceiling(monkeypatch):
    for name in (pg.ENV_MAX_STEPS, pg.ENV_MAX_TOOL_CALLS, pg.ENV_DEADLINE_SECONDS,
                 pg.ENV_PROVIDER_TIMEOUT_SECONDS, pg.ENV_TOOL_TIMEOUT_SECONDS):
        monkeypatch.delenv(name, raising=False)
    budget = pg.pilot_budget()
    assert 1 <= budget.max_steps <= pg.MAX_STEPS_CEILING
    assert 0 <= budget.max_tool_calls <= pg.MAX_TOOL_CALLS_CEILING
    assert 0 < budget.deadline_seconds <= pg.DEADLINE_CEILING_SECONDS
    assert 0 < budget.provider_timeout_seconds <= pg.PROVIDER_TIMEOUT_CEILING_SECONDS
    assert 0 < budget.tool_timeout_seconds <= pg.TOOL_TIMEOUT_CEILING_SECONDS


def test_configuration_may_lower_a_limit(monkeypatch):
    monkeypatch.setenv(pg.ENV_MAX_STEPS, "2")
    monkeypatch.setenv(pg.ENV_DEADLINE_SECONDS, "20")
    budget = pg.pilot_budget()
    assert budget.max_steps == 2 and budget.deadline_seconds == 20.0


def test_configuration_can_never_raise_a_limit_above_its_ceiling(monkeypatch):
    monkeypatch.setenv(pg.ENV_MAX_STEPS, "500")
    monkeypatch.setenv(pg.ENV_MAX_TOOL_CALLS, "500")
    monkeypatch.setenv(pg.ENV_DEADLINE_SECONDS, "86400")
    monkeypatch.setenv(pg.ENV_PROVIDER_TIMEOUT_SECONDS, "3600")
    monkeypatch.setenv(pg.ENV_TOOL_TIMEOUT_SECONDS, "3600")
    budget = pg.pilot_budget()
    assert budget.max_steps == pg.MAX_STEPS_CEILING
    assert budget.max_tool_calls == pg.MAX_TOOL_CALLS_CEILING
    assert budget.deadline_seconds == pg.DEADLINE_CEILING_SECONDS
    assert budget.provider_timeout_seconds == pg.PROVIDER_TIMEOUT_CEILING_SECONDS
    assert budget.tool_timeout_seconds == pg.TOOL_TIMEOUT_CEILING_SECONDS


@pytest.mark.parametrize("value", ["", "   ", "nonsense", "0", "-5"])
def test_a_mis_set_limit_falls_back_to_the_bounded_default(monkeypatch, value):
    monkeypatch.setenv(pg.ENV_DEADLINE_SECONDS, value)
    monkeypatch.setenv(pg.ENV_MAX_TOOL_CALLS, value)
    budget = pg.pilot_budget()
    assert 0 < budget.deadline_seconds <= pg.DEADLINE_CEILING_SECONDS
    assert 1 <= budget.max_tool_calls <= pg.MAX_TOOL_CALLS_CEILING


def test_the_budget_the_pilot_hands_the_loop_is_one_the_loop_accepts(monkeypatch):
    from core.commerce_runtime import agent_contracts as ac

    monkeypatch.setenv(pg.ENV_MAX_STEPS, "3")
    assert ac.validate_budget(pg.pilot_budget()).max_steps == 3
