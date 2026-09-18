"""Shared helpers for the runtime tests.

Everything here either loads real application code unchanged or turns
observed facts into the structured :class:`TurnEvidence` the evaluator
judges. Nothing re-implements runtime behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from unittest.mock import MagicMock, patch

from tests.commerce_reliability import reliability_evaluator as ev

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
PR1084_HARNESS_PATH = BACKEND_DIR / "tests" / "test_customer_product_silence_delivery_recovery.py"
PR1084_HARNESS_MODULE = "nahla_pr1084_delivery_harness"

TENANT_A = 1
TENANT_B = 2
CUSTOMER_PHONE = "966500000001"

# Guarded-text outcomes that the runtime records as *recovered* deliveries.
RECOVERED_OUTCOMES = frozenset({"rich_rejected_text_recovered", "suppressed_text_recovered"})

# Provenance vocabulary the runtime actually writes into outbound metadata
# (core/product_reply_recovery.fallback_provenance_metadata, the persona
# composer, and the Commerce Agent V2 owner path). Anything outside these
# sets is UNKNOWN provenance and is never reported as "no fallback".
MODEL_OWNED_COMPOSE_SOURCES = frozenset({"persona_llm", "llm"})
MODEL_OWNED_TEXT_SOURCES = frozenset({"llm", "persona_llm", "llm_postprocess"})
DETERMINISTIC_FALLBACK_SOURCE = "fallback_deterministic"
# Deterministic text produced because the model/runtime failed: a runtime
# fallback, never a compliant answer.
RUNTIME_FAILURE_FALLBACK_REASONS = frozenset({
    "timeout", "compose_empty", "compose_exception", "empty_llm", "error", "call_error",
    "route_unconfigured", "compose_disabled", "owner_runtime_error",
})
# Deterministic text produced by a policy guard that withholds an unknown claim.
SAFE_POLICY_FALLBACK_REASONS = frozenset({"merchant_policy_unknown_claim"})
# Deterministic grounded text produced by the PR #1084 delivery-recovery path.
DELIVERY_RECOVERY_TRIGGERS = frozenset({
    "interactive_rejected", "pre_provider_suppression", "rich_presentation_undeliverable",
})


def ensure_sys_path() -> None:
    for entry in reversed([str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]):
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    import observability  # noqa: F401,PLC0415
    import observability.event_logger  # noqa: F401,PLC0415


def load_pr1084_harness():
    """Import PR #1084's incident harness by path, unchanged.

    The module's content hash is pinned in the reviewed manifest, so any edit
    to the harness is a gate blocker until the manifest is re-reviewed.
    """
    mod = sys.modules.get(PR1084_HARNESS_MODULE)
    if mod is not None:
        return mod
    ensure_sys_path()
    spec = importlib.util.spec_from_file_location(PR1084_HARNESS_MODULE, PR1084_HARNESS_PATH)
    assert spec is not None and spec.loader is not None, PR1084_HARNESS_PATH
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PR1084_HARNESS_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Provider scripting with response recording ──────────────────────────────


class RecordingScript:
    """Wrap a PR #1084 provider script and record what the provider answered."""

    def __init__(self, base: Callable[[Dict[str, Any], int], Any]) -> None:
        self.base = base
        self.responses: List[Dict[str, Any]] = []

    def __call__(self, payload: Dict[str, Any], n: int):
        record: Dict[str, Any] = {"n": n, "type": payload.get("type"), "status": None, "wamid": None, "error": None}
        try:
            resp = self.base(payload, n)
        except Exception as exc:  # noqa: BLE001 — recorded, then re-raised for the runtime
            record["error"] = type(exc).__name__
            self.responses.append(record)
            raise
        record["status"] = int(getattr(resp, "status_code", 0) or 0)
        if record["status"] == 200:
            messages = (resp.json() or {}).get("messages") or []
            if messages and isinstance(messages[0], dict):
                record["wamid"] = messages[0].get("id")
        self.responses.append(record)
        return resp

    def accepted_wamids(self) -> List[str]:
        return [str(r["wamid"]) for r in self.responses if r.get("wamid")]


