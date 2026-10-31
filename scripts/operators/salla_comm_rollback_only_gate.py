#!/usr/bin/env python3
"""Container rollback-only gate for an operator-verified release action.

The operator must verify Railway and owner evidence before installing the
SHA-pinned approval and command. This process only checks local invariants.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as existing
from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_render as render
from scripts.operators import salla_comm_rollback_only_source as source


class ClosedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise terminal.Refused("invalid_arguments")


def parser() -> ClosedParser:
    result = ClosedParser(prog="salla_comm_rollback_only_gate", add_help=False)
    for name in ("root", "action", "release-sha", "inventory-digest", "inventory-count",
                 "approval-sha256", "expect-project", "expect-environment", "expect-service",
                 "expect-db", "expect-backup-service", "expect-backup-volume"):
        result.add_argument("--" + name, required=True)
    return result


class PinnedApprovalReadPort:
    """Read the exact payload SHA pinned by the verified command bootstrap."""

    def __init__(self, environ: Mapping[str, str], approval_sha256: str):
        self.environ = environ
        self.approval_sha256 = approval_sha256

    def approved_action(self) -> terminal.Approval:
        record, reason = gate.read_approval_payload(self.environ, self.approval_sha256)
        terminal.require(reason is None and isinstance(record, dict), "approval_payload_invalid")
        fields = {
            "record_version", "action", "target", "candidate_source_sha",
            "reference_deployment_id", "reference_source_sha", "reference_image_digest",
            "reference_predeploy_command_sha256",
            "owner_approval_reference", "backup_service_id", "backup_volume_id",
            "backup_receipt_sha256",
            "restore_receipt_sha256",
            "rollback_command_template_sha256", "inventory_digest", "inventory_count",
            "shopify_schema_sha256", "communication_schema_sha256",
            "approved_at_utc", "expires_at_utc", "approved",
        }
        terminal.require(set(record) == fields and record["record_version"] == 1,
                         "approval_shape_invalid")
        target = record["target"]
        terminal.require(isinstance(target, dict) and set(target) == {
            "project", "environment", "service", "database_identity"},
            "approval_target_invalid")
        return terminal.Approval(
            action=record["action"], target=terminal.Target(**target),
            candidate_source_sha=record["candidate_source_sha"],
            reference_deployment_id=record["reference_deployment_id"],
            reference_source_sha=record["reference_source_sha"],
            reference_image_digest=record["reference_image_digest"],
            reference_predeploy_command_sha256=record["reference_predeploy_command_sha256"],
            owner_approval_reference=record["owner_approval_reference"],
            backup_service_id=record["backup_service_id"],
            backup_volume_id=record["backup_volume_id"],
            backup_receipt_sha256=record["backup_receipt_sha256"],
            restore_receipt_sha256=record["restore_receipt_sha256"],
            rollback_command_template_sha256=record["rollback_command_template_sha256"],
            inventory_digest=record["inventory_digest"],
            inventory_count=record["inventory_count"],
            shopify_schema_sha256=record["shopify_schema_sha256"],
            communication_schema_sha256=record["communication_schema_sha256"],
            approved_at=existing.parse_utc(record["approved_at_utc"]),
            expires_at=existing.parse_utc(record["expires_at_utc"]),
            approved=record["approved"],
        )


class FixedApprovalReadPort:
    """Re-use the one parsed record; never read a mutable payload twice."""

    def __init__(self, approval: terminal.Approval):
        self.approval = approval

    def approved_action(self) -> terminal.Approval:
        return self.approval


def evaluate(argv: Sequence[str], environ: Mapping[str, str], now: datetime,
             *, database: Optional[terminal.DatabaseReadPort] = None,
             approvals: Optional[terminal.ApprovalReadPort] = None,
             renderer: Optional[terminal.CommandRenderPort] = None) -> terminal.AuthorizedRollback:
    args = parser().parse_args(list(argv))
    terminal.require(args.action == terminal.ACTION, "action_invalid")
    terminal.require(terminal._source(args.release_sha), "source_invalid")
    terminal.require(terminal._digest(args.inventory_digest), "inventory_invalid")
    terminal.require(terminal._digest(args.approval_sha256), "approval_digest_invalid")
    try:
        count = int(args.inventory_count)
    except ValueError:
        raise terminal.Refused("inventory_count_invalid") from None
    target = terminal.Target(args.expect_project, args.expect_environment,
                             args.expect_service, args.expect_db)
    # The operator's recovery receipt identities must match the command.
    root = Path(args.root)
    source.source_preflight(root, environ, args.release_sha, target,
                            count, args.inventory_digest)
    template = render.command_template(
        root, release_sha=args.release_sha, target=target,
        backup_service=args.expect_backup_service,
        backup_volume=args.expect_backup_volume,
        container_root=args.root,
    )
    approval_port = approvals or PinnedApprovalReadPort(environ, args.approval_sha256)
    approval = approval_port.approved_action()
    terminal.require(approval.backup_service_id == args.expect_backup_service
                     and approval.backup_volume_id == args.expect_backup_volume,
                     "recovery_identity_mismatch")
    return terminal.validate_predeploy(
        approvals=FixedApprovalReadPort(approval),
        renderer=renderer or render.CandidateCommandRenderer(args.approval_sha256),
        database=database or source.GateDatabaseReadPort(root, environ, args.expect_db),
        expected_target=target, command_template=template,
        process_source_sha=args.release_sha,
        process_inventory_digest=args.inventory_digest,
        process_inventory_count=count,
        process_flags={name: environ.get(name, "") for name in terminal.FLAG_NAMES},
        now=now,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        evaluate(argv if argv is not None else __import__("sys").argv[1:],
                 dict(os.environ), datetime.now(timezone.utc))
    except terminal.Refused as refusal:
        print("RESULT=FAIL action=rollback_only_0125 reason=" + str(refusal), flush=True)
        return 1
    except Exception:
        print("RESULT=FAIL action=rollback_only_0125 reason=internal_error", flush=True)
        return 1
    print("RESULT=PASS action=rollback_only_0125", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
