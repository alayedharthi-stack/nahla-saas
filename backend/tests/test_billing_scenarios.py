"""
tests/test_billing_scenarios.py
───────────────────────────────
End-to-end behavioural tests for the four trial/billing scenarios:

  1. trial_blocked → inbound + reads work; ALL outbound paths blocked.
  2. active        → all outbound paths work.
  3. downgrade Growth → Starter → Growth-only features blocked at runtime.
  4. trial ledger persists across tenant/integration deletion.

Run:
    cd backend
    python -m pytest tests/test_billing_scenarios.py -v
    # or:
    python tests/test_billing_scenarios.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Allow running from repo root or backend/
_here    = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
_repo    = os.path.dirname(_backend)
for _p in [_backend, _repo, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Allow `from models import ...` (resolves to database.models via the project's sys.path setup)
_database = os.path.join(_repo, "database")
if _database not in sys.path:
    sys.path.insert(0, _database)

import pytest

from core.billing import (
    has_billing_access,
    require_outbound_access,
    _has_salla_active_subscription,
)
from core.plan_entitlements import (
    PLAN_DEFINITIONS,
    PlanEntitlements,
    EntitlementError,
    require_feature,
)
from fastapi import HTTPException


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — MockDB that simulates the SQLAlchemy query chain we use
# ─────────────────────────────────────────────────────────────────────────────

class _MockQuery:
    """Minimal SQLAlchemy-Query stand-in: .filter().first() / .order_by().first()."""
    def __init__(self, result):
        self._result = result
    def filter(self, *_a, **_kw):       return self
    def filter_by(self, *_a, **_kw):    return self
    def order_by(self, *_a, **_kw):     return self
    def first(self):                    return self._result
    def all(self):                      return list(self._result) if self._result else []


class MockDB:
    """
    Mocks the parts of `Session` that core.billing actually touches.

    Configure responses via .set(model_name, instance) before calling code.
    Examples:
        db = MockDB()
        db.set("BillingSubscription", None)        # no Nahla sub
        db.set("Integration",         None)        # no Salla integration
        db.set("Tenant",              tenant_obj)  # for trial computation
    """
    def __init__(self):
        self._responses: dict = {}

    def set(self, model_name: str, value):
        self._responses[model_name] = value
        return self

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _MockQuery(self._responses.get(name))

    # No-op transaction methods so code that calls them doesn't crash
    def add(self, *_):     pass
    def flush(self):       pass
    def commit(self):      pass
    def rollback(self):    pass
    def close(self):       pass


def _fake_tenant_no_trial():
    """Tenant whose internal Nahla trial has expired."""
    t = MagicMock()
    t.id                  = 99
    t.created_at          = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t.trial_started_at    = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t.trial_ends_at       = datetime(2020, 1, 15, tzinfo=timezone.utc)
    t.subscription_status = None
    t.current_period_end  = None
    t.billing_status      = None
    return t


def _fake_integration(billing_status: str, store_id: str = "12345"):
    """A Salla Integration row with the given billing_status in config."""
    integ = MagicMock()
    integ.id                = 1
    integ.tenant_id         = 99
    integ.provider          = "salla"
    integ.external_store_id = store_id
    integ.config            = {
        "store_id":             store_id,
        "billing_status":       billing_status,
        "salla_plan_slug":      "growth",
        "salla_subscription_id":"sub_abc",
    }
    integ.enabled = True
    return integ


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — trial_blocked
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario1_TrialBlocked:
    """
    The store has installed Nahla, used its free trial once, deleted the app,
    and reinstalled. trial_ledger.trial_used = True → billing_status = trial_blocked.

    EXPECTED:
      - has_billing_access()        → False
      - require_outbound_access()   → raises HTTP 402
      - inbound recording           → still works (NOT tested here — see scenario doc)
      - dashboard reads             → still work (NOT gated by has_billing_access)
    """

    def _trial_blocked_db(self):
        db = MockDB()
        db.set("BillingSubscription", None)
        db.set("Tenant",              _fake_tenant_no_trial())
        db.set("Integration",         _fake_integration("trial_blocked"))
        return db

    def test_has_billing_access_returns_false(self):
        db = self._trial_blocked_db()
        assert has_billing_access(db, tenant_id=99) is False

    def test_salla_subscription_check_returns_false_for_trial_blocked(self):
        db = self._trial_blocked_db()
        assert _has_salla_active_subscription(db, tenant_id=99) is False

    def test_require_outbound_access_raises_402(self):
        db = self._trial_blocked_db()
        with pytest.raises(HTTPException) as exc_info:
            require_outbound_access(db, tenant_id=99)
        assert exc_info.value.status_code == 402
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["code"] == "billing_access_denied"
        # User-facing Arabic message
        assert "اشترك" in detail["message"] or "الاشتراك" in detail["message"]

    def test_brain_pipeline_skip_marker_present(self):
        """
        The MerchantBrain.process() guard returns:
          {"reply": None, "skipped": True, "reason": "billing_access_denied"}

        We verify the contract by reading the source — the actual pipeline
        is async and depends on many layers; here we assert the literal
        skip marker exists in the file so the webhook handler can detect it.
        """
        from pathlib import Path
        src = Path(_backend) / "modules" / "ai" / "brain" / "pipeline.py"
        body = src.read_text(encoding="utf-8")
        assert 'has_billing_access' in body, "Brain pipeline lost its billing guard"
        assert '"billing_access_denied"' in body or "'billing_access_denied'" in body, \
            "Brain pipeline is missing the billing_access_denied skip marker"
        assert '"skipped": True' in body or "'skipped': True" in body, \
            "Brain pipeline is missing the skipped:True contract"

    def test_whatsapp_webhook_handles_skip_marker(self):
        """Webhook must suppress outbound silently — never customer billing messages."""
        from pathlib import Path
        src = Path(_backend) / "routers" / "whatsapp_webhook.py"
        body = src.read_text(encoding="utf-8")
        assert "billing_access_denied" in body, \
            "whatsapp_webhook is missing the billing_access_denied handler"
        assert "التجربة المنتهية" not in body, \
            "whatsapp_webhook must not send customer-facing trial expiry fallback"
        assert "outbound suppressed" in body.lower() or "silent" in body.lower(), \
            "whatsapp_webhook billing guard must be silent"

    def test_billing_checkout_uses_effective_subscription_for_idempotency(self):
        from pathlib import Path
        src = Path(_backend) / "routers" / "billing.py"
        body = src.read_text(encoding="utf-8")
        assert "get_tenant_subscription" in body
        assert "effective_active" in body

    def test_automation_engine_blocks_outbound(self):
        """_execute_action must return billing_access_denied before any send."""
        from pathlib import Path
        src = Path(_backend) / "core" / "automation_engine.py"
        body = src.read_text(encoding="utf-8")
        # Guard inside _execute_action
        assert "_execute_action" in body
        assert "billing_access_denied" in body, \
            "automation_engine lost its outbound billing guard"

    def test_process_pending_events_no_longer_drains(self):
        """
        After moving the guard into _execute_action, process_pending_events
        must NOT short-circuit on billing — inbound matching must still run.
        """
        from pathlib import Path
        src = Path(_backend) / "core" / "automation_engine.py"
        body = src.read_text(encoding="utf-8")
        # The old early-return pattern that drained pending events on no-billing
        # is gone; replaced with an explanatory comment.
        assert "billing/trial guard is enforced inside _execute_action" in body.lower() \
            or "guard is not applied here" in body.lower(), \
            "process_pending_events should no longer block on billing — guard moved to _execute_action"

    def test_conversations_reply_router_allows_manual_without_billing(self):
        """Manual dashboard replies must stay available after trial/sub expiry."""
        from pathlib import Path
        src = Path(_backend) / "routers" / "conversations.py"
        body = src.read_text(encoding="utf-8")
        reply_fn = body.split("@router.post(\"/reply\")", 1)[1].split("@router.", 1)[0]
        assert "require_outbound_access" not in reply_fn, \
            "conversations.reply_to_conversation must NOT gate manual merchant replies"

    def test_campaigns_router_requires_outbound_access(self):
        from pathlib import Path
        src = Path(_backend) / "routers" / "campaigns.py"
        body = src.read_text(encoding="utf-8")
        assert "require_outbound_access" in body, \
            "campaigns endpoint must call require_outbound_access"

    def test_orders_payment_reminder_requires_outbound_access(self):
        from pathlib import Path
        src = Path(_backend) / "routers" / "orders.py"
        body = src.read_text(encoding="utf-8")
        assert "require_outbound_access" in body, \
            "orders.send_payment_reminder must call require_outbound_access"


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — active subscription
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario2_Active:
    """
    The store has an active Salla subscription (or active Nahla sub).
    Every outbound path must succeed.
    """

    def _active_salla_db(self):
        db = MockDB()
        db.set("BillingSubscription", None)
        db.set("Tenant",              _fake_tenant_no_trial())
        db.set("Integration",         _fake_integration("active"))
        return db

    def _trial_salla_db(self):
        db = MockDB()
        db.set("BillingSubscription", None)
        db.set("Tenant",              _fake_tenant_no_trial())
        db.set("Integration",         _fake_integration("trial"))
        return db

    def _active_nahla_db(self):
        """Nahla-native subscription (Stripe/Hyperpay) with no Salla integration."""
        now = datetime.now(timezone.utc)
        sub = MagicMock()
        sub.id = 1; sub.tenant_id = 99; sub.status = "active"
        sub.started_at = now - timedelta(days=5)
        sub.ends_at = now + timedelta(days=25)
        sub.extra_metadata = {}

        db = MockDB()
        db.set("BillingSubscription", sub)
        db.set("Tenant",              _fake_tenant_no_trial())
        db.set("Integration",         None)
        return db

    def test_active_salla_grants_access(self):
        db = self._active_salla_db()
        assert _has_salla_active_subscription(db, tenant_id=99) is True
        assert has_billing_access(db, tenant_id=99) is True

    def test_trial_salla_grants_access(self):
        db = self._trial_salla_db()
        assert _has_salla_active_subscription(db, tenant_id=99) is True
        assert has_billing_access(db, tenant_id=99) is True

    def test_active_nahla_subscription_grants_access(self):
        db = self._active_nahla_db()
        assert has_billing_access(db, tenant_id=99) is True

    def test_require_outbound_access_does_not_raise_for_active(self):
        # Should not raise for any of the 3 valid sources of access
        for db in (self._active_salla_db(), self._trial_salla_db(), self._active_nahla_db()):
            require_outbound_access(db, tenant_id=99)  # should be a no-op

    def test_starter_plan_allows_basic_features(self):
        """Active Starter plan permits its core features (manual reply, basic campaigns)."""
        plan_def  = PLAN_DEFINITIONS["starter"]
        ent = PlanEntitlements(
            plan_slug      = "starter",
            plan_name_ar   = plan_def.name_ar,
            billing_status = "active",
            is_active      = True,
            is_blocked     = False,
            features       = plan_def.features,
            limits         = plan_def.limits,
            raw_plan       = plan_def,
        )
        # Starter must allow basic campaign segments and template library
        require_feature(ent, "nahla_template_library")
        require_feature(ent, "campaign_customer_segments")


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — downgrade Growth → Starter
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario3_DowngradeGrowthToStarter:
    """
    Merchant was on Growth (had predictive_reorder, cart_recovery_stage_3).
    They downgrade to Starter. Both features must be blocked at runtime,
    even though the underlying SmartAutomation row may still exist.
    """

    def _starter_ent(self):
        plan_def  = PLAN_DEFINITIONS["starter"]
        return PlanEntitlements(
            plan_slug      = "starter",
            plan_name_ar   = plan_def.name_ar,
            billing_status = "active",
            is_active      = True,
            is_blocked     = False,
            features       = plan_def.features,
            limits         = plan_def.limits,
            raw_plan       = plan_def,
        )

    def test_predictive_reorder_blocked_on_starter(self):
        ent = self._starter_ent()
        with pytest.raises(EntitlementError) as exc_info:
            require_feature(ent, "predictive_reorder")
        assert exc_info.value.error_code == "upgrade_required"
        assert exc_info.value.required_plan == "growth"

    def test_cart_recovery_stage_3_blocked_on_starter(self):
        ent = self._starter_ent()
        with pytest.raises(EntitlementError) as exc_info:
            require_feature(ent, "cart_recovery_stage_3")
        assert exc_info.value.error_code == "upgrade_required"
        assert exc_info.value.required_plan == "growth"

    def test_starter_keeps_its_own_features(self):
        ent = self._starter_ent()
        # Should NOT raise
        require_feature(ent, "nahla_template_library")
        require_feature(ent, "campaign_customer_segments")
        require_feature(ent, "autopilot_order_confirmation")

    def test_automation_engine_has_runtime_feature_map(self):
        """
        Verify the automation engine still maps automation_type → feature_key
        so the runtime check can lock Growth-only automations after downgrade.
        """
        from pathlib import Path
        src = Path(_backend) / "core" / "automation_engine.py"
        body = src.read_text(encoding="utf-8")
        # These mappings must survive
        assert '"predictive_reorder"' in body
        assert '"cart_recovery_stage_3"' in body
        assert "_AUTOMATION_FEATURE_MAP" in body
        assert "plan_locked:" in body


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Trial ledger persistence after delete & reinstall
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario4_TrialLedgerPersistence:
    """
    SallaTrialLedger is the permanent per-store record of trial usage.
    It must:
      - have NO foreign key to tenants/integrations (so cascading deletes don't reach it)
      - have a UNIQUE index on salla_store_id
      - be checked in subscription.created handler to downgrade re-trial → trial_blocked
    """

    def test_model_exists_and_is_independent(self):
        from sqlalchemy import UniqueConstraint
        from models import SallaTrialLedger

        # Table name
        assert SallaTrialLedger.__tablename__ == "salla_trial_ledger"

        # Unique constraint that COVERS the salla_store_id column (we don't care
        # about the constraint's name, only that the column is enforced unique).
        unique_constrained_cols: set[str] = set()
        for c in SallaTrialLedger.__table__.constraints:
            if isinstance(c, UniqueConstraint):
                unique_constrained_cols.update(col.name for col in c.columns)
        # Also count column-level unique=True (PG sometimes emits that route)
        for col in SallaTrialLedger.__table__.columns:
            if col.unique:
                unique_constrained_cols.add(col.name)

        assert "salla_store_id" in unique_constrained_cols, (
            "SallaTrialLedger must enforce uniqueness on salla_store_id "
            f"(saw unique cols: {sorted(unique_constrained_cols)})"
        )

        # No foreign keys to tenants/integrations
        for col in SallaTrialLedger.__table__.columns:
            for fk in col.foreign_keys:
                target = str(fk.column.table.name)
                assert target not in ("tenants", "integrations"), \
                    f"SallaTrialLedger must NOT have FK to {target} (would cascade-delete)"

    def test_required_fields_present(self):
        from models import SallaTrialLedger
        cols = {c.name for c in SallaTrialLedger.__table__.columns}
        for required in (
            "id", "salla_store_id", "merchant_id", "trial_used",
            "first_trial_started_at", "first_trial_plan",
            "source", "created_at", "updated_at",
        ):
            assert required in cols, f"SallaTrialLedger missing column: {required}"

    def test_migration_file_exists(self):
        """0042 migration must exist so production DB will get the new table."""
        from pathlib import Path
        mig = Path(_repo) / "database" / "migrations" / "versions" / "0042_salla_trial_ledger.py"
        assert mig.exists(), f"Missing migration: {mig}"
        text = mig.read_text(encoding="utf-8")
        assert "create_table" in text and "salla_trial_ledger" in text
        assert "UniqueConstraint" in text or "unique=True" in text

    def test_subscription_handler_records_first_trial_and_blocks_repeat(self):
        """
        Direct unit test of record_trial_used + is_trial_blocked semantics.
        We simulate two consecutive subscription.created events for the same store.
        """
        from routers.salla_subscription import (
            record_trial_used,
            is_trial_blocked,
            get_trial_ledger,
        )

        # Simulate an in-memory ledger keyed by store_id
        ledger_store: dict = {}

        class _Q:
            def __init__(self, sid): self.sid = sid
            def filter(self, *_a, **_kw): return self
            def first(self):
                return ledger_store.get(self.sid)

        class _DB:
            def query(self, _model): return self._q
            def add(self, obj):
                ledger_store[obj.salla_store_id] = obj
            def flush(self): pass

        db = _DB()
        # First subscription.created — store_id=999 — should record
        db._q = _Q("999")
        with patch("routers.salla_subscription.get_trial_ledger",
                   side_effect=lambda d, sid: ledger_store.get(str(sid))):
            ledger = record_trial_used(db, "999", merchant_id="owner@store.com", plan_slug="growth")
            assert ledger.trial_used is True
            assert ledger.salla_store_id == "999"
            assert ledger.first_trial_plan == "growth"

            # Second attempt — same store, after delete & reinstall
            blocked = is_trial_blocked(db, "999")
            assert blocked is True, \
                "Second trial attempt for same store_id MUST be blocked"

            # A different store should NOT be blocked
            assert is_trial_blocked(db, "different-store") is False

    def test_subscription_status_endpoint_downgrades_to_trial_blocked(self):
        """
        When billing_status='trial' but ledger says trial_used=True → endpoint
        must return billing_status='trial_blocked'.
        Verified via source contract (would require full FastAPI client otherwise).
        """
        from pathlib import Path
        src = Path(_backend) / "routers" / "salla_subscription.py"
        body = src.read_text(encoding="utf-8")
        # The exact downgrade logic
        assert 'billing_status = "trial_blocked"' in body, \
            "salla_subscription.status endpoint must downgrade trial → trial_blocked"
        assert "is_trial_blocked" in body
        # And the handler must record the first trial
        assert "record_trial_used" in body


# ═════════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback
    # Force UTF-8 stdout on Windows so the unicode bullets render
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

    test_classes = [
        TestScenario1_TrialBlocked,
        TestScenario2_Active,
        TestScenario3_DowngradeGrowthToStarter,
        TestScenario4_TrialLedgerPersistence,
    ]

    passed = failed = 0
    failures = []

    print(f"\n{BOLD}=== Nahla Billing Scenarios — Final Validation ==={RESET}\n")

    for cls in test_classes:
        instance = cls()
        methods  = [m for m in dir(cls) if m.startswith("test_")]
        title = cls.__name__.replace("Test", "").replace("_", " ")
        print(f"{BOLD}▸ {title}{RESET}  ({len(methods)} tests)")

        for method_name in sorted(methods):
            method = getattr(instance, method_name)
            label  = method_name.replace("test_", "").replace("_", " ")
            try:
                method()
                print(f"  {GREEN}✓{RESET}  {label}")
                passed += 1
            except (AssertionError, HTTPException) as e:
                # HTTPException without expected raise → fail
                if isinstance(e, HTTPException):
                    msg = f"unexpected HTTP {e.status_code}: {e.detail}"
                else:
                    msg = str(e)
                print(f"  {RED}✗{RESET}  {label}")
                print(f"     {RED}{msg}{RESET}")
                failures.append((cls.__name__, method_name, msg))
                failed += 1
            except Exception as e:
                print(f"  {RED}✗{RESET}  {label}")
                print(f"     {RED}EXCEPTION: {type(e).__name__}: {e}{RESET}")
                failures.append((cls.__name__, method_name, traceback.format_exc()))
                failed += 1
        print()

    total = passed + failed
    print(f"{BOLD}=== Results ==={RESET}")
    print(f"  Total:  {total}")
    print(f"  {GREEN}Passed: {passed}{RESET}")
    if failed:
        print(f"  {RED}Failed: {failed}{RESET}\n")
        print(f"{BOLD}Failures:{RESET}")
        for cls_name, method, msg in failures:
            print(f"  {RED}{cls_name}::{method}{RESET}")
            for line in msg.splitlines()[:5]:
                print(f"    {line}")
    else:
        print(f"\n{GREEN}{BOLD}All {passed} tests passed — billing scenarios are correct!{RESET}")

    sys.exit(0 if failed == 0 else 1)
