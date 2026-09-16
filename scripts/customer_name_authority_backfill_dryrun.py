#!/usr/bin/env python3
"""
Historical customer-name backfill — FEASIBILITY DRY RUN.

READ-ONLY BY CONSTRUCTION. This script opens a session, runs SELECTs,
prints a report, and rolls back. It contains no UPDATE, INSERT, DELETE
or DDL, and it never calls ``commit()``. There is no ``--apply`` flag
and adding one is out of scope for this change.

What it answers
===============
For every existing ``customers`` row, what would the canonical name
authority say about the name we are currently storing?

  CLEAN                  Name is backed by an authority that still
                         holds today. Nothing to do.
  DEVOTIONAL_LEAK        Stored name / profile hint is a devotional or
                         status phrase ("الحمد لله"). This is the
                         population the fix exists for.
  AMBIGUOUS_HINT         A single-token profile hint that the
                         classifier will no longer display. Retained,
                         but demoted from display.
  UNVERIFIED_LOW_TRUST   Name sits at WHATSAPP_PROFILE authority with
                         no higher-authority evidence behind it.
  NO_NAME                Nothing stored.

Usage
-----
    python scripts/customer_name_authority_backfill_dryrun.py
    python scripts/customer_name_authority_backfill_dryrun.py --tenant 1
    python scripts/customer_name_authority_backfill_dryrun.py --samples 20
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend"), os.path.join(_REPO_ROOT, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


VERDICT_CLEAN = "CLEAN"
VERDICT_DEVOTIONAL = "DEVOTIONAL_LEAK"
VERDICT_AMBIGUOUS = "AMBIGUOUS_HINT"
VERDICT_LOW_TRUST = "UNVERIFIED_LOW_TRUST"
VERDICT_NO_NAME = "NO_NAME"


def classify_row(name: str, meta: Dict[str, Any], channel: str) -> Tuple[str, str]:
    """Return ``(verdict, detail)`` for one customer row. Pure function."""
    from core.customer_name_authority import (
        AMBIGUOUS,
        NOT_PERSON_NAME,
        NameAuthority,
        authority_for_source,
        classify_whatsapp_profile_name,
    )

    name = (name or "").strip()
    hint = str((meta or {}).get("proposed_name") or "").strip()
    source = str(
        (meta or {}).get("customer_name_source")
        or (meta or {}).get("name_source")
        or channel
        or ""
    ).strip()
    authority = authority_for_source(source)
    merchant_locked = bool((meta or {}).get("manual_name_override"))

    if not name and not hint:
        return VERDICT_NO_NAME, ""

    # Merchant-typed names are the merchant's call — never reclassified.
    if merchant_locked and name:
        return VERDICT_CLEAN, "merchant_locked"

    subject = name or hint
    verdict = classify_whatsapp_profile_name(subject)

    if verdict.classification == NOT_PERSON_NAME:
        # A verified store name that trips the classifier is still the
        # store's fact — we do not overrule Salla with a heuristic.
        if authority >= NameAuthority.CUSTOMER_SELF_REPORTED and name:
            return VERDICT_CLEAN, f"high_authority:{authority.label}"
        return VERDICT_DEVOTIONAL, verdict.reason

    if verdict.classification == AMBIGUOUS and not name and hint:
        return VERDICT_AMBIGUOUS, verdict.reason

    if name and authority <= NameAuthority.WHATSAPP_PROFILE:
        return VERDICT_LOW_TRUST, f"authority:{authority.label}"

    return VERDICT_CLEAN, f"authority:{authority.label}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Customer-name authority backfill feasibility (DRY RUN ONLY)",
    )
    parser.add_argument("--tenant", type=int, default=None, help="Limit to one tenant id")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to scan (0 = all)")
    parser.add_argument("--samples", type=int, default=10, help="Sample rows per verdict")
    args = parser.parse_args()

    from models import Customer
    from session import SessionLocal

    session = SessionLocal()
    try:
        query = session.query(
            Customer.id,
            Customer.tenant_id,
            Customer.name,
            Customer.extra_metadata,
            Customer.acquisition_channel,
        )
        if args.tenant is not None:
            query = query.filter(Customer.tenant_id == args.tenant)
        if args.limit:
            query = query.limit(args.limit)

        counts: Counter = Counter()
        per_tenant: Dict[int, Counter] = defaultdict(Counter)
        samples: Dict[str, List[str]] = defaultdict(list)
        reasons: Counter = Counter()
        scanned = 0

        for cid, tid, name, meta, channel in query.yield_per(500):
            scanned += 1
            verdict, detail = classify_row(name or "", meta or {}, channel or "")
            counts[verdict] += 1
            per_tenant[tid][verdict] += 1
            if detail:
                reasons[f"{verdict}:{detail}"] += 1
            if len(samples[verdict]) < args.samples:
                shown = (name or "").strip() or str(
                    (meta or {}).get("proposed_name") or ""
                ).strip()
                samples[verdict].append(f"customer={cid} tenant={tid} value={shown!r}")

        print("=" * 68)
        print("CUSTOMER NAME AUTHORITY — BACKFILL FEASIBILITY (DRY RUN)")
        print("=" * 68)
        print(f"rows scanned: {scanned}")
        print()
        print("VERDICT TOTALS")
        for verdict, n in counts.most_common():
            pct = (100.0 * n / scanned) if scanned else 0.0
            print(f"  {verdict:24} {n:8}  {pct:5.1f}%")

        print()
        print("TOP REASONS")
        for reason, n in reasons.most_common(15):
            print(f"  {reason:48} {n:8}")

        print()
        print("SAMPLES")
        for verdict in (
            VERDICT_DEVOTIONAL, VERDICT_AMBIGUOUS,
            VERDICT_LOW_TRUST, VERDICT_CLEAN,
        ):
            if not samples.get(verdict):
                continue
            print(f"  [{verdict}]")
            for line in samples[verdict]:
                print(f"    {line}")

        print()
        print("TENANTS WITH DEVOTIONAL LEAKS")
        leaky = sorted(
            ((t, c[VERDICT_DEVOTIONAL]) for t, c in per_tenant.items() if c[VERDICT_DEVOTIONAL]),
            key=lambda kv: kv[1], reverse=True,
        )
        for tid, n in leaky[:20]:
            print(f"  tenant={tid:6} devotional_leaks={n}")
        if not leaky:
            print("  (none)")

        print()
        print("=" * 68)
        print("DRY RUN ONLY — no rows were modified, nothing was committed.")
        print("=" * 68)
        return 0
    finally:
        # Explicit: this session must never persist anything.
        session.rollback()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
