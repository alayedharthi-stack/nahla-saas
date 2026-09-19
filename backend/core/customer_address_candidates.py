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
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unidentifiable bind is treated as "no savepoints", the conservative branch
        return False


@contextmanager
def _nested_or_passthrough(db: Any):
    """Run a write inside a SAVEPOINT when the session supports one.

    A SAVEPOINT keeps a failed write from poisoning the caller's
    transaction: only the nested block rolls back, so work the caller had
    already done — an address row it is committing, for instance —
    survives. Where savepoints are unavailable the block runs inline and
    the caller's own transaction semantics apply unchanged.
    """
    if not _supports_savepoints(db):
        yield None
        return
    begin_nested = db.begin_nested
    nested = begin_nested()
    try:
        yield nested
    except Exception:
        try:
            if getattr(nested, "is_active", False):
                nested.rollback()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — the original exception below is the one that matters
            pass
        raise
    else:
        try:
            if getattr(nested, "is_active", False):
                nested.commit()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — a session without real SAVEPOINT support (test doubles) needs no release
            pass

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
) -> List[Any]:
    from models import CustomerAddressProvenance  # noqa: PLC0415

    query = db.query(CustomerAddressProvenance).filter_by(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        source=source,
    )
    if source_ref:
        query = query.filter(CustomerAddressProvenance.source_ref == source_ref)
    return list(query.order_by(CustomerAddressProvenance.id.asc()).all())


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

    now = observed_at or _utcnow()
    fingerprint = address_content_fingerprint(components)
    rows = _provenance_rows(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
        source=source,
        source_ref=source_ref,
    )

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
    reason = "selected_revision_preserved" if rows else "first_import"
    # The insert races another importer of the same payload. The
    # (tenant, customer, source, source_ref, content_fingerprint) unique
    # constraint lets exactly one of them commit; the loser rolls its
    # SAVEPOINT back and re-reads the winner, so one source revision is one
    # address row no matter how many callers arrive together.
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


def _find_provenance_by_revision(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    source: str,
    source_ref: str,
    fingerprint: str,
) -> Any:
    from models import CustomerAddressProvenance  # noqa: PLC0415

    return (
        db.query(CustomerAddressProvenance)
        .filter_by(
            tenant_id=int(tenant_id),
            customer_id=int(customer_id),
            source=source,
            source_ref=source_ref or None,
            content_fingerprint=fingerprint,
        )
        .first()
    )


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
    now = selected_at or _utcnow()
    fingerprint = address_content_fingerprint(resolved)
    prov = CustomerAddressProvenance(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        source=source,
        source_country=resolved.country or None,
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
    prov.customer_address = address_row
    db.add(prov)
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
