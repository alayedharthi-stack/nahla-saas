"""Dormant Google Merchant connection binding and offer ledger.

Revision ID: 0124
Revises: 0123

Creates the two Google Merchant tables declared on ``GoogleMerchantBase`` in
``backend/services/google_merchant_models.py`` (deliberately not part of
``models.Base``, so startup ``create_all`` never builds them), with their
guard functions and triggers:

  google_merchant_connections   one row per tenant and per Merchant account,
                                for every status (rows are never deleted);
                                identity immutable, data source bound once
  google_merchant_offer_ledger  one row per (account, language, feed label,
                                offer id): ownership evidence, outbox and
                                idempotency; scope frozen, dispatched
                                operation frozen, monotonic lease fence,
                                admitted-generation high-water mark, lease
                                liveness by clock_timestamp(), attempt
                                outcomes bound to their attempt; rows are
                                never deleted or truncated (tombstones)

Additive only. If either table or either guard function already exists —
complete, partial or malformed — the upgrade refuses and changes nothing: they
are new here, so a pre-existing one was not built by this revision and its
constraints cannot be trusted. Chained on 0123 and replaces it as that
branch's head; it merges nothing and normal bootstrap (pinned to 0093) never
applies it. Apply only as an explicit, owner-approved step after a read-only
check of the target database's ``alembic_version``.

Downgrade is not a routine rollback: it takes ACCESS EXCLUSIVE locks on both
tables first, refuses (changing nothing) while either holds any row, and
otherwise drops only these two tables and their guard functions.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

CONNECTIONS = "google_merchant_connections"
LEDGER = "google_merchant_offer_ledger"
TABLES = (CONNECTIONS, LEDGER)
CONNECTIONS_GUARD = "google_merchant_connections_guard"
LEDGER_GUARD = "google_merchant_offer_ledger_guard"
FUNCTIONS = (CONNECTIONS_GUARD, LEDGER_GUARD)

_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_HEX = "0123456789abcdef"
_OP_COLUMNS = ("op_id", "op_action", "op_snapshot", "op_snapshot_hash", "op_idempotency_key", "op_generation")
_APPLIED_COLUMNS = ("applied_attributes", "applied_fingerprint", "applied_generation", "applied_idempotency_key")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _merchant_id_check(column: str) -> str:
    return f"(length({column}) BETWEEN 1 AND 20 AND {column} !~ '[^{_DIGITS}]' AND left({column}, 1) <> '0')"


def _hex64(column: str) -> str:
    return f"({column} IS NULL OR (length({column}) = 64 AND {column} !~ '[^{_HEX}]'))"


def _token(column: str) -> str:
    return f"({column} IS NULL OR (length({column}) BETWEEN 1 AND 64 AND {column} !~ '[^{_HEX}-]'))"


def _all(columns, predicate: str) -> str:
    return " AND ".join(f"{c} {predicate}" for c in columns)


def _ts(name: str, nullable: bool = True, default: bool = False) -> sa.Column:
    kwargs = {"server_default": sa.text("now()")} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kwargs)


def _zero(name: str, kind=sa.BigInteger) -> sa.Column:
    return sa.Column(name, kind(), nullable=False, server_default=sa.text("0"))


def _definitions() -> tuple:
    meta = sa.MetaData()
    sa.Table("tenants", meta, sa.Column("id", sa.Integer, primary_key=True))
    connections = sa.Table(
        CONNECTIONS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("account_id", sa.String(20), nullable=False),
        sa.Column("data_source_id", sa.String(20), nullable=True),
        sa.Column("content_language", sa.String(2), nullable=False),
        sa.Column("feed_label", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("credential_enc", sa.Text(), nullable=True),
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=True),
        sa.Column("authorized_by_user_id", sa.Integer(), nullable=True),
        _ts("verified_at"),
        _ts("binding_verified_at"),
        _ts("discovery_checked_at"),
        sa.Column("discovery_summary", postgresql.JSONB(), nullable=True),
        _zero("plan_sequence"),
        _zero("version"),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.UniqueConstraint("tenant_id", name="uq_google_merchant_connections_tenant"),
        sa.UniqueConstraint("account_id", name="uq_google_merchant_connections_account"),
        sa.UniqueConstraint("account_id", "data_source_id", name="uq_google_merchant_connections_account_source"),
        sa.UniqueConstraint(
            "id", "tenant_id", "account_id", "data_source_id", "content_language", "feed_label",
            name="uq_google_merchant_connections_scope",
        ),
        sa.CheckConstraint(
            f"status IN ({_in(('authorized', 'bound', 'disabled', 'revoked'))})",
            name="ck_google_merchant_connections_status",
        ),
        sa.CheckConstraint(_merchant_id_check("account_id"), name="ck_google_merchant_connections_account_id"),
        sa.CheckConstraint(
            f"data_source_id IS NULL OR {_merchant_id_check('data_source_id')}",
            name="ck_google_merchant_connections_data_source_id",
        ),
        sa.CheckConstraint(
            f"length(content_language) = 2 AND content_language !~ '[^{_LOWER}]'",
            name="ck_google_merchant_connections_content_language",
        ),
        sa.CheckConstraint(
            f"length(feed_label) BETWEEN 1 AND 20 AND feed_label !~ '[^{_UPPER}{_DIGITS}_-]'",
            name="ck_google_merchant_connections_feed_label",
        ),
        sa.CheckConstraint(
            "status <> 'bound' OR (data_source_id IS NOT NULL AND binding_verified_at IS NOT NULL)",
            name="ck_google_merchant_connections_bound",
        ),
        sa.CheckConstraint(
            "(credential_enc IS NULL OR left(credential_enc, 5) = 'gmc1:')"
            " AND (status <> 'revoked' OR credential_enc IS NULL)",
            name="ck_google_merchant_connections_credential",
        ),
        sa.CheckConstraint("version >= 0 AND plan_sequence >= 0", name="ck_google_merchant_connections_counters"),
    )
    ledger = sa.Table(
        LEDGER, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.String(20), nullable=False),
        sa.Column("data_source_id", sa.String(20), nullable=False),
        sa.Column("content_language", sa.String(2), nullable=False),
        sa.Column("feed_label", sa.String(20), nullable=False),
        sa.Column("offer_id", sa.String(50), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("variant_id", sa.Integer(), nullable=True),
        sa.Column("ownership", sa.String(16), nullable=False),
        sa.Column("applied_attributes", postgresql.JSONB(), nullable=True),
        sa.Column("applied_fingerprint", sa.String(64), nullable=True),
        sa.Column("applied_generation", sa.BigInteger(), nullable=True),
        sa.Column("applied_idempotency_key", sa.String(64), nullable=True),
        _zero("admitted_generation"),
        sa.Column("op_state", sa.String(24), nullable=False),
        sa.Column("op_id", sa.String(64), nullable=True),
        sa.Column("op_action", sa.String(8), nullable=True),
        sa.Column("op_snapshot", sa.Text(), nullable=True),
        sa.Column("op_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("op_idempotency_key", sa.String(64), nullable=True),
        sa.Column("op_generation", sa.BigInteger(), nullable=True),
        _zero("attempts", sa.Integer),
        _zero("readback_attempts", sa.Integer),
        _ts("next_attempt_at"),
        _ts("dispatched_at"),
        sa.Column("attempt_id", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        _ts("lease_expires_at"),
        _zero("lease_fence"),
        sa.Column("last_outcome", sa.String(48), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("publication_state", sa.String(16), nullable=True),
        _ts("observed_at"),
        _zero("version"),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.ForeignKeyConstraint(
            ["connection_id", "tenant_id", "account_id", "data_source_id", "content_language", "feed_label"],
            [f"{CONNECTIONS}.id", f"{CONNECTIONS}.tenant_id", f"{CONNECTIONS}.account_id",
             f"{CONNECTIONS}.data_source_id", f"{CONNECTIONS}.content_language", f"{CONNECTIONS}.feed_label"],
            name="fk_google_merchant_offer_ledger_scope",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="FULL",
        ),
        sa.UniqueConstraint(
            "account_id", "content_language", "feed_label", "offer_id",
            name="uq_google_merchant_offer_ledger_identity",
        ),
        sa.UniqueConstraint("op_id", name="uq_google_merchant_offer_ledger_op_id"),
        sa.UniqueConstraint("op_idempotency_key", name="uq_google_merchant_offer_ledger_op_key"),
        sa.CheckConstraint(
            f"ownership IN ({_in(('claiming', 'owned', 'released'))})",
            name="ck_google_merchant_offer_ledger_ownership",
        ),
        sa.CheckConstraint(
            f"op_state IN ({_in(('idle', 'pending', 'dispatching', 'readback_required', 'unresolved', 'rejected'))})",
            name="ck_google_merchant_offer_ledger_op_state",
        ),
        sa.CheckConstraint(
            f"op_action IS NULL OR op_action IN ({_in(('insert', 'patch', 'delete'))})",
            name="ck_google_merchant_offer_ledger_op_action",
        ),
        sa.CheckConstraint(
            f"(op_state = 'idle' AND {_all(_OP_COLUMNS, 'IS NULL')})"
            f" OR (op_state <> 'idle' AND {_all(_OP_COLUMNS, 'IS NOT NULL')})",
            name="ck_google_merchant_offer_ledger_op_shape",
        ),
        sa.CheckConstraint(
            "op_snapshot IS NULL OR op_snapshot_hash = encode(sha256(convert_to(op_snapshot, 'UTF8')), 'hex')",
            name="ck_google_merchant_offer_ledger_op_snapshot_hash",
        ),
        sa.CheckConstraint(
            "(op_state = 'dispatching' AND dispatched_at IS NOT NULL AND attempt_id IS NOT NULL"
            " AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)"
            " OR (op_state <> 'dispatching' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_google_merchant_offer_ledger_lease",
        ),
        sa.CheckConstraint(
            "(last_outcome IS NULL AND last_error_code IS NULL AND next_attempt_at IS NULL)"
            " OR (op_state <> 'dispatching' AND attempt_id IS NOT NULL)",
            name="ck_google_merchant_offer_ledger_attempt_outcome",
        ),
        sa.CheckConstraint(
            f"({_all(_APPLIED_COLUMNS, 'IS NULL')}) OR ({_all(_APPLIED_COLUMNS, 'IS NOT NULL')})",
            name="ck_google_merchant_offer_ledger_applied_shape",
        ),
        sa.CheckConstraint(
            "ownership <> 'owned' OR applied_fingerprint IS NOT NULL",
            name="ck_google_merchant_offer_ledger_owned_evidence",
        ),
        sa.CheckConstraint(
            "admitted_generation >= 0"
            " AND (op_generation IS NULL OR (op_generation >= 1 AND op_generation <= admitted_generation))"
            " AND (applied_generation IS NULL OR applied_generation <= admitted_generation)",
            name="ck_google_merchant_offer_ledger_generations",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND readback_attempts >= 0 AND lease_fence >= 0 AND version >= 0",
            name="ck_google_merchant_offer_ledger_counters",
        ),
        sa.CheckConstraint(
            " AND ".join(_hex64(c) for c in (
                "applied_fingerprint", "applied_idempotency_key", "op_idempotency_key", "op_snapshot_hash"))
            + " AND " + _token("op_id") + " AND " + _token("attempt_id"),
            name="ck_google_merchant_offer_ledger_tokens",
        ),
        sa.CheckConstraint(
            "length(offer_id) BETWEEN 1 AND 50 AND offer_id !~ '[[:cntrl:]]' AND offer_id = btrim(offer_id)",
            name="ck_google_merchant_offer_ledger_offer_id",
        ),
        sa.Index("ix_google_merchant_offer_ledger_due", "op_state", "next_attempt_at"),
        sa.Index("ix_google_merchant_offer_ledger_tenant_ownership", "tenant_id", "ownership"),
    )
    sa.Index(
        "ux_google_merchant_offer_ledger_identity_lower",
        ledger.c.account_id, ledger.c.content_language, ledger.c.feed_label, sa.func.lower(ledger.c.offer_id),
        unique=True,
    )
    return connections, ledger


CONNECTIONS_GUARD_SQL = f"""
CREATE FUNCTION {CONNECTIONS_GUARD}() RETURNS trigger
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
CREATE TRIGGER trg_google_merchant_connections_guard
  BEFORE INSERT OR UPDATE OR DELETE ON {CONNECTIONS}
  FOR EACH ROW EXECUTE FUNCTION {CONNECTIONS_GUARD}();
