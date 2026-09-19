"""
core/customer_address_candidates.py
───────────────────────────────────
Durable, source-labelled customer address candidates and explicit
customer selection.

Policy implemented here (platform-wide, merchant-agnostic):

* Information imported from a provider customer profile is an address
  **candidate** — never an automatically confirmed/default delivery address.
* Only supported components are persisted. ``city`` alone is never
  sufficient evidence of a complete delivery address.
* A **sufficient explicitly selected** address is reusable without asking
  for the full address again.
* Multiple candidates require an explicit, valid selection — there is no
  implicit default by row id, by latest import, or by latest order.
* Selection is bound to the exact address **content revision** the
  customer approved. A provider refresh can never silently rewrite an
  approved revision: changed content becomes a new candidate instead.
* Repeated and out-of-order source events are safe. Missing, empty and
  the literal string ``"null"`` are all *absent*, never deletion; deletion
  semantics are not inferred without provider evidence. A missing provider
  timestamp is never replaced by an invented revision.

``CustomerAddress`` keeps the address content. ``CustomerAddressProvenance``
(one row per address) keeps the source binding, the observed/source
timestamps, the content revision and the selection state.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("nahla.customer_address_candidates")


def _supports_savepoints(db: Any) -> bool:
    """True only where a SAVEPOINT behaves like one.

    Restricted to PostgreSQL on purpose. pysqlite does not emit BEGIN the
    way SQLAlchemy's SAVEPOINT support needs, so releasing a nested
    transaction there makes the write durable and a later outer rollback no
    longer undoes it — the opposite of what this wrapper is for. On any
    other dialect the block runs inline, which is exactly the behaviour
    these paths had before.
    """
    if getattr(db, "begin_nested", None) is None:
        return False
    return _is_postgres(db)


def _is_postgres(db: Any) -> bool:
    """True when this session speaks to PostgreSQL."""
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unidentifiable bind is treated as "not PostgreSQL", the conservative branch for every caller
        return False


@contextmanager
def _nested_or_passthrough(db: Any):
    """Run a write inside a SAVEPOINT when the session supports one.

    A SAVEPOINT keeps a failed statement from poisoning the caller's
    transaction: only the nested block rolls back, so work the caller had
    already done — an address row it is committing, for instance —
    survives.

    The rollback is unconditional on failure. A failed flush leaves the
    nested transaction DEACTIVE, and skipping ``rollback()`` because it is
    no longer ``is_active`` leaves SQLAlchemy's transaction stack
    un-unwound: the caller's next commit then raises PendingRollbackError
    and takes its own work down. Deactivated is precisely the state that
    most needs the rollback.
    """
    if not _supports_savepoints(db):
        yield None
        return
    nested = db.begin_nested()
    try:
        yield nested
    except Exception:
        try:
            nested.rollback()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — the original exception below is the one the caller must see
            pass
        raise
    else:
        nested.commit()


@contextmanager
def _read_guard(db: Any):
    """Isolate an OPTIONAL read so its failure cannot abort the caller.

    On PostgreSQL a failed statement aborts the whole transaction: every
    later statement raises until a rollback. Catching the exception is not
    enough — the caller's session is already unusable. Reading inside a
    SAVEPOINT and rolling it back restores usability.
    """
    if not _supports_savepoints(db):
        yield None
        return
    nested = db.begin_nested()
    try:
        yield nested
    except Exception:
        try:
            nested.rollback()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — see above
            pass
        raise
    else:
        nested.commit()


# ── Source labels (closed vocabulary for this slice) ────────────────────
SOURCE_SALLA_CUSTOMER_PROFILE = "salla_customer_profile"
SOURCE_ORDER_CONFIRMED_SHIPPING = "order_confirmed_shipping"

# ── Selection states ────────────────────────────────────────────────────
SELECTION_STATE_CANDIDATE = "candidate"
SELECTION_STATE_SELECTED = "selected"

# ── Selection sources ───────────────────────────────────────────────────
SELECTION_SOURCE_CUSTOMER_CONFIRMED = "customer_confirmed_previous_address"
SELECTION_SOURCE_DELIVERY_CONTINUATION = "delivery_continuation_saved_address"
SELECTION_SOURCE_ORDER_CONFIRMED_SHIPPING = "order_confirmed_shipping"

# ``CustomerAddress.address_type`` for imported candidates. Legacy rows use
# ``confirmed_shipping``; they are read as legacy selections (see
# ``resolve_customer_address_selection``) so existing reuse never regresses.
ADDRESS_TYPE_IMPORTED_CANDIDATE = "imported_profile_candidate"
ADDRESS_TYPE_CONFIRMED_SHIPPING = "confirmed_shipping"

# Upsert / selection outcomes.
ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_UNCHANGED = "unchanged"
ACTION_SKIPPED = "skipped"

# Resolution reasons.
REASON_NO_ADDRESS = "no_address"
REASON_SELECTED_ADDRESS = "selected_address"
REASON_SINGLE_CANDIDATE = "single_candidate"
REASON_MULTIPLE_CANDIDATES = "multiple_candidates_require_selection"

# Values a provider may send that mean "absent", never "delete".
_NULLISH = frozenset({"", "null", "none", "nil", "n/a", "na", "-", "--", "undefined"})

# Component keys persisted by this slice, in fingerprint order.
_COMPONENT_KEYS: Tuple[str, ...] = (
    "city",
    "district",
    "address_line",
    "country",
    "short_address_code",
    "maps_url",
    "lat",
    "lng",
)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Compare stored and incoming timestamps on one timeline.

    Backends that drop the offset (SQLite in tests) hand back naive
    datetimes; they are stored as UTC, so they are read back as UTC.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def clean_source_value(value: Any) -> str:
    """Normalise a provider value; nullish placeholders collapse to ``""``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return ""
    return text


@dataclass(frozen=True)
class AddressComponents:
    """Supported address components. Empty string means *absent*."""

    city: str = ""
    district: str = ""
    address_line: str = ""
    country: str = ""
    short_address_code: str = ""
    maps_url: str = ""
    lat: str = ""
    lng: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {key: getattr(self, key) for key in _COMPONENT_KEYS}

    def is_empty(self) -> bool:
        return not any(self.as_dict().values())

    def populated_keys(self) -> Tuple[str, ...]:
        return tuple(key for key, value in self.as_dict().items() if value)


