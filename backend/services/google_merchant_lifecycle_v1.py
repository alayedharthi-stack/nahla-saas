"""services/google_merchant_lifecycle_v1.py
───────────────────────────────────────────
Fail-closed Google Merchant API v1 lifecycle planning (offline) and a
fake-transport-only simulation.

Inputs are caller-supplied, in-memory records: ownership evidence, remote
observations, unresolved prior writes and an optional complete-catalog
snapshot proof. Nothing here is a durable ledger, and nothing verifies
ownership with Google; every record must carry its full scope (tenant,
account, data source, language, feed label, offer id) and a fresh
``observed_at`` or it blocks the offer.

Planning never calls a transport. :func:`simulate_plan` runs only with an
explicit :class:`SimulationOptIn` AND an exact :class:`FakeMerchantTransport`;
there is no real publishing interface in this module.

Identity rules
──────────────
* A desired offer must carry mapper tenant provenance equal to the
  binding's tenant, and its envelope ``offer_id`` must equal the payload
  ``offerId`` exactly.
* Offer ids are case-sensitive. Case-folding is used only to DETECT
  collisions: any offer or record whose id differs from the decided id
  (even by case) blocks; it never authorizes a write.
* An insert payload may carry only ``offerId`` / ``contentLanguage`` /
  ``feedLabel`` / ``productAttributes``; identity-affecting options such as
  ``legacyLocal`` are refused.
* Executable actions carry an immutable canonical snapshot taken at
  planning time. Before a simulated send the action must still equal it
  and target exactly the decided identity, data source and payload, or it
  is refused unsent; what is sent is rebuilt from the snapshot.

Decision rules (per offer identity)
───────────────────────────────────
* INSERT: no ownership evidence, offer passes offline checks, and a fresh
  ``products.get`` 404 observation. A list page never proves absence; a
  product present in another source blocks (insert could move ownership).
* PATCH / NOOP: fresh ownership evidence AND a fresh observation that the
  product is present in the bound API source; NOOP when the attribute
  fingerprint is unchanged.
* DELETE: owned offer no longer desired, an explicit complete
  :class:`CatalogSnapshotProof`, and fresh present-in-source observation.
* Anything else — foreign / stale / future / ambiguous evidence, unresolved
  prior write, unknown remote state — is BLOCKED with reasons.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.google_merchant_products_v1 import (
    OFFER_STATUS_PASSES_OFFLINE_CHECKS,
    GoogleMerchantTarget,
    OfferPreview,
)
from services.google_merchant_protocol_v1 import (
    READBACK_FOREIGN_SOURCE,
    INSERT_BODY_FIELDS,
    OP_DELETE,
    OP_INSERT,
    OP_PATCH,
    READBACK_IDENTITY_MATCHES,
    RESP_ACCEPTED,
    RESP_NOT_FOUND,
    RESP_UNCERTAIN_WRITE,
    SUPPORTED_PRODUCT_ATTRIBUTES,
    UPDATE_MASK_ATTRIBUTE_PREFIX,
    MerchantApiBinding,
    MerchantRequest,
    ProductReadback,
    ProtocolShapeError,
    RetryPolicy,
    TransportResponse,
    attribute_changes,
    attributes_match,
    canonical_json,
    classify_response,
    delete_request,
    freshness_issue,
    get_product_request,
    insert_request,
    next_retry_at,
    parse_product_readback,
    patch_request,
    payload_fingerprint,
    readback_identity_state,
    request_from_snapshot,
    request_snapshot,
    validate_binding,
    write_target,
)

ACTION_INSERT = "INSERT"
ACTION_PATCH = "PATCH"
ACTION_DELETE = "DELETE"
ACTION_NOOP = "NOOP"
ACTION_BLOCKED = "BLOCKED"
EXECUTABLE_ACTIONS = frozenset({ACTION_INSERT, ACTION_PATCH, ACTION_DELETE})

OBS_ABSENT = "absent"
OBS_PRESENT_IN_SOURCE = "present_in_source"
OBS_PRESENT_FOREIGN_SOURCE = "present_foreign_source"
OBS_UNKNOWN = "unknown"
ORIGIN_PRODUCTS_GET = "products.get"
ORIGIN_PRODUCTS_LIST = "products.list"

SIMULATION_ACK = "fake_transport_only"

OUTCOME_CONFIRMED = "content_confirmed"  # write applied; publication is separate
OUTCOME_UNRESOLVED = "unresolved_requires_later_readback"
OUTCOME_PERMANENT_FAILURE = "permanent_failure"
OUTCOME_GAVE_UP = "gave_up"
OUTCOME_REFUSED = "refused_request_integrity"


class SimulationNotPermitted(RuntimeError):
    """Simulation needs the explicit opt-in and the exact fake transport."""


# ─────────────────────────────────────────────────────────────────────────────
# Caller-supplied records
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OfferScope:
    tenant_id: int
    account_id: str
    data_source_id: str
    content_language: str
    feed_label: str
    offer_id: str

    def key(self) -> Tuple[str, str, str]:
        return (self.content_language, self.feed_label, self.offer_id.casefold())

    def matches(self, binding: MerchantApiBinding) -> bool:
        return (
            self.tenant_id, self.account_id, self.data_source_id, self.content_language, self.feed_label,
        ) == (
            binding.tenant_id, binding.account_id, binding.data_source_id,
            binding.content_language, binding.feed_label,
        )


def scope_for(binding: MerchantApiBinding, offer_id: str) -> OfferScope:
    return OfferScope(
        binding.tenant_id, binding.account_id, binding.data_source_id,
        binding.content_language, binding.feed_label, offer_id,
    )


@dataclass(frozen=True)
class FreshnessPolicy:
    binding_max_age_s: int
    ownership_max_age_s: int
    observation_max_age_s: int

    def __post_init__(self) -> None:
        for name in ("binding_max_age_s", "ownership_max_age_s", "observation_max_age_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}_invalid")


@dataclass(frozen=True)
class OwnedOfferEvidence:
    """Attributes last CONFIRMED applied by this platform for one scope."""

    scope: OfferScope
    product_attributes: Mapping[str, Any]
    observed_at: int

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(dict(self.product_attributes))


@dataclass(frozen=True)
class RemoteObservation:
    scope: OfferScope
    state: str
    origin: str
    observed_at: int


@dataclass(frozen=True)
class PendingWrite:
    """A prior write whose outcome is not yet confirmed by read-back."""

    scope: OfferScope
    submitted_at: int


@dataclass(frozen=True)
class CatalogSnapshotProof:
    """Caller's explicit claim that the offers passed are the COMPLETE current catalog."""

    tenant_id: int
    content_language: str
    feed_label: str
    complete: bool
    observed_at: int


