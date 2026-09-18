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
from typing import Any, Callable, Dict, Iterator, List, Optional
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
    fallback = ev.FALLBACK_NONE
    if any(o in RECOVERED_OUTCOMES for o in outcomes):
        fallback = ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY
    persisted_outbound = [
        {
            "tenant_id": r.get("tenant_id"),
            "conversation_id": r.get("conversation_id"),
            "reply_owner": (r.get("extra_metadata") or {}).get("reply_owner"),
            "body_len": len(str(r.get("body") or "")),
        }
        for r in persisted_rows if r.get("direction") == "outbound"
    ]
    return ev.TurnEvidence(
        tenant_ids_touched=tenants,
        accepted_wamids=accepted,
        send_attempts=list(script.responses),
        terminal=terminal,
        terminal_source=source,
        persisted_outbound=persisted_outbound,
        evidence_refs=[f"lifecycle:{e.name}" for e in getattr(trace, "events", [])],
        guardrail_results=[],
        fallback_kind=fallback,
        fixture_bindings=["provider:scripted_httpx", "brain:scripted"],
        notes={
            "final_token": getattr(trace, "final_token", None),
            "provider_types": list(harness_evidence.provider.types),
            "outcomes": outcomes,
        },
    )


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