def components_from_mapping(payload: Mapping[str, Any]) -> AddressComponents:
    data = payload if isinstance(payload, Mapping) else {}
    return AddressComponents(
        **{key: clean_source_value(data.get(key)) for key in _COMPONENT_KEYS}
    )


def components_from_salla_customer_payload(payload: Mapping[str, Any]) -> AddressComponents:
    """Project the supported Salla customer-profile address surface.

    Supported here: ``location`` (free-text street/address line), ``city``
    and ``country``. Nothing else on the customer profile is treated as an
    address component. In particular ``location`` is **never** written to
    ``saudi_national_address`` (the national short address) — the two are
    different things and must never be conflated, and no postal code is
    inferred.
    """
    data = payload if isinstance(payload, Mapping) else {}
    city = clean_source_value(data.get("city"))
    country = clean_source_value(data.get("country"))
    location = data.get("location")
    address_line = ""
    if isinstance(location, Mapping):
        # Some payload shapes nest the free-text under the object.
        address_line = clean_source_value(
            location.get("description")
            or location.get("address")
            or location.get("street")
        )
    else:
        address_line = clean_source_value(location)
    return AddressComponents(city=city, country=country, address_line=address_line)


def source_updated_at_from_salla_customer_payload(
    payload: Mapping[str, Any],
) -> Optional[datetime]:
    """Provider revision timestamp, or ``None``.

    ``None`` stays ``None``: a missing provider timestamp is never replaced
    by ``now()`` or any other invented revision.
    """
    data = payload if isinstance(payload, Mapping) else {}
    raw = data.get("updated_at")
    if isinstance(raw, Mapping):
        raw = raw.get("date") or raw.get("datetime") or raw.get("value")
    return parse_source_timestamp(raw)