def observation_from_readback(
    binding: MerchantApiBinding, offer_id: str, readback: ProductReadback, observed_at: int,
    origin: str = ORIGIN_PRODUCTS_GET,
) -> RemoteObservation:
    identity = readback_identity_state(readback, binding, offer_id)
    state = {
        READBACK_IDENTITY_MATCHES: OBS_PRESENT_IN_SOURCE,
        READBACK_FOREIGN_SOURCE: OBS_PRESENT_FOREIGN_SOURCE,
    }.get(identity, OBS_UNKNOWN)
    return RemoteObservation(scope_for(binding, offer_id), state, origin, observed_at)


def observation_absent_from_get(binding: MerchantApiBinding, offer_id: str, observed_at: int) -> RemoteObservation:
    """A ``products.get`` 404 for exactly this identity."""
    return RemoteObservation(scope_for(binding, offer_id), OBS_ABSENT, ORIGIN_PRODUCTS_GET, observed_at)


# ─────────────────────────────────────────────────────────────────────────────
# Planning
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlannedAction:
    offer_id: str
    action: str
    reasons: Tuple[str, ...]
    request: Optional[MerchantRequest] = None
    desired_attributes: Optional[Dict[str, Any]] = None
    removed_attributes: Tuple[str, ...] = ()
    # Immutable canonical text of the decision taken at planning time.
    # Simulation refuses an action whose current state no longer matches it.
    snapshot: str = ""


