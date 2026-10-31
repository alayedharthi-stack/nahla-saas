"""Synthetic rollback policy tests; no network, deployment, or production data."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest

from scripts.operators.salla_comm_rollback_only_contract import (
    ACTION, FLAG_NAMES, HEADS_BEFORE, HEADS_AFTER, Approval, ArtifactReceipt,
    DatabaseState, Deployment, RecoveryReceipt, Refused, Target,
    UnconfiguredApprovalReadPort, UnconfiguredRailwayReadPort,
    UnconfiguredCommandRenderPort, approval_payload_bytes,
    validate_operator_predeploy, validate_predeploy, validate_result,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
COMMAND = b"synthetic reviewed rollback-only-0125 predeploy command"
COMMAND_SHA = sha256(COMMAND).hexdigest()
TARGET = Target("project-test", "environment-test", "service-test", "database-test")
FLAGS = {name: "false" for name in FLAG_NAMES}


class Provider:
    def __init__(self, *deployments):
        self.items = {item.deployment_id: item for item in deployments}
        self.reads = 0
        self.current_id = deployments[0].deployment_id
        self.launchable = True

    def deployment(self, deployment_id):
        self.reads += 1
        return self.items[deployment_id]

    def current_deployment_id(self, target):
        assert target == TARGET
        return self.current_id

    def artifact_launchable(self, deployment_id, image_digest):
        return self.launchable and self.items[deployment_id].image_digest == image_digest


class Approvals:
    def __init__(self, approval):
        self.approval = approval

    def approved_action(self):
        return self.approval


class Operator:
    def __init__(self, approval):
        self.approval = approval
        self.owner_ok = True
        self.backup = "1" * 64
        self.restore = "2" * 64
        self.restore_complete = True
        self.artifact_completed = True
        self.artifact_id = "rollback-artifact"
        self.artifact_source = approval.candidate_source_sha
        self.artifact_image = "c" * 64
        self.artifact_command = COMMAND_SHA

    def owner_approved(self, reference, approval, payload):
        return (self.owner_ok and reference == "synthetic-owner-message"
                and approval == self.approval and payload == approval_payload_bytes(approval))

    def recovery_receipt(self, kind, target, service_id, volume_id):
        if service_id != "synthetic-backup-service" or volume_id != "synthetic-backup-volume":
            raise ValueError("wrong recovery target")
        return RecoveryReceipt(kind, target, service_id, volume_id,
                               self.backup if kind == "backup" else self.restore,
                               kind != "restore" or self.restore_complete)

    def artifact_receipt(self, approval_payload_sha256, target):
        return ArtifactReceipt(approval_payload_sha256, target, self.artifact_id,
                               self.artifact_source, self.artifact_image,
                               self.artifact_command, self.artifact_completed)


class Database:
    def __init__(self, state):
        self.value = state
        self.writes = 0

    def state_read_only(self):
        return self.value


class Renderer:
    def render(self, command_template, approval):
        return command_template


def fixture(heads=HEADS_BEFORE):
    approval = Approval(
        action=ACTION, target=TARGET, candidate_source_sha="a" * 40,
        heads_before=tuple(sorted(HEADS_BEFORE)),
        heads_after=tuple(sorted(HEADS_AFTER)),
        owner_approval_reference="synthetic-owner-message",
        backup_service_id="synthetic-backup-service",
        backup_volume_id="synthetic-backup-volume",
        backup_receipt_sha256="1" * 64, restore_receipt_sha256="2" * 64,
        rollback_command_template_sha256=COMMAND_SHA,
        inventory_digest="c" * 64, inventory_count=160,
        shopify_schema_sha256="d" * 64, communication_schema_sha256="e" * 64,
        approved_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=15),
        approved=True,
    )
    reference = Deployment("prior-deployment", TARGET, "9" * 40, "b" * 64,
                           "f" * 64, "SUCCESS", True)
    artifact = Deployment("rollback-artifact", TARGET, "a" * 40, "c" * 64,
                          COMMAND_SHA, "REMOVED", False)
    return approval, Provider(reference, artifact), Database(DatabaseState(heads, "d" * 64, "e" * 64))


def check(approval, provider, database, **overrides):
    params = dict(approvals=Approvals(approval), database=database, renderer=Renderer(),
                  expected_target=TARGET, command_template=COMMAND,
                  process_source_sha="a" * 40, process_inventory_digest="c" * 64,
                  process_inventory_count=160, process_flags=FLAGS.copy(), now=NOW)
    params.update(overrides)
    return validate_predeploy(**params)


def operator_check(approval, provider, operator=None, **overrides):
    payload = approval_payload_bytes(approval)
    params = dict(approval=approval, payload=payload,
                  pinned_payload_sha256=sha256(payload).hexdigest(),
                  provider=provider, operator=operator or Operator(approval),
                  renderer=Renderer(), command_template=COMMAND, now=NOW)
    params.update(overrides)
    return validate_operator_predeploy(**params)


class ContractTests(unittest.TestCase):
    def test_same_approval_accepts_0124_and_0125_read_only(self):
        approval, provider, database = fixture()
        for heads in (HEADS_BEFORE, HEADS_AFTER):
            database.value = DatabaseState(heads, "d" * 64, "e" * 64)
            authorized = check(approval, provider, database)
            self.assertEqual(authorized.approval, approval)
            self.assertEqual(authorized.execution_command_sha256, COMMAND_SHA)
        self.assertEqual(provider.reads, 0)
        self.assertEqual(database.writes, 0)

    def test_absent_wrong_action_and_expired_approval_fail_closed(self):
        approval, provider, database = fixture()
        with self.assertRaisesRegex(Refused, "trusted_approval_read_unavailable"):
            check(approval, provider, database, approvals=UnconfiguredApprovalReadPort())
        for altered in (replace(approval, approved=False),
                        replace(approval, action="forward_0125"),
                        replace(approval, action="migrate_0125")):
            with self.assertRaisesRegex(Refused, "action_not_approved"):
                check(altered, provider, database)
        with self.assertRaisesRegex(Refused, "approval_expired"):
            check(replace(approval, expires_at=NOW), provider, database)
        with self.assertRaisesRegex(Refused, "approval_window_too_long"):
            check(replace(approval, expires_at=NOW + timedelta(days=2)), provider, database)
        self.assertEqual(provider.reads, 0)

    def test_source_command_inventory_flags_and_target_stay_pinned(self):
        approval, provider, database = fixture()
        cases = (
            ({"process_source_sha": "f" * 40}, "source_mismatch"),
            ({"process_inventory_digest": "f" * 64}, "inventory_mismatch"),
            ({"process_inventory_count": 159}, "inventory_count_mismatch"),
            ({"command_template": b"other"}, "command_template_mismatch"),
            ({"process_flags": {**FLAGS, "SALLA_COMM_WHATSAPP_ENABLED": "true"}}, "flags_not_off"),
            ({"expected_target": replace(TARGET, service="other")}, "approval_target_mismatch"),
        )
        for changed, token in cases:
            with self.subTest(token=token), self.assertRaisesRegex(Refused, token):
                check(approval, provider, database, **changed)
        with self.assertRaisesRegex(Refused, "trusted_command_renderer_unavailable"):
            check(approval, provider, database, renderer=UnconfiguredCommandRenderPort())

    def test_only_two_exact_head_sets_and_schema_hashes_pass(self):
        approval, provider, database = fixture()
        cases = (
            (DatabaseState(frozenset({"0111", "0113", "0123"}), "d" * 64, "e" * 64),
             "database_heads_mismatch"),
            (DatabaseState(HEADS_AFTER, "f" * 64, "e" * 64), "shopify_schema_mismatch"),
            (DatabaseState(HEADS_BEFORE, "d" * 64, "f" * 64), "communication_schema_mismatch"),
        )
        for state, token in cases:
            database.value = state
            with self.subTest(token=token), self.assertRaisesRegex(Refused, token):
                check(approval, provider, database)
        with self.assertRaisesRegex(Refused, "approved_heads_mismatch"):
            check(replace(approval, heads_before=("0111", "0113", "0123")), provider, database)
        self.assertEqual(database.writes, 0)

    def test_approval_payload_is_exact_and_owner_verified(self):
        approval, provider, _ = fixture()
        payload = approval_payload_bytes(approval)
        with self.assertRaisesRegex(Refused, "approval_payload_invalid"):
            operator_check(approval, provider, payload=payload + b"x")
        with self.assertRaisesRegex(Refused, "approval_payload_invalid"):
            operator_check(replace(approval, candidate_source_sha="f" * 40), provider,
                           payload=payload, pinned_payload_sha256=sha256(payload).hexdigest())
        operator = Operator(approval)
        operator.owner_ok = False
        with self.assertRaisesRegex(Refused, "approval_provenance_mismatch"):
            operator_check(approval, provider, operator)
        with self.assertRaisesRegex(Refused, "command_template_mismatch"):
            operator_check(approval, provider, command_template=b"changed")

    def test_recovery_receipts_must_be_completed_and_bound(self):
        approval, provider, _ = fixture()
        operator = Operator(approval)
        self.assertEqual(operator_check(approval, provider, operator).backup_receipt_sha256,
                         approval.backup_receipt_sha256)
        operator.restore_complete = False
        with self.assertRaisesRegex(Refused, "recovery_evidence_mismatch"):
            operator_check(approval, provider, operator)
        operator.restore_complete = True
        operator.restore = "3" * 64
        with self.assertRaisesRegex(Refused, "recovery_evidence_mismatch"):
            operator_check(approval, provider, operator)

    def test_provider_and_receipt_are_required_after_artifact_exists(self):
        approval, provider, _ = fixture()
        with self.assertRaisesRegex(Refused, "trusted_railway_read_unavailable"):
            operator_check(approval, UnconfiguredRailwayReadPort())
        del provider.items["rollback-artifact"]
        with self.assertRaisesRegex(Refused, "trusted_railway_read_failed"):
            operator_check(approval, provider)

    def test_removed_artifact_requires_prior_success_and_launchability(self):
        approval, provider, _ = fixture()
        operator = Operator(approval)
        evidence = operator_check(approval, provider, operator)
        self.assertEqual(evidence.rollback_artifact.status, "REMOVED")
        operator.artifact_completed = False
        with self.assertRaisesRegex(Refused, "artifact_receipt_mismatch"):
            operator_check(approval, provider, operator)
        operator.artifact_completed = True
        provider.launchable = False
        with self.assertRaisesRegex(Refused, "artifact_not_launchable"):
            operator_check(approval, provider, operator)
        provider.launchable = True
        provider.items["rollback-artifact"] = replace(
            provider.items["rollback-artifact"], status="FAILED")
        with self.assertRaisesRegex(Refused, "artifact_not_launchable"):
            operator_check(approval, provider, operator)

    def test_artifact_identity_source_image_command_and_target_are_checked(self):
        approval, provider, _ = fixture()
        operator = Operator(approval)
        for field, value, token in (
            ("source_sha", "f" * 40, "artifact_source_mismatch"),
            ("image_digest", "f" * 64, "artifact_image_mismatch"),
            ("predeploy_command_sha256", "f" * 64, "artifact_command_mismatch"),
            ("target", replace(TARGET, service="wrong"), "artifact_target_mismatch"),
        ):
            original = provider.items["rollback-artifact"]
            provider.items["rollback-artifact"] = replace(original, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(Refused, token):
                operator_check(approval, provider, operator)
            provider.items["rollback-artifact"] = original
        operator.artifact_command = "0" * 64
        with self.assertRaisesRegex(Refused, "artifact_receipt_mismatch"):
            operator_check(approval, provider, operator)
        operator.artifact_command = COMMAND_SHA
        provider.current_id = "rollback-artifact"
        with self.assertRaisesRegex(Refused, "reference_unhealthy"):
            operator_check(approval, provider, operator)

    def test_result_is_new_serving_image_and_command(self):
        approval, provider, database = fixture()
        authorized = check(approval, provider, database)
        evidence = operator_check(approval, provider)
        result = Deployment("result-deployment", TARGET, "a" * 40, "c" * 64,
                            COMMAND_SHA, "SUCCESS", True)
        provider.items[result.deployment_id] = result
        provider.current_id = result.deployment_id
        self.assertEqual(validate_result(provider=provider, authorized=authorized,
                                         evidence=evidence, result_deployment_id=result.deployment_id,
                                         now=NOW), result)
        with self.assertRaisesRegex(Refused, "result_id_not_new"):
            validate_result(provider=provider, authorized=authorized, evidence=evidence,
                            result_deployment_id="rollback-artifact", now=NOW)
        for field, value, token in (
            ("image_digest", "f" * 64, "result_image_mismatch"),
            ("predeploy_command_sha256", "f" * 64, "result_command_mismatch"),
            ("status", "FAILED", "result_unhealthy"),
        ):
            provider.items[result.deployment_id] = replace(result, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(Refused, token):
                validate_result(provider=provider, authorized=authorized, evidence=evidence,
                                result_deployment_id=result.deployment_id, now=NOW)
        provider.items[result.deployment_id] = result
        provider.current_id = "prior-deployment"
        with self.assertRaisesRegex(Refused, "result_not_serving"):
            validate_result(provider=provider, authorized=authorized, evidence=evidence,
                            result_deployment_id=result.deployment_id, now=NOW)
        with self.assertRaisesRegex(Refused, "approval_expired"):
            validate_result(provider=provider, authorized=authorized, evidence=evidence,
                            result_deployment_id=result.deployment_id, now=approval.expires_at)


if __name__ == "__main__":
    unittest.main()
