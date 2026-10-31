"""Rollback-only policy, with separate operator and container trust boundaries.

The operator verifies Railway and owner evidence through authenticated tools
before installing a SHA-pinned approval and command. The container rechecks
that exact payload, source, flags and database. Neither side writes the DB.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from typing import Protocol

from scripts.operators import shopify_release_gate_contract as existing


ACTION = "rollback_only_0125"
HEADS = frozenset({"0111", "0113", "0125"})
FLAG_NAMES = frozenset({
    "NAHLA_SHOPIFY_CONNECTION_ENABLED",
    "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED",
    "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED",
    "VITE_SHOPIFY_CONNECTION_UI",
    "VITE_SHOPIFY_INSTALL_FIRST_ENABLED",
    "VITE_SHOPIFY_CATALOG_IMPORT_UI",
    "SALLA_COMM_WHATSAPP_ENABLED",
})
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class Refused(ValueError):
    """Closed refusal token. Never attach a URL, secret, or raw exception."""


def require(condition: bool, token: str) -> None:
    if not condition:
        raise Refused(token)


@dataclass(frozen=True)
class Target:
    project: str
    environment: str
    service: str
    database_identity: str


@dataclass(frozen=True)
class Deployment:
    """Operator read; image_digest denotes Railway's build-output digest."""

    deployment_id: str
    target: Target
    source_sha: str
    image_digest: str
    predeploy_command_sha256: str
    status: str
    healthy: bool


@dataclass(frozen=True)
class Approval:
    """Payload bound by the command pin; provenance is verified by the operator."""

    action: str
    target: Target
    candidate_source_sha: str
    reference_deployment_id: str
    reference_source_sha: str
    reference_image_digest: str
    reference_predeploy_command_sha256: str
    owner_approval_reference: str
    backup_service_id: str
    backup_volume_id: str
    backup_receipt_sha256: str
    restore_receipt_sha256: str
    rollback_command_template_sha256: str
    inventory_digest: str
    inventory_count: int
    shopify_schema_sha256: str
    communication_schema_sha256: str
    approved_at: datetime
    expires_at: datetime
    approved: bool
    rollback_artifact_deployment_id: str = ""
    rollback_artifact_source_sha: str = ""
    rollback_artifact_image_digest: str = ""
    rollback_artifact_predeploy_command_sha256: str = ""


@dataclass(frozen=True)
class DatabaseState:
    heads: frozenset[str]
    shopify_schema_sha256: str
    communication_schema_sha256: str


@dataclass(frozen=True)
class AuthorizedRollback:
    approval: Approval
    execution_command_sha256: str


@dataclass(frozen=True)
class OperatorEvidence:
    """Outcome of separate operator reads, never an input to the container.

    image_digest is the Railway build-output digest. It is not a claim that
    the currently running container image was independently measured.
    """

    approval_payload_sha256: str
    backup_receipt_sha256: str
    restore_receipt_sha256: str
    reference: Deployment
    rollback_artifact: Deployment


@dataclass(frozen=True)
class RecoveryReceipt:
    """Result of an authenticated operator read, not a self-authored hash."""

    kind: str
    target: Target
    service_id: str
    volume_id: str
    sha256: str
    completed: bool


class RailwayReadPort(Protocol):
    def deployment(self, deployment_id: str) -> Deployment: ...
    def current_deployment_id(self, target: Target) -> str: ...
    def artifact_launchable(self, deployment_id: str, image_digest: str) -> bool: ...


class OperatorReadPort(Protocol):
    """Authenticated owner and recovery reads held outside the container."""

    def owner_approved(self, reference: str, approval: Approval,
                       payload: bytes) -> bool: ...
    def recovery_receipt(self, kind: str, target: Target,
                         service_id: str, volume_id: str) -> RecoveryReceipt: ...


class ApprovalReadPort(Protocol):
    def approved_action(self) -> Approval: ...


class DatabaseReadPort(Protocol):
    def state_read_only(self) -> DatabaseState: ...


class CommandRenderPort(Protocol):
    def render(self, command_template: bytes, approval: Approval) -> bytes: ...


