"""Synthetic policy tests. No production data, network, or deployment."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest

from scripts.operators.salla_comm_rollback_only_contract import (
    ACTION, FLAG_NAMES, HEADS, Approval, DatabaseState, Deployment, Refused, Target,
    UnconfiguredApprovalReadPort, UnconfiguredRailwayReadPort, UnconfiguredCommandRenderPort,
    approval_payload_bytes, validate_operator_predeploy, validate_predeploy, validate_result,
)


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
COMMAND = b"synthetic reviewed rollback-only-0125 predeploy command"
TARGET = Target("project-test", "environment-test", "service-test", "database-test")
FLAGS = {name: "false" for name in FLAG_NAMES}


class Provider:
    def __init__(self, *deployments):
        self.items = {item.deployment_id: item for item in deployments}
        self.reads = 0
        self.current_id = deployments[0].deployment_id if deployments else None
        self.launchable = True

    def deployment(self, deployment_id):
        self.reads += 1
        return self.items[deployment_id]

    def current_deployment_id(self, target):
        return self.current_id

    def artifact_launchable(self, deployment_id, image_digest):
        return self.launchable and self.items[deployment_id].image_digest == image_digest


class Approvals:
    def __init__(self, record):
        self.record = record

    def approved_action(self):
        return self.record


class Operator:
    def __init__(self):
        self.owner_ok = True
        self.backup = "1" * 64
        self.restore = "2" * 64
        self.restore_complete = True

    def owner_approved(self, reference, approval, payload):
        if payload != approval_payload_bytes(approval):
            return False
        return self.owner_ok and reference == "synthetic-owner-message"

    def recovery_receipt(self, kind, target, service_id, volume_id):
        if service_id != "synthetic-backup-service" or volume_id != "synthetic-backup-volume":
            raise ValueError("wrong recovery target")
        from scripts.operators.salla_comm_rollback_only_contract import RecoveryReceipt
        return RecoveryReceipt(kind, target, service_id, volume_id,
                               self.backup if kind == "backup" else self.restore,
                               kind != "restore" or self.restore_complete)


class Database:
    def __init__(self, state):
        self.value = state
        self.writes = 0

    def state_read_only(self):
        return self.value


class Renderer:
    def render(self, command_template, approval):
        return command_template


def fixture():
    approval = Approval(
        action=ACTION, target=TARGET, candidate_source_sha="a" * 40,
        reference_deployment_id="prior-deployment",
        reference_source_sha="9" * 40, reference_image_digest="b" * 64,
        reference_predeploy_command_sha256="f" * 64,
        owner_approval_reference="synthetic-owner-message",
        backup_service_id="synthetic-backup-service",
        backup_volume_id="synthetic-backup-volume",
        backup_receipt_sha256="1" * 64, restore_receipt_sha256="2" * 64,
        rollback_command_template_sha256=sha256(COMMAND).hexdigest(), inventory_digest="c" * 64,
        inventory_count=160, shopify_schema_sha256="d" * 64,
        communication_schema_sha256="e" * 64,
        approved_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=15),
        approved=True,
        rollback_artifact_deployment_id="rollback-artifact",
        rollback_artifact_source_sha="a" * 40,
        rollback_artifact_image_digest="c" * 64,
        rollback_artifact_predeploy_command_sha256="0" * 64,
    )
    reference = Deployment("prior-deployment", TARGET, "9" * 40, "b" * 64,
                           "f" * 64, "SUCCESS", True)
    state = DatabaseState(HEADS, "d" * 64, "e" * 64)
    artifact = Deployment("rollback-artifact", TARGET, "a" * 40, "c" * 64,
                          "0" * 64, "SUCCESS", False)
    return approval, Provider(reference, artifact), Database(state)


PAYLOAD = approval_payload_bytes(fixture()[0])
PAYLOAD_SHA = sha256(PAYLOAD).hexdigest()


def check(approval, provider, database, **overrides):
    params = dict(approvals=Approvals(approval), database=database,
                  renderer=Renderer(), expected_target=TARGET, command_template=COMMAND,
                  process_source_sha="a" * 40,
                  process_inventory_digest="c" * 64, process_inventory_count=160,
                  process_flags=FLAGS.copy(), now=NOW)
    params.update(overrides)
    return validate_predeploy(**params)


class ContractTests(unittest.TestCase):
    def test_approved_terminal_action_is_read_only(self):
        approval, provider, database = fixture()
        authorized = check(approval, provider, database)
        self.assertEqual(authorized.approval, approval)
        self.assertEqual(authorized.execution_command_sha256, sha256(COMMAND).hexdigest())
        self.assertEqual(provider.reads, 0)
        self.assertEqual(database.writes, 0)

    def test_missing_or_untrusted_approval_fails_closed(self):
        approval, provider, database = fixture()
        with self.assertRaisesRegex(Refused, "trusted_approval_read_unavailable"):
            check(approval, provider, database, approvals=UnconfiguredApprovalReadPort())
        with self.assertRaisesRegex(Refused, "action_not_approved"):
            check(replace(approval, approved=False), provider, database)

    def test_forward_or_migration_approval_cannot_authorize_rollback(self):
        approval, provider, database = fixture()
        for action in ("forward_0125", "migrate_0125"):
            with self.subTest(action=action), self.assertRaisesRegex(Refused, "action_not_approved"):
                check(replace(approval, action=action), provider, database)

    def test_expired_approval_rejects_before_provider_or_database_read(self):
        approval, provider, database = fixture()
        with self.assertRaisesRegex(Refused, "approval_expired"):
            check(replace(approval, expires_at=NOW), provider, database)
        self.assertEqual(provider.reads, 0)
        self.assertEqual(database.writes, 0)

    def test_unbounded_approval_window_is_rejected(self):
        approval, provider, database = fixture()
        with self.assertRaisesRegex(Refused, "approval_window_too_long"):
            check(replace(approval, expires_at=NOW + timedelta(days=2)), provider, database)
        self.assertEqual(provider.reads, 0)

    def test_operator_provider_read_is_separate_and_required(self):
        approval, _, database = fixture()
        with self.assertRaisesRegex(Refused, "trusted_railway_read_unavailable"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=UnconfiguredRailwayReadPort(),
                                        operator=Operator(), now=NOW)
        self.assertEqual(database.writes, 0)

    def test_unconfigured_command_renderer_fails_closed(self):
        approval, provider, database = fixture()
        with self.assertRaisesRegex(Refused, "trusted_command_renderer_unavailable"):
            check(approval, provider, database, renderer=UnconfiguredCommandRenderPort())

    def test_provider_failure_is_a_closed_refusal(self):
        approval, provider, database = fixture()
        del provider.items[approval.reference_deployment_id]
        with self.assertRaisesRegex(Refused, "trusted_railway_read_failed"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=Operator(), now=NOW)

    def test_preflight_rejects_tampering(self):
        cases = (
            ({"process_source_sha": "f" * 40}, "source_mismatch"),
            ({"process_inventory_digest": "f" * 64}, "inventory_mismatch"),
            ({"process_inventory_count": 159}, "inventory_count_mismatch"),
            ({"command_template": b"other"}, "command_template_mismatch"),
            ({"process_flags": {**FLAGS, "SALLA_COMM_WHATSAPP_ENABLED": "true"}}, "flags_not_off"),
            ({"expected_target": replace(TARGET, service="other-service")}, "approval_target_mismatch"),
        )
        for change, token in cases:
            approval, provider, database = fixture()
            with self.subTest(token=token), self.assertRaisesRegex(Refused, token):
                check(approval, provider, database, **change)
            self.assertEqual(database.writes, 0)

    def test_reference_source_image_and_target_are_provider_bound(self):
        for field, value, token in (
            ("source_sha", "f" * 40, "reference_source_mismatch"),
            ("image_digest", "f" * 64, "reference_image_mismatch"),
            ("predeploy_command_sha256", "0" * 64, "reference_command_mismatch"),
            ("target", replace(TARGET, service="wrong"), "reference_target_mismatch"),
            ("status", "FAILED", "reference_unhealthy"),
        ):
            approval, provider, database = fixture()
            provider.items[approval.reference_deployment_id] = replace(
                provider.items[approval.reference_deployment_id], **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(Refused, token):
                validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                            provider=provider, operator=Operator(), now=NOW)

    def test_operator_owner_and_recovery_evidence_must_match(self):
        approval, provider, _ = fixture()
        operator = Operator()
        evidence = validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                                provider=provider, operator=operator, now=NOW)
        self.assertEqual(evidence.reference.deployment_id, "prior-deployment")
        operator.owner_ok = False
        with self.assertRaisesRegex(Refused, "approval_provenance_mismatch"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=operator, now=NOW)
        operator.owner_ok = True
        operator.restore = "3" * 64
        with self.assertRaisesRegex(Refused, "recovery_evidence_mismatch"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=operator, now=NOW)
        operator.restore = "2" * 64
        operator.restore_complete = False
        with self.assertRaisesRegex(Refused, "recovery_evidence_mismatch"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD, pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=operator, now=NOW)

    def test_operator_rejects_payload_pin_and_recovery_target_drift(self):
        approval, provider, _ = fixture()
        with self.assertRaisesRegex(Refused, "approval_payload_invalid"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD + b"x",
                                        pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=Operator(), now=NOW)
        with self.assertRaisesRegex(Refused, "recovery_evidence_read_failed"):
            changed = replace(approval, backup_service_id="wrong")
            validate_operator_predeploy(
                approval=changed, payload=approval_payload_bytes(changed),
                pinned_payload_sha256=sha256(approval_payload_bytes(changed)).hexdigest(),
                provider=provider, operator=Operator(), now=NOW)

    def test_typed_approval_must_match_the_exact_pinned_payload(self):
        approval, provider, _ = fixture()
        for changed in (replace(approval, candidate_source_sha="f" * 40),
                        replace(approval, rollback_artifact_image_digest="f" * 64)):
            with self.assertRaisesRegex(Refused, "approval_payload_invalid"):
                validate_operator_predeploy(approval=changed, payload=PAYLOAD,
                                            pinned_payload_sha256=PAYLOAD_SHA,
                                            provider=provider, operator=Operator(), now=NOW)
        ambiguous = PAYLOAD[:-1] + b',"approved":true}'
        with self.assertRaisesRegex(Refused, "approval_payload_invalid"):
            validate_operator_predeploy(approval=approval, payload=ambiguous,
                                        pinned_payload_sha256=sha256(ambiguous).hexdigest(),
                                        provider=provider, operator=Operator(), now=NOW)

    def test_rollback_artifact_is_distinct_and_provider_bound(self):
        approval, provider, _ = fixture()
        evidence = validate_operator_predeploy(approval=approval, payload=PAYLOAD,
                                                pinned_payload_sha256=PAYLOAD_SHA,
                                                provider=provider, operator=Operator(), now=NOW)
        self.assertEqual(evidence.rollback_artifact.deployment_id, "rollback-artifact")
        provider.launchable = False
        with self.assertRaisesRegex(Refused, "artifact_not_launchable"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD,
                                        pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=Operator(), now=NOW)
        provider.launchable = True
        for field, value, token in (
            ("source_sha", "f" * 40, "artifact_source_mismatch"),
            ("image_digest", "f" * 64, "artifact_image_mismatch"),
            ("predeploy_command_sha256", "f" * 64, "artifact_command_mismatch"),
            ("target", replace(TARGET, service="wrong"), "artifact_target_mismatch"),
        ):
            before = provider.items["rollback-artifact"]
            provider.items["rollback-artifact"] = replace(before, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(Refused, token):
                validate_operator_predeploy(approval=approval, payload=PAYLOAD,
                                            pinned_payload_sha256=PAYLOAD_SHA,
                                            provider=provider, operator=Operator(), now=NOW)
            provider.items["rollback-artifact"] = before

    def test_operator_requires_the_reference_to_be_serving(self):
        approval, provider, _ = fixture()
        provider.current_id = "different-serving-deployment"
        with self.assertRaisesRegex(Refused, "reference_not_serving"):
            validate_operator_predeploy(approval=approval, payload=PAYLOAD,
                                        pinned_payload_sha256=PAYLOAD_SHA,
                                        provider=provider, operator=Operator(), now=NOW)

    def test_0124_or_schema_drift_rejects_without_a_migration(self):
        for state, token in (
            (DatabaseState(frozenset({"0111", "0113", "0124"}), "d" * 64, "e" * 64),
             "database_heads_mismatch"),
            (DatabaseState(HEADS, "f" * 64, "e" * 64), "shopify_schema_mismatch"),
            (DatabaseState(HEADS, "d" * 64, "f" * 64), "communication_schema_mismatch"),
        ):
            approval, provider, database = fixture()
            database.value = state
            with self.subTest(token=token), self.assertRaisesRegex(Refused, token):
                check(approval, provider, database)
            self.assertEqual(database.writes, 0)

    def test_result_id_is_distinct_and_result_image_command_are_read_back(self):
        approval, provider, database = fixture()
        authorized = check(approval, provider, database)
        result = Deployment("new-deployment", TARGET, "a" * 40, "c" * 64,
                            authorized.execution_command_sha256, "SUCCESS", True)
        provider.items[result.deployment_id] = result
        provider.current_id = result.deployment_id
        self.assertEqual(validate_result(provider=provider, authorized=authorized,
                                         result_deployment_id="new-deployment", now=NOW), result)
        with self.assertRaisesRegex(Refused, "result_id_not_new"):
            validate_result(provider=provider, authorized=authorized,
                            result_deployment_id="prior-deployment", now=NOW)
        for field, value, token in (
            ("image_digest", "", "result_image_mismatch"),
            ("predeploy_command_sha256", "f" * 64, "result_command_mismatch"),
            ("status", "FAILED", "result_unhealthy"),
        ):
            provider.items[result.deployment_id] = replace(result, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(Refused, token):
                validate_result(provider=provider, authorized=authorized,
                                result_deployment_id="new-deployment", now=NOW)
        provider.items[result.deployment_id] = result
        provider.current_id = "some-other-deployment"
        with self.assertRaisesRegex(Refused, "result_not_serving"):
            validate_result(provider=provider, authorized=authorized,
                            result_deployment_id="new-deployment", now=NOW)
        provider.current_id = result.deployment_id
        with self.assertRaisesRegex(Refused, "approval_expired"):
            validate_result(provider=provider, authorized=authorized,
                            result_deployment_id="new-deployment", now=approval.expires_at)

    def test_failed_preflight_never_calls_a_deployment_writer(self):
        approval, provider, database = fixture()
        writer_calls = []

        def synthetic_operator():
            check(replace(approval, expires_at=NOW), provider, database)
            writer_calls.append("start")

        with self.assertRaisesRegex(Refused, "approval_expired"):
            synthetic_operator()
        self.assertEqual(writer_calls, [])
        self.assertEqual(database.writes, 0)


if __name__ == "__main__":
    unittest.main()