def action_snapshot(
    offer_id: str, action: str, request: Optional[MerchantRequest],
    desired: Optional[Mapping[str, Any]], removed: Sequence[str],
) -> str:
    return canonical_json({
        "offer_id": offer_id,
        "action": action,
        "request": None if request is None else json.loads(request_snapshot(request)),
        "desired": desired,
        "removed": list(removed),
    })


def _executable(
    offer_id: str, action: str, reasons: Tuple[str, ...], request: MerchantRequest,
    desired: Optional[Dict[str, Any]] = None, removed: Tuple[str, ...] = (),
) -> PlannedAction:
    return PlannedAction(
        offer_id, action, reasons, request, desired, removed,
        action_snapshot(offer_id, action, request, desired, removed),
    )


@dataclass(frozen=True)
class LifecyclePlan:
    tenant_id: int
    binding: Optional[MerchantApiBinding]
    planned_at: int
    binding_reasons: Tuple[str, ...]
    actions: Tuple[PlannedAction, ...]

    @property
    def executable(self) -> Tuple[PlannedAction, ...]:
        return tuple(a for a in self.actions if a.action in EXECUTABLE_ACTIONS)

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint({
            "tenant_id": self.tenant_id,
            "data_source": self.binding.data_source_name if self.binding else None,
            "binding_reasons": list(self.binding_reasons),
            "actions": [
                {
                    "offer_id": a.offer_id, "action": a.action, "reasons": list(a.reasons),
                    "request": None if a.request is None else [
                        a.request.method, a.request.path, [list(q) for q in a.request.query], a.request.body,
                    ],
                }
                for a in self.actions
            ],
        })


def _remote_state(obs: Optional[RemoteObservation], now: int, max_age_s: int) -> Tuple[str, Optional[str]]:
    """``(state, unverified_reason)``; reason is set whenever state cannot be trusted."""
    if obs is None:
        return OBS_UNKNOWN, "remote_observation_missing"
    stale = freshness_issue(obs.observed_at, now, max_age_s)
    if stale:
        return OBS_UNKNOWN, f"remote_observation_{stale}"
    if obs.state == OBS_ABSENT and obs.origin != ORIGIN_PRODUCTS_GET:
        return OBS_UNKNOWN, "absence_not_proven_by_list_page"
    if obs.state not in (OBS_ABSENT, OBS_PRESENT_IN_SOURCE, OBS_PRESENT_FOREIGN_SOURCE):
        return OBS_UNKNOWN, "remote_state_unknown"
    return obs.state, None


def _snapshot_reason(
    proof: Optional[CatalogSnapshotProof], binding: MerchantApiBinding, now: int, max_age_s: int,
) -> Optional[str]:
    if not isinstance(proof, CatalogSnapshotProof) or proof.complete is not True:
        return "retirement_requires_complete_snapshot_proof"
    if (proof.tenant_id, proof.content_language, proof.feed_label) != (
        binding.tenant_id, binding.content_language, binding.feed_label,
    ):
        return "snapshot_proof_scope_mismatch"
    stale = freshness_issue(proof.observed_at, now, max_age_s)
    return f"snapshot_proof_{stale}" if stale else None


def _blocked(offer_id: str, reasons: Iterable[str]) -> PlannedAction:
    return PlannedAction(offer_id, ACTION_BLOCKED, tuple(sorted(set(reasons))))