CREATE TRIGGER trg_google_merchant_connections_guard_truncate
  BEFORE TRUNCATE ON {CONNECTIONS}
  FOR EACH STATEMENT EXECUTE FUNCTION {CONNECTIONS_GUARD}();
"""

LEDGER_GUARD_SQL = f"""
CREATE FUNCTION {LEDGER_GUARD}() RETURNS trigger
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
    PERFORM 1 FROM {CONNECTIONS} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
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
    PERFORM 1 FROM {CONNECTIONS} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
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
        PERFORM 1 FROM {CONNECTIONS} WHERE id = NEW.connection_id AND status = 'bound' FOR SHARE;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'google_merchant_offer_ledger dispatches only under a bound connection'
            USING ERRCODE = 'restrict_violation';
        END IF;
        -- A fresh attempt never inherits the previous attempt's outcome or retry time.
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
        ok := live_lease AND NEW.last_outcome = 'rate_limited' AND NEW.next_attempt_at > OLD.dispatched_at;
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
CREATE TRIGGER trg_google_merchant_offer_ledger_guard
  BEFORE INSERT OR UPDATE OR DELETE ON {LEDGER}
  FOR EACH ROW EXECUTE FUNCTION {LEDGER_GUARD}();
CREATE TRIGGER trg_google_merchant_offer_ledger_guard_truncate
  BEFORE TRUNCATE ON {LEDGER}
  FOR EACH STATEMENT EXECUTE FUNCTION {LEDGER_GUARD}();
