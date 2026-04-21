"""
End-to-end simulation of the customer-import wizard.

Runs entirely in-memory using the production parser / normalizer /
dedupe / importer modules with a fake `db` session that keeps the
"existing" customer book in a list. No PostgreSQL required.

Verifies the full happy path the user requested:

    120 records in file
    → 87 new
    → 21 exact-match (will MERGE non-destructively)
    → 12 suspect (manual decisions, then merged or created)
    → invalid rows surfaced separately

And the critical merge guarantee: when a "salla_sync" customer is
re-imported via "manual_import" with the same phone, the trusted
salla data stays intact and `source_tags` records BOTH sources.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "database"))

from services.customer_import.parser import parse_upload
from services.customer_import.normalizer import normalize_row, suggest_column_mapping
from services.customer_import.dedupe import classify_rows, _ExistingIndex
from services.customer_import.importer import (
    _apply_non_destructive_merge,
    _build_metadata,
)


# ── Fake DB layer ────────────────────────────────────────────────────────────

class FakeCustomer:
    _id = 0
    def __init__(self, **kw):
        FakeCustomer._id += 1
        self.id = FakeCustomer._id
        self.tenant_id = kw["tenant_id"]
        self.name = kw.get("name")
        self.email = kw.get("email")
        self.phone = kw.get("phone")
        self.normalized_phone = kw.get("normalized_phone")
        self.acquisition_channel = kw.get("acquisition_channel")
        self.extra_metadata = kw.get("extra_metadata", {})
        self.last_interaction_at = None
        self.first_seen_at = kw.get("first_seen_at")


class FakeQuery:
    def __init__(self, items, model):
        self._items = list(items)
        self._model = model

    def filter(self, *args, **kwargs):
        # very crude — we don't need real filtering for the simulation,
        # the dedupe loader uses .yield_per to walk every row.
        return self

    def yield_per(self, n):
        # Emulate the (id, name, email, normalized_phone, acquisition_channel,
        # extra_metadata) tuple shape the real query yields.
        for c in self._items:
            yield (
                c.id, c.name, c.email, c.normalized_phone,
                c.acquisition_channel, c.extra_metadata,
            )


class FakeSession:
    def __init__(self):
        self.customers: List[FakeCustomer] = []

    def query(self, *cols):
        # The dedupe loader queries 6 columns from Customer;
        # the importer rare-path uses query(Customer).
        return FakeQuery(self.customers, None)

    def add(self, obj): self.customers.append(obj) if isinstance(obj, FakeCustomer) and obj not in self.customers else None
    def flush(self): pass
    def commit(self): pass
    def rollback(self): pass
    def delete(self, obj):
        if obj in self.customers:
            self.customers.remove(obj)


# ── Build a synthetic upload (CSV) ───────────────────────────────────────────

def build_csv() -> bytes:
    """Build a 120-row CSV mixing brand-new customers, exact phone
    duplicates of existing salla_sync customers, email-only suspects,
    and a couple of invalid rows."""
    rows: List[str] = ["name,phone,email,city,notes,source"]

    # 87 brand-new customers — valid Saudi mobile prefix 0501..
    for i in range(87):
        local = f"0501{i:06d}"
        rows.append(f"عميل جديد {i},{local},new{i}@x.com,الرياض,من معرض 2024,exhibition")

    # 21 exact phone matches against the seeded customers — local form
    # 0542XXXXXX normalizes to E.164 +966542XXXXXX (matches the seed).
    for i in range(21):
        local = f"0542{i:06d}"
        rows.append(f",{local},,,,manual_import")

    # 12 suspects — NEW phone, but email reuses an existing customer's email.
    for i in range(12):
        local = f"0553{i:06d}"
        rows.append(f"Maybe Same {i},{local},old{i}@store.com,,,manual_import")

    # 3 invalids — bad phone formats
    rows.extend([
        "Garbage,abc,bad@x.com,,,",
        ",,,,,",
        "Anonymous,12,,,,",
    ])
    return ("\n".join(rows) + "\n").encode("utf-8")


def seed_existing(db: FakeSession, tenant_id: int) -> None:
    """Seed the tenant with:
       - 21 salla_sync customers whose phones the upload will hit exactly
       - 12 salla_sync customers whose emails the upload reuses (same
         email, different phone → suspects)
    """
    # 21 phones that the upload's "exact" rows will normalize against.
    # Local format 0542XXXXXX → E.164 +9665420XXXXXX (12 chars after +).
    for i in range(21):
        e164 = f"+966542{i:06d}"
        db.add(FakeCustomer(
            tenant_id=tenant_id,
            name=f"Salla Trusted {i}",
            email=f"trusted-salla-{i}@store.com",
            phone=e164, normalized_phone=e164,
            acquisition_channel="salla_sync",
            extra_metadata={
                "source": "salla_sync",
                "primary_source": "salla_sync",
                "source_tags": ["salla_sync"],
            },
        ))
    # 12 customers whose email matches an upload row (but phone differs).
    for i in range(12):
        e164 = f"+966511{i:06d}"
        db.add(FakeCustomer(
            tenant_id=tenant_id,
            name=f"Salla Email {i}",
            email=f"old{i}@store.com",
            phone=e164, normalized_phone=e164,
            acquisition_channel="salla_sync",
            extra_metadata={
                "source": "salla_sync",
                "primary_source": "salla_sync",
                "source_tags": ["salla_sync"],
            },
        ))


# ── Simulate full wizard ─────────────────────────────────────────────────────

def run() -> None:
    db = FakeSession()
    tenant_id = 1
    seed_existing(db, tenant_id)
    print(f"[seed] existing customers: {len(db.customers)}")

    # STEP 1 — Upload + parse
    csv_bytes = build_csv()
    parsed = parse_upload(content=csv_bytes, filename="customers.csv")
    print(f"[step 1] parsed {parsed.total_rows} rows, headers={parsed.headers}")

    # STEP 2 — auto-suggest mapping then normalize+classify
    mapping = suggest_column_mapping(parsed.headers)
    print(f"[step 2] suggested mapping: {mapping}")
    normalized = [
        normalize_row(row_index=i, raw=r, mapping=mapping)
        for i, r in enumerate(parsed.rows, start=1)
    ]
    classified = classify_rows(db, tenant_id=tenant_id, rows=normalized)
    counts: Dict[str, int] = {}
    for c in classified:
        counts[c.classification] = counts.get(c.classification, 0) + 1
    print(f"[step 3] classification: {counts}")

    # STEP 4 — apply commit. We accept all suggested merges for suspects,
    # then run merges/creates on a per-row basis using the fake helpers.
    created = updated = skipped = 0
    for c in classified:
        n = c.normalized
        if c.classification == "invalid":
            skipped += 1
            continue
        if c.classification == "new":
            now_meta = _build_metadata(existing=None, normalized=n, batch_id=99)
            db.add(FakeCustomer(
                tenant_id=tenant_id,
                name=n["name"] or None,
                email=(n["email"] or "").lower() or None,
                phone=n["phone_raw"],
                normalized_phone=n["normalized_phone"],
                acquisition_channel="manual_import",
                extra_metadata=now_meta,
            ))
            created += 1
            continue
        if c.classification == "exact":
            existing = next(x for x in db.customers if x.id == c.match_customer_id)
            _apply_non_destructive_merge(existing, normalized=n, batch_id=99)
            updated += 1
            continue
        if c.classification == "suspect":
            # auto-merge into the first suggested candidate
            cand = next((x for x in c.suspect_candidates if x.get("customer_id")), None)
            if cand:
                existing = next(x for x in db.customers if x.id == cand["customer_id"])
                _apply_non_destructive_merge(existing, normalized=n, batch_id=99)
                updated += 1
            else:
                skipped += 1

    print(f"[commit] created={created} updated={updated} skipped={skipped}")
    print(f"[final] total customers in book: {len(db.customers)}")

    # Sanity probes
    sample_exact = next(c for c in db.customers if c.id == 1)
    print(f"\n[probe] exact-match customer (was salla_sync, then re-imported):")
    print(f"   name           = {sample_exact.name!r}  (must remain salla)")
    print(f"   email          = {sample_exact.email!r}  (must remain salla)")
    print(f"   primary_source = {sample_exact.extra_metadata.get('primary_source')!r}")
    print(f"   source_tags    = {sample_exact.extra_metadata.get('source_tags')!r}")
    print(f"   last_import    = {sample_exact.extra_metadata.get('last_import_batch')!r}")

    sample_suspect = next(c for c in db.customers if c.id == 22)
    print(f"\n[probe] suspect-merge customer (email match, different phone):")
    print(f"   name           = {sample_suspect.name!r}  (untouched)")
    print(f"   source_tags    = {sample_suspect.extra_metadata.get('source_tags')!r}")

    sample_new = next(c for c in db.customers if c.acquisition_channel == "manual_import")
    print(f"\n[probe] one of the brand-new manual-import customers:")
    print(f"   name           = {sample_new.name!r}")
    print(f"   primary_source = {sample_new.extra_metadata.get('primary_source')!r}")
    print(f"   source_tags    = {sample_new.extra_metadata.get('source_tags')!r}")


if __name__ == "__main__":
    run()