class UnconfiguredRailwayReadPort:
    """No JSON receipt is accepted as a substitute for a provider read."""

    def deployment(self, deployment_id: str) -> Deployment:
        raise Refused("trusted_railway_read_unavailable")

    def current_deployment_id(self, target: Target) -> str:
        raise Refused("trusted_railway_read_unavailable")

    def artifact_launchable(self, deployment_id: str, image_digest: str) -> bool:
        raise Refused("trusted_railway_read_unavailable")


class UnconfiguredApprovalReadPort:
    def approved_action(self) -> Approval:
        raise Refused("trusted_approval_read_unavailable")


class UnconfiguredCommandRenderPort:
    def render(self, command_template: bytes, approval: Approval) -> bytes:
        raise Refused("trusted_command_renderer_unavailable")


def _utc(value: datetime) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None and value.utcoffset().total_seconds() == 0)


def _digest(value: str) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _source(value: str) -> bool:
    return isinstance(value, str) and HEX40.fullmatch(value) is not None


def approval_payload_bytes(approval: Approval) -> bytes:
    """Canonical transport bytes; the owner and provider must still attest them."""
    require(_utc(approval.approved_at) and _utc(approval.expires_at), "approval_time_invalid")
    record = asdict(approval)
    record["approved_at_utc"] = record.pop("approved_at").isoformat().replace("+00:00", "Z")
    record["expires_at_utc"] = record.pop("expires_at").isoformat().replace("+00:00", "Z")
    record["record_version"] = 1
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def approval_matches_payload(approval: Approval, payload: bytes) -> bool:
    """Bind the typed policy input to the exact bytes installed in Railway."""
    if not isinstance(payload, bytes):
        return False
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError, TypeError):
        return False
    if not isinstance(record, dict):
        return False
    expected = {
        "record_version": 1, "action": approval.action,
        "target": vars(approval.target),
        "candidate_source_sha": approval.candidate_source_sha,
        "reference_deployment_id": approval.reference_deployment_id,
        "reference_source_sha": approval.reference_source_sha,
        "reference_image_digest": approval.reference_image_digest,
        "reference_predeploy_command_sha256": approval.reference_predeploy_command_sha256,
        "rollback_artifact_deployment_id": approval.rollback_artifact_deployment_id,
        "rollback_artifact_source_sha": approval.rollback_artifact_source_sha,
        "rollback_artifact_image_digest": approval.rollback_artifact_image_digest,
        "rollback_artifact_predeploy_command_sha256": approval.rollback_artifact_predeploy_command_sha256,
        "owner_approval_reference": approval.owner_approval_reference,
        "backup_service_id": approval.backup_service_id,
        "backup_volume_id": approval.backup_volume_id,
        "backup_receipt_sha256": approval.backup_receipt_sha256,
        "restore_receipt_sha256": approval.restore_receipt_sha256,
        "rollback_command_template_sha256": approval.rollback_command_template_sha256,
        "inventory_digest": approval.inventory_digest,
        "inventory_count": approval.inventory_count,
        "shopify_schema_sha256": approval.shopify_schema_sha256,
        "communication_schema_sha256": approval.communication_schema_sha256,
        "approved": approval.approved,
    }
    if set(record) != set(expected) | {"approved_at_utc", "expires_at_utc"}:
        return False
    if type(record.get("inventory_count")) is not int or type(record.get("approved")) is not bool:
        return False
    actual_times = (existing.parse_utc(record["approved_at_utc"]),
                    existing.parse_utc(record["expires_at_utc"]))
    if actual_times != (approval.approved_at, approval.expires_at):
        return False
    return all(record[key] == value for key, value in expected.items())