"""


def _existing(bind) -> list:
    tables = set(sa.inspect(bind).get_table_names()) & set(TABLES)
    functions = set(bind.execute(
        sa.text("SELECT proname FROM pg_proc WHERE proname = ANY(:names)"), {"names": list(FUNCTIONS)},
    ).scalars())
    return sorted(tables | functions)


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    if present:
        # New in this revision. Any pre-existing table or guard function —
        # complete, partial or malformed — was created outside this migration
        # and its constraints cannot be trusted. Refuse without changing
        # anything; an operator inspects and removes it before applying 0124.
        raise RuntimeError(
            "Google Merchant objects already exist (" + ", ".join(present) + "); they were not created "
            "by revision 0124. Inspect and remove them manually before applying this revision."
        )
    connections, ledger = _definitions()
    connections.create(bind)
    ledger.create(bind)
    bind.exec_driver_sql(CONNECTIONS_GUARD_SQL)
    bind.exec_driver_sql(LEDGER_GUARD_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    if not present:
        return
    if present != sorted(set(TABLES) | set(FUNCTIONS)):
        raise RuntimeError(
            "Google Merchant objects are only partly present (" + ", ".join(present) + "); "
            "refusing to downgrade. Inspect them manually."
        )
    # Lock both tables before looking at them, so no row can arrive between the
    # emptiness check and the drop. Ownership and pending evidence is never
    # discarded by a downgrade: any row makes it refuse.
    bind.exec_driver_sql(f"LOCK TABLE {LEDGER}, {CONNECTIONS} IN ACCESS EXCLUSIVE MODE")
    populated = [name for name in (LEDGER, CONNECTIONS)
                 if bind.exec_driver_sql(f"SELECT EXISTS (SELECT 1 FROM {name})").scalar()]
    if populated:
        raise RuntimeError(
            "Google Merchant tables hold evidence (" + ", ".join(populated) + "); downgrade refused. "
            "Rows are retained; this revision is never rolled back over data."
        )
    op.drop_table(LEDGER)
    op.drop_table(CONNECTIONS)
    bind.exec_driver_sql(f"DROP FUNCTION {LEDGER_GUARD}()")
    bind.exec_driver_sql(f"DROP FUNCTION {CONNECTIONS_GUARD}()")