def _decide(
    binding: MerchantApiBinding,
    offers: List[OfferPreview],
    evidence: List[OwnedOfferEvidence],
    observations: List[RemoteObservation],
    pending: List[PendingWrite],
    *,
    now: int,
    freshness: FreshnessPolicy,
    snapshot_proof: Optional[CatalogSnapshotProof],
) -> PlannedAction:
    offer = offers[0] if offers else None
    owned = evidence[0] if evidence else None
    offer_id = offer.offer_id if offer is not None else evidence[0].scope.offer_id
    reasons: List[str] = []

    records = [*evidence, *observations, *pending]
    if any(not r.scope.matches(binding) for r in records):
        reasons.append("evidence_scope_mismatch")
    # Case-folding only DETECTS collisions. Every offer and record must carry
    # the decided identity exactly; a case-only variant never authorizes.
    if len({o.offer_id for o in offers} | {r.scope.offer_id for r in records}) > 1:
        reasons.append("offer_id_case_conflict")
    if len(offers) > 1:
        reasons.append("duplicate_offer_identity")
    if len(evidence) > 1:
        reasons.append("ambiguous_ownership_evidence")
    if len(observations) > 1:
        reasons.append("ambiguous_remote_observation")
    if pending:
        reasons.append("prior_write_unresolved")
    desired: Dict[str, Any] = {}
    if offer is not None:
        body = offer.product_input
        if offer.tenant_id is None:
            reasons.append("offer_tenant_provenance_missing")
        elif type(offer.tenant_id) is not int or offer.tenant_id != binding.tenant_id:
            reasons.append("offer_tenant_mismatch")
        if body.get("offerId") != offer.offer_id:
            reasons.append("offer_identity_envelope_mismatch")
        if set(body) != INSERT_BODY_FIELDS:
            reasons.append("product_input_field_not_allowed")
        if offer.status != OFFER_STATUS_PASSES_OFFLINE_CHECKS:
            reasons.append("offer_blocked_offline")
        if (body.get("contentLanguage"), body.get("feedLabel")) != (binding.content_language, binding.feed_label):
            reasons.append("offer_target_mismatch")
        attrs = body.get("productAttributes")
        if isinstance(attrs, Mapping):
            desired = copy.deepcopy(dict(attrs))
        else:
            reasons.append("product_attributes_missing")
        if set(desired) - SUPPORTED_PRODUCT_ATTRIBUTES:
            reasons.append("attribute_not_in_supported_schema")
    if owned is not None:
        stale = freshness_issue(owned.observed_at, now, freshness.ownership_max_age_s)
        if stale:
            reasons.append(f"ownership_evidence_{stale}")
        if set(owned.product_attributes) - SUPPORTED_PRODUCT_ATTRIBUTES:
            reasons.append("owned_attributes_not_in_supported_schema")
    if offer is None and owned is not None:
        retire = _snapshot_reason(snapshot_proof, binding, now, freshness.observation_max_age_s)
        if retire:
            reasons.append(retire)
    if reasons:
        return _blocked(offer_id, reasons)

    remote, unverified = _remote_state(observations[0] if observations else None, now, freshness.observation_max_age_s)
    if remote == OBS_PRESENT_FOREIGN_SOURCE:
        return _blocked(offer_id, ["foreign_source_owns_offer"])
    if unverified:
        return _blocked(offer_id, [unverified])

    try:
        if offer is not None and owned is None:
            if remote == OBS_PRESENT_IN_SOURCE:
                return _blocked(offer_id, ["ownership_evidence_missing"])
            return _executable(
                offer_id, ACTION_INSERT, ("absence_verified",),
                insert_request(binding, offer.product_input, offer_id=offer_id), desired,
            )
        if offer is not None:
            if remote == OBS_ABSENT:
                return _blocked(offer_id, ["owned_offer_absent_remotely"])
            if payload_fingerprint(desired) == owned.fingerprint:
                return PlannedAction(offer_id, ACTION_NOOP, ("fingerprint_unchanged",))
            _changed, removed = attribute_changes(desired, owned.product_attributes)
            return _executable(
                offer_id, ACTION_PATCH, ("fingerprint_changed",),
                patch_request(binding, offer_id, desired, owned.product_attributes), desired, removed,
            )
        if remote == OBS_ABSENT:
            return PlannedAction(offer_id, ACTION_NOOP, ("already_absent",))
        return _executable(offer_id, ACTION_DELETE, ("retired_from_complete_snapshot",), delete_request(binding, offer_id))
    except ProtocolShapeError as exc:
        return _blocked(offer_id, [str(exc)])


