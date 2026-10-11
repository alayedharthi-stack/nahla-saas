"""
services/google_merchant_models.py
──────────────────────────────────
Dormant Google Merchant tables on their own ``GoogleMerchantBase`` metadata.

Deliberately outside ``models.Base``: production startup materialises
``models.Base`` through ``create_all`` and normal bootstrap is pinned to
``alembic upgrade 0093``, so nothing here reaches a database until revision
``0124`` is applied on purpose. Nothing in the application imports this
module; there is no router, worker, scheduler, flag, transport, OAuth or
crypto here. The revision and this module declare the same schema;
``backend/tests/test_google_merchant_schema_pg.py`` proves their equivalence
and every invariant below on PostgreSQL.

``google_merchant_connections`` — one tenant ↔ one Merchant account binding.
  * ``UNIQUE(tenant_id)`` and ``UNIQUE(account_id)`` hold for every status: a
    disabled or revoked row stays as the account tombstone (rows are never
    deleted, neither by DELETE nor by TRUNCATE), so another tenant can never
    claim the account.
  * tenant, account, language and feed label are immutable; the API data
    source is bound exactly once (NULL → value) and is then immutable.
  * ``credential_enc`` is a future ``gmc1:`` envelope column only; nothing
    here encrypts, decrypts or reads it.

``google_merchant_offer_ledger`` — one row per offer identity: ownership
evidence, outbox and idempotency in one row.
  * identity ``UNIQUE(account_id, content_language, feed_label, offer_id)``,
    independent of data source and connection, plus a ``lower(offer_id)``
    backstop in the same account scope; no ``legacyLocal`` namespace.
  * every scope column is NOT NULL and ``MATCH FULL`` references the
    connection's exact scope key with ``RESTRICT``; the guard also freezes
    every scope column, so a row can never be re-pointed or re-parented.
  * a dispatched operation (id, action, full request snapshot, its SHA-256,
    idempotency key, generation) is frozen until it resolves; ``lease_fence``
    only grows and changes only when a new dispatch lease is taken, with a
    fresh ``attempt_id``, under a still-bound connection; the fresh attempt
    is stamped with the database clock and starts with no outcome or retry
    time, and a live attempt keeps its owner.
    A completion from a superseded attempt cannot match.
  * an attempt's outcome (a 429 with its retry time, or a rejection code) is
    written only by the transition that ends that live attempt, names it
    (``attempt_id``) and is then frozen; new work carries none.
  * ``admitted_generation`` is a high-water mark that never decreases, also
    across rejected or superseded operations.
  * an expired dispatch lease can only go to read-back; only the live lease
    holder may record a definitive 429 (back to ``pending``) or rejection. Lease
    liveness is judged by ``clock_timestamp()`` with the row locked, never by
    the transaction-start ``now()``.
  * ownership and applied evidence change only through confirmation; ledger
    rows are never deleted (neither DELETE nor TRUNCATE), so every identity
    keeps its high-water generation and applied-key history as a tombstone.

PostgreSQL semantics are assumed (JSONB, PL/pgSQL, ``sha256``).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

GoogleMerchantBase = declarative_base()

# Reference-only stub so the tenant foreign key resolves inside this metadata.
# It is never created from here; the application owns ``tenants``.
tenants_reference = Table("tenants", GoogleMerchantBase.metadata, Column("id", Integer, primary_key=True))

CONNECTIONS_TABLE = "google_merchant_connections"
LEDGER_TABLE = "google_merchant_offer_ledger"
CONNECTIONS_GUARD_FUNCTION = "google_merchant_connections_guard"
LEDGER_GUARD_FUNCTION = "google_merchant_offer_ledger_guard"
CONNECTIONS_GUARD_TRIGGER = "trg_google_merchant_connections_guard"
LEDGER_GUARD_TRIGGER = "trg_google_merchant_offer_ledger_guard"

CONNECTION_STATUSES = ("authorized", "bound", "disabled", "revoked")
OWNERSHIP_STATES = ("claiming", "owned", "released")
OP_STATES = ("idle", "pending", "dispatching", "readback_required", "unresolved", "rejected")
OP_ACTIONS = ("insert", "patch", "delete")
CREDENTIAL_PREFIX = "gmc1:"
# Stable outcome code the live lease holder records for a definitive 429.
RATE_LIMITED_OUTCOME = "rate_limited"

_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_HEX = "0123456789abcdef"


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _merchant_id_check(column: str) -> str:
    # Enumerated sets, no ranges and no anchors: a value is rejected when ANY
    # character is outside the set, independent of regex anchor semantics.
    return f"(length({column}) BETWEEN 1 AND 20 AND {column} !~ '[^{_DIGITS}]' AND left({column}, 1) <> '0')"


def _hex64(column: str) -> str:
    return f"({column} IS NULL OR (length({column}) = 64 AND {column} !~ '[^{_HEX}]'))"


def _token(column: str) -> str:
    return f"({column} IS NULL OR (length({column}) BETWEEN 1 AND 64 AND {column} !~ '[^{_HEX}-]'))"


_OP_COLUMNS = ("op_id", "op_action", "op_snapshot", "op_snapshot_hash", "op_idempotency_key", "op_generation")
_APPLIED_COLUMNS = ("applied_attributes", "applied_fingerprint", "applied_generation", "applied_idempotency_key")


def _all(columns, predicate: str) -> str:
    return " AND ".join(f"{c} {predicate}" for c in columns)


class GoogleMerchantConnection(GoogleMerchantBase):
    __tablename__ = CONNECTIONS_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    account_id = Column(String(20), nullable=False)
    data_source_id = Column(String(20), nullable=True)
    content_language = Column(String(2), nullable=False)
    feed_label = Column(String(20), nullable=False)
    status = Column(String(16), nullable=False)
    credential_enc = Column(Text, nullable=True)
    granted_scopes = Column(JSONB, nullable=True)
    authorized_by_user_id = Column(Integer, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    binding_verified_at = Column(DateTime(timezone=True), nullable=True)
    discovery_checked_at = Column(DateTime(timezone=True), nullable=True)
    discovery_summary = Column(JSONB, nullable=True)
    plan_sequence = Column(BigInteger, nullable=False, server_default=text("0"))
    version = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_google_merchant_connections_tenant"),
        UniqueConstraint("account_id", name="uq_google_merchant_connections_account"),
        UniqueConstraint("account_id", "data_source_id", name="uq_google_merchant_connections_account_source"),
        UniqueConstraint(
            "id", "tenant_id", "account_id", "data_source_id", "content_language", "feed_label",
            name="uq_google_merchant_connections_scope",
        ),
        CheckConstraint(f"status IN ({_in(CONNECTION_STATUSES)})", name="ck_google_merchant_connections_status"),
        CheckConstraint(_merchant_id_check("account_id"), name="ck_google_merchant_connections_account_id"),
        CheckConstraint(
            f"data_source_id IS NULL OR {_merchant_id_check('data_source_id')}",
            name="ck_google_merchant_connections_data_source_id",
        ),
        CheckConstraint(
            f"length(content_language) = 2 AND content_language !~ '[^{_LOWER}]'",
            name="ck_google_merchant_connections_content_language",
        ),
        CheckConstraint(
            f"length(feed_label) BETWEEN 1 AND 20 AND feed_label !~ '[^{_UPPER}{_DIGITS}_-]'",
            name="ck_google_merchant_connections_feed_label",
        ),
        CheckConstraint(
            "status <> 'bound' OR (data_source_id IS NOT NULL AND binding_verified_at IS NOT NULL)",
            name="ck_google_merchant_connections_bound",
        ),
        CheckConstraint(
            f"(credential_enc IS NULL OR left(credential_enc, 5) = '{CREDENTIAL_PREFIX}')"
            " AND (status <> 'revoked' OR credential_enc IS NULL)",
            name="ck_google_merchant_connections_credential",
        ),
        CheckConstraint("version >= 0 AND plan_sequence >= 0", name="ck_google_merchant_connections_counters"),
    )


class GoogleMerchantOfferLedger(GoogleMerchantBase):
    __tablename__ = LEDGER_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    account_id = Column(String(20), nullable=False)
    data_source_id = Column(String(20), nullable=False)
    content_language = Column(String(2), nullable=False)
    feed_label = Column(String(20), nullable=False)
    offer_id = Column(String(50), nullable=False)
    product_id = Column(Integer, nullable=True)
    variant_id = Column(Integer, nullable=True)
    ownership = Column(String(16), nullable=False)
    applied_attributes = Column(JSONB, nullable=True)
    applied_fingerprint = Column(String(64), nullable=True)
    applied_generation = Column(BigInteger, nullable=True)
    applied_idempotency_key = Column(String(64), nullable=True)
    admitted_generation = Column(BigInteger, nullable=False, server_default=text("0"))
    op_state = Column(String(24), nullable=False)
    op_id = Column(String(64), nullable=True)
    op_action = Column(String(8), nullable=True)
    op_snapshot = Column(Text, nullable=True)
    op_snapshot_hash = Column(String(64), nullable=True)
    op_idempotency_key = Column(String(64), nullable=True)
    op_generation = Column(BigInteger, nullable=True)
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    readback_attempts = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    attempt_id = Column(String(64), nullable=True)
    lease_owner = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    lease_fence = Column(BigInteger, nullable=False, server_default=text("0"))
    last_outcome = Column(String(48), nullable=True)
    last_error_code = Column(String(64), nullable=True)
    publication_state = Column(String(16), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id", "account_id", "data_source_id", "content_language", "feed_label"],
            [f"{CONNECTIONS_TABLE}.id", f"{CONNECTIONS_TABLE}.tenant_id", f"{CONNECTIONS_TABLE}.account_id",
             f"{CONNECTIONS_TABLE}.data_source_id", f"{CONNECTIONS_TABLE}.content_language",
             f"{CONNECTIONS_TABLE}.feed_label"],
            name="fk_google_merchant_offer_ledger_scope",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="FULL",
        ),
        UniqueConstraint(
            "account_id", "content_language", "feed_label", "offer_id",
            name="uq_google_merchant_offer_ledger_identity",
        ),
        UniqueConstraint("op_id", name="uq_google_merchant_offer_ledger_op_id"),
        UniqueConstraint("op_idempotency_key", name="uq_google_merchant_offer_ledger_op_key"),
        CheckConstraint(f"ownership IN ({_in(OWNERSHIP_STATES)})", name="ck_google_merchant_offer_ledger_ownership"),
        CheckConstraint(f"op_state IN ({_in(OP_STATES)})", name="ck_google_merchant_offer_ledger_op_state"),
        CheckConstraint(
            f"op_action IS NULL OR op_action IN ({_in(OP_ACTIONS)})", name="ck_google_merchant_offer_ledger_op_action",
        ),
        CheckConstraint(
            f"(op_state = 'idle' AND {_all(_OP_COLUMNS, 'IS NULL')})"
            f" OR (op_state <> 'idle' AND {_all(_OP_COLUMNS, 'IS NOT NULL')})",
            name="ck_google_merchant_offer_ledger_op_shape",
        ),
        CheckConstraint(
            "op_snapshot IS NULL OR op_snapshot_hash = encode(sha256(convert_to(op_snapshot, 'UTF8')), 'hex')",
            name="ck_google_merchant_offer_ledger_op_snapshot_hash",
        ),
        CheckConstraint(
            "(op_state = 'dispatching' AND dispatched_at IS NOT NULL AND attempt_id IS NOT NULL"
            " AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)"
            " OR (op_state <> 'dispatching' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_google_merchant_offer_ledger_lease",
        ),
        CheckConstraint(
            "(last_outcome IS NULL AND last_error_code IS NULL AND next_attempt_at IS NULL)"
            " OR (op_state <> 'dispatching' AND attempt_id IS NOT NULL)",
            name="ck_google_merchant_offer_ledger_attempt_outcome",
        ),
        CheckConstraint(
            f"({_all(_APPLIED_COLUMNS, 'IS NULL')}) OR ({_all(_APPLIED_COLUMNS, 'IS NOT NULL')})",
            name="ck_google_merchant_offer_ledger_applied_shape",
        ),
        CheckConstraint(
            "ownership <> 'owned' OR applied_fingerprint IS NOT NULL",
            name="ck_google_merchant_offer_ledger_owned_evidence",
        ),
        CheckConstraint(
            "admitted_generation >= 0"
            " AND (op_generation IS NULL OR (op_generation >= 1 AND op_generation <= admitted_generation))"
            " AND (applied_generation IS NULL OR applied_generation <= admitted_generation)",
            name="ck_google_merchant_offer_ledger_generations",
        ),
        CheckConstraint(
            "attempts >= 0 AND readback_attempts >= 0 AND lease_fence >= 0 AND version >= 0",
            name="ck_google_merchant_offer_ledger_counters",
        ),
        CheckConstraint(
            " AND ".join(_hex64(c) for c in (
                "applied_fingerprint", "applied_idempotency_key", "op_idempotency_key", "op_snapshot_hash"))
            + " AND " + _token("op_id") + " AND " + _token("attempt_id"),
            name="ck_google_merchant_offer_ledger_tokens",
        ),
        CheckConstraint(
            "length(offer_id) BETWEEN 1 AND 50 AND offer_id !~ '[[:cntrl:]]' AND offer_id = btrim(offer_id)",
            name="ck_google_merchant_offer_ledger_offer_id",
        ),
        Index("ix_google_merchant_offer_ledger_due", "op_state", "next_attempt_at"),
        Index("ix_google_merchant_offer_ledger_tenant_ownership", "tenant_id", "ownership"),
    )


# Same-account case-collision backstop. Python ``casefold`` in the planner
# stays authoritative; ``lower`` differs from it for a few characters.
Index(
    "ux_google_merchant_offer_ledger_identity_lower",
    GoogleMerchantOfferLedger.__table__.c.account_id,
    GoogleMerchantOfferLedger.__table__.c.content_language,
    GoogleMerchantOfferLedger.__table__.c.feed_label,
    func.lower(GoogleMerchantOfferLedger.__table__.c.offer_id),
    unique=True,
)


CONNECTIONS_GUARD_SQL = f"""
CREATE FUNCTION {CONNECTIONS_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
  -- Row DELETE and statement TRUNCATE alike: TRUNCATE never fires row triggers.
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'google_merchant_connections rows are retained as account tombstones'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'authorized' OR NEW.data_source_id IS NOT NULL
       OR NEW.version <> 0 OR NEW.plan_sequence <> 0 THEN
      RAISE EXCEPTION 'google_merchant_connections rows start authorized, unbound, at version 0'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.account_id IS DISTINCT FROM OLD.account_id
     OR NEW.content_language IS DISTINCT FROM OLD.content_language
     OR NEW.feed_label IS DISTINCT FROM OLD.feed_label
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'google_merchant_connections identity is immutable'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF OLD.data_source_id IS NOT NULL AND NEW.data_source_id IS DISTINCT FROM OLD.data_source_id THEN
    RAISE EXCEPTION 'google_merchant_connections data source is bound once and then immutable'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'google_merchant_connections version must advance by exactly 1'
      USING ERRCODE = 'serialization_failure';
  END IF;
  IF NEW.plan_sequence < OLD.plan_sequence THEN
    RAISE EXCEPTION 'google_merchant_connections plan_sequence never decreases'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END;
$guard$;
CREATE TRIGGER {CONNECTIONS_GUARD_TRIGGER}
  BEFORE INSERT OR UPDATE OR DELETE ON {CONNECTIONS_TABLE}
  FOR EACH ROW EXECUTE FUNCTION {CONNECTIONS_GUARD_FUNCTION}();
CREATE TRIGGER {CONNECTIONS_GUARD_TRIGGER}_truncate
  BEFORE TRUNCATE ON {CONNECTIONS_TABLE}
  FOR EACH STATEMENT EXECUTE FUNCTION {CONNECTIONS_GUARD_FUNCTION}();
"""

LEDGER_GUARD_SQL = f"""
CREATE FUNCTION {LEDGER_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $guard$
DECLARE
  op_same boolean;
  applied_same boolean;
  attempt_same boolean;
  outcome_same boolean;
  evidence_same boolean;
  new_op boolean;
  live_lease boolean;
  ok boolean := false;
BEGIN
  -- Row DELETE and statement TRUNCATE alike: TRUNCATE never fires row triggers.
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'google_merchant_offer_ledger rows are retained as identity, generation and evidence tombstones'
      USING ERRCODE = 'restrict_violation';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.ownership <> 'claiming' OR NEW.op_state <> 'pending' OR NEW.op_action IS DISTINCT FROM 'insert'
       OR NEW.attempts <> 0 OR NEW.readback_attempts <> 0 OR NEW.lease_fence <> 0 OR NEW.version <> 0
       OR NEW.dispatched_at IS NOT NULL OR NEW.attempt_id IS NOT NULL OR NEW.applied_fingerprint IS NOT NULL
       OR NEW.admitted_generation IS DISTINCT FROM NEW.op_generation THEN
      RAISE EXCEPTION 'google_merchant_offer_ledger rows start as a never-dispatched insert claim'
        USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM {CONNECTIONS_TABLE} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'google_merchant_offer_ledger admits work only under a bound connection'
        USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
     OR NEW.account_id IS DISTINCT FROM OLD.account_id
     OR NEW.data_source_id IS DISTINCT FROM OLD.data_source_id
     OR NEW.content_language IS DISTINCT FROM OLD.content_language
     OR NEW.feed_label IS DISTINCT FROM OLD.feed_label
     OR NEW.offer_id IS DISTINCT FROM OLD.offer_id
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'google_merchant_offer_ledger identity is immutable'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'google_merchant_offer_ledger version must advance by exactly 1'
      USING ERRCODE = 'serialization_failure';
  END IF;
  IF NEW.admitted_generation < OLD.admitted_generation OR NEW.lease_fence < OLD.lease_fence THEN
    RAISE EXCEPTION 'google_merchant_offer_ledger high-water and fence never decrease'
      USING ERRCODE = 'restrict_violation';
  END IF;

  op_same := (NEW.op_id, NEW.op_action, NEW.op_snapshot, NEW.op_snapshot_hash, NEW.op_idempotency_key, NEW.op_generation)
             IS NOT DISTINCT FROM
             (OLD.op_id, OLD.op_action, OLD.op_snapshot, OLD.op_snapshot_hash, OLD.op_idempotency_key, OLD.op_generation);
  applied_same := (NEW.applied_attributes, NEW.applied_fingerprint, NEW.applied_generation, NEW.applied_idempotency_key)
                  IS NOT DISTINCT FROM
                  (OLD.applied_attributes, OLD.applied_fingerprint, OLD.applied_generation, OLD.applied_idempotency_key);
  attempt_same := NEW.attempt_id IS NOT DISTINCT FROM OLD.attempt_id AND NEW.lease_fence = OLD.lease_fence
                  AND NEW.attempts = OLD.attempts AND NEW.dispatched_at IS NOT DISTINCT FROM OLD.dispatched_at;
  -- Outcome evidence of an attempt (and the 429 retry time) is written only by
  -- the transition that ends that live attempt, and is then frozen.
  evidence_same := (NEW.last_outcome, NEW.last_error_code) IS NOT DISTINCT FROM (OLD.last_outcome, OLD.last_error_code);
  outcome_same := evidence_same AND NEW.next_attempt_at IS NOT DISTINCT FROM OLD.next_attempt_at;
  -- The real clock, not the transaction start time: a transaction begun before
  -- expiry (or one that waited on the row lock past it) must not see a live
  -- lease. This trigger runs with the row already locked.
  live_lease := OLD.lease_expires_at IS NOT NULL AND OLD.lease_expires_at > clock_timestamp();
  -- A newly admitted, never-dispatched operation with a fresh id and key and a
  -- generation above the retained high-water mark.
  new_op := NEW.op_state = 'pending'
            AND NEW.op_id IS DISTINCT FROM OLD.op_id
            AND NEW.op_idempotency_key IS DISTINCT FROM OLD.op_idempotency_key
            AND NEW.op_idempotency_key IS DISTINCT FROM OLD.applied_idempotency_key
            AND NEW.op_generation > OLD.admitted_generation
            AND NEW.admitted_generation = NEW.op_generation
            AND NEW.attempts = 0 AND NEW.readback_attempts = 0
            AND NEW.dispatched_at IS NULL AND NEW.attempt_id IS NULL
            AND NEW.lease_fence = OLD.lease_fence
            AND applied_same
            AND ((NEW.op_action = 'insert' AND OLD.ownership IN ('claiming', 'released') AND NEW.ownership = 'claiming')
                 OR (NEW.op_action IN ('patch', 'delete') AND OLD.ownership = 'owned' AND NEW.ownership = 'owned'));
  IF new_op THEN
    PERFORM 1 FROM {CONNECTIONS_TABLE} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'google_merchant_offer_ledger admits work only under a bound connection'
        USING ERRCODE = 'restrict_violation';
    END IF;
    -- New work has no attempt yet, so it carries no attempt outcome either.
    NEW.last_outcome := NULL;
    NEW.last_error_code := NULL;
    NEW.next_attempt_at := NULL;
  END IF;

  IF OLD.op_state = 'idle' THEN
    IF NEW.op_state = 'idle' THEN
      ok := applied_same AND NEW.ownership = OLD.ownership AND attempt_same AND outcome_same
            AND NEW.admitted_generation = OLD.admitted_generation;
    ELSE
      ok := new_op;
    END IF;
  ELSIF OLD.op_state = 'pending' THEN
    IF NEW.op_state = 'pending' THEN
      -- Same operation (e.g. rescheduled after a 429), or replacement of an
      -- operation that was never dispatched.
      ok := (op_same AND applied_same AND attempt_same AND outcome_same AND NEW.ownership = OLD.ownership
             AND NEW.admitted_generation = OLD.admitted_generation)
            OR (new_op AND OLD.attempts = 0 AND OLD.dispatched_at IS NULL AND OLD.attempt_id IS NULL);
    ELSIF NEW.op_state = 'dispatching' THEN
      ok := op_same AND applied_same AND NEW.ownership = OLD.ownership
            AND NEW.admitted_generation = OLD.admitted_generation
            AND NEW.lease_fence = OLD.lease_fence + 1 AND NEW.attempts = OLD.attempts + 1
            AND NEW.attempt_id IS NOT NULL AND NEW.attempt_id IS DISTINCT FROM OLD.attempt_id;
      IF ok THEN
        -- A fresh dispatch is new external work: the connection must still be
        -- bound, read under the same shared lock that serializes it against a
        -- concurrent disable or revoke.
        PERFORM 1 FROM {CONNECTIONS_TABLE} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'google_merchant_offer_ledger dispatches only under a bound connection'
            USING ERRCODE = 'restrict_violation';
        END IF;
        -- The attempt's start is the database clock, not a caller's claim; a
        -- fresh attempt never inherits the previous attempt's outcome or retry time.
        NEW.dispatched_at := clock_timestamp();
        NEW.last_outcome := NULL;
        NEW.last_error_code := NULL;
        NEW.next_attempt_at := NULL;
      END IF;
    END IF;
  ELSIF OLD.op_state = 'dispatching' THEN
    IF op_same AND applied_same AND attempt_same AND NEW.ownership = OLD.ownership
       AND NEW.admitted_generation = OLD.admitted_generation THEN
      IF NEW.op_state = 'readback_required' THEN
        ok := true;  -- the reply, a crash or an expired lease: read back, never resend
      ELSIF NEW.op_state = 'dispatching' THEN
        -- Renewal by the live holder only; the owner of a live attempt never
        -- changes (a new owner needs a new fenced attempt, or reconciliation).
        ok := live_lease AND NEW.lease_owner = OLD.lease_owner;
      ELSIF NEW.op_state = 'pending' THEN
        ok := live_lease AND NEW.last_outcome = '{RATE_LIMITED_OUTCOME}' AND NEW.next_attempt_at > OLD.dispatched_at;
      ELSIF NEW.op_state = 'rejected' THEN
        ok := live_lease AND NEW.last_error_code IS NOT NULL;
      END IF;
    END IF;
  ELSIF OLD.op_state IN ('readback_required', 'unresolved') THEN
    IF NEW.op_state IN ('readback_required', 'unresolved') THEN
      ok := op_same AND applied_same AND attempt_same AND evidence_same AND NEW.ownership = OLD.ownership
            AND NEW.admitted_generation = OLD.admitted_generation;
    ELSIF NEW.op_state = 'idle' THEN
      -- Confirmation by read-back: the applied evidence is exactly this operation.
      ok := attempt_same AND evidence_same AND NEW.admitted_generation = OLD.admitted_generation
            AND NEW.applied_fingerprint IS NOT NULL
            AND NEW.applied_idempotency_key = OLD.op_idempotency_key
            AND NEW.applied_generation = OLD.op_generation
            AND ((OLD.op_action IN ('insert', 'patch') AND NEW.ownership = 'owned')
                 OR (OLD.op_action = 'delete' AND NEW.ownership = 'released'));
    END IF;
  ELSIF OLD.op_state = 'rejected' THEN
    IF NEW.op_state = 'rejected' THEN
      ok := op_same AND applied_same AND attempt_same AND outcome_same AND NEW.ownership = OLD.ownership
            AND NEW.admitted_generation = OLD.admitted_generation;
    ELSIF NEW.op_state = 'idle' THEN
      -- A definitive rejection had no effect: close it; an unconfirmed claim is released.
      ok := applied_same AND attempt_same AND outcome_same AND NEW.admitted_generation = OLD.admitted_generation
            AND ((OLD.ownership = 'claiming' AND NEW.ownership = 'released')
                 OR (OLD.ownership <> 'claiming' AND NEW.ownership = OLD.ownership));
    ELSE
      ok := new_op;
    END IF;
  END IF;

  IF NOT coalesce(ok, false) THEN
    RAISE EXCEPTION USING
      MESSAGE = 'google_merchant_offer_ledger transition ' || OLD.op_state || ' -> ' || NEW.op_state || ' is not allowed',
      ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END;
$guard$;
CREATE TRIGGER {LEDGER_GUARD_TRIGGER}
  BEFORE INSERT OR UPDATE OR DELETE ON {LEDGER_TABLE}
  FOR EACH ROW EXECUTE FUNCTION {LEDGER_GUARD_FUNCTION}();
CREATE TRIGGER {LEDGER_GUARD_TRIGGER}_truncate
  BEFORE TRUNCATE ON {LEDGER_TABLE}
  FOR EACH STATEMENT EXECUTE FUNCTION {LEDGER_GUARD_FUNCTION}();
"""

GOOGLE_MERCHANT_TABLES = (GoogleMerchantConnection.__table__, GoogleMerchantOfferLedger.__table__)
GOOGLE_MERCHANT_TABLE_NAMES = tuple(t.name for t in GOOGLE_MERCHANT_TABLES)
GOOGLE_MERCHANT_GUARD_FUNCTIONS = (CONNECTIONS_GUARD_FUNCTION, LEDGER_GUARD_FUNCTION)


def create_google_merchant_tables(bind) -> None:
    """Test/provisioning helper: the two Google tables and guards (never ``tenants``)."""
    GoogleMerchantBase.metadata.create_all(bind, tables=list(GOOGLE_MERCHANT_TABLES))
    bind.exec_driver_sql(CONNECTIONS_GUARD_SQL)
    bind.exec_driver_sql(LEDGER_GUARD_SQL)


__all__ = [
    "CONNECTIONS_TABLE",
    "CREDENTIAL_PREFIX",
    "GOOGLE_MERCHANT_GUARD_FUNCTIONS",
    "GOOGLE_MERCHANT_TABLES",
    "GOOGLE_MERCHANT_TABLE_NAMES",
    "GoogleMerchantBase",
    "GoogleMerchantConnection",
    "GoogleMerchantOfferLedger",
    "LEDGER_TABLE",
    "RATE_LIMITED_OUTCOME",
    "create_google_merchant_tables",
]