def validate_predeploy(
    *, approvals: ApprovalReadPort, database: DatabaseReadPort,
    renderer: CommandRenderPort, expected_target: Target, command_template: bytes,
    process_source_sha: str,
    process_inventory_digest: str, process_inventory_count: int,
    process_flags: dict[str, str], now: datetime,
) -> AuthorizedRollback:
    """Container-only preflight; never treats payload as provider attestation.

    The bootstrap separately pins gate and inventory bytes. Operator evidence
    is checked outside the container before this command is installed.
    """
    require(_utc(now), "clock_not_utc")
    try:
        approval = approvals.approved_action()
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_approval_read_failed") from None
    require(approval.approved is True and approval.action == ACTION, "action_not_approved")
    require(_utc(approval.approved_at) and _utc(approval.expires_at), "approval_time_invalid")
    require(approval.approved_at <= now < approval.expires_at, "approval_expired")
    require(approval.expires_at - approval.approved_at <= timedelta(hours=24), "approval_window_too_long")
    require(approval.target == expected_target, "approval_target_mismatch")
    require(_source(approval.candidate_source_sha), "source_invalid")
    require(process_source_sha == approval.candidate_source_sha, "source_mismatch")
    require(_digest(approval.inventory_digest) and process_inventory_digest == approval.inventory_digest,
            "inventory_mismatch")
    require(type(process_inventory_count) is int and process_inventory_count > 0
            and process_inventory_count == approval.inventory_count, "inventory_count_mismatch")
    # The template excludes the approval-payload digest. The trusted renderer
    # inserts that digest into execution_command later; hashing the finished
    # command *inside the payload* would make a circular hash dependency.
    require(_digest(approval.rollback_command_template_sha256)
            and sha256(command_template).hexdigest() == approval.rollback_command_template_sha256,
            "command_template_mismatch")
    try:
        execution_command = renderer.render(command_template, approval)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_command_render_failed") from None
    require(isinstance(execution_command, bytes) and bool(execution_command), "execution_command_missing")
    require(set(process_flags) == FLAG_NAMES
            and all(process_flags[name] == "false" for name in FLAG_NAMES), "flags_not_off")
    require(_digest(approval.reference_image_digest)
            and _digest(approval.reference_predeploy_command_sha256)
            and _digest(approval.shopify_schema_sha256)
            and _digest(approval.communication_schema_sha256), "digest_invalid")
    require(bool(approval.reference_deployment_id)
            and _source(approval.reference_source_sha)
            and bool(approval.rollback_artifact_deployment_id)
            and _source(approval.rollback_artifact_source_sha)
            and approval.rollback_artifact_source_sha == approval.candidate_source_sha
            and _digest(approval.rollback_artifact_image_digest)
            and _digest(approval.rollback_artifact_predeploy_command_sha256)
            and bool(approval.owner_approval_reference)
            and bool(approval.backup_service_id)
            and bool(approval.backup_volume_id)
            and _digest(approval.backup_receipt_sha256)
            and _digest(approval.restore_receipt_sha256), "operator_binding_missing")

    try:
        state = database.state_read_only()
    except Exception:
        raise Refused("database_read_failed") from None
    require(state.heads == HEADS, "database_heads_mismatch")
    require(state.shopify_schema_sha256 == approval.shopify_schema_sha256, "shopify_schema_mismatch")
    require(state.communication_schema_sha256 == approval.communication_schema_sha256,
            "communication_schema_mismatch")
    return AuthorizedRollback(approval=approval,
                              execution_command_sha256=sha256(execution_command).hexdigest())