def plan_lifecycle(
    target: GoogleMerchantTarget,
    binding: Optional[MerchantApiBinding],
    offers: Sequence[OfferPreview],
    *,
    now: int,
    freshness: FreshnessPolicy,
    owned: Sequence[OwnedOfferEvidence] = (),
    observations: Sequence[RemoteObservation] = (),
    pending: Sequence[PendingWrite] = (),
    snapshot_proof: Optional[CatalogSnapshotProof] = None,
) -> LifecyclePlan:
    """Deterministic plan. Performs no I/O and never calls a transport."""
    binding_reasons = validate_binding(binding, target, now=now, max_age_s=freshness.binding_max_age_s)

    for offer in offers:
        if (
            not isinstance(offer, OfferPreview)
            or not isinstance(offer.offer_id, str) or not offer.offer_id
            or not isinstance(offer.product_input, Mapping)
        ):
            raise TypeError("offer_not_mapper_preview")
    groups: Dict[Tuple[str, str, str], Dict[str, list]] = {}

    def slot(key: Tuple[str, str, str]) -> Dict[str, list]:
        return groups.setdefault(key, {"offers": [], "evidence": [], "observations": [], "pending": []})

    for offer in offers:
        body = offer.product_input
        slot((str(body.get("contentLanguage", "")), str(body.get("feedLabel", "")), offer.offer_id.casefold()))["offers"].append(offer)
    for record in owned:
        slot(record.scope.key())["evidence"].append(record)
    for record in observations:
        if record.scope.key() in groups:
            groups[record.scope.key()]["observations"].append(record)
    for record in pending:
        if record.scope.key() in groups:
            groups[record.scope.key()]["pending"].append(record)

    actions: List[PlannedAction] = []
    for key in sorted(groups):
        group = groups[key]
        if binding_reasons:
            first = group["offers"][0].offer_id if group["offers"] else group["evidence"][0].scope.offer_id
            actions.append(_blocked(first, binding_reasons))
            continue
        actions.append(_decide(
            binding, group["offers"], group["evidence"], group["observations"], group["pending"],
            now=now, freshness=freshness, snapshot_proof=snapshot_proof,
        ))
    return LifecyclePlan(target.tenant_id, binding, now, binding_reasons, tuple(actions))


# ─────────────────────────────────────────────────────────────────────────────
# Fake-only simulation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SimulationOptIn:
    acknowledge: str


class FakeMerchantTransport:
    """Scripted in-process responses keyed by ``(method, path)``. No I/O."""

    def __init__(self, script: Optional[Mapping[Tuple[str, str], Sequence[TransportResponse]]] = None) -> None:
        self._queues: Dict[Tuple[str, str], List[TransportResponse]] = {
            key: list(value) for key, value in (script or {}).items()
        }
        self.calls: List[MerchantRequest] = []

    def send(self, request: MerchantRequest) -> TransportResponse:
        self.calls.append(request)
        queue = self._queues.get((request.method, request.path))
        if not queue:
            return TransportResponse(None, error="fake_script_exhausted")
        return queue.pop(0)


@dataclass(frozen=True)
class AttemptRecord:
    op: str
    at: int
    status: Optional[int]
    error: Optional[str]
    classification: str


@dataclass(frozen=True)
class SimulationResult:
    scope: OfferScope
    action: str
    outcome: str
    reasons: Tuple[str, ...]
    attempts: Tuple[AttemptRecord, ...]
    retry_schedule: Tuple[int, ...]
    publication_state: Optional[str] = None
    readback: Optional[ProductReadback] = None
    confirmed_at: Optional[int] = None
    desired_attributes: Optional[Dict[str, Any]] = field(default=None)

    @property
    def observation(self) -> Optional[RemoteObservation]:
        """Fresh observation implied by a CONFIRMED result; None otherwise."""
        if self.outcome != OUTCOME_CONFIRMED:
            return None
        state = OBS_ABSENT if self.action == ACTION_DELETE else OBS_PRESENT_IN_SOURCE
        return RemoteObservation(self.scope, state, ORIGIN_PRODUCTS_GET, self.confirmed_at)


def _send(transport, request, now, policy, attempts, schedule):
    """Send with resend only where classification allows it (429; reads on 5xx/timeout)."""
    attempt, clock = 1, now
    while True:
        response = transport.send(request)
        cls = classify_response(response, write=request.is_write, now=clock)
        attempts.append(AttemptRecord(request.op, clock, response.status, response.error, cls.kind))
        if not cls.resend_allowed:
            return response, cls, clock, False
        nxt = next_retry_at(policy, attempt, clock, cls.retry_after_s)
        if nxt is None:
            return response, cls, clock, True
        schedule.append(nxt)
        clock, attempt = nxt, attempt + 1