def parse_source_timestamp(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = clean_source_value(raw)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    for value in (candidate, candidate.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def address_content_fingerprint(components: AddressComponents) -> str:
    """Stable fingerprint of the address **content** only.

    Deliberately customer-independent so a selection can be bound to the
    exact revision the customer approved and compared after any refresh.
    """
    parts = []
    for key in _COMPONENT_KEYS:
        value = getattr(components, key) or ""
        parts.append(value.strip().lower() if key != "short_address_code" else value.strip().upper())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def has_delivery_address_evidence(components: AddressComponents) -> bool:
    """Evidence that locates the delivery point, beyond the city.

    Deliberately the same artefacts the platform already accepts as a
    delivery address (``core.wa_order_lifecycle.has_accepted_delivery_address``):
    a national short address, a maps URL, or coordinates. Free text is
    preserved and shown, but it does not locate a delivery point here, so
    it never counts as one.
    """
    if components.short_address_code or components.maps_url:
        return True
    return bool(components.lat and components.lng)


def is_sufficient_delivery_address(components: AddressComponents) -> bool:
    """Shared completeness decision for every reader in this slice.

    A city alone is never sufficient: a delivery point needs the city plus
    at least one locating component.
    """
    return bool(components.city) and has_delivery_address_evidence(components)


def missing_address_requirements(components: AddressComponents) -> Tuple[str, ...]:
    """Only what is still required — known fields are never re-asked."""
    missing: List[str] = []
    if not components.city:
        missing.append("city")
    if not has_delivery_address_evidence(components):
        missing.append("delivery_address")
    return tuple(missing)


# ── Row ↔ components mapping ────────────────────────────────────────────

def components_from_address_row(row: Any, provenance: Any = None) -> AddressComponents:
    """Read stored content back as components.

    ``customer_addresses`` has no country column and ``whatsapp_location``
    means a WhatsApp location pin — never a country — so the observed
    country is round-tripped through ``CustomerAddressProvenance``.
    """
    country = clean_source_value(getattr(provenance, "source_country", None))
    lat = clean_source_value(getattr(row, "lat", None))
    lng = clean_source_value(getattr(row, "lng", None))
    if not lat or not lng:
        # A WhatsApp location pin IS a coordinate pair; read it so a row
        # that only carries the pin keeps its delivery evidence.
        pin = getattr(row, "whatsapp_location", None)
        if isinstance(pin, dict):
            lat = lat or clean_source_value(pin.get("latitude") or pin.get("lat"))
            lng = lng or clean_source_value(pin.get("longitude") or pin.get("lng"))
    return AddressComponents(
        city=clean_source_value(getattr(row, "city", None)),
        district=clean_source_value(getattr(row, "district", None)),
        address_line=clean_source_value(getattr(row, "address_text", None)),
        country=country,
        short_address_code=clean_source_value(getattr(row, "saudi_national_address", None)),
        maps_url=clean_source_value(getattr(row, "google_maps_link", None)),
        lat=lat,
        lng=lng,
    )


def row_has_location_pin(row: Any) -> bool:
    pin = getattr(row, "whatsapp_location", None)
    return bool(isinstance(pin, dict) and pin)


def _apply_components_to_row(row: Any, components: AddressComponents) -> None:
    """Write the components that ``customer_addresses`` has columns for.

    ``short_address_code`` maps to ``saudi_national_address`` (the national
    SHORT address) and to nothing else: it is never merged with, or written
    as, a postal code.
    """
    row.city = components.city or None
    row.district = components.district or None
    row.address_text = components.address_line or None
    row.raw_address = components.address_line or None
    row.saudi_national_address = components.short_address_code or None
    row.google_maps_link = components.maps_url or None
    row.lat = components.lat or None
    row.lng = components.lng or None


def merge_components(
    stored: AddressComponents,
    incoming: AddressComponents,
    *,
    authoritative: bool,
) -> AddressComponents:
    """Merge a source observation into stored content.

    * An absent incoming value never clears a stored value (missing, empty
      and ``"null"`` are absence, not deletion).
    * A non-authoritative observation (older, or with no provider revision
      while a revision is already stored) may only **fill gaps**; it can
      never overwrite information already held.
    """
    merged: Dict[str, str] = stored.as_dict()
    for key, value in incoming.as_dict().items():
        if not value:
            continue
        if merged.get(key) and not authoritative:
            continue
        merged[key] = value
    return AddressComponents(**merged)


# ── Persistence ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandidateUpsertResult:
    action: str
    reason: str
    address_id: Optional[int] = None
    fingerprint: str = ""
    components: AddressComponents = AddressComponents()

    @property
    def persisted(self) -> bool:
        return self.action in {ACTION_CREATED, ACTION_UPDATED, ACTION_UNCHANGED}


def _provenance_rows(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    source: str,
    source_ref: str,
    integration_connection_id: Optional[int] = None,
) -> List[Any]:
    """Source history for this customer, optionally scoped to one store.

    Freshness, idempotency and the update decision are all judged against
    this list, so leaving the store out of it let one connection's revision
    silence or overwrite another's.
    """
    from models import CustomerAddressProvenance  # noqa: PLC0415

    query = db.query(CustomerAddressProvenance).filter_by(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        source=source,
    )
    if source_ref:
        query = query.filter(CustomerAddressProvenance.source_ref == source_ref)
    if integration_connection_id is not None:
        query = query.filter(
            CustomerAddressProvenance.integration_connection_id
            == int(integration_connection_id)
        )
    return list(query.order_by(CustomerAddressProvenance.id.asc()).all())


# One customer's address book is one durable scope, and every write to it
# — import, refresh, ownership acquisition, selection — serializes on it.
#
# Row locks are not enough: the decisions these writers make are derived
# from a READ of the whole scope (which store owns this source, is this
# revision fresher, is this the revision the customer approved), and a
# competing session can commit between that read and the write. Two
# concurrent first imports had no row to lock at all, so they both "won"
# and left the source with two owners.
#
# Key convention matches ``services.meta_catalog_onboarding``: a dedicated
# namespace key plus a hashed scope key.
_ADDRESS_SCOPE_LOCK_KEY = 904223
_SCOPE_LOCK_WAIT_SECONDS = 3.0
_SCOPE_LOCK_POLL_SECONDS = 0.02

LOCK_ACQUIRED = "acquired"
LOCK_UNSUPPORTED = "unsupported"
LOCK_BUSY = "busy"


def _acquire_customer_address_scope_lock(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    wait_seconds: float = _SCOPE_LOCK_WAIT_SECONDS,
) -> str:
    """Take authority over this customer's address book, or report failure.

    Transaction-scoped, so it is released by the caller's own commit or
    rollback and never outlives the work it protects.

    A bounded wait rather than an unbounded block: a writer that cannot
    get authority must REFUSE, not proceed on a row it read without it,
    and must not pin a connection indefinitely either.

    Returns ``LOCK_UNSUPPORTED`` on a backend without advisory locks —
    SQLite serializes writers itself, so there is nothing to take.
    """
    from sqlalchemy import text  # noqa: PLC0415

    if not _is_postgres(db):
        return LOCK_UNSUPPORTED
    statement = text(
        "SELECT pg_try_advisory_xact_lock(:k, hashtext(:scope))"
    )
    params = {"k": _ADDRESS_SCOPE_LOCK_KEY, "scope": f"{int(tenant_id)}:{int(customer_id)}"}
    deadline = _monotonic() + max(0.0, float(wait_seconds))
    while True:
        try:
            if bool(db.execute(statement, params).scalar()):
                return LOCK_ACQUIRED
        except Exception:  # noqa: BLE001  # noqa: silent-ok — an unusable lock is reported as failure, and every caller refuses on failure
            logger.debug(
                "[CUSTOMER_ADDRESS] scope lock unavailable tenant=%s customer=%s",
                tenant_id,
                customer_id,
                exc_info=True,
            )
            return LOCK_BUSY
        if _monotonic() >= deadline:
            logger.info(
                "[CUSTOMER_ADDRESS] scope busy tenant=%s customer=%s",
                tenant_id,
                customer_id,
            )
            return LOCK_BUSY
        _sleep(_SCOPE_LOCK_POLL_SECONDS)


def _monotonic() -> float:
    import time  # noqa: PLC0415

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time  # noqa: PLC0415

    time.sleep(seconds)


def _address_row(db: Any, *, tenant_id: int, address_id: int) -> Any:
    from models import CustomerAddress  # noqa: PLC0415

    return (
        db.query(CustomerAddress)
        .filter_by(tenant_id=int(tenant_id), id=int(address_id))
        .first()
    )


def upsert_imported_address_candidate(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    components: AddressComponents,
    source: str = SOURCE_SALLA_CUSTOMER_PROFILE,
    source_ref: str = "",
    integration_connection_id: Optional[int] = None,
    source_updated_at: Optional[datetime] = None,
    observed_at: Optional[datetime] = None,
) -> CandidateUpsertResult:
    """Idempotently persist an imported address candidate.

    The caller owns the transaction: this function flushes but never
    commits, so a failed transaction can never leave a half-written
    candidate behind (and never yields save evidence — see
    ``core.customer_address_persistence_evidence``).
    """
    from models import CustomerAddress, CustomerAddressProvenance  # noqa: PLC0415

    if not tenant_id or not customer_id:
        return CandidateUpsertResult(action=ACTION_SKIPPED, reason="missing_scope")
    if components.is_empty():
        # Nothing supported arrived. Absence is not deletion: stored
        # candidates are left exactly as they are.
        return CandidateUpsertResult(
            action=ACTION_SKIPPED,
            reason="no_supported_address_components",
        )

    # Authority FIRST. Every decision below — who owns this source, is this
    # revision fresher, does this content already exist — is derived from a
    # read, so the read itself has to happen under authority. Acquiring it
    # afterwards would only protect the write, and the decision it carries
    # out would already be stale.
    lock = _acquire_customer_address_scope_lock(
        db, tenant_id=tenant_id, customer_id=customer_id,
    )
    if lock == LOCK_BUSY:
        # Refuse rather than write from an unauthorized read. An import
        # repeats: the next sync or webhook re-delivers this payload.
        return CandidateUpsertResult(action=ACTION_SKIPPED, reason="address_scope_busy")

    now = observed_at or _utcnow()
    fingerprint = address_content_fingerprint(components)
    # ONE read of the whole source scope, under that authority. Ownership is
    # decided against all of it; freshness, idempotency and the update
    # decision are then judged against this store's own rows only.
    all_rows = _provenance_rows(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
        source=source,
        source_ref=source_ref,
    )
    ownership = _source_connection_ownership(
        db,
        tenant_id=tenant_id,
        rows=all_rows,
        integration_connection_id=integration_connection_id,
    )
    if ownership == "source_owned_by_another_connection":
        return CandidateUpsertResult(action=ACTION_SKIPPED, reason=ownership)
    rows = _rows_for_connection(all_rows, integration_connection_id)

    # 0. Freshness is judged against the WHOLE source history, selected rows
    #    included. Judging it only against a mutable row let an older payload
    #    slip in as a new candidate once the newer revision had been selected.
    if source_updated_at is not None and rows:
        known_revisions = [
            rev for rev in (_as_utc(r.source_updated_at) for r in rows) if rev is not None
        ]
        if known_revisions and source_updated_at < max(known_revisions):
            newest = max(rows, key=lambda r: (_as_utc(r.source_updated_at) or _EPOCH, r.id))
            return CandidateUpsertResult(
                action=ACTION_SKIPPED,
                reason="stale_source_event",
                address_id=int(newest.customer_address_id),
                fingerprint=str(newest.content_fingerprint or ""),
            )

    # 1. Same content already stored for this source → idempotent no-op.
    for prov in rows:
        if prov.content_fingerprint == fingerprint:
            prov.source_observed_at = now
            prov_rev = _as_utc(prov.source_updated_at)
            if source_updated_at is not None and (
                prov_rev is None or source_updated_at > prov_rev
            ):
                prov.source_updated_at = source_updated_at
            prov.updated_at = now
            db.add(prov)
            db.flush()
            return CandidateUpsertResult(
                action=ACTION_UNCHANGED,
                reason="identical_source_content",
                address_id=int(prov.customer_address_id),
                fingerprint=fingerprint,
                components=components,
            )

    # 2. A row the source still owns (never an approved revision).
    mutable = [r for r in rows if r.selection_state != SELECTION_STATE_SELECTED]
    if mutable:
        prov = mutable[0]
        stored_rev = _as_utc(prov.source_updated_at)
        # Redundant after the source-wide check above, kept as a local guard
        # so this branch stays correct if called with narrower inputs.
        if (
            source_updated_at is not None
            and stored_rev is not None
            and source_updated_at < stored_rev
        ):
            return CandidateUpsertResult(
                action=ACTION_SKIPPED,
                reason="stale_source_event",
                address_id=int(prov.customer_address_id),
                fingerprint=prov.content_fingerprint,
            )
        authoritative = source_updated_at is not None and (
            stored_rev is None or source_updated_at >= stored_rev
        )
        # Acquire authority over the row, then REVALIDATE. Between the
        # history read above and this write another session may have
        # selected this very candidate; mutating it then would rewrite an
        # approved revision in place.
        prov = _lock_provenance(db, provenance=prov)
        if prov.selection_state == SELECTION_STATE_SELECTED:
            return _create_new_candidate(
                db,
                tenant_id=tenant_id,
                customer_id=customer_id,
                components=components,
                source=source,
                source_ref=source_ref,
                integration_connection_id=integration_connection_id,
                source_updated_at=source_updated_at,
                now=now,
                fingerprint=fingerprint,
                reason="selected_revision_preserved",
            )
        row = _address_row(db, tenant_id=tenant_id, address_id=int(prov.customer_address_id))
        if row is None:
            return CandidateUpsertResult(
                action=ACTION_SKIPPED,
                reason="candidate_address_row_missing",
            )
        stored_components = components_from_address_row(row, prov)
        merged = merge_components(stored_components, components, authoritative=authoritative)
        merged_fp = address_content_fingerprint(merged)
        if merged_fp == prov.content_fingerprint:
            prov.source_observed_at = now
            if source_updated_at is not None and (
                stored_rev is None or source_updated_at > stored_rev
            ):
                prov.source_updated_at = source_updated_at
            prov.updated_at = now
            db.add(prov)
            db.flush()
            return CandidateUpsertResult(
                action=ACTION_UNCHANGED,
                reason="no_new_information",
                address_id=int(prov.customer_address_id),
                fingerprint=merged_fp,
                components=merged,
            )
        _apply_components_to_row(row, merged)
        row.address_type = row.address_type or ADDRESS_TYPE_IMPORTED_CANDIDATE
        prov.source_country = merged.country or None
        prov.content_fingerprint = merged_fp
        prov.source_observed_at = now
        if source_updated_at is not None and (
            stored_rev is None or source_updated_at > stored_rev
        ):
            prov.source_updated_at = source_updated_at
        if integration_connection_id is not None:
            prov.integration_connection_id = int(integration_connection_id)
        prov.updated_at = now
        db.add(row)
        db.add(prov)
        db.flush()
        return CandidateUpsertResult(
            action=ACTION_UPDATED,
            reason="source_refresh",
            address_id=int(prov.customer_address_id),
            fingerprint=merged_fp,
            components=merged,
        )

    # 3. Every row for this source is an approved revision. Refreshed
    #    content becomes a NEW candidate; the approved revision is never
    #    silently rewritten.
    return _create_new_candidate(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
        components=components,
        source=source,
        source_ref=source_ref,
        integration_connection_id=integration_connection_id,
        source_updated_at=source_updated_at,
        now=now,
        fingerprint=fingerprint,
        reason="selected_revision_preserved" if rows else "first_import",
    )


def _create_new_candidate(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    components: AddressComponents,
    source: str,
    source_ref: str,
    integration_connection_id: Optional[int],
    source_updated_at: Optional[datetime],
    now: datetime,
    fingerprint: str,
    reason: str,
) -> CandidateUpsertResult:
    """Insert one source revision as a fresh, unselected candidate row.

    Used both for a first import and whenever the rows this source already
    owns are approved revisions, which are never rewritten in place.

    The insert races another importer of the same payload. The
    (tenant, customer, source, source_ref, content_fingerprint) unique
    constraint lets exactly one of them commit; the loser rolls its
    SAVEPOINT back and re-reads the winner, so one source revision is one
    address row no matter how many callers arrive together.
    """
    from models import CustomerAddress, CustomerAddressProvenance  # noqa: PLC0415

    try:
        with _nested_or_passthrough(db):
            row = CustomerAddress(
                tenant_id=int(tenant_id),
                customer_id=int(customer_id),
                address_type=ADDRESS_TYPE_IMPORTED_CANDIDATE,
            )
            _apply_components_to_row(row, components)
            db.add(row)
            db.flush()
            prov = CustomerAddressProvenance(
                tenant_id=int(tenant_id),
                customer_id=int(customer_id),
                customer_address_id=int(row.id),
                source=source,
                source_ref=source_ref or None,
                integration_connection_id=(
                    int(integration_connection_id)
                    if integration_connection_id is not None
                    else None
                ),
                source_country=components.country or None,
                content_fingerprint=fingerprint,
                source_updated_at=source_updated_at,
                source_observed_at=now,
                selection_state=SELECTION_STATE_CANDIDATE,
                created_at=now,
                updated_at=now,
            )
            db.add(prov)
            db.flush()
            address_id = int(row.id)
    except IntegrityError:
        existing = _find_provenance_by_revision(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            source=source,
            source_ref=source_ref,
            fingerprint=fingerprint,
            integration_connection_id=integration_connection_id,
        )
        if existing is None:
            raise
        return CandidateUpsertResult(
            action=ACTION_UNCHANGED,
            reason="concurrent_import_deduplicated",
            address_id=int(existing.customer_address_id),
            fingerprint=fingerprint,
            components=components,
        )
    return CandidateUpsertResult(
        action=ACTION_CREATED,
        reason=reason,
        address_id=address_id,
        fingerprint=fingerprint,
        components=components,
    )


def _lock_provenance(db: Any, *, provenance: Any) -> Any:
    """Re-read the provenance row holding a write lock, where supported."""
    from models import CustomerAddressProvenance  # noqa: PLC0415

    if not _supports_savepoints(db):
        return provenance
    try:
        locked = (
            db.query(CustomerAddressProvenance)
            .filter_by(id=int(provenance.id))
            .with_for_update()
            # populate_existing is what makes this a REVALIDATION rather
            # than a no-op: without it the identity map hands back the row
            # as this session first read it, and a selection another
            # session committed in the meantime stays invisible.
            .populate_existing()
            .first()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — without a lock the caller keeps the unlocked row and the unique constraint remains the backstop
        return provenance
    return locked if locked is not None else provenance


def _find_provenance_by_revision(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    source: str,
    source_ref: str,
    fingerprint: str,
    integration_connection_id: Optional[int] = None,
) -> Any:
    """Locate the row a uniqueness conflict collided with.

    Scoped to the same store connection the conflicting insert used, so
    the lookup mirrors the constraint exactly. Matching without it would
    hand back another store's row as "the one we just wrote".
    """
    from models import CustomerAddressProvenance  # noqa: PLC0415

    query = db.query(CustomerAddressProvenance).filter_by(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        source=source,
        source_ref=source_ref or None,
        content_fingerprint=fingerprint,
    )
    if integration_connection_id is None:
        query = query.filter(
            CustomerAddressProvenance.integration_connection_id.is_(None)
        )
    else:
        query = query.filter(
            CustomerAddressProvenance.integration_connection_id
            == int(integration_connection_id)
        )
    return query.first()


def _rows_for_connection(
    rows: List[Any],
    integration_connection_id: Optional[int],
) -> List[Any]:
    """This store's own history, plus rows no store ever claimed.

    A row written before a connection was recorded belongs to nobody, so
    the verified store may adopt and refresh it rather than duplicating it.
    Another store's rows are never in here.
    """
    if integration_connection_id is None:
        return list(rows)
    return [
        row
        for row in rows
        if getattr(row, "integration_connection_id", None) is None
        or int(row.integration_connection_id) == int(integration_connection_id)
    ]


def _source_connection_ownership(
    db: Any,
    *,
    tenant_id: int,
    rows: List[Any],
    integration_connection_id: Optional[int],
) -> str:
    """Whether THIS store connection may write this customer's source scope.

    One provider customer reference under one tenant is owned by one store
    connection at a time. Without this, a second enabled connection could
    import the same reference and rewrite the first store's address —
    "an enabled connection exists" is not the same as "this connection
    owns this customer".

    Ownership does transfer, but only when the previous owner is no longer
    a usable connection (replaced, removed or disabled). That transition is
    explicit and logged, never a silent overwrite.
    """
    if integration_connection_id is None:
        return "ok"
    owners = {
        int(row.integration_connection_id)
        for row in rows
        if getattr(row, "integration_connection_id", None) is not None
    }
    foreign = owners - {int(integration_connection_id)}
    if not foreign:
        return "ok"
    if _any_connection_still_usable(db, tenant_id=tenant_id, connection_ids=foreign):
        return "source_owned_by_another_connection"
    logger.info(
        "[CUSTOMER_ADDRESS] source ownership transferred tenant=%s from=%s to=%s",
        tenant_id,
        sorted(foreign),
        integration_connection_id,
    )
    return "ownership_transferred"


def _any_connection_still_usable(
    db: Any,
    *,
    tenant_id: int,
    connection_ids: Any,
) -> bool:
    """True when at least one of these connections still exists and is enabled."""
    from models import Integration  # noqa: PLC0415

    ids = [int(c) for c in connection_ids if c]
    if not ids:
        return False
    try:
        rows = (
            db.query(Integration)
            .filter(
                Integration.tenant_id == int(tenant_id),
                Integration.id.in_(ids),
            )
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable connection table is treated as "still owned", which refuses the write rather than overwriting another store's address
        return True
    return any(bool(getattr(row, "enabled", False)) for row in rows)


@dataclass(frozen=True)
class SelectionResult:
    action: str
    reason: str
    address_id: Optional[int] = None
    fingerprint: str = ""

    @property
    def selected(self) -> bool:
        return self.action in {ACTION_CREATED, ACTION_UPDATED, ACTION_UNCHANGED}


def record_explicit_address_selection(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    address_id: int,
    selection_source: str,
    expected_fingerprint: str = "",
    source: str = "",
    operation_ref: str = "",
    selected_at: Optional[datetime] = None,
) -> SelectionResult:
    """Bind an explicit customer selection to one exact address revision.

    Fails safe (writes nothing) when the address is not this tenant's, is
    not this customer's, or its content changed since the revision the
    customer reviewed.
    """
    from models import CustomerAddressProvenance  # noqa: PLC0415

    if not tenant_id or not customer_id or not address_id:
        return SelectionResult(action=ACTION_SKIPPED, reason="missing_scope")

    # Same authority the importer takes, for the same reason: the revision
    # check below is only meaningful if the content cannot change between
    # reading it and recording approval of it. Without this, a refresh
    # committing in between left the customer recorded as having approved
    # content they never saw — or, worse, left no selection at all.
    lock = _acquire_customer_address_scope_lock(
        db, tenant_id=tenant_id, customer_id=customer_id,
    )
    if lock == LOCK_BUSY:
        return SelectionResult(action=ACTION_SKIPPED, reason="address_scope_busy")

    row = _address_row(db, tenant_id=tenant_id, address_id=int(address_id))
    if row is None:
        return SelectionResult(action=ACTION_SKIPPED, reason="address_not_found")
    if int(getattr(row, "customer_id", 0) or 0) != int(customer_id):
        return SelectionResult(action=ACTION_SKIPPED, reason="customer_mismatch")

    now = selected_at or _utcnow()
    prov = (
        db.query(CustomerAddressProvenance)
        .filter_by(tenant_id=int(tenant_id), customer_address_id=int(address_id))
        .first()
    )
    components = components_from_address_row(row, prov)
    fingerprint = address_content_fingerprint(components)
    if expected_fingerprint and expected_fingerprint != fingerprint:
        return SelectionResult(
            action=ACTION_SKIPPED,
            reason="address_revision_changed",
            address_id=int(address_id),
            fingerprint=fingerprint,
        )

    if prov is None:
        prov = CustomerAddressProvenance(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            customer_address_id=int(address_id),
            source=source or _legacy_source_for_row(row),
            source_country=components.country or None,
            content_fingerprint=fingerprint,
            source_updated_at=None,
            source_observed_at=now,
            selection_state=SELECTION_STATE_CANDIDATE,
            created_at=now,
            updated_at=now,
        )
        db.add(prov)

    # Retry identity, NOT "is already selected". A row that was selected
    # before and then superseded is a HISTORICAL approval: choosing it again
    # is a new selection and must become the current one. Only the same
    # operation delivered twice is a no-op — otherwise re-selecting A after
    # B silently left B current.
    ref = str(operation_ref or "").strip()
    already_selected = (
        prov.selection_state == SELECTION_STATE_SELECTED
        and prov.selected_fingerprint == fingerprint
    )
    if already_selected and ref and str(prov.selection_operation_ref or "") == ref:
        return SelectionResult(
            action=ACTION_UNCHANGED,
            reason="duplicate_selection_operation",
            address_id=int(address_id),
            fingerprint=fingerprint,
        )
    if already_selected and not ref and _is_current_selection(
        db, tenant_id=int(tenant_id), customer_id=int(customer_id), provenance=prov
    ):
        # No operation identity supplied and this row is already the current
        # selection: nothing changes.
        return SelectionResult(
            action=ACTION_UNCHANGED,
            reason="already_current_selection",
            address_id=int(address_id),
            fingerprint=fingerprint,
        )

    prov.content_fingerprint = fingerprint
    prov.selection_state = SELECTION_STATE_SELECTED
    prov.selected_fingerprint = fingerprint
    prov.selected_at = now
    prov.selection_source = selection_source
    prov.selection_operation_ref = ref or None
    prov.updated_at = now
    db.add(prov)
    db.flush()
    return SelectionResult(
        action=ACTION_UPDATED,
        reason="explicit_selection",
        address_id=int(address_id),
        fingerprint=fingerprint,
    )


def _is_current_selection(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    provenance: Any,
) -> bool:
    """True when this row is the selection resolution would return today."""
    from models import CustomerAddressProvenance  # noqa: PLC0415

    try:
        rows = (
            db.query(CustomerAddressProvenance)
            .filter_by(
                tenant_id=int(tenant_id),
                customer_id=int(customer_id),
                selection_state=SELECTION_STATE_SELECTED,
            )
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — falls back to "not current", which re-writes the selection rather than silently keeping another one
        return False
    if not rows:
        return False
    newest = max(rows, key=lambda r: (_as_utc(r.selected_at) or _EPOCH, r.id))
    return int(newest.id) == int(getattr(provenance, "id", 0) or 0)


def attach_selection_provenance_for_new_address(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    address_row: Any,
    selection_source: str,
    source: str,
    components: Optional[AddressComponents] = None,
    selected_at: Optional[datetime] = None,
) -> Optional[Any]:
    """Record a selection for an address row created in this transaction.

    The row may still be pending: the provenance is linked by relationship,
    so the caller's own flush fills the foreign key. Nothing is queried and
    nothing is flushed here — a freshly created row cannot already carry
    provenance, and forcing I/O on this path would change the write
    behaviour callers depend on.
    """
    from models import CustomerAddressProvenance  # noqa: PLC0415

    if not tenant_id or not customer_id or address_row is None:
        return None
    if not provenance_table_available(db):
        # Declared migration-optional behaviour, made real: with no table
        # there is nothing to attach, and queueing an insert that the
        # CALLER's commit would raise on — outside this function's try —
        # would roll the address write back with it.
        return None
    resolved = components if components is not None else components_from_address_row(address_row)
    if resolved.is_empty():
        return None
    prov = _build_selection_provenance(
        tenant_id=tenant_id,
        customer_id=customer_id,
        components=resolved,
        source=source,
        selection_source=selection_source,
        selected_at=selected_at,
    )
    prov.customer_address = address_row
    db.add(prov)
    return prov


def _build_selection_provenance(
    *,
    tenant_id: int,
    customer_id: int,
    components: AddressComponents,
    source: str,
    selection_source: str,
    selected_at: Optional[datetime] = None,
) -> Any:
    """Construct (but do not add) the provenance row for a selection."""
    from models import CustomerAddressProvenance  # noqa: PLC0415

    now = selected_at or _utcnow()
    fingerprint = address_content_fingerprint(components)
    return CustomerAddressProvenance(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        source=source,
        source_country=components.country or None,
        content_fingerprint=fingerprint,
        source_updated_at=None,
        source_observed_at=now,
        selection_state=SELECTION_STATE_SELECTED,
        selected_fingerprint=fingerprint,
        selected_at=now,
        selection_source=selection_source,
        created_at=now,
        updated_at=now,
    )


def attach_selection_provenance_contained(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    address_row: Any,
    selection_source: str,
    source: str,
    components: Optional[AddressComponents] = None,
    selected_at: Optional[datetime] = None,
) -> Optional[Any]:
    """Attach selection provenance without risking the caller's address write.

    Provenance only LABELS an address the caller already decided to write.
    Queueing the insert and letting the caller's COMMIT execute it puts the
    two in the same statement batch: a rejected provenance insert then
    poisons the transaction, the commit raises ``PendingRollbackError`` and
    the address the customer confirmed is lost with it.

    So on a backend with savepoints the caller's pending work is flushed
    FIRST, outside the savepoint, and only the provenance insert runs
    inside it. A failure there rolls back to the savepoint — the
    provenance row and nothing else — leaving the caller's transaction
    healthy and its address write intact. Nothing here commits or rolls
    back the caller's transaction.

    Without savepoints there is no containment boundary to use, so the
    queued, I/O-free attachment is kept: forcing a flush on that path would
    change write behaviour callers depend on without buying any
    containment.
    """
    if not tenant_id or not customer_id or address_row is None:
        return None
    if not _supports_savepoints(db):
        return attach_selection_provenance_for_new_address(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            address_row=address_row,
            selection_source=selection_source,
            source=source,
            components=components,
            selected_at=selected_at,
        )
    if not provenance_table_available(db):
        return None
    resolved = components if components is not None else components_from_address_row(address_row)
    if resolved.is_empty():
        return None

    # The caller's address INSERT lands outside the savepoint, so rolling
    # the savepoint back can never undo it. It also assigns the id the
    # provenance row needs.
    db.flush()
    address_id = int(getattr(address_row, "id", 0) or 0)
    if not address_id:
        return None

    prov = _build_selection_provenance(
        tenant_id=tenant_id,
        customer_id=customer_id,
        components=resolved,
        source=source,
        selection_source=selection_source,
        selected_at=selected_at,
    )
    prov.customer_address_id = address_id
    with _nested_or_passthrough(db):
        db.add(prov)
        db.flush()
    return prov


def provenance_table_available(db: Any) -> bool:
    """Probe for the provenance table on the SESSION'S OWN connection.

    Deliberately not ``inspect(db.get_bind())``: reflecting on the engine
    checks out a second connection, and closing it again ends a
    transaction the caller is still using — under SQLite's shared
    in-memory connection that silently discarded the caller's pending
    work. Reflecting on ``db.connection()`` reuses the connection the
    session already holds, so the caller's transaction is untouched.
    """
    try:
        conn = db.connection()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unusable connection is treated as "unavailable", the conservative branch
        return False
    if conn is None:
        return False
    try:
        from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

        return bool(sa_inspect(conn).has_table("customer_address_provenance"))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — probe failure is treated as "unavailable" so nothing is queued
        return False


def _legacy_source_for_row(row: Any) -> str:
    address_type = clean_source_value(getattr(row, "address_type", None))
    if address_type == ADDRESS_TYPE_IMPORTED_CANDIDATE:
        return SOURCE_SALLA_CUSTOMER_PROFILE
    return SOURCE_ORDER_CONFIRMED_SHIPPING


# ── Read projection ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResolvedAddress:
    address_id: int
    components: AddressComponents
    fingerprint: str
    source: str
    selection_state: str
    selection_source: str = ""
    # The selection operation that approved this revision. It is what lets
    # a later claim be tied to THE operation a turn performed, rather than
    # to any selection that happens to exist.
    selection_operation_ref: str = ""
    selected_at: Optional[datetime] = None
    legacy: bool = False
    # False when the provenance read failed. The row is then classified from
    # its own address_type alone, and callers can tell "known candidate"
    # apart from "provenance unknown".
    provenance_known: bool = True
    # A WhatsApp location pin on the stored row, independent of whether it
    # yielded usable coordinates. Pre-slice rows relied on its presence.
    location_pin: bool = False

    @property
    def selected(self) -> bool:
        return self.selection_state == SELECTION_STATE_SELECTED

    @property
    def has_delivery_evidence(self) -> bool:
        return has_delivery_address_evidence(self.components) or self.location_pin

    @property
    def sufficient(self) -> bool:
        return bool(self.components.city) and self.has_delivery_evidence

    @property
    def missing_requirements(self) -> Tuple[str, ...]:
        missing: List[str] = []
        if not self.components.city:
            missing.append("city")
        if not self.has_delivery_evidence:
            missing.append("delivery_address")
        return tuple(missing)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "address_id": self.address_id,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "selection_state": self.selection_state,
            "provenance_known": self.provenance_known,
            "location_pin": self.location_pin,
            "selection_source": self.selection_source,
            "selection_operation_ref": self.selection_operation_ref,
            "selected": self.selected,
            "sufficient": self.sufficient,
            "missing_requirements": list(self.missing_requirements),
            "selected_at": self.selected_at.isoformat() if self.selected_at else None,
        }
        payload.update(self.components.as_dict())
        return payload


@dataclass(frozen=True)
class AddressResolution:
    reason: str
    selected: Optional[ResolvedAddress] = None
    candidates: Tuple[ResolvedAddress, ...] = ()
    # Addresses the customer selected earlier that a later selection
    # superseded. They remain durable history, not candidates.
    superseded_selections: Tuple[ResolvedAddress, ...] = ()
    # Every durable address, whatever its state — the full inventory the
    # read projection advertises.
    addresses: Tuple[ResolvedAddress, ...] = ()

    @property
    def selectable(self) -> Tuple[ResolvedAddress, ...]:
        """Everything the customer may explicitly choose between."""
        return self.addresses or tuple(
            x for x in ((self.selected,) if self.selected else ()) + self.candidates
        )

    @property
    def reusable(self) -> Optional[ResolvedAddress]:
        """The one address this customer's context may reuse.

        An explicit selection wins. Exactly one unselected candidate may be
        surfaced for a brief confirmation. Several unselected candidates
        never produce an implicit default.
        """
        if self.selected is not None:
            return self.selected
        if len(self.candidates) == 1:
            return self.candidates[0]
        return None

    @property
    def requires_explicit_selection(self) -> bool:
        return self.selected is None and len(self.candidates) > 1


def _selection_sort_key(item: ResolvedAddress) -> Tuple[int, float, int]:
    # Explicit selections (with a recorded time) outrank legacy rows, which
    # keep their historical newest-row ordering.
    stamp = _as_utc(item.selected_at)
    if stamp is not None:
        return (1, stamp.timestamp(), item.address_id)
    return (0, 0.0, item.address_id)


def resolve_customer_address_selection(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
) -> AddressResolution:
    """Tenant-scoped projection of this customer's addresses."""
    if not tenant_id or not customer_id:
        return AddressResolution(reason=REASON_NO_ADDRESS)

    from models import CustomerAddress, CustomerAddressProvenance  # noqa: PLC0415

    rows = (
        db.query(CustomerAddress)
        .filter_by(tenant_id=int(tenant_id), customer_id=int(customer_id))
        .order_by(CustomerAddress.id.asc())
        .all()
    )
    if not rows:
        return AddressResolution(reason=REASON_NO_ADDRESS)

    provenance: Dict[int, Any] = {}
    provenance_readable = True
    try:
        with _read_guard(db):
            for prov in (
                db.query(CustomerAddressProvenance)
                .filter_by(tenant_id=int(tenant_id), customer_id=int(customer_id))
                .all()
            ):
                provenance[int(prov.customer_address_id)] = prov
    except Exception:  # noqa: BLE001  # noqa: silent-ok — a provenance read failure degrades the projection conservatively (below); it must not fail the caller's read path
        # Read-optional, but NOT proof of anything: a failed read says only
        # that provenance is unknown. Rows are classified from what the
        # address row itself states (see below), never upgraded.
        logger.debug(
            "[CUSTOMER_ADDRESS] provenance unavailable tenant=%s",
            tenant_id,
            exc_info=True,
        )
        provenance = {}
        provenance_readable = False

    selected: List[ResolvedAddress] = []
    candidates: List[ResolvedAddress] = []
    for row in rows:
        prov = provenance.get(int(row.id))
        components = components_from_address_row(row, prov)
        location_pin = row_has_location_pin(row)
        if components.is_empty() and not location_pin:
            continue
        fingerprint = address_content_fingerprint(components)
        if prov is None:
            # No provenance row. Only a POSITIVELY identified pre-slice
            # confirmed-shipping row is read as a legacy selection — those
            # were written solely on confirmed shipping evidence, so
            # existing reuse is unchanged. An imported candidate, or any
            # row whose own type does not say "confirmed", stays a
            # candidate. A failed provenance read (provenance_readable
            # False) is not evidence either: it says provenance is unknown,
            # and unknown must never promote a candidate to selected.
            legacy_selected = (
                clean_source_value(getattr(row, "address_type", None))
                == ADDRESS_TYPE_CONFIRMED_SHIPPING
            )
            resolved = ResolvedAddress(
                address_id=int(row.id),
                components=components,
                fingerprint=fingerprint,
                source=_legacy_source_for_row(row),
                selection_state=(
                    SELECTION_STATE_SELECTED if legacy_selected else SELECTION_STATE_CANDIDATE
                ),
                legacy=True,
                provenance_known=provenance_readable,
                location_pin=location_pin,
            )
            (selected if legacy_selected else candidates).append(resolved)
            continue
        is_selected = (
            prov.selection_state == SELECTION_STATE_SELECTED
            and prov.selected_fingerprint == fingerprint
        )
        resolved = ResolvedAddress(
            address_id=int(row.id),
            components=components,
            fingerprint=fingerprint,
            source=str(prov.source or ""),
            selection_state=(
                SELECTION_STATE_SELECTED if is_selected else SELECTION_STATE_CANDIDATE
            ),
            selection_source=str(prov.selection_source or "") if is_selected else "",
            selection_operation_ref=(
                str(prov.selection_operation_ref or "") if is_selected else ""
            ),
            selected_at=prov.selected_at if is_selected else None,
            provenance_known=True,
            location_pin=location_pin,
        )
        (selected if is_selected else candidates).append(resolved)

    everything = tuple(sorted(selected + candidates, key=lambda r: r.address_id))
    if selected:
        ordered = sorted(selected, key=_selection_sort_key)
        winner = ordered[-1]
        # Selections that are no longer current are historical approvals,
        # not candidates — they stay in the inventory rather than vanishing
        # from a projection that advertises every durable address.
        superseded = tuple(ordered[:-1])
        return AddressResolution(
            reason=REASON_SELECTED_ADDRESS,
            selected=winner,
            candidates=tuple(candidates),
            superseded_selections=superseded,
            addresses=everything,
        )
    if len(candidates) == 1:
        return AddressResolution(
            reason=REASON_SINGLE_CANDIDATE,
            candidates=tuple(candidates),
            addresses=everything,
        )
    if candidates:
        return AddressResolution(
            reason=REASON_MULTIPLE_CANDIDATES,
            candidates=tuple(candidates),
            addresses=everything,
        )
    return AddressResolution(reason=REASON_NO_ADDRESS)


__all__ = [
    "ACTION_CREATED",
    "ACTION_SKIPPED",
    "ACTION_UNCHANGED",
    "ACTION_UPDATED",
    "ADDRESS_TYPE_IMPORTED_CANDIDATE",
    "AddressComponents",
    "AddressResolution",
    "CandidateUpsertResult",
    "REASON_MULTIPLE_CANDIDATES",
    "REASON_NO_ADDRESS",
    "REASON_SELECTED_ADDRESS",
    "REASON_SINGLE_CANDIDATE",
    "ResolvedAddress",
    "SELECTION_SOURCE_CUSTOMER_CONFIRMED",
    "SELECTION_SOURCE_DELIVERY_CONTINUATION",
    "SELECTION_SOURCE_ORDER_CONFIRMED_SHIPPING",
    "SELECTION_STATE_CANDIDATE",
    "SELECTION_STATE_SELECTED",
    "SOURCE_ORDER_CONFIRMED_SHIPPING",
    "SOURCE_SALLA_CUSTOMER_PROFILE",
    "SelectionResult",
    "address_content_fingerprint",
    "attach_selection_provenance_for_new_address",
    "clean_source_value",
    "components_from_address_row",
    "components_from_mapping",
    "components_from_salla_customer_payload",
    "is_sufficient_delivery_address",
    "merge_components",
    "missing_address_requirements",
    "parse_source_timestamp",
    "provenance_table_available",
    "has_delivery_address_evidence",
    "record_explicit_address_selection",
    "resolve_customer_address_selection",
    "row_has_location_pin",
    "source_updated_at_from_salla_customer_payload",
    "upsert_imported_address_candidate",
]