def validate_operator_predeploy(*, approval: Approval, payload: bytes,
                                pinned_payload_sha256: str,
                                provider: RailwayReadPort, operator: OperatorReadPort,
                                now: datetime) -> OperatorEvidence:
    """Compare independent authenticated reads before installing a command.

    The operator ports must use the existing Railway and owner channels. A
    caller-supplied JSON object or this function alone is no attestation.
    """
    require(_utc(now) and _utc(approval.approved_at) and _utc(approval.expires_at)
            and approval.approved_at <= now < approval.expires_at,
            "approval_expired")
    require(approval.expires_at - approval.approved_at <= timedelta(hours=24),
            "approval_window_too_long")
    require(approval.action == ACTION and approval.approved is True,
            "action_not_approved")
    require(isinstance(payload, bytes) and _digest(pinned_payload_sha256)
            and sha256(payload).hexdigest() == pinned_payload_sha256
            and approval_matches_payload(approval, payload)
            and bool(approval.owner_approval_reference)
            and _digest(approval.backup_receipt_sha256)
            and _digest(approval.restore_receipt_sha256),
            "approval_payload_invalid")
    try:
        owner_ok = operator.owner_approved(approval.owner_approval_reference,
                                           approval, payload)
    except Exception:
        raise Refused("approval_provenance_read_failed") from None
    require(owner_ok is True, "approval_provenance_mismatch")
    try:
        backup = operator.recovery_receipt(
            "backup", approval.target, approval.backup_service_id, approval.backup_volume_id)
        restore = operator.recovery_receipt(
            "restore", approval.target, approval.backup_service_id, approval.backup_volume_id)
    except Exception:
        raise Refused("recovery_evidence_read_failed") from None
    require(all(isinstance(receipt, RecoveryReceipt) and receipt.completed
                and receipt.kind == kind and receipt.target == approval.target
                and receipt.service_id == approval.backup_service_id
                and receipt.volume_id == approval.backup_volume_id
                for receipt, kind in ((backup, "backup"), (restore, "restore")))
            and _digest(backup.sha256) and backup.sha256 == approval.backup_receipt_sha256
            and _digest(restore.sha256) and restore.sha256 == approval.restore_receipt_sha256,
            "recovery_evidence_mismatch")
    try:
        reference = provider.deployment(approval.reference_deployment_id)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_railway_read_failed") from None
    require(reference.deployment_id == approval.reference_deployment_id,
            "reference_id_mismatch")
    require(reference.target == approval.target, "reference_target_mismatch")
    require(reference.source_sha == approval.reference_source_sha,
            "reference_source_mismatch")
    require(reference.image_digest == approval.reference_image_digest,
            "reference_image_mismatch")
    require(reference.predeploy_command_sha256 == approval.reference_predeploy_command_sha256,
            "reference_command_mismatch")
    require(reference.status == "SUCCESS" and reference.healthy,
            "reference_unhealthy")
    try:
        current_id = provider.current_deployment_id(approval.target)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_railway_read_failed") from None
    require(current_id == reference.deployment_id, "reference_not_serving")
    require(approval.rollback_artifact_deployment_id != reference.deployment_id,
            "artifact_not_distinct")
    try:
        artifact = provider.deployment(approval.rollback_artifact_deployment_id)
        launchable = provider.artifact_launchable(
            approval.rollback_artifact_deployment_id, approval.rollback_artifact_image_digest)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_railway_read_failed") from None
    require(artifact.deployment_id == approval.rollback_artifact_deployment_id,
            "artifact_id_mismatch")
    require(artifact.target == approval.target, "artifact_target_mismatch")
    require(artifact.source_sha == approval.rollback_artifact_source_sha,
            "artifact_source_mismatch")
    require(artifact.image_digest == approval.rollback_artifact_image_digest,
            "artifact_image_mismatch")
    require(artifact.predeploy_command_sha256 == approval.rollback_artifact_predeploy_command_sha256,
            "artifact_command_mismatch")
    require(artifact.status == "SUCCESS" and launchable is True, "artifact_not_launchable")
    require(provider.current_deployment_id(approval.target) == reference.deployment_id,
            "reference_changed")
    return OperatorEvidence(pinned_payload_sha256, backup.sha256, restore.sha256,
                            reference, artifact)


def validate_result(*, provider: RailwayReadPort, authorized: AuthorizedRollback,
                    result_deployment_id: str, now: datetime) -> Deployment:
    """Read back the *new* deployment; never assume the reference ID survives."""
    approval = authorized.approval
    require(_utc(now) and approval.approved_at <= now < approval.expires_at, "approval_expired")
    require(result_deployment_id != approval.reference_deployment_id, "result_id_not_new")
    require(result_deployment_id != approval.rollback_artifact_deployment_id,
            "result_id_not_new")
    try:
        result = provider.deployment(result_deployment_id)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_railway_read_failed") from None
    require(result.deployment_id == result_deployment_id, "result_id_mismatch")
    require(result.target == approval.target, "result_target_mismatch")
    require(result.source_sha == approval.candidate_source_sha, "result_source_mismatch")
    require(result.image_digest == approval.rollback_artifact_image_digest,
            "result_image_mismatch")
    require(result.predeploy_command_sha256 == authorized.execution_command_sha256,
            "result_command_mismatch")
    require(result.status == "SUCCESS" and result.healthy, "result_unhealthy")
    try:
        current_id = provider.current_deployment_id(approval.target)
    except Refused:
        raise
    except Exception:
        raise Refused("trusted_railway_read_failed") from None
    require(current_id == result_deployment_id, "result_not_serving")
    return result