_ACTION_OPS = {ACTION_INSERT: OP_INSERT, ACTION_PATCH: OP_PATCH, ACTION_DELETE: OP_DELETE}


def _integrity_reason(binding: MerchantApiBinding, action: PlannedAction) -> Optional[str]:
    """The request about to be sent must still target exactly the decided identity."""
    req = action.request
    op = _ACTION_OPS.get(action.action)
    if not isinstance(req, MerchantRequest) or op is None:
        return "request_missing"
    try:
        current = action_snapshot(
            action.offer_id, action.action, req, action.desired_attributes, action.removed_attributes,
        )
    except (TypeError, ValueError):
        return "request_not_serializable"
    if not action.snapshot or current != action.snapshot:
        return "request_mutated_after_planning"
    if (req.op, req.method, req.path) != (op, *write_target(binding, op, action.offer_id)):
        return "request_target_mismatch"
    if not req.query or req.query[0] != ("dataSource", binding.data_source_name):
        return "request_data_source_mismatch"
    desired = action.desired_attributes
    if op == OP_DELETE:
        return None if req.query == (("dataSource", binding.data_source_name),) and req.body is None else (
            "request_shape_mismatch")
    if not isinstance(req.body, Mapping) or not isinstance(desired, Mapping):
        return "request_shape_mismatch"
    if op == OP_INSERT:
        if set(req.body) != INSERT_BODY_FIELDS:
            return "request_field_not_allowed"
        identity = (req.body.get("offerId"), req.body.get("contentLanguage"), req.body.get("feedLabel"))
        if identity != (action.offer_id, binding.content_language, binding.feed_label):
            return "request_identity_mismatch"
        if len(req.query) != 1 or canonical_json(req.body.get("productAttributes")) != canonical_json(desired):
            return "request_payload_mismatch"
        return None
    sent = req.body.get("productAttributes")
    if set(req.body) != {"productAttributes"} or not isinstance(sent, Mapping):
        return "request_payload_mismatch"
    mask = dict(req.query[1:]).get("updateMask", "") if len(req.query) == 2 else ""
    expected_mask = {UPDATE_MASK_ATTRIBUTE_PREFIX + k for k in (*sent, *action.removed_attributes)}
    if (
        any(k not in desired or canonical_json(v) != canonical_json(desired[k]) for k, v in sent.items())
        or any(k in desired or k in sent for k in action.removed_attributes)
        or set(mask.split(",")) != expected_mask
    ):
        return "request_payload_mismatch"
    return None


def _simulate_action(binding, action, transport, now, policy) -> Tuple[SimulationResult, int]:
    attempts: List[AttemptRecord] = []
    schedule: List[int] = []
    scope = scope_for(binding, action.offer_id)

    def result(outcome, reasons, clock, **extra):
        return SimulationResult(
            scope, action.action, outcome, tuple(reasons), tuple(attempts), tuple(schedule),
            desired_attributes=action.desired_attributes, **extra,
        ), clock

    integrity = _integrity_reason(binding, action)
    if integrity:
        return result(OUTCOME_REFUSED, [integrity], now)
    # From here on work only on a copy rebuilt from the verified snapshot: no
    # object the caller (or the transport) holds can alter what is sent or
    # what a read-back is compared against.
    frozen = json.loads(action.snapshot)
    action = replace(
        action,
        request=request_from_snapshot(canonical_json(frozen["request"])),
        desired_attributes=frozen["desired"],
        removed_attributes=tuple(frozen["removed"]),
    )
    _resp, cls, clock, gave_up = _send(transport, action.request, now, policy, attempts, schedule)
    if gave_up:
        return result(OUTCOME_GAVE_UP, ["write_retries_exhausted"], clock)
    if cls.kind not in (RESP_ACCEPTED, RESP_UNCERTAIN_WRITE):
        return result(OUTCOME_PERMANENT_FAILURE, [cls.kind], clock)
    note = ["write_uncertain_readback_only"] if cls.kind == RESP_UNCERTAIN_WRITE else []

    rb_resp, rcls, clock, gave_up = _send(
        transport, get_product_request(binding, action.offer_id), clock, policy, attempts, schedule,
    )
    if gave_up:
        return result(OUTCOME_UNRESOLVED, note + ["readback_retries_exhausted"], clock)
    if action.action == ACTION_DELETE:
        if rcls.kind == RESP_NOT_FOUND:
            return result(OUTCOME_CONFIRMED, note + ["readback_absent_after_owned_delete"], clock, confirmed_at=clock)
        return result(OUTCOME_UNRESOLVED, note + [f"readback_{rcls.kind}"], clock)
    if rcls.kind != RESP_ACCEPTED:
        return result(OUTCOME_UNRESOLVED, note + [f"readback_{rcls.kind}"], clock)
    readback = parse_product_readback(rb_resp.body)
    if readback.malformed:
        return result(OUTCOME_UNRESOLVED, note + ["readback_malformed"], clock, readback=readback)
    identity = readback_identity_state(readback, binding, action.offer_id)
    if identity != READBACK_IDENTITY_MATCHES:
        return result(OUTCOME_UNRESOLVED, note + [f"readback_{identity}"], clock, readback=readback)
    if not attributes_match(action.desired_attributes or {}, readback.product_attributes, action.removed_attributes):
        return result(OUTCOME_UNRESOLVED, note + ["readback_content_not_matching"], clock, readback=readback)
    return result(
        OUTCOME_CONFIRMED, note + ["readback_matches_desired"], clock,
        publication_state=readback.publication_state, readback=readback, confirmed_at=clock,
    )


