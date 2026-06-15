#!/usr/bin/env python3
"""PR-B operational evidence — API responses, multi-branch, runtime isolation.

Run from repo root:
    python scripts/_pr_b_evidence_capture.py

Outputs JSON + markdown under docs/evidence/pr-b/
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Generator, List

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
_DATABASE = _REPO / "database"
_OUT = _REPO / "docs" / "evidence" / "pr-b"
for _p in (_BACKEND, _DATABASE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")
os.environ["NAHLA_ALLOW_RUNTIME_TENANT_AUTO_CREATE"] = "1"
os.environ["USE_STRUCTURED_BRANCH_CONTACTS"] = "0"

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import JSON, create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import get_db  # noqa: E402
from models import Base, Tenant  # noqa: E402
from routers.operations_center import router as operations_router  # noqa: E402

TENANT_ID = 99


def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return engine


def _build_client() -> tuple[TestClient, Session]:
    engine = _make_engine()
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    db.add(Tenant(id=TENANT_ID, name="Evidence Tenant", is_active=True))
    db.commit()

    def _override_db() -> Generator[Session, None, None]:
        try:
            yield db
        finally:
            pass

    app = FastAPI(title="PR-B Evidence API")
    app.include_router(operations_router)

    @app.middleware("http")
    async def _tenant_scope(request: Request, call_next):  # noqa: ANN001
        request.state.jwt_payload = {
            "tenant_id": TENANT_ID,
            "role": "merchant",
            "sub": "evidence@test.nahla.ai",
        }
        request.state.tenant_id = TENANT_ID
        return await call_next(request)

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app), db


def _branch_payload(city: str, district: str, maps_q: str) -> Dict[str, Any]:
    return {
        "name": f"فرع {city}",
        "city": city,
        "district": district,
        "address": f"شارع تجريبي، {district}، {city}",
        "maps_url": f"https://maps.google.com/?q={maps_q}",
        "hours_json": {"sat": "9-22", "sun": "9-22"},
        "is_active": True,
        "sort_order": 0,
    }


def capture_api_evidence(client: TestClient) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {"tenant_id": TENANT_ID, "calls": []}

    def record(method: str, path: str, body: Any | None, resp) -> Any:  # noqa: ANN001
        entry = {
            "method": method,
            "path": path,
            "request_body": body,
            "status_code": resp.status_code,
            "response": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        }
        evidence["calls"].append(entry)
        return entry["response"]

    # ── Multi-branch seed ────────────────────────────────────────────────
    branches_spec = [
        ("الرياض", "العليا", "riyadh-showroom"),
        ("جدة", "الروضة", "jeddah-showroom"),
        ("الطائف", "الشفا", "taif-showroom"),
    ]
    branch_ids: List[int] = []
    for city, district, maps_q in branches_spec:
        payload = _branch_payload(city, district, maps_q)
        resp = client.post("/operations-center/branches", json=payload)
        created = record("POST", "/operations-center/branches", payload, resp)
        branch_ids.append(int(created["id"]))

    # Contacts + default reception per branch
    contacts_spec = [
        (0, "استقبال الرياض", "reception", "0501111111", True),
        (0, "مدير الرياض", "admin", "0501111112", False),
        (1, "استقبال جدة", "reception", "0502222221", True),
        (1, "بائع جدة", "showroom", "0502222222", False),
        (2, "استقبال الطائف", "reception", "0503333331", True),
        (2, "خدمة الطائف", "customer_service", "0503333332", False),
    ]
    for bidx, name, role, phone, is_default in contacts_spec:
        bid = branch_ids[bidx]
        contact = record(
            "POST",
            f"/operations-center/branches/{bid}/contacts",
            {
                "display_name": name,
                "role": role,
                "phone_e164": phone,
                "is_default_reception": is_default,
            },
            client.post(
                f"/operations-center/branches/{bid}/contacts",
                json={
                    "display_name": name,
                    "role": role,
                    "phone_e164": phone,
                    "is_default_reception": is_default,
                },
            ),
        )
        if is_default:
            record(
                "POST",
                f"/operations-center/branches/{bid}/contacts/{contact['id']}/set-default-reception",
                None,
                client.post(
                    f"/operations-center/branches/{bid}/contacts/{contact['id']}/set-default-reception",
                ),
            )

    # Escalation chains — independent per branch
    escalation_spec = [
        (0, [("بائع الرياض", "showroom", "0504111111"), ("إدارة الرياض", "admin", "0504111112")]),
        (1, [("بائع جدة", "showroom", "0504222221"), ("CS جدة", "customer_service", "0504222222")]),
        (2, [("بائع الطائف", "showroom", "0504333331"), ("إدارة الطائف", "admin", "0504333332")]),
    ]
    for bidx, levels in escalation_spec:
        bid = branch_ids[bidx]
        for level, (name, role, phone) in enumerate(levels, start=1):
            record(
                "POST",
                f"/operations-center/branches/{bid}/escalation-steps",
                {
                    "escalation_level": level,
                    "display_name": name,
                    "role": role,
                    "phone_e164": phone,
                },
                client.post(
                    f"/operations-center/branches/{bid}/escalation-steps",
                    json={
                        "escalation_level": level,
                        "display_name": name,
                        "role": role,
                        "phone_e164": phone,
                    },
                ),
            )

    # Deactivate Taif to prove soft-disable
    record(
        "POST",
        f"/operations-center/branches/{branch_ids[2]}/deactivate",
        None,
        client.post(f"/operations-center/branches/{branch_ids[2]}/deactivate"),
    )

    # GET list (primary evidence)
    list_resp = record(
        "GET",
        "/operations-center/branches",
        None,
        client.get("/operations-center/branches"),
    )
    evidence["multi_branch_summary"] = []
    for row in list_resp["branches"]:
        bid = row["id"]
        contacts = client.get(f"/operations-center/branches/{bid}/contacts").json()
        steps = client.get(f"/operations-center/branches/{bid}/escalation-steps").json()
        default_rec = [
            c for c in contacts.get("contacts", [])
            if c.get("is_default_reception")
        ]
        evidence["multi_branch_summary"].append({
            "branch": row["name"],
            "city": row["city"],
            "maps_url": row["maps_url"],
            "is_active": row["is_active"],
            "default_reception": default_rec[0] if default_rec else None,
            "escalation_levels": [
                {"level": s["escalation_level"], "name": s["display_name"], "phone": s["phone_e164"]}
                for s in steps.get("steps", [])
            ],
        })

    evidence["branch_ids"] = branch_ids
    return evidence


def capture_runtime_isolation(db: Session) -> Dict[str, Any]:
    """Prove USE_STRUCTURED_BRANCH_CONTACTS=0 keeps KB paths active."""
    os.environ["USE_STRUCTURED_BRANCH_CONTACTS"] = "0"
    results: Dict[str, Any] = {"flag": "USE_STRUCTURED_BRANCH_CONTACTS=0", "checks": []}

    from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
        lookup_structured_maps_url,
        load_structured_staff_contact_registry,
        resolve_reception_contact,
        structured_branch_contacts_enabled,
    )
    from modules.operations.branch_escalation_evidence import (  # noqa: PLC0415
        load_structured_escalation_chain,
    )

    flag_off = not structured_branch_contacts_enabled()
    results["checks"].append({
        "module": "branch_contact_evidence.structured_branch_contacts_enabled()",
        "pass": flag_off,
        "value": structured_branch_contacts_enabled(),
    })

    maps_url, src, _bid = lookup_structured_maps_url(db, TENANT_ID)
    results["checks"].append({
        "module": "location_link_policy / lookup_structured_maps_url",
        "pass": maps_url == "" and src == "none",
        "structured_url": maps_url,
        "structured_source": src,
        "kb_path": "safety_nets._lookup_tenant_maps_url continues to snapshot/KB when flag OFF",
    })

    reception = resolve_reception_contact(db, TENANT_ID)
    results["checks"].append({
        "module": "arrival_contact_delivery / resolve_reception_contact",
        "pass": reception is None,
        "structured_reception": reception,
        "kb_path": "resolve_arrival_contact_evidence falls through to arrival_contact_compile_v0",
    })

    registry = load_structured_staff_contact_registry(db, TENANT_ID)
    results["checks"].append({
        "module": "staff_contact_registry / load_structured_staff_contact_registry",
        "pass": registry is None,
        "structured_registry": registry,
    })

    chain = load_structured_escalation_chain(db, TENANT_ID)
    results["checks"].append({
        "module": "escalation_chain / load_structured_escalation_chain",
        "pass": not chain,
        "structured_chain_len": len(chain or ()),
    })

    # KB registry still works (existing PR-A test pattern)
    from modules.ai.brain.commerce.staff_contact_evidence import load_staff_contact_registry  # noqa: PLC0415

    class _Section:
        id = 1
        kind = "branches"
        body = "بائع المعرض: 966555555555"
        title = ""
        metadata = {}
        metadata_json = {}

    class _Q:
        def filter(self, *a: Any, **k: Any) -> "_Q":
            return self

        def order_by(self, *a: Any, **k: Any) -> "_Q":
            return self

        def limit(self, _n: int) -> "_Q":
            return self

        def all(self) -> List[_Section]:
            return [_Section()]

    class _KBDB:
        def query(self, _m: Any) -> _Q:
            return _Q()

    kb_registry = load_staff_contact_registry(_KBDB(), TENANT_ID)
    kb_ok = bool(kb_registry.records and kb_registry.records[0].phone)
    results["checks"].append({
        "module": "staff_contact_registry KB fallback",
        "pass": kb_ok,
        "kb_phone": kb_registry.records[0].phone if kb_registry.records else None,
    })

    # arrival policy skips structured when flag off
    from modules.ai.brain.commerce.arrival_contact_delivery_policy import (  # noqa: PLC0415
        resolve_arrival_contact_evidence,
    )

    arrival = resolve_arrival_contact_evidence(_KBDB(), TENANT_ID, message="أنا جاي")
    results["checks"].append({
        "module": "arrival_contact_delivery resolve_arrival_contact_evidence",
        "pass": arrival is None or getattr(arrival, "compile_reason", "") != "structured_branch_reception",
        "evidence": None if arrival is None else {
            "compile_reason": arrival.compile_reason,
            "phone": arrival.phone,
        },
        "note": "With empty KB compile, None is expected; structured_branch_reception must not appear when flag OFF",
    })

    results["all_pass"] = all(c["pass"] for c in results["checks"])
    return results


def _write_migration_evidence() -> Dict[str, str]:
    mig76 = (_REPO / "database/migrations/versions/0076_merchant_branches_operations_center.py").read_text(encoding="utf-8")
    mig77 = (_REPO / "database/migrations/versions/0077_branch_contact_default_reception.py").read_text(encoding="utf-8")
    models_snip = (_REPO / "database/models.py").read_text(encoding="utf-8")
    start = models_snip.find("class BranchContact")
    end = models_snip.find("class BranchEscalationStep", start)
    contact_model = models_snip[start:end].strip()
    return {
        "0076_revision": "0076",
        "0076_down_revision": "0074",
        "0077_revision": "0077",
        "0077_down_revision": "0076",
        "chain": "0074 → 0076 (tables) → 0077 (is_default_reception column only)",
        "conflict_check": "0077 only adds branch_contacts.is_default_reception; does not recreate 0076 tables",
        "0076_head": "\n".join(mig76.splitlines()[:20]),
        "0077_full": mig77,
        "BranchContact_model": contact_model,
    }


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    client, db = _build_client()

    api_evidence = capture_api_evidence(client)
    runtime = capture_runtime_isolation(db)
    migration = _write_migration_evidence()

    # Extract headline API samples for markdown
    calls = api_evidence["calls"]
    get_branches = next(c for c in calls if c["method"] == "GET" and c["path"] == "/operations-center/branches")
    post_branch = next(c for c in calls if c["method"] == "POST" and c["path"] == "/operations-center/branches")
    post_contact = next(
        c for c in calls
        if c["method"] == "POST" and "/contacts" in c["path"] and "set-default" not in c["path"]
    )
    post_esc = next(c for c in calls if c["method"] == "POST" and "/escalation-steps" in c["path"])

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "branch": "feat/operations-center-pr-b-dashboard-wt",
        "api_evidence": {
            "GET /operations-center/branches": get_branches,
            "POST /operations-center/branches (sample)": post_branch,
            "POST /operations-center/branches/{id}/contacts (sample)": post_contact,
            "POST /operations-center/branches/{id}/escalation-steps (sample)": post_esc,
        },
        "multi_branch_validation": api_evidence["multi_branch_summary"],
        "runtime_isolation": runtime,
        "migration": migration,
        "full_api_trace": api_evidence,
    }

    json_path = _OUT / "pr-b-evidence.json"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# PR-B Operational Evidence",
        "",
        f"Generated: `{bundle['generated_at']}`",
        "",
        "## Runtime isolation (USE_STRUCTURED_BRANCH_CONTACTS=0)",
        "",
        f"**All checks pass:** `{runtime['all_pass']}`",
        "",
    ]
    for chk in runtime["checks"]:
        md_lines.append(f"- {'PASS' if chk['pass'] else 'FAIL'} — `{chk['module']}`")
    md_lines.extend(["", "## Multi-branch validation", ""])
    for row in api_evidence["multi_branch_summary"]:
        md_lines.append(f"### {row['branch']} ({row['city']})")
        md_lines.append(f"- maps: `{row['maps_url']}`")
        md_lines.append(f"- active: `{row['is_active']}`")
        dr = row.get("default_reception") or {}
        md_lines.append(f"- default reception: `{dr.get('display_name')}` / `{dr.get('phone_e164')}`")
        md_lines.append("- escalation:")
        for lvl in row["escalation_levels"]:
            md_lines.append(f"  - L{lvl['level']}: {lvl['name']} ({lvl['phone']})")
        md_lines.append("")

    (_OUT / "pr-b-evidence.md").write_text("\n".join(md_lines), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "json": str(json_path),
        "runtime_all_pass": runtime["all_pass"],
        "branches_created": len(api_evidence["multi_branch_summary"]),
    }, indent=2))


if __name__ == "__main__":
    main()
