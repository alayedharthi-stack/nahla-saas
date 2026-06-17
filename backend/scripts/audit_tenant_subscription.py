#!/usr/bin/env python3
"""Audit tenant subscription/trial state (operator tool).

Usage:
    DATABASE_URL=postgresql://... python backend/scripts/audit_tenant_subscription.py --tenant 33
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "backend"))
sys.path.insert(0, os.path.join(REPO, "database"))

# Load .env when present so operators can point at production before/after migration.
_env_file = os.path.join(REPO, ".env")
if os.path.isfile(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

from core.database import SessionLocal  # noqa: E402
from core.trial_lifecycle import audit_tenant_subscription  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit tenant billing/trial state")
    parser.add_argument("--tenant", type=int, required=True)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = audit_tenant_subscription(db, args.tenant)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