def simulate_plan(
    plan: LifecyclePlan,
    transport: FakeMerchantTransport,
    *,
    opt_in: SimulationOptIn,
    now: int,
    retry_policy: RetryPolicy,
) -> Tuple[SimulationResult, ...]:
    """Run executable actions against the exact fake transport on a virtual clock."""
    if not isinstance(opt_in, SimulationOptIn) or opt_in.acknowledge != SIMULATION_ACK:
        raise SimulationNotPermitted("explicit_fake_opt_in_required")
    if type(transport) is not FakeMerchantTransport:  # exact type: subclasses and mocks are refused
        raise SimulationNotPermitted("fake_transport_only")
    if plan.binding_reasons or plan.binding is None:
        return ()
    results: List[SimulationResult] = []
    clock = now
    for action in plan.executable:
        outcome, clock = _simulate_action(plan.binding, action, transport, clock, retry_policy)
        results.append(outcome)
    return tuple(results)


def evidence_after_apply(
    owned: Sequence[OwnedOfferEvidence], results: Sequence[SimulationResult],
) -> Tuple[OwnedOfferEvidence, ...]:
    """New in-memory evidence: only CONFIRMED results change ownership."""
    by_key: Dict[Tuple[Any, ...], OwnedOfferEvidence] = {
        (r.scope.tenant_id, r.scope.account_id, r.scope.data_source_id, *r.scope.key()): r for r in owned
    }
    for res in results:
        if res.outcome != OUTCOME_CONFIRMED:
            continue
        key = (res.scope.tenant_id, res.scope.account_id, res.scope.data_source_id, *res.scope.key())
        if res.action == ACTION_DELETE:
            by_key.pop(key, None)
        else:
            attrs = copy.deepcopy(dict(res.desired_attributes or {}))
            by_key[key] = OwnedOfferEvidence(res.scope, attrs, res.confirmed_at)
    return tuple(by_key[k] for k in sorted(by_key, key=canonical_json))


__all__ = [
    "ACTION_BLOCKED",
    "ACTION_DELETE",
    "ACTION_INSERT",
    "ACTION_NOOP",
    "ACTION_PATCH",
    "CatalogSnapshotProof",
    "FakeMerchantTransport",
    "FreshnessPolicy",
    "LifecyclePlan",
    "OfferScope",
    "OwnedOfferEvidence",
    "PendingWrite",
    "PlannedAction",
    "RemoteObservation",
    "SimulationNotPermitted",
    "SimulationOptIn",
    "SimulationResult",
    "action_snapshot",
    "evidence_after_apply",
    "observation_absent_from_get",
    "observation_from_readback",
    "plan_lifecycle",
    "scope_for",
    "simulate_plan",
]
