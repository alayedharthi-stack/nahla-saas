#!/usr/bin/env python3
"""Read-only readout of a tenant's WhatsApp-catalog trial preconditions.

Prints one JSON document (no tokens, no secrets) describing the tenant's
products, variants and channel identities, the WhatsApp connection and
catalog binding, plan entitlement, publish readiness, the proposed trial
products with the exact scope environment and, with ``--include-graph``,
what Meta Graph reports (GET only): WABA ↔ catalog link, catalog owner
business, token ``catalog_management`` permission and live items.

Run it where the service's own environment lives (secrets never leave it)::

    railway ssh --environment production --service <backend-service> \
      python -m scripts.catalog_trial_readout --tenant-id 35 --include-graph --pretty

or, with the backend as working directory::

    python backend/scripts/catalog_trial_readout.py --tenant-id 35 --pretty

Exit code 0 when the readout was produced, 2 when the tenant has no row.
Nothing is written to the database or to Meta.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from services.catalog_trial_readout import build_catalog_trial_readout  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only WhatsApp catalog trial readout for one tenant (no secrets in output).",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument(
        "--include-graph",
        action="store_true",
        help="Also read Meta Graph (GET only): WABA link, catalog owner, token permission, live items.",
    )
    parser.add_argument("--candidates", type=int, default=3, help="How many trial products to propose.")
    parser.add_argument("--candidate-ids", default="", help="Comma-separated local product ids chosen by the owner; evaluated instead of the automatic pick.")
    parser.add_argument("--expected-business-id", default="", help="Business Manager id expected to own the WABA and its catalog.")
    parser.add_argument("--include-salla", action="store_true", help="Re-read anomalous products from Salla (GET only, stored token as is: never refreshed or saved; refused when expired or rejected).")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set in this environment", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    Session = sessionmaker(bind=create_engine(db_url))
    db = Session()
    try:
        report = build_catalog_trial_readout(
            db,
            int(args.tenant_id),
            include_graph=bool(args.include_graph),
            candidate_count=int(args.candidates),
            candidate_ids=[int(x) for x in args.candidate_ids.replace(";", ",").split(",") if x.strip().isdigit()] or None,
            expected_business_id=args.expected_business_id or None,
            include_salla=bool(args.include_salla),
        )
        db.rollback()  # defensive: the readout never writes; make sure nothing is left open
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
        print(
            "trial-readout tenant=%s products=%s eligible=%s missing=%s"
            % (
                report["tenant_id"],
                report["product_counts"]["total"],
                report["product_counts"]["publish_eligible"],
                ",".join(report["missing_requirements"]) or "none",
            ),
            file=sys.stderr,
        )
        return 0 if report["tenant"]["exists"] else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
