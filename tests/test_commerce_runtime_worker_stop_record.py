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
    node = {"id": DEPLOYMENT, "status": "CRASHED"}
    document = {"list": [node], "edges": {"deployments": {"edges": [{"node": node}]}},
                "single": node}[shape]
    record = job.build_record(deployment=DEPLOYMENT, incarnation=f"{DEPLOYMENT}/0",
                              platform_json=document, source="api", observed_at="2026-09-20T18:05:00+00:00")
    assert record["state"] == "crashed"


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
    assert verified == {"state": "removed", "incarnation": f"{DEPLOYMENT}/0"}

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
