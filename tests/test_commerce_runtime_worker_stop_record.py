"""The stop-record job: the platform's answer, read, before a worker is retired.

Offline: the platform capture is a file, as the runbook has the operator write
it. No Railway call is made, no fleet operation is performed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.path.join(_ROOT, "backend"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.commerce_runtime import handover  # noqa: E402
from scripts.operators import commerce_runtime_worker_stop_record as job  # noqa: E402

DEPLOYMENT = "477f560f-a47c-4750-966e-8acffc4c9596"
OTHER = "bc496dea-1ec8-40dd-bbed-dca67962ff70"


def capture(status: str, *, deployment: str = DEPLOYMENT) -> dict:
    """The shape ``railway deployment list --json`` / the API listing answers."""
    return {"deployments": [
        {"id": OTHER, "status": "SUCCESS", "meta": {"branch": "main"}},
        {"id": deployment, "status": status, "meta": {"branch": "main"},
         "updatedAt": "2026-09-20T18:00:00.000Z"},
    ]}


def run(tmp_path: Path, status: str, **overrides) -> tuple:
    source = tmp_path / "deployments.json"
    source.write_text(json.dumps(capture(status)), encoding="utf-8")
    out = tmp_path / "stop-record.json"
    argv = ["--deployment", overrides.get("deployment", DEPLOYMENT),
            "--incarnation", f"{DEPLOYMENT}/0",
            "--platform-json", str(source),
            "--source", "railway deployment list --service nahla-saas --json",
            "--out", str(out)]
    if overrides.get("observed_at"):
        argv += ["--observed-at", overrides["observed_at"]]
    code = job.main(argv)
    return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None)


def test_a_removed_deployment_yields_a_structured_record(tmp_path: Path, capsys) -> None:
    code, record = run(tmp_path, "REMOVED", observed_at="2026-09-20T18:05:00+00:00")
    assert code == job.EXIT_OK
    assert record["deployment"] == DEPLOYMENT
    assert record["incarnation"] == f"{DEPLOYMENT}/0"
    assert record["state"] == "removed" and record["platform_status"] == "REMOVED"
    assert record["active_replicas"] == 0
    assert record["observed_at"] == "2026-09-20T18:05:00+00:00"
    assert record["platform"] == "railway"
    assert json.loads(record["raw"])["id"] == DEPLOYMENT      # the capture, retained
    assert "RESULT=RECORDED" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["SUCCESS", "DEPLOYING", "BUILDING", "SLEEPING", "REMOVING"])
def test_an_active_or_transitional_deployment_yields_no_record(tmp_path: Path, status, capsys) -> None:
    code, record = run(tmp_path, status)
    assert code == job.EXIT_REFUSED and record is None
    out = capsys.readouterr().out
    assert "RESULT=REFUSED" in out and f"deployment_active:{status}" in out


def test_a_deployment_missing_from_the_capture_yields_no_record(tmp_path: Path, capsys) -> None:
    code, record = run(tmp_path, "REMOVED", deployment="not-in-this-listing")
    assert code == job.EXIT_REFUSED and record is None
    assert "deployment_not_in_capture" in capsys.readouterr().out


def test_an_unrecognised_status_is_refused_rather_than_guessed(tmp_path: Path, capsys) -> None:
    code, record = run(tmp_path, "PAUSED")
    assert code == job.EXIT_REFUSED and record is None
    assert "deployment_status_unrecognised:PAUSED" in capsys.readouterr().out


@pytest.mark.parametrize("shape", ["list", "edges", "single"])
def test_every_capture_shape_the_platform_answers_is_read(shape: str) -> None:
    node = {"id": DEPLOYMENT, "status": "REMOVED"}
    document = {"list": [node], "edges": {"deployments": {"edges": [{"node": node}]}},
                "single": node}[shape]
    record = job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0",
                              platform_json=document, source="api", observed_at="2026-09-20T18:05:00+00:00")
    assert record["state"] == "removed"


def test_the_record_the_job_writes_is_the_one_retirement_accepts(tmp_path: Path) -> None:
    """The record and the retirement validator agree on the contract: what the
    job writes for a removed deployment passes; the same record for a running
    one is never written, and a hand-edited one that says running is refused."""
    observed = "2026-09-20T18:05:00+00:00"
    code, record = run(tmp_path, "REMOVED", observed_at=observed)
    assert code == job.EXIT_OK
    text = json.dumps(record)
    moment = dt.datetime.fromisoformat(observed)
    verified = handover._validated_stop_record(text, deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    assert verified == {"state": "removed", "incarnation": f"{DEPLOYMENT}/0",
                        "active_replicas_basis": "inferred_from_status:REMOVED"}

    running = dict(record, state="running")
    with pytest.raises(handover.RetirementRefused, match="stop_record_state_is_not_inactive:running"):
        handover._validated_stop_record(json.dumps(running), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    replicas = dict(record, active_replicas=1)
    with pytest.raises(handover.RetirementRefused, match="stop_record_reports_active_replicas:1"):
        handover._validated_stop_record(json.dumps(replicas), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    elsewhere = dict(record, deployment=OTHER)
    with pytest.raises(handover.RetirementRefused, match="stop_record_does_not_name_the_deployment"):
        handover._validated_stop_record(json.dumps(elsewhere), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    later = dt.datetime.fromisoformat("2026-09-20T19:00:00+00:00")
    with pytest.raises(handover.RetirementRefused, match="stop_record_observation_differs_from_observed_at"):
        handover._validated_stop_record(text, deployment=DEPLOYMENT, observed=later)  # noqa: SLF001
    with pytest.raises(handover.RetirementRefused, match="stop_record_not_structured"):
        handover._validated_stop_record("deploy-1234 status=REMOVED", deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    partial = {k: v for k, v in record.items() if k != "incarnation"}
    with pytest.raises(handover.RetirementRefused, match="stop_record_missing:incarnation"):
        handover._validated_stop_record(json.dumps(partial), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001


def test_the_worker_identity_names_its_deployment_when_the_platform_does(monkeypatch) -> None:
    monkeypatch.delenv("RAILWAY_DEPLOYMENT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_REPLICA_ID", raising=False)
    bare = handover.worker_id()
    assert handover.worker_deployment(bare) is None
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", DEPLOYMENT)
    monkeypatch.setenv("RAILWAY_REPLICA_ID", "2")
    named = handover.worker_id()
    assert named == f"{DEPLOYMENT}/2@{bare}"
    assert handover.worker_deployment(named) == DEPLOYMENT


# ── R3: contradictory source evidence never becomes a record ────────────────


CONTRADICTORY_REPLICA_KEYS = ["active_replicas", "activeReplicas", "runningReplicas",
                              "running_instances", "activeInstances"]


@pytest.mark.parametrize("key", CONTRADICTORY_REPLICA_KEYS)
def test_a_capture_that_reports_an_active_replica_yields_no_record(tmp_path: Path, key, capsys) -> None:
    """Reproduction of the residual finding: the capture says REMOVED and, in
    the same breath, that one replica is active. The builder used to write
    ``active_replicas: 0`` and keep the contradiction only inside ``raw``. It
    now refuses by name and writes nothing."""
    source = tmp_path / "deployments.json"
    source.write_text(json.dumps({"id": DEPLOYMENT, "status": "REMOVED", key: 1}), encoding="utf-8")
    out = tmp_path / "stop-record.json"
    code = job.main(["--deployment", DEPLOYMENT, "--incarnation", f"{DEPLOYMENT}/0",
                     "--platform-json", str(source), "--source", "api", "--out", str(out)])
    assert code == job.EXIT_REFUSED and not out.exists()
    assert f"capture_reports_active_replicas:1:{key}" in capsys.readouterr().out


def test_instances_listed_as_running_in_the_capture_yield_no_record() -> None:
    capture = {"id": DEPLOYMENT, "status": "REMOVED",
               "instances": [{"id": "i-1", "status": "RUNNING"}, {"id": "i-2", "status": "STOPPED"}]}
    with pytest.raises(ValueError, match="capture_reports_active_replicas:1:instances"):
        job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0",
                         platform_json=capture, source="api", observed_at="2026-09-20T18:05:00+00:00")


def test_a_measured_zero_is_recorded_as_measured_and_an_absent_count_as_inferred() -> None:
    measured = job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0",
                                platform_json={"id": DEPLOYMENT, "status": "REMOVED", "activeReplicas": 0},
                                source="api", observed_at="2026-09-20T18:05:00+00:00")
    assert measured["active_replicas"] == 0
    assert measured["active_replicas_basis"] == "measured:activeReplicas"
    inferred = job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0",
                                platform_json={"id": DEPLOYMENT, "status": "REMOVED",
                                               "meta": {"serviceManifest": {"deploy": {"numReplicas": 2}}}},
                                source="api", observed_at="2026-09-20T18:05:00+00:00")
    # A configured replica count is what the deployment *asked for*, not what
    # is running; it is carried as configuration and does not contradict.
    assert inferred["active_replicas"] == 0
    assert inferred["active_replicas_basis"] == "inferred_from_status:REMOVED"
    assert inferred["configured_replicas"] == 2


@pytest.mark.parametrize("status", ["CRASHED"])
def test_a_status_the_platform_may_restart_from_is_not_a_fence(tmp_path: Path, status, capsys) -> None:
    """A crashed container is restarted under the service's restart policy
    with the same deployment and replica identity: the worker can come back.
    No record is written for it; the procedure is to remove the deployment."""
    code, record = run(tmp_path, status)
    assert code == job.EXIT_REFUSED and record is None
    out = capsys.readouterr().out
    assert f"deployment_status_not_a_fence:{status}" in out and "remove" in out


def test_contradictory_source_evidence_never_reaches_retirement(tmp_path: Path) -> None:
    """Builder → retirement, the path the finding travelled: the builder
    refuses the contradictory capture, and a record assembled by hand around
    that same capture is refused by the retirement validator, which re-reads
    the retained capture rather than trusting the top-level summary."""
    observed = "2026-09-20T18:05:00+00:00"
    moment = dt.datetime.fromisoformat(observed)
    capture = {"id": DEPLOYMENT, "status": "REMOVED", "active_replicas": 1}
    with pytest.raises(ValueError, match="capture_reports_active_replicas:1:active_replicas"):
        job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0", platform_json=capture,
                         source="api", observed_at=observed)
    by_hand = {"deployment": DEPLOYMENT, "incarnation": f"{DEPLOYMENT}/0", "state": "removed",
               "active_replicas": 0, "observed_at": observed, "source": "api",
               "raw": json.dumps(capture)}
    with pytest.raises(handover.RetirementRefused, match="stop_record_raw_reports_active_replicas:1"):
        handover._validated_stop_record(json.dumps(by_hand), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    for raw, needle in (
        (dict(capture, active_replicas=0, status="SUCCESS"), "stop_record_raw_state_is_active:success"),
        (dict(capture, active_replicas=0, status="CRASHED"), "stop_record_raw_state_is_not_a_fence:crashed"),
        (dict(capture, active_replicas=0, id=OTHER), "stop_record_raw_names_another_deployment"),
    ):
        contradicted = dict(by_hand, raw=json.dumps(raw))
        with pytest.raises(handover.RetirementRefused, match=needle):
            handover._validated_stop_record(json.dumps(contradicted), deployment=DEPLOYMENT, observed=moment)  # noqa: SLF001
    consistent = dict(by_hand, raw=json.dumps(dict(capture, active_replicas=0)))
    assert handover._validated_stop_record(json.dumps(consistent), deployment=DEPLOYMENT, observed=moment)[  # noqa: SLF001
        "state"] == "removed"


@pytest.mark.parametrize("state", ["crashed", "stopped", "exited", "inactive"])
def test_a_restartable_state_word_is_refused_as_not_a_fence(state: str) -> None:
    """Inactive is not fenced: a stopped, exited or crashed process can be
    started again under the same identity. Only a state the platform will not
    run again establishes the fence a retirement rests on."""
    record = {"deployment": DEPLOYMENT, "incarnation": f"{DEPLOYMENT}/0", "state": state,
              "active_replicas": 0, "observed_at": "2026-09-20T18:05:00+00:00", "source": "api"}
    with pytest.raises(handover.RetirementRefused, match=f"stop_record_state_is_not_a_fence:{state}"):
        handover._validated_stop_record(json.dumps(record), deployment=DEPLOYMENT,  # noqa: SLF001
                                        observed=dt.datetime.fromisoformat("2026-09-20T18:05:00+00:00"))

