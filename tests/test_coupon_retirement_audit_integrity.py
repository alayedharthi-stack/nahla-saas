"""Audit persistence contract using in-memory fakes, no database or provider."""
from copy import deepcopy
import ast
import json
from pathlib import Path

import pytest

from services.coupon_identity import CouponIdentityConflict
from services.coupon_retirement import _persist_audit
from scripts.operators import coupon_retirement_schema as schema


VALUES = dict(rid=42, cid=41, tid=7, iid=9, store="202", pid="101", code="coat20",
              audit=json.dumps({"before_digest": "reviewed", "after": {"42": {"active": False}}}))
STORED = dict(retired_id=42, canonical_id=41, tenant_id=7, integration_id=9,
              store_id="202", provider_id="101", code_key="coat20", audit=json.loads(VALUES["audit"]))


class Result:
    def __init__(self, value): self.value = value
    def scalar_one_or_none(self): return self.value
    def mappings(self): return self
    def one_or_none(self): return self.value


class Database:
    def __init__(self, receipt=42, stored=None):
        self.results = [Result(receipt), Result(stored)]
        self.calls = []
    def execute(self, statement, parameters):
        self.calls.append((str(statement), deepcopy(parameters)))
        return self.results.pop(0)


def test_exact_write_receipt_and_persisted_record_are_required():
    db = Database(stored=deepcopy(STORED))
    _persist_audit(db, deepcopy(VALUES))
    assert len(db.calls) == 2
    assert "RETURNING retired_id" in db.calls[0][0]
    assert "FOR SHARE" in db.calls[1][0]
    assert db.calls[1][1] == {"rid": 42}


@pytest.mark.parametrize("receipt", [None, 0, 43])
def test_absent_or_wrong_write_receipt_refuses_success(receipt):
    db = Database(receipt=receipt, stored=deepcopy(STORED))
    with pytest.raises(CouponIdentityConflict, match="repair_audit_not_persisted"):
        _persist_audit(db, deepcopy(VALUES))
    assert len(db.calls) == 1


@pytest.mark.parametrize("field", [None, "retired_id", "canonical_id", "tenant_id", "integration_id",
                                   "store_id", "provider_id", "code_key", "audit"])
def test_receipt_without_exact_persisted_fence_refuses_success(field):
    stored = deepcopy(STORED)
    if field is None:
        stored = None
    elif field == "audit":
        stored[field]["after"]["42"]["active"] = True
    else:
        stored[field] = "different"
    with pytest.raises(CouponIdentityConflict, match="repair_audit_not_persisted"):
        _persist_audit(Database(stored=stored), deepcopy(VALUES))


def test_reconciliation_changes_and_audit_check_share_one_savepoint():
    from services import coupon_retirement as module
    tree = ast.parse(Path(module.__file__).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "reconcile_duplicate")
    savepoint = next(node for node in function.body if isinstance(node, ast.With))
    assert isinstance(savepoint.items[0].context_expr, ast.Call)
    assert savepoint.items[0].context_expr.func.attr == "begin_nested"
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_persist_audit"
               for node in ast.walk(savepoint))
    assignments = [node for node in ast.walk(savepoint) if isinstance(node, ast.Assign)]
    assert any(isinstance(target, ast.Attribute) and target.attr == "extra_metadata"
               for assignment in assignments for target in assignment.targets)
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"commit", "rollback"}
                   for node in ast.walk(function))


@pytest.mark.parametrize("state", ["pre", "post"])
def test_closed_expectations_reject_any_unexpected_rule_observation(state):
    root = Path(__file__).resolve().parents[1]
    expected = json.loads((root/"scripts/operators/salla_coupon_schema_expectations.json").read_text())[state]
    assert expected["rules"] == []
    changed = deepcopy(expected)
    # Catalog-shaped data only; never execute a rule or mutate any database.
    changed["rules"] = [["public", "coupon_identity_retirements", "unexpected", "O", "unapproved definition"]]
    assert not schema.matches(changed, expected)
