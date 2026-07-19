#!/usr/bin/env python3
"""Validate confined-runner config and emit sanitized host pinning metadata."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ops.internal_e2e_runner.lib.config import (
    database_url_fingerprint,
    load_runner_config,
    validate_runner_config_blockers,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--database-url-file", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as handle:
        raw = json.load(handle)

    database_url = ""
    if args.database_url_file:
        database_url = Path(args.database_url_file).read_text(encoding="utf-8").strip()

    blockers = validate_runner_config_blockers(raw, database_url=database_url or None)
    if blockers:
        print(json.dumps({"ok": False, "blockers": sorted(set(blockers))}, sort_keys=True))
        return 2

    config = load_runner_config(raw)
    sidecar_expected_dns = {
        config.llm_endpoint.hostname: list(config.llm_endpoint.ips),
        config.db_proxy_endpoint.hostname: list(config.db_proxy_endpoint.ips),
    }
    payload = {
        "ok": True,
        "config": config.to_public_mapping(),
        "sidecar_expected_dns": sidecar_expected_dns,
        "database_url_fingerprint": database_url_fingerprint(database_url) if database_url else None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
