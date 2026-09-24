"""Who owns a turn under each ownership mode — one rule, two readers.

The pilot narrows itself with an operator-configured recipient allowlist, which
is the right authority while the runtime reaches the owner's own conversations
and nothing else. It is the wrong authority once the runtime is the platform's
default AI path: the question becomes "does this store's own AI setting admit
this customer", which ``core.ai_disabled_gate`` already answers for the legacy
path and must answer identically here.

So these cases are not about a flag. They are about the property that makes the
flag safe to turn on:

    widening the runtime's ownership must not widen who receives AI at all.

The population is decided by the merchant's ``store_ai_mode`` and, in ``test``,
by the numbers that merchant saved. Whether the Commerce Runtime or the legacy
Brain composes the reply is a different question with a different answer, and
these cases drive **both real implementations** against one database row to
prove the first answer does not move when the second one does.

Everything runs against an in-memory copy of the real schema — real
``TenantSettings``, real ``WhatsAppConnection``, the real guard, the real store
gate, the real ``is_ai_allowed_by_store_mode``. No prompt, model, persona or
customer-facing text is involved on any path here.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterator, List, Optional

import pytest

from core.commerce_runtime import pilot_guard as pg
from core.commerce_runtime import store_gate as sg

# Generic, merchant-agnostic: nothing here is specific to one store or category.
ALLOWLISTED_TENANT = 7101
OTHER_TENANT = 7202
DENYLISTED_TENANT = 7303

PHONE_ID_ALLOWLISTED = "1555000701"
PHONE_ID_OTHER = "1555000702"
PHONE_ID_DENYLISTED = "1555000703"

SAVED_TEST_NUMBER = "+966500000701"
OTHER_CUSTOMER = "+966500000999"

# A placeholder. Which model the runtime runs on is an owner decision and
# nothing in the repository may stand in for it.
MODEL = "model-configured-for-this-runtime"


# ── An in-memory copy of the real schema ────────────────────────────────────


class _Fixture:
    def __init__(self, db: Any, engine: Any, models: Any) -> None:
        self.db = db
        self.engine = engine
        self.M = models

    def set_store_ai(self, tenant_id: int, ai_settings: Optional[Dict[str, Any]]) -> None:
        """Save (or clear) one tenant's AI settings, as the dashboard would."""
        row = (
            self.db.query(self.M.TenantSettings)
            .filter(self.M.TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if row is None:
            row = self.M.TenantSettings(tenant_id=tenant_id)
            self.db.add(row)
        row.ai_settings = ai_settings
        self.db.commit()

    def settings_rows(self) -> List[int]:
        return sorted(int(r.tenant_id) for r in self.db.query(self.M.TenantSettings).all())

    def grant_billing(self, tenant_id: int) -> None:
        """Put this tenant inside Nahla's own free-trial window, for real.

        Not a stub: ``has_billing_access`` reads the same tenant row through
        ``has_active_trial`` and ``compute_trial_info``, and the cases below
        assert it actually answers true before relying on it.
        """
        now = _dt.datetime.now(_dt.timezone.utc)
        row = self.db.query(self.M.Tenant).filter(self.M.Tenant.id == tenant_id).first()
        row.first_whatsapp_connected_at = now - _dt.timedelta(days=1)
        row.trial_started_at = now - _dt.timedelta(days=1)
        row.trial_ends_at = now + _dt.timedelta(days=7)
        self.db.commit()


@pytest.fixture()
def platform() -> Iterator[_Fixture]:
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models as M

    swapped = []
    for table in M.Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                swapped.append((col, col.type))
                col.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    try:
        M.Base.metadata.create_all(engine)
    finally:
        for col, original in swapped:
            col.type = original

    db = sessionmaker(bind=engine)()
    for tenant_id, phone_id in ((ALLOWLISTED_TENANT, PHONE_ID_ALLOWLISTED),
                                (OTHER_TENANT, PHONE_ID_OTHER),
                                (DENYLISTED_TENANT, PHONE_ID_DENYLISTED)):
        db.add(M.Tenant(id=tenant_id, name=f"متجر تجريبي عام {tenant_id}", is_active=True))
        db.flush()
        db.add(M.WhatsAppConnection(tenant_id=tenant_id, phone_number_id=phone_id,
                                    status="connected", provider="meta"))
    db.commit()
    try:
        yield _Fixture(db, engine, M)
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime on, configured for one tenant and one operator recipient."""
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(ALLOWLISTED_TENANT))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, SAVED_TEST_NUMBER)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)
    monkeypatch.delenv(pg.ENV_MODE, raising=False)
    monkeypatch.delenv(pg.ENV_GLOBAL_TENANT_DENYLIST, raising=False)
    monkeypatch.delenv(pg.ENV_DRAINING, raising=False)


def route(db: Any, *, tenant_id: int = ALLOWLISTED_TENANT,
          phone_number_id: str = PHONE_ID_ALLOWLISTED,
          customer_phone: str = SAVED_TEST_NUMBER) -> pg.PilotDecision:
    return pg.evaluate_pilot_route(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
        phone_number_id=phone_number_id, inbound_text="وش المنتجات المتوفرة عندكم؟",
        legacy_already_answered=False, ai_gate_skipped=False,
    )


STORE_OFF = {"store_ai_mode": "off"}
STORE_TEST_WITH_NUMBER = {"store_ai_mode": "test",
                          "ai_test_allowed_numbers": [SAVED_TEST_NUMBER]}
STORE_TEST_EMPTY = {"store_ai_mode": "test", "ai_test_allowed_numbers": []}
STORE_ON = {"store_ai_mode": "on"}


# ── The mode itself: a mis-set variable narrows, never widens ───────────────


@pytest.mark.parametrize("raw", ["", "  ", "pilot", "PILOT", "globl", "store-gated",
                                 "true", "1", "all", "everyone"])
def test_an_unrecognised_mode_is_the_narrowest_mode(monkeypatch, raw):
    monkeypatch.setenv(pg.ENV_MODE, raw)
    assert pg.runtime_mode() == pg.MODE_PILOT
    assert pg.store_gate_decides_recipient() is False


def test_an_unset_mode_is_the_narrowest_mode(monkeypatch):
    monkeypatch.delenv(pg.ENV_MODE, raising=False)
    assert pg.runtime_mode() == pg.MODE_PILOT


@pytest.mark.parametrize("raw,expected", [
    ("store_gated", pg.MODE_STORE_GATED), ("  store_gated  ", pg.MODE_STORE_GATED),
    ("STORE_GATED", pg.MODE_STORE_GATED),
    ("global", pg.MODE_GLOBAL), (" Global ", pg.MODE_GLOBAL),
])
def test_the_two_wider_modes_are_named_exactly(monkeypatch, raw, expected):
    """Surrounding space and case are forgiven; a different word is not."""
    monkeypatch.setenv(pg.ENV_MODE, raw)
    assert pg.runtime_mode() == expected
    assert pg.store_gate_decides_recipient() is True


def test_the_modes_are_a_closed_set():
    assert pg.KNOWN_MODES == {pg.MODE_PILOT, pg.MODE_STORE_GATED, pg.MODE_GLOBAL}


# ── Pilot mode is untouched, and provably does not read the store ───────────


def test_pilot_mode_never_asks_the_store_and_still_answers_the_allowlist(
        platform, switched_on, monkeypatch):
    """The store's own setting is irrelevant while the operator's list decides.

    Proved by making the store refuse everybody. In pilot mode that must not
    change a single answer — if the guard had consulted the store, the
    allowlisted recipient would have been refused.
    """
    platform.set_store_ai(ALLOWLISTED_TENANT, STORE_OFF)
    asked: List[Any] = []
    monkeypatch.setattr(sg, "read_store_gate",
                        lambda *a, **k: asked.append(k) or sg.StoreGateRead(sg.GATE_REFUSED))

    permitted = route(platform.db)
    assert permitted.permitted is True and permitted.reason == pg.PERMITTED

    stranger = route(platform.db, customer_phone=OTHER_CUSTOMER)
    assert stranger.permitted is False
    assert stranger.reason == pg.RECIPIENT_NOT_ALLOWLISTED

    assert asked == []                    # the store was never asked, in either case


def test_pilot_mode_still_requires_the_tenant_allowlist(platform, switched_on):
    refused = route(platform.db, tenant_id=OTHER_TENANT, phone_number_id=PHONE_ID_OTHER)
    assert refused.permitted is False and refused.reason == pg.TENANT_NOT_ALLOWLISTED


# ── The merchant's own setting, once it is the authority ────────────────────


@pytest.mark.parametrize("mode", [pg.MODE_STORE_GATED, pg.MODE_GLOBAL])
@pytest.mark.parametrize("ai_settings,phone,permitted,reason", [
    (STORE_OFF, SAVED_TEST_NUMBER, False, pg.STORE_AI_DISABLED),
    (STORE_OFF, OTHER_CUSTOMER, False, pg.STORE_AI_DISABLED),
    (STORE_TEST_WITH_NUMBER, SAVED_TEST_NUMBER, True, pg.PERMITTED),
    (STORE_TEST_WITH_NUMBER, OTHER_CUSTOMER, False, pg.STORE_AI_TEST_MODE_NOT_ALLOWED),
    (STORE_TEST_EMPTY, SAVED_TEST_NUMBER, False, pg.STORE_AI_TEST_MODE_NOT_ALLOWED),
    (STORE_ON, SAVED_TEST_NUMBER, True, pg.PERMITTED),
    (STORE_ON, OTHER_CUSTOMER, True, pg.PERMITTED),
    (None, OTHER_CUSTOMER, True, pg.PERMITTED),        # nothing saved → platform default
])
def test_the_store_decides_the_recipient_in_both_wider_modes(
        platform, switched_on, monkeypatch, mode, ai_settings, phone, permitted, reason):
    monkeypatch.setenv(pg.ENV_MODE, mode)
    platform.set_store_ai(ALLOWLISTED_TENANT, ai_settings)
    decision = route(platform.db, customer_phone=phone)
    assert decision.permitted is permitted, decision
    assert decision.reason == reason, decision


def test_the_operator_recipient_list_stops_being_consulted(platform, switched_on, monkeypatch):
    """A recipient the operator never listed is admitted when the store admits it.

    This is the whole behavioural difference, and it is stated as its own case
    so that deleting the store-gate branch cannot pass as a refactor.
    """
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_STORE_GATED)
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "")     # no operator list at all
    platform.set_store_ai(ALLOWLISTED_TENANT, STORE_ON)
    decision = route(platform.db, customer_phone=OTHER_CUSTOMER)
    assert decision.permitted is True and decision.reason == pg.PERMITTED


# ── The tenant axis: what each wider mode does and does not widen ───────────


def test_store_gated_widens_recipients_and_nothing_else(platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_STORE_GATED)
    platform.set_store_ai(OTHER_TENANT, STORE_ON)
    refused = route(platform.db, tenant_id=OTHER_TENANT, phone_number_id=PHONE_ID_OTHER,
                    customer_phone=OTHER_CUSTOMER)
    assert refused.permitted is False and refused.reason == pg.TENANT_NOT_ALLOWLISTED


def test_global_reaches_a_tenant_no_allowlist_names(platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    platform.set_store_ai(OTHER_TENANT, STORE_ON)
    decision = route(platform.db, tenant_id=OTHER_TENANT, phone_number_id=PHONE_ID_OTHER,
                     customer_phone=OTHER_CUSTOMER)
    assert decision.permitted is True and decision.reason == pg.PERMITTED


def test_global_honours_a_denylisted_tenant(platform, switched_on, monkeypatch):
    """One store returns to the legacy path without every store returning to it."""
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_GLOBAL_TENANT_DENYLIST, str(DENYLISTED_TENANT))
    platform.set_store_ai(DENYLISTED_TENANT, STORE_ON)
    refused = route(platform.db, tenant_id=DENYLISTED_TENANT,
                    phone_number_id=PHONE_ID_DENYLISTED, customer_phone=OTHER_CUSTOMER)
    assert refused.permitted is False and refused.reason == pg.TENANT_DENYLISTED

    # ...and the tenant beside it on the same platform is unaffected.
    platform.set_store_ai(OTHER_TENANT, STORE_ON)
    allowed = route(platform.db, tenant_id=OTHER_TENANT, phone_number_id=PHONE_ID_OTHER,
                    customer_phone=OTHER_CUSTOMER)
    assert allowed.permitted is True


def test_an_empty_denylist_holds_nothing_back(platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_GLOBAL_TENANT_DENYLIST, "  , ,")
    platform.set_store_ai(DENYLISTED_TENANT, STORE_ON)
    assert route(platform.db, tenant_id=DENYLISTED_TENANT,
                 phone_number_id=PHONE_ID_DENYLISTED).permitted is True


# ── The property the rollout rests on ──────────────────────────────────────


@pytest.mark.parametrize("mode", [pg.MODE_STORE_GATED, pg.MODE_GLOBAL])
@pytest.mark.parametrize("ai_settings", [STORE_OFF, STORE_TEST_WITH_NUMBER, STORE_TEST_EMPTY,
                                         STORE_ON, None])
@pytest.mark.parametrize("phone", [SAVED_TEST_NUMBER, OTHER_CUSTOMER])
def test_neither_wider_mode_admits_a_recipient_the_store_itself_refuses(
        platform, switched_on, monkeypatch, mode, ai_settings, phone):
    """Widening the runtime must never widen who receives AI.

    The legacy gate is the authority on that population. In both modes where
    the runtime takes that question on, a turn it permits must be one that gate
    would also have allowed — otherwise the runtime is answering somebody the
    platform decided not to answer.
    """
    from core.ai_disabled_gate import is_ai_allowed_by_store_mode

    monkeypatch.setenv(pg.ENV_MODE, mode)
    platform.set_store_ai(ALLOWLISTED_TENANT, ai_settings)

    decision = route(platform.db, customer_phone=phone)
    legacy = is_ai_allowed_by_store_mode(platform.db, ALLOWLISTED_TENANT, phone)
    if decision.permitted:
        assert legacy.allowed is True, (mode, ai_settings, phone, decision.reason)


@pytest.mark.parametrize("ai_settings", [STORE_OFF, STORE_TEST_EMPTY])
def test_pilot_mode_does_not_have_that_property_and_does_not_need_it(
        platform, switched_on, ai_settings):
    """Recorded, not hidden: in ``pilot`` the operator's list is the authority.

    A store with AI off still yields ``permitted`` here, because the pilot never
    asks the store — and on the webhook path it does not have to: the store gate
    runs first and returns before the runtime is reached, so no such customer is
    answered. The one place this is reachable is the acceptance boundary, which
    records the inbound and sends nothing; the reply is still suppressed
    upstream.

    It is left as it is on purpose. Making the pilot consult the store would
    change the behaviour of the stage that is live in production today, in a PR
    whose subject is the two stages that are not. It belongs to whoever retires
    ``pilot``, and this case is here so that decision is taken deliberately
    rather than discovered.
    """
    from core.ai_disabled_gate import is_ai_allowed_by_store_mode

    platform.set_store_ai(ALLOWLISTED_TENANT, ai_settings)
    assert pg.runtime_mode() == pg.MODE_PILOT
    assert route(platform.db, customer_phone=SAVED_TEST_NUMBER).permitted is True
    assert is_ai_allowed_by_store_mode(
        platform.db, ALLOWLISTED_TENANT, SAVED_TEST_NUMBER).allowed is False


def test_the_wider_modes_admit_exactly_what_the_legacy_gate_admits(
        platform, switched_on, monkeypatch):
    """Not merely "no wider" — the same set, for the tenant the runtime owns.

    Stated separately because "never wider" alone would be satisfied by a mode
    that answers nobody, and that would silently take AI away from stores the
    platform does answer today.
    """
    from core.ai_disabled_gate import is_ai_allowed_by_store_mode

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    for ai_settings in (STORE_OFF, STORE_TEST_WITH_NUMBER, STORE_TEST_EMPTY, STORE_ON, None):
        platform.set_store_ai(ALLOWLISTED_TENANT, ai_settings)
        for phone in (SAVED_TEST_NUMBER, OTHER_CUSTOMER):
            runtime = route(platform.db, customer_phone=phone).permitted
            legacy = is_ai_allowed_by_store_mode(platform.db, ALLOWLISTED_TENANT, phone).allowed
            assert runtime is legacy, (ai_settings, phone, runtime, legacy)


# ── Unreadable is not refused ──────────────────────────────────────────────


class _Unreadable:
    """A session whose settings read fails, while the connection read succeeds.

    Matched on the mapped table rather than on class identity: ``models`` and
    ``database.models`` are two module objects over one file, so their mapped
    classes are not the same object even though they name the same table, and an
    ``is`` comparison here would silently never fire.
    """

    def __init__(self, inner: Any, models: Any) -> None:
        self._inner = inner
        self._M = models
        self._table = str(models.TenantSettings.__tablename__)

    def query(self, model: Any) -> Any:
        if str(getattr(model, "__tablename__", "")) == self._table:
            raise RuntimeError("settings unavailable")
        return self._inner.query(model)


def test_settings_nobody_can_read_are_not_a_refusal(platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    db = _Unreadable(platform.db, platform.M)
    decision = route(db)
    assert decision.permitted is False
    assert decision.reason == pg.STORE_GATE_UNAVAILABLE
    assert decision.reason != pg.STORE_AI_DISABLED


def test_the_acceptance_boundary_tells_a_refusal_from_an_unreadable_setting(
        platform, switched_on, monkeypatch):
    """A refusal is a fact about the store; an unreadable setting is about us.

    The first keeps today's behaviour and the request may be acknowledged; the
    second may not, because the work behind it may well be the runtime's, and
    acknowledging it as accepted is how such work gets dropped.
    """
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)

    platform.set_store_ai(ALLOWLISTED_TENANT, STORE_OFF)
    verdict, target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_ALLOWLISTED, recipient=SAVED_TEST_NUMBER)
    assert verdict == acc.OUT_OF_SCOPE and target is None
    assert detail == pg.STORE_AI_DISABLED

    platform.set_store_ai(ALLOWLISTED_TENANT, STORE_TEST_EMPTY)
    verdict, _target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_ALLOWLISTED, recipient=SAVED_TEST_NUMBER)
    assert verdict == acc.OUT_OF_SCOPE and detail == pg.STORE_AI_TEST_MODE_NOT_ALLOWED

    verdict, _target, detail = acc._classify(
        _Unreadable(platform.db, platform.M),
        phone_number_id=PHONE_ID_ALLOWLISTED, recipient=SAVED_TEST_NUMBER)
    assert verdict == acc.UNDECIDABLE, "an unreadable setting must not be acknowledged"
    assert detail == pg.STORE_GATE_UNAVAILABLE


def test_a_denylisted_tenant_is_decided_at_the_acceptance_boundary_too(
        platform, switched_on, monkeypatch):
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_GLOBAL_TENANT_DENYLIST, str(DENYLISTED_TENANT))
    platform.set_store_ai(DENYLISTED_TENANT, STORE_ON)
    verdict, target, _detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_DENYLISTED, recipient=OTHER_CUSTOMER)
    assert verdict == acc.OUT_OF_SCOPE and target is None


# ── One rule, two readers ──────────────────────────────────────────────────


@pytest.mark.parametrize("ai_settings", [STORE_OFF, STORE_TEST_WITH_NUMBER, STORE_TEST_EMPTY,
                                         STORE_ON, None, {}, {"store_ai_enabled": False},
                                         {"store_ai_mode": "nonsense"},
                                         {"store_ai_mode": "test",
                                          "ai_test_allowed_numbers": "not-a-list"}])
@pytest.mark.parametrize("phone", [SAVED_TEST_NUMBER, OTHER_CUSTOMER, "", "   "])
def test_the_read_only_gate_answers_exactly_what_the_writing_one_answers(
        platform, ai_settings, phone):
    from core.ai_disabled_gate import is_ai_allowed_by_store_mode

    platform.set_store_ai(ALLOWLISTED_TENANT, ai_settings)
    mine = sg.read_store_gate(platform.db, tenant_id=ALLOWLISTED_TENANT, customer_phone=phone)
    theirs = is_ai_allowed_by_store_mode(platform.db, ALLOWLISTED_TENANT, phone)
    assert mine.allowed is theirs.allowed, (ai_settings, phone)
    assert mine.reason == (theirs.reason or "")
    assert mine.mode == theirs.mode


def test_the_read_only_gate_creates_no_row_for_a_store_that_saved_none(platform):
    """The guard runs before a webhook request is acknowledged. It is a read.

    ``is_ai_allowed_by_store_mode`` reaches the settings through
    ``get_or_create_settings``, which inserts a defaulted row. That is right on
    the webhook path and wrong at the acceptance boundary, so the answer has to
    be reachable without the insert — and it has to be the *same* answer.
    """
    from core.ai_disabled_gate import is_ai_allowed_by_store_mode

    assert platform.settings_rows() == []
    mine = sg.read_store_gate(platform.db, tenant_id=OTHER_TENANT,
                              customer_phone=OTHER_CUSTOMER)
    assert platform.settings_rows() == []                    # nothing was written
    assert mine.allowed is True and mine.mode == "on"        # the platform's own default

    theirs = is_ai_allowed_by_store_mode(platform.db, OTHER_TENANT, OTHER_CUSTOMER)
    platform.db.commit()
    assert platform.settings_rows() == [OTHER_TENANT]        # the writing reader wrote
    assert theirs.allowed is mine.allowed and theirs.mode == mine.mode


def test_the_guard_reports_the_merchants_refusal_in_the_merchants_own_words():
    """The operator reading a runtime refusal and a legacy suppression must not
    have to work out that the two strings mean the same thing."""
    from core import ai_disabled_gate as gate

    assert pg.STORE_AI_DISABLED == gate.REASON_STORE_AI_DISABLED
    assert pg.STORE_AI_TEST_MODE_NOT_ALLOWED == gate.REASON_STORE_AI_TEST_MODE_NOT_ALLOWED


# ── Scope resolution follows the same mode ─────────────────────────────────


def test_pilot_scope_resolves_only_an_allowlisted_tenant(platform, switched_on):
    mine = pg.resolve_pilot_scope(platform.db, phone_number_id=PHONE_ID_ALLOWLISTED)
    assert mine.resolved and mine.tenant_id == ALLOWLISTED_TENANT

    theirs = pg.resolve_pilot_scope(platform.db, phone_number_id=PHONE_ID_OTHER)
    assert theirs.status == pg.SCOPE_NOT_OURS


def test_global_scope_resolves_the_owner_from_the_connection_row(
        platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "")        # no list at all
    found = pg.resolve_pilot_scope(platform.db, phone_number_id=PHONE_ID_OTHER)
    assert found.resolved and found.tenant_id == OTHER_TENANT
    assert found.connection_ref == f"wa:{PHONE_ID_OTHER}"


def test_global_scope_reports_a_denylisted_owner_as_decided_not_unavailable(
        platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_GLOBAL_TENANT_DENYLIST, str(DENYLISTED_TENANT))
    found = pg.resolve_pilot_scope(platform.db, phone_number_id=PHONE_ID_DENYLISTED)
    assert found.status == pg.SCOPE_NOT_OURS
    assert found.decided is True and found.detail == "tenant_denylisted"


def test_an_unknown_number_is_still_nobodys_in_global(platform, switched_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    found = pg.resolve_pilot_scope(platform.db, phone_number_id="1555000999")
    assert found.status == pg.SCOPE_NOT_OURS


# ── Unfinished work stays reachable without an allowlist to walk ───────────


def test_duplicate_recovery_finds_the_owner_when_there_is_no_allowlist(
        platform, switched_on, monkeypatch):
    """An empty allowlist used to mean "inspect nobody".

    Under ``global`` that would have silently switched duplicate recovery off —
    a retry carrying an unfinished turn would be dropped and the customer left
    with neither an answer nor a record. The connection row names the one tenant
    that can own the number, which is narrower than the list ever was.
    """
    from core.commerce_runtime import recovery as rec

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "")
    monkeypatch.setattr("database.session.SessionLocal", lambda: platform.db)

    inspected: List[int] = []

    def _no_turn(*, tenant_id: int, **_kw: Any) -> None:
        inspected.append(int(tenant_id))
        return None

    monkeypatch.setattr(rec, "unfinished_turn_for", _no_turn)
    monkeypatch.setattr(rec, "accepted_but_unadmitted", lambda **_kw: False)

    assert rec.duplicate_carries_unfinished_work(
        phone_number_id=PHONE_ID_OTHER, customer_phone=OTHER_CUSTOMER,
        provider_message_id="wamid.generic-1") is None
    assert inspected == [OTHER_TENANT]        # the connection's owner, and only it


def test_duplicate_recovery_still_walks_the_allowlist_in_pilot_mode(
        platform, switched_on, monkeypatch):
    from core.commerce_runtime import recovery as rec

    inspected: List[int] = []
    monkeypatch.setattr(rec, "unfinished_turn_for",
                        lambda *, tenant_id, **_kw: inspected.append(int(tenant_id)) or None)
    monkeypatch.setattr(rec, "accepted_but_unadmitted", lambda **_kw: False)

    rec.duplicate_carries_unfinished_work(
        phone_number_id=PHONE_ID_ALLOWLISTED, customer_phone=SAVED_TEST_NUMBER,
        provider_message_id="wamid.generic-2")
    assert inspected == [ALLOWLISTED_TENANT]

    inspected.clear()
    rec.duplicate_carries_unfinished_work(
        phone_number_id=PHONE_ID_ALLOWLISTED, customer_phone=OTHER_CUSTOMER,
        provider_message_id="wamid.generic-3")
    assert inspected == []                    # an unlisted recipient is still nobody's


def test_an_unreadable_connection_names_no_candidate(platform, switched_on, monkeypatch):
    """A failed lookup answers the same "nobody" an empty allowlist answered.

    It must never answer *more* than a successful one would.
    """
    from core.commerce_runtime import recovery as rec

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)

    def _explode() -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("database.session.SessionLocal", _explode)
    assert rec._connection_owner_tenants(PHONE_ID_OTHER) == []


# ── Nothing about the model, the prompt or the persona ─────────────────────


def test_the_change_reaches_no_intelligence_surface():
    """GOV-001, asserted rather than promised."""
    import inspect

    from core.commerce_runtime import store_gate

    for module in (pg, store_gate):
        source = inspect.getsource(module)
        for forbidden in ("SYSTEM_PROMPT", "persona", "pilot_instructions",
                          "REPLY_TOOL_DESCRIPTION"):
            assert forbidden not in source, (module.__name__, forbidden)


# ── An obligation the runtime could never discharge ─────────────────────────
#
# The acceptance boundary writes a durable record *before* the webhook is
# acknowledged, and only the runtime's own post-turn bookkeeping
# (``commerce_runtime_pilot`` → ``handover.resolve_inbound``) ever clears one.
# Gates ahead of the runtime in ``whatsapp_webhook`` return before that seam, so
# an obligation taken for traffic they silence is never discharged: one pending
# row per inbound until ``handover.MAX_PENDING_DEFERRED`` refuses the next, at
# which point the batch is "not accepted" and the request is answered 503 — that
# merchant's webhook stops being acknowledged at all.
#
# Under ``pilot`` this was unreachable: in scope required an explicitly
# allowlisted recipient. It became reachable the moment the merchant's own
# setting started deciding the recipient, which is what these cases pin.


def test_an_obligation_is_never_taken_for_a_tenant_the_platform_will_silence(
        platform, switched_on, monkeypatch):
    """The regression, in the shape it actually occurred.

    A store that never touched the coupon dashboard defaults to
    ``store_ai_mode=on``; billing access is independent of it and defaults to
    false. In ``global`` that combination reached ``in_scope``.
    """
    from core.billing import has_billing_access
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    platform.set_store_ai(OTHER_TENANT, None)            # nothing saved → default "on"

    assert has_billing_access(platform.db, OTHER_TENANT) is False
    assert sg.read_store_gate(platform.db, tenant_id=OTHER_TENANT,
                              customer_phone=OTHER_CUSTOMER).allowed is True

    verdict, target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_OTHER, recipient=OTHER_CUSTOMER)
    assert verdict == acc.OUT_OF_SCOPE, "an unresolvable obligation must not be taken"
    assert target is None
    assert detail == acc.BILLING_ACCESS_DENIED


def test_a_tenant_the_platform_does_answer_for_is_still_in_scope(
        platform, switched_on, monkeypatch):
    """The other half: the fix must not stop the runtime owning real traffic."""
    from core.billing import has_billing_access
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    platform.set_store_ai(OTHER_TENANT, STORE_ON)
    platform.grant_billing(OTHER_TENANT)
    assert has_billing_access(platform.db, OTHER_TENANT) is True

    verdict, target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_OTHER, recipient=OTHER_CUSTOMER)
    assert verdict == acc.IN_SCOPE, detail
    assert target is not None and int(target[0]) == OTHER_TENANT


def test_an_unreadable_entitlement_is_undecidable_never_unrelated(
        platform, switched_on, monkeypatch):
    """A failed read is a fact about us. Acknowledging it would drop the work."""
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    platform.set_store_ai(OTHER_TENANT, STORE_ON)

    def _explode(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("billing unavailable")

    monkeypatch.setattr("core.billing.has_billing_access", _explode)
    verdict, target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_OTHER, recipient=OTHER_CUSTOMER)
    assert verdict == acc.UNDECIDABLE
    assert target is None
    assert detail.startswith(acc.BILLING_UNREADABLE)
    assert acc.BILLING_ACCESS_DENIED not in detail


def test_the_pilot_stage_does_not_ask_at_all_and_keeps_its_contract(
        platform, switched_on):
    """The live stage is left byte-for-byte as it is, deliberately.

    In ``pilot`` an obligation is bounded to the handful of recipients an
    operator explicitly configured and supervises, so the accumulation the check
    exists to prevent needs traffic nobody chose — which is what the merchant's
    own setting introduced, and only there. Asked in ``pilot`` it would change
    the contract of the stage running in production for no defect.

    So a tenant *without* billing is still in scope here, and that is the
    assertion: the narrowing is a decision, not an oversight.
    """
    from core.billing import has_billing_access
    from services import commerce_runtime_acceptance as acc

    assert pg.runtime_mode() == pg.MODE_PILOT
    assert has_billing_access(platform.db, ALLOWLISTED_TENANT) is False

    verdict, target, _detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_ALLOWLISTED, recipient=SAVED_TEST_NUMBER)
    assert verdict == acc.IN_SCOPE
    assert target is not None and int(target[0]) == ALLOWLISTED_TENANT


def test_the_stage_gated_stage_asks_it_too_not_only_global(
        platform, switched_on, monkeypatch):
    """``store_gated`` is where the merchant's setting first decides recipients,
    so it is where the obligation first outruns what an operator chose."""
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_STORE_GATED)
    platform.set_store_ai(ALLOWLISTED_TENANT, STORE_ON)
    verdict, _target, detail = acc._classify(
        platform.db, phone_number_id=PHONE_ID_ALLOWLISTED, recipient=OTHER_CUSTOMER)
    assert verdict == acc.OUT_OF_SCOPE and detail == acc.BILLING_ACCESS_DENIED


def test_the_entitlement_read_writes_nothing(platform, switched_on, monkeypatch):
    """The acceptance boundary is a read. ``has_billing_access`` must keep it one."""
    from services import commerce_runtime_acceptance as acc

    monkeypatch.setenv(pg.ENV_MODE, pg.MODE_GLOBAL)
    platform.set_store_ai(OTHER_TENANT, STORE_ON)
    def _census() -> Dict[str, int]:
        from sqlalchemy import func, select

        counts: Dict[str, int] = {}
        for table in platform.M.Base.metadata.sorted_tables:
            counts[table.name] = int(
                platform.db.execute(select(func.count()).select_from(table)).scalar() or 0)
        return counts

    before = _census()
    acc._classify(platform.db, phone_number_id=PHONE_ID_OTHER, recipient=OTHER_CUSTOMER)
    platform.db.commit()
    after = _census()

    assert after == before, {k: (before[k], after[k]) for k in after if after[k] != before[k]}