@contextmanager
def capture_persistence(harness_evidence) -> Iterator[List[Dict[str, Any]]]:
    """Record tenant/conversation of every persisted row (inner patch)."""
    rows: List[Dict[str, Any]] = []

    def _save(_db, phone, body, direction, *args, **kwargs):
        conversation_id = kwargs.get("conversation_id", args[0] if len(args) > 0 else None)
        tenant_id = kwargs.get("tenant_id", args[1] if len(args) > 1 else None)
        extra = dict(kwargs.get("extra_metadata") or {})
        harness_evidence.saved.append({
            "phone": phone, "body": body, "direction": direction, "extra_metadata": extra,
        })
        rows.append({
            "phone": phone, "body": body, "direction": direction, "tenant_id": tenant_id,
            "conversation_id": conversation_id, "extra_metadata": extra,
        })
        return len(harness_evidence.saved)

    with patch("routers.whatsapp_webhook.StateManager.save_message", side_effect=_save):
        yield rows


def classify_row_provenance(meta: Mapping[str, Any]) -> Tuple[str, str]:
    """(provenance_class, reason) for one persisted outbound row.

    provenance_class is ``model``, ``deterministic_fallback`` or ``unknown``.
    """
    compose_source = str(meta.get("compose_source") or "").strip()
    text_source = str(meta.get("final_customer_text_source") or "").strip()
    recovery = meta.get("delivery_recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    reason = str(meta.get("fallback_reason") or recovery.get("trigger") or "").strip()
    if str(meta.get("reply_owner") or "") == "commerce_agent_v2":
        status = str(meta.get("v2_status") or "").strip()
        if status == "completed":
            return "model", ""
        if status == "failed":
            return "deterministic_fallback", "owner_runtime_error"
        return "unknown", status or "v2_status_missing"
    deterministic = (
        DETERMINISTIC_FALLBACK_SOURCE in (compose_source, text_source, str(recovery.get("text_source") or ""))
        or bool(meta.get("fallback_action_type"))
    )
    if deterministic:
        return "deterministic_fallback", reason or "unspecified"
    if compose_source in MODEL_OWNED_COMPOSE_SOURCES or text_source in MODEL_OWNED_TEXT_SOURCES:
        return "model", ""
    return "unknown", compose_source or text_source or "no_provenance"


def classify_fallback_provenance(
    outbound_meta: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    """Fallback kind for a turn from persisted provenance + recovery outcomes.

    Fail closed: no rows and no outcomes, any row with unknown provenance, or
    a deterministic fallback with an unrecognised reason is UNKNOWN
    provenance, never ``none``.
    """
    rows = [classify_row_provenance(m) for m in outbound_meta]
    outcome_names = [str(o.get("product_reply_outcome") or "") for o in outcomes if isinstance(o, Mapping)]
    detail: Dict[str, Any] = {"rows": rows, "outcomes": outcome_names}
    if not rows and not outcome_names:
        return ev.FALLBACK_UNKNOWN_PROVENANCE, detail
    if any(cls == "unknown" for cls, _ in rows):
        return ev.FALLBACK_UNKNOWN_PROVENANCE, detail
    fallback_reasons = [reason for cls, reason in rows if cls == "deterministic_fallback"]
    if any(r in RUNTIME_FAILURE_FALLBACK_REASONS for r in fallback_reasons):
        return ev.FALLBACK_UNEXPECTED_RUNTIME, detail
    if any(r in SAFE_POLICY_FALLBACK_REASONS for r in fallback_reasons):
        return ev.FALLBACK_EXPECTED_SAFE, detail
    if any(r not in DELIVERY_RECOVERY_TRIGGERS for r in fallback_reasons):
        return ev.FALLBACK_UNKNOWN_PROVENANCE, detail
    if fallback_reasons or any(o in RECOVERED_OUTCOMES for o in outcome_names):
        return ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY, detail
    return ev.FALLBACK_NONE, detail


# Dimensions the delivery characterisation does NOT evaluate. Mocked
# persistence is not durable delivery recording; fixture labels are not
# knowledge-binding validation; the PR #1084 seam exposes no guardrail
# execution records; no order side effects are exercised.
DELIVERY_NOT_EVALUATED = (
    ev.DIMENSION_SEMANTIC_BINDING,
    ev.DIMENSION_GUARDRAIL_EXECUTION,
    ev.DIMENSION_DURABLE_DELIVERY_RECORD,
    ev.DIMENSION_ORDER_IDEMPOTENCY,
    ev.DIMENSION_CONVERSATION_SERIALIZATION,
)


def build_turn_evidence(
    harness_evidence,
    trace,
    script: RecordingScript,
    persisted_rows: List[Dict[str, Any]],
    *,
    human_handoff: bool = False,
) -> ev.TurnEvidence:
    """Turn the PR #1084 harness observations into evaluator evidence."""
    accepted = script.accepted_wamids()
    terminal, source = ev.derive_terminal(
        getattr(trace, "final_token", None) or None, accepted, human_handoff=human_handoff,
    )
    tenants: List[int] = []
    for stamp in harness_evidence.stamps:
        if stamp.get("tenant_id") is not None:
            tenants.append(int(stamp["tenant_id"]))
    for attempt in harness_evidence.wire_attempts:
        if attempt.get("tenant_id") is not None:
            tenants.append(int(attempt["tenant_id"]))
    for row in persisted_rows:
        if row.get("tenant_id") is not None:
            tenants.append(int(row["tenant_id"]))
    outcomes = [str(o.get("product_reply_outcome") or "") for o in harness_evidence.outcomes]
    outbound_rows = [r for r in persisted_rows if r.get("direction") == "outbound"]
    fallback, provenance = classify_fallback_provenance(
        [dict(r.get("extra_metadata") or {}) for r in outbound_rows], list(harness_evidence.outcomes),
    )
    persisted_outbound = [
        {
            "tenant_id": r.get("tenant_id"),
            "conversation_id": r.get("conversation_id"),
            "reply_owner": (r.get("extra_metadata") or {}).get("reply_owner"),
            "compose_source": (r.get("extra_metadata") or {}).get("compose_source"),
            "provenance": classify_row_provenance(r.get("extra_metadata") or {}),
            "body_len": len(str(r.get("body") or "")),
        }
        for r in outbound_rows
    ]
    return ev.TurnEvidence(
        tenant_ids_touched=tenants,
        accepted_wamids=accepted,
        send_attempts=list(script.responses),
        terminal=terminal,
        terminal_source=source,
        persisted_outbound=persisted_outbound,
        evidence_refs=[f"lifecycle:{e.name}" for e in getattr(trace, "events", [])],
        guardrail_results=[],  # the merchant-handler seam exposes no guardrail execution records
        fallback_kind=fallback,
        fixture_bindings=["provider:scripted_httpx", "brain:scripted"],
        transport_outcome=ev.derive_transport_outcome(list(script.responses)),
        not_evaluated=list(DELIVERY_NOT_EVALUATED),
        notes={
            "final_token": getattr(trace, "final_token", None),
            "provider_types": list(harness_evidence.provider.types),
            "outcomes": outcomes,
            "provenance": provenance,
        },
    )


# ── Baseline-defect predicates (pure; adversarially self-tested) ─────────────
#
# Each returns the recorded signature only when the observation matches the
# recorded defect exactly; any other observation is a changed cause that must
# fail on its own instead of being absorbed by the allowance.

UC01_EXPECTED_BLOCKERS = frozenset({
    "missing_terminal:lifecycle:end_ok:inferred_without_provider_acceptance",
    "fallback_provenance_unknown",
})


def classify_uc01_observation(
    *,
    blockers: Sequence[str],
    final_token: Optional[str],
    accepted_wamids: Sequence[str],
    persisted_outbound_count: int,
    outcome_count: int,
    transport_outcome: str,
) -> str:
    """``recorded_defect`` | ``passes`` | ``changed_cause:<detail>``."""
    if not blockers:
        return "passes"
    unrelated = sorted(set(blockers) - UC01_EXPECTED_BLOCKERS)
    if unrelated:
        return "changed_cause:unrelated_blockers=" + ",".join(unrelated)
    if set(blockers) != UC01_EXPECTED_BLOCKERS:
        return "changed_cause:partial_signature=" + ",".join(sorted(blockers))
    if final_token != ev.LIFECYCLE_END_OK:
        return f"changed_cause:final_token={final_token}"
    if list(accepted_wamids):
        return "changed_cause:provider_accepted"
    if transport_outcome != ev.TRANSPORT_REJECTED_DEFINITIVE:
        return f"changed_cause:transport_outcome={transport_outcome}"
    if persisted_outbound_count or outcome_count:
        return f"changed_cause:records_present={persisted_outbound_count},{outcome_count}"
    return "recorded_defect"


def classify_state_save_outcome(
    *,
    worker_status: Mapping[str, str],
    persisted: Mapping[str, Any],
    expected: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> str:
    """``both_persisted`` | ``single_lost_update`` | ``neither_persisted`` |
    ``worker_failed:<field>`` | ``value_mismatch:<field>``.

    A field counts as persisted only when it equals the value its worker
    wrote, and as lost only when it still equals the pre-race ``baseline``
    value (the stale state the losing worker overwrote it with). Any other
    value is a changed cause.
    """
    for field_name in expected:
        if worker_status.get(field_name) != "saved":
            return f"worker_failed:{field_name}"
    kept: Dict[str, bool] = {}
    mismatched: List[str] = []
    for name, value in expected.items():
        observed = persisted.get(name)
        if observed == value:
            kept[name] = True
        elif observed == baseline.get(name):
            kept[name] = False
        else:
            mismatched.append(name)
    if mismatched:
        return "value_mismatch:" + ",".join(sorted(mismatched))
    kept_count = sum(1 for ok in kept.values() if ok)
    if kept_count == len(expected):
        return "both_persisted"
    if kept_count == 0:
        return "neither_persisted"
    return "single_lost_update"


RB05_RECORDED_ACTION = "llm_reply"
RB05_RECORDED_REASON_FRAGMENT = "active order"


def classify_lock_decision(
    *,
    locked: bool,
    action: str,
    reason: str,
    interpreter_result: Any,
    control_action: str,
) -> str:
    """``recorded_defect`` | ``passes`` | ``changed_cause:<detail>``."""
    if action == "search_products":
        return "passes"
    if not locked:
        return "changed_cause:session_not_locked"
    if control_action != "search_products":
        return f"changed_cause:unlocked_control_action={control_action}"
    if action != RB05_RECORDED_ACTION:
        return f"changed_cause:action={action}"
    if RB05_RECORDED_REASON_FRAGMENT not in str(reason or ""):
        return f"changed_cause:reason={str(reason or '')[:80]}"
    if interpreter_result is not None:
        return "changed_cause:interpreter_not_gated"
    return "recorded_defect"


# ── Catalog data shared by the sqlite and PostgreSQL browse tests ───────────


def catalog_rows() -> List[Dict[str, Any]]:
    """Tenant A catalogue: one skirt (singular title) among other products."""
    return [
        {"external_id": "p-11", "title": "فستان سهرة أزرق", "price": "250", "category": "فساتين"},
        {"external_id": "p-12", "title": "فستان صيفي", "price": "300", "category": "فساتين"},
        {"external_id": "p-13", "title": "جاكيت شتوي", "price": "400", "category": "جواكيت"},
        {"external_id": "p-14", "title": "تنورة قطنية سوداء", "price": "120", "category": "تنانير"},
        {"external_id": "p-15", "title": "حذاء رياضي أبيض", "price": "249", "category": "أحذية"},
        {"external_id": "p-16", "title": "عطر ورد 100ml", "price": "199", "category": "عطور"},
    ]


def foreign_tenant_rows() -> List[Dict[str, Any]]:
    """Tenant B catalogue: a skirt that must never leak into tenant A results."""
    return [
        {"external_id": "x-1", "title": "تنورة جلد", "price": "180", "category": "تنانير"},
    ]


@dataclass
class CatalogFixture:
    db: Any
    engine: Any
    tenant_a: int
    tenant_b: int
    product_ids: Dict[str, int] = field(default_factory=dict)
    close: Callable[[], None] = lambda: None


def unique_store_name(label: str = "متجر تجريبي") -> str:
    """Tenant names are unique on PostgreSQL; one disposable database serves many tests."""
    return f"{label} {uuid.uuid4().hex[:8]}"


def seed_catalog(db, M) -> CatalogFixture:
    tenant_a = M.Tenant(name=unique_store_name("متجر تجريبي عام"), is_active=True)
    tenant_b = M.Tenant(name=unique_store_name("متجر آخر"), is_active=True)
    db.add_all([tenant_a, tenant_b])
    db.commit()
    ids: Dict[str, int] = {}
    for tenant, rows in ((tenant_a, catalog_rows()), (tenant_b, foreign_tenant_rows())):
        for row in rows:
            product = M.Product(
                tenant_id=tenant.id, external_id=row["external_id"], title=row["title"],
                price=row["price"], in_stock=True, stock_quantity=5,
                extra_metadata={"status": "active", "category": row["category"]},
                catalog_status="active", source="salla",
            )
            db.add(product)
            db.flush()
            ids[row["external_id"]] = int(product.id)
    db.commit()
    return CatalogFixture(db=db, engine=None, tenant_a=int(tenant_a.id), tenant_b=int(tenant_b.id), product_ids=ids)


def build_sqlite_catalog() -> CatalogFixture:
    """In-memory sqlite copy of the schema (JSONB columns swapped to JSON)."""
    ensure_sys_path()
    from sqlalchemy import JSON, create_engine  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import JSONB  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415

    import models as M  # noqa: PLC0415

    swapped = []
    for table in M.Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                swapped.append((col, col.type))
                col.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        M.Base.metadata.create_all(engine)
    finally:
        for col, original in swapped:
            col.type = original
    db = sessionmaker(bind=engine)()
    fixture = seed_catalog(db, M)
    fixture.engine = engine

    def _close() -> None:
        db.close()
        engine.dispose()

    fixture.close = _close
    return fixture


# ── Brain context construction (real types, scripted model) ─────────────────


def shown_candidates() -> List[Dict[str, Any]]:
    """Three candidates exactly as V1 keeps them in ``last_search_candidates``."""
    return [
        {"id": 11, "external_id": "p-11", "title": "فستان سهرة أزرق", "price": "250", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 4, "status": "active",
         "product_url": "https://example.test/p/11", "image_url": "https://example.test/i/11.jpg", "tenant_id": TENANT_A},
        {"id": 12, "external_id": "p-12", "title": "فستان صيفي", "price": "300", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 2, "status": "active",
         "product_url": "https://example.test/p/12", "image_url": "https://example.test/i/12.jpg", "tenant_id": TENANT_A},
        {"id": 13, "external_id": "p-13", "title": "جاكيت شتوي", "price": "400", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 1, "status": "active",
         "product_url": "https://example.test/p/13", "image_url": "https://example.test/i/13.jpg", "tenant_id": TENANT_A},
    ]


def catalog_pool() -> List[Dict[str, Any]]:
    """The shown candidates plus three products the customer has not seen."""
    extra = [
        {"id": 14, "external_id": "p-14", "title": "تنورة قطنية سوداء", "price": "120", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 5, "status": "active", "tenant_id": TENANT_A},
        {"id": 15, "external_id": "p-15", "title": "حذاء رياضي أبيض", "price": "249", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 5, "status": "active", "tenant_id": TENANT_A},
        {"id": 16, "external_id": "p-16", "title": "عطر ورد 100ml", "price": "199", "in_stock": True,
         "can_checkout": True, "orderable": True, "stock_qty": 5, "status": "active", "tenant_id": TENANT_A},
    ]
    return shown_candidates() + extra


def commerce_facts(rows: Optional[List[Dict[str, Any]]] = None):
    from modules.ai.brain.types import CommerceFacts  # noqa: PLC0415

    rows = rows or catalog_pool()
    return CommerceFacts(
        has_products=True, product_count=len(rows), in_stock_count=len(rows),
        has_active_integration=True, orderable=True, snapshot_fresh=True,
        store_name="متجر تجريبي عام", top_products=rows, discovery_products=rows,
    )


def brain_context(message: str, *, intent=None, state=None, facts=None, profile=None, tenant_id: int = TENANT_A):
    """A real BrainContext with a mocked DB handle (no database access)."""
    from modules.ai.brain.intent import rules  # noqa: PLC0415
    from modules.ai.brain.types import BrainContext, Intent, MerchantConversationState  # noqa: PLC0415

    resolved_intent = intent or rules.match(message) or Intent(name="general", confidence=0.5, raw_message=message)
    ctx = BrainContext(
        tenant_id=tenant_id, customer_phone=CUSTOMER_PHONE, conversation_id=9001, message=message,
        intent=resolved_intent,
        state=state or MerchantConversationState(greeted=True, stage="browsing", turn=3),
        facts=facts or commerce_facts(),
        history=[
            {"direction": "inbound", "body": "وش منتجاتكم؟"},
            {"direction": "outbound", "body": "عندنا فساتين وجاكيت"},
        ],
        profile=profile or {"inbound_metadata": {}},
        customer_id=7,
    )
    ctx._db = MagicMock()
    return ctx


class ScriptedLLMProvider:
    """Stands in for ``modules.ai.orchestrator.providers.registry.get_provider``."""

    provider_name = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: List[Any] = []

    def is_configured(self) -> bool:
        return True

    def call(self, message, prompt, *args, **kwargs):
        self.calls.append((message, prompt))
        return {"reply_text": self.reply, "actions": [], "usage": {}}


@contextmanager
def scripted_model(reply: str) -> Iterator[ScriptedLLMProvider]:
    provider = ScriptedLLMProvider(reply)
    with patch("modules.ai.orchestrator.providers.registry.get_provider", return_value=provider), \
         patch("modules.ai.brain.intent.slot_extractor._resolve_slot_model", return_value="scripted", create=True):
        yield provider


def no_products_templates() -> List[str]:
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    return [T.no_products(variant=i) for i in range(3)]
