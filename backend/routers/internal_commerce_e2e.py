"""Narrow admin-only API for the disabled-by-default INTERNAL_E2E channel."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from core.auth import require_admin
from core.database import get_db
from services.commerce_v2_internal_e2e import (
    InternalE2EContractError,
    InternalE2ETurnRequest,
    provision_internal_e2e_fixtures,
    reset_internal_e2e_customer,
    submit_internal_customer_turn,
)
from services.commerce_v2_internal_e2e_operator import (
    OPERATOR_TENANT_ID,
    batch_status,
    create_batch,
    execute_batch,
    inspect_result,
    operator_status,
    score_completed_batch,
)
from services.commerce_v2_phase_2_7a_acceptance import (
    acceptance_run_status,
    create_acceptance_run,
    execute_acceptance_run,
    load_acceptance_matrix,
    record_acceptance_review,
)


router = APIRouter(
    prefix="/admin/internal-e2e",
    tags=["Admin · Commerce INTERNAL_E2E"],
    dependencies=[Depends(require_admin)],
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvisionBody(_StrictBody):
    reset_aliases: list[Literal["A", "B", "C"]] = Field(default_factory=list, max_length=3)


class TurnBody(_StrictBody):
    alias: Literal["A", "B", "C"]
    text: str = Field(min_length=1, max_length=6000)
    case_id: str = Field(default="", max_length=96)
    service_tier: Literal["auto", "fast"] = "auto"


class BatchBody(_StrictBody):
    seed: int = Field(default=260914, ge=0, le=2_147_483_647)
    concurrency_waves: bool = False


class AcceptanceRunBody(_StrictBody):
    halt_on_first_failure: bool = False


class AcceptanceReviewBody(_StrictBody):
    turn_id: Literal["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4"]
    assertion_index: int = Field(ge=0, le=15)
    verdict: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=3, max_length=120)
    evidence: str = Field(min_length=5, max_length=2000)


def _raise_contract(exc: InternalE2EContractError) -> None:
    code = str(exc)
    status = 404 if code.endswith("not_found") else 409
    raise HTTPException(status_code=status, detail=code) from exc


@router.get("/status")
def get_status(db: Session = Depends(get_db)) -> dict:
    try:
        return operator_status(db)
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.post("/fixtures/provision")
def provision(body: ProvisionBody, db: Session = Depends(get_db)) -> dict:
    try:
        fixtures = provision_internal_e2e_fixtures(db, tenant_id=OPERATOR_TENANT_ID)
        resets = {
            alias: reset_internal_e2e_customer(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                synthetic_customer_alias=alias,
            )
            for alias in dict.fromkeys(body.reset_aliases)
        }
        if resets:
            fixtures = provision_internal_e2e_fixtures(
                db, tenant_id=OPERATOR_TENANT_ID
            )
        return {
            "tenant_id": OPERATOR_TENANT_ID,
            "fixtures": {alias: vars(item) for alias, item in fixtures.items()},
            "resets": resets,
        }
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


@router.post("/fixtures/{alias}/reset")
def reset(alias: Literal["A", "B", "C"], db: Session = Depends(get_db)) -> dict:
    try:
        return {
            "tenant_id": OPERATOR_TENANT_ID,
            "alias": alias,
            "deleted": reset_internal_e2e_customer(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                synthetic_customer_alias=alias,
            ),
        }
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


@router.post("/turns")
async def submit_turn(body: TurnBody, db: Session = Depends(get_db)) -> dict:
    try:
        return await submit_internal_customer_turn(
            db,
            InternalE2ETurnRequest(
                tenant_id=OPERATOR_TENANT_ID,
                synthetic_customer_alias=body.alias,
                text=body.text,
                case_id=body.case_id,
                service_tier=body.service_tier,
            ),
        )
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


@router.post("/batches", status_code=202)
def start_batch(
    body: BatchBody, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    try:
        result = create_batch(
            db, seed=body.seed, concurrency_waves=body.concurrency_waves
        )
        background.add_task(execute_batch, str(result["batch_id"]))
        return result
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: str = Path(min_length=36, max_length=36),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return batch_status(db, str(batch_id))
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.post("/batches/{batch_id}/score")
def score_batch_result(
    batch_id: str = Path(min_length=36, max_length=36), db: Session = Depends(get_db)
) -> dict:
    try:
        return score_completed_batch(db, str(batch_id))
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.get("/results")
def get_result(
    internal_message_id: str | None = Query(default=None, min_length=16, max_length=160),
    trace_id: str | None = Query(default=None, min_length=1, max_length=256),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return inspect_result(
            db, internal_message_id=internal_message_id, trace_id=trace_id
        )
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.get("/acceptance/phase-2-7a/matrix")
def get_acceptance_matrix() -> dict:
    """Return the checked-in canonical A1–C4 matrix (read-only, no execution)."""
    try:
        return load_acceptance_matrix().to_mapping()
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.post("/acceptance/phase-2-7a/runs", status_code=202)
def start_acceptance_run(
    body: AcceptanceRunBody, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Queue the deterministic 12-turn run; refused unless INTERNAL_E2E is enabled
    and the A/B/C fixtures (including C's synthetic order and shipment) exist."""
    try:
        result = create_acceptance_run(
            db, halt_on_first_failure=body.halt_on_first_failure
        )
        background.add_task(execute_acceptance_run, str(result["run_id"]))
        return result
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


@router.get("/acceptance/phase-2-7a/runs/{run_id}")
def get_acceptance_run(
    run_id: str = Path(min_length=36, max_length=36), db: Session = Depends(get_db)
) -> dict:
    try:
        return acceptance_run_status(db, str(run_id))
    except InternalE2EContractError as exc:
        _raise_contract(exc)


@router.post("/acceptance/phase-2-7a/runs/{run_id}/reviews")
def review_acceptance_run(
    body: AcceptanceReviewBody,
    run_id: str = Path(min_length=36, max_length=36),
    db: Session = Depends(get_db),
) -> dict:
    """Record one reviewer verdict on one required assertion of a finished run.

    Machine checks never produce a final PASS on their own: the run stays at
    MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING until every required assertion of
    every executed turn is approved here."""
    try:
        return record_acceptance_review(
            db,
            str(run_id),
            turn_id=body.turn_id,
            assertion_index=body.assertion_index,
            verdict=body.verdict,
            reviewer=body.reviewer,
            evidence=body.evidence,
        )
    except (InternalE2EContractError, ValueError) as exc:
        _raise_contract(InternalE2EContractError(str(exc)))


__all__ = ["router"]
