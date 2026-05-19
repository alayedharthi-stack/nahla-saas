"""
core/merchant_provisioning.py
─────────────────────────────
Idempotent "find or create" merchant identity used by every storefront
OAuth path (Salla today, Zid in a follow-up).

Why this module exists
──────────────────────
The Salla token-login + legacy callback handlers have grown three
overlapping flows that resolve a (Tenant, User) pair from the
introspected store identity:

  1. Integration by ``external_store_id`` → tenant_id → user on tenant
  2. Integration by ``config.store_id`` (legacy, repair to top-level)
  3. Email lookup (only when no store_id was returned)

Each call site re-implemented these branches with subtle differences:
the worst was ``salla_token_login`` (Phase 1A) where branch 1 found a
returning store but **never inserted a User row** when the merchant's
email had changed in Salla — leaving the JWT with ``user_id=None``.

This helper centralises the logic with one explicit guarantee:

    >>> after a successful call, db.query(User).filter(...).first()
    >>> for the returned ``tenant_id`` is ALWAYS non-null.

It also issues a single-use ``PasswordSetupToken`` whenever a User row
is auto-created — that token is what the caller embeds in the welcome
email so the merchant can later log in directly to ``app.nahlah.ai``.

Decoupling promise
──────────────────
The provider OAuth path never reads or writes ``User.password_hash``
during login — it only matches by ``external_store_id``. That means a
merchant who later changes / forgets / wipes their local password can
still log in from inside Salla without any breakage. See
``docs/security/MERCHANT_AUTH.md`` for the threat model.
"""
from __future__ import annotations

import logging
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.merchant_provisioning")


# ── Result type ───────────────────────────────────────────────────────────────
@dataclass
class ProvisioningResult:
    """Outcome of ``get_or_create_merchant_user``.

    Caller uses these flags to:
      * decide whether to send a welcome email (``created_user`` only)
      * pick the right audit event name
      * log the right line in the OAuth callback's structured log
    """
    user_id:           int
    tenant_id:         int
    email:             str
    role:              str
    created_user:      bool   # we just inserted this User row
    created_tenant:    bool   # we just inserted this Tenant row
    linked_existing:   bool   # integration + user both already existed
    filled_gap:        bool   # integration existed, user was missing, we created
    set_password_token: Optional[str]   # raw single-use token, only when created_user=True

    @property
    def is_brand_new(self) -> bool:
        return self.created_tenant and self.created_user


# ── Public helper ─────────────────────────────────────────────────────────────
def get_or_create_merchant_user(
    db: Session,
    *,
    provider: str,
    external_store_id: str,
    owner_email: str,
    store_name: str,
    is_email_derived: bool = False,
    issued_via: str = "",
    request_ip: str = "",
) -> ProvisioningResult:
    """Resolve (Tenant, User) for a merchant arriving via OAuth.

    Lookup priority:

        1. ``Integration(provider, external_store_id) → tenant_id``
           — this is the AUTHORITATIVE key. A returning store always
           hits this branch.
        2. ``Integration.config['store_id'] → tenant_id``
           — legacy data; we repair the row by promoting ``store_id``
           into the top-level column so step 1 wins next time.
        3. ``User.email → tenant_id``
           — last resort, ONLY when the OAuth introspect failed to
           give us a store_id AND the email is genuinely from the
           merchant (not a derived ``@salla-merchant.nahlah.ai``).

    If none match we create a new Tenant + User pair.

    On any path that creates a User (steps 2-fix-up, 3, or "new"), we
    issue a single-use set-password token and surface its raw value in
    the result. The caller is responsible for embedding it in the
    welcome email.

    Parameters
    ──────────
    provider : "salla" | "zid"
        Used both as the ``Integration.provider`` filter key AND as a
        prefix for audit log fields. Caller must already have validated
        the OAuth introspect response.

    external_store_id : str
        Stable store identifier from the OAuth provider. May be empty
        when introspect failed; in that case we fall back to email
        lookup but ``is_email_derived`` MUST be False (otherwise we'd
        be matching on a ``@salla-merchant.nahlah.ai`` placeholder
        which is meaningless cross-store).

    owner_email : str
        Already lowercased + stripped by the caller. May be a derived
        placeholder when the provider didn't return a real email — in
        which case ``is_email_derived=True`` MUST be set so we don't
        cross-link unrelated stores via fake email collisions.

    issued_via : str
        Free-form label persisted on the ``PasswordSetupToken`` row.
        Use stable values like "salla_token_login" /
        "salla_oauth_callback" / "zid_oauth_callback".
    """
    from models import Integration, Tenant, User  # noqa: PLC0415
    from core.auth import hash_password  # noqa: PLC0415
    from core.password_setup import issue_token as issue_set_password_token  # noqa: PLC0415

    if not provider:
        raise ValueError("provider is required")
    owner_email = (owner_email or "").strip().lower()
    if not owner_email:
        raise ValueError("owner_email is required")

    now_iso = datetime.now(timezone.utc).isoformat()
    store_id_str = str(external_store_id or "").strip()

    # ─ Branch 1 / 2 — store-id-based lookup (authoritative) ────────────────
    integration = None
    if store_id_str:
        integration = (
            db.query(Integration)
            .filter(
                Integration.provider == provider,
                Integration.external_store_id == store_id_str,
            )
            .first()
        )
        if integration is None:
            # Branch 2: legacy rows where store_id was only stored in the
            # config JSONB. Promote to top-level so future lookups hit
            # branch 1.
            integration = (
                db.query(Integration)
                .filter(
                    Integration.provider == provider,
                    Integration.config["store_id"].as_string() == store_id_str,
                )
                .first()
            )
            if integration is not None:
                integration.external_store_id = store_id_str
                logger.info(
                    "[provisioning] repaired external_store_id | provider=%s "
                    "integration_id=%s tenant=%s store_id=%s",
                    provider, integration.id, integration.tenant_id, store_id_str,
                )

    if integration is not None:
        tenant_id = integration.tenant_id
        cfg = dict(integration.config or {})
        canonical_email = (cfg.get("salla_owner_email") or "").strip().lower() or owner_email

        existing_user = (
            db.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.email == canonical_email,
            )
            .first()
        )
        if existing_user is not None:
            logger.info(
                "[provisioning] linked_existing | provider=%s tenant_id=%s "
                "user_id=%s store_id=%s",
                provider, tenant_id, existing_user.id, store_id_str,
            )
            return ProvisioningResult(
                user_id           = existing_user.id,
                tenant_id         = tenant_id,
                email             = existing_user.email,
                role              = existing_user.role or "merchant",
                created_user      = False,
                created_tenant    = False,
                linked_existing   = True,
                filled_gap        = False,
                set_password_token = None,
            )

        # Integration exists but the user row is missing. This is the
        # exact gap that left ``salla_token_login`` issuing a JWT with
        # user_id=None pre-Phase-1A. Fix by inserting the user now.
        new_user = _insert_user(db, email=canonical_email, tenant_id=tenant_id)
        # Also persist the canonical email on the integration so the next
        # lookup hits the user-by-email path immediately.
        cfg["salla_owner_email"] = canonical_email
        cfg["last_user_filled_gap_at"] = now_iso
        integration.config = cfg
        flag_modified(integration, "config")

        token = issue_set_password_token(
            db, new_user,
            purpose="welcome",
            issued_via=issued_via or f"{provider}_oauth",
        )

        logger.info(
            "[provisioning] filled_gap | provider=%s tenant_id=%s user_id=%s "
            "store_id=%s email=%s",
            provider, tenant_id, new_user.id, store_id_str, canonical_email,
        )
        return ProvisioningResult(
            user_id           = new_user.id,
            tenant_id         = tenant_id,
            email             = new_user.email,
            role              = new_user.role or "merchant",
            created_user      = True,
            created_tenant    = False,
            linked_existing   = False,
            filled_gap        = True,
            set_password_token = token,
        )

    # ─ Branch 3 — email fallback ONLY when no store_id was provided ────────
    if not store_id_str and not is_email_derived:
        existing_user = (
            db.query(User).filter(User.email == owner_email).first()
        )
        if existing_user is not None:
            logger.info(
                "[provisioning] linked_existing_via_email | provider=%s "
                "tenant_id=%s user_id=%s",
                provider, existing_user.tenant_id, existing_user.id,
            )
            return ProvisioningResult(
                user_id           = existing_user.id,
                tenant_id         = existing_user.tenant_id,
                email             = existing_user.email,
                role              = existing_user.role or "merchant",
                created_user      = False,
                created_tenant    = False,
                linked_existing   = True,
                filled_gap        = False,
                set_password_token = None,
            )

    # ─ Branch 4 — first-time install: create Tenant + User ─────────────────
    tenant_name = _build_unique_tenant_name(db, store_name, store_id_str)
    new_tenant = Tenant(name=tenant_name)
    db.add(new_tenant)
    db.flush()

    # If a user with this email already exists in another tenant, derive a
    # store-scoped placeholder email so we don't fail the unique constraint.
    chosen_email = owner_email
    if db.query(User).filter(User.email == owner_email).first() is not None:
        safe_name = "".join(c for c in (store_name or "") if c.isalnum() or c in "-_").lower()[:30]
        suffix    = store_id_str or _secrets.token_hex(6)
        chosen_email = f"{safe_name or 'store'}-{suffix}@{provider}-merchant.nahlah.ai"
        logger.info(
            "[provisioning] email collision — using store-scoped: provider=%s email=%s",
            provider, chosen_email,
        )

    new_user = _insert_user(db, email=chosen_email, tenant_id=new_tenant.id)

    token = issue_set_password_token(
        db, new_user,
        purpose="welcome",
        issued_via=issued_via or f"{provider}_oauth",
    )

    logger.info(
        "[provisioning] created_new | provider=%s tenant_id=%s user_id=%s "
        "store_id=%s email=%s",
        provider, new_tenant.id, new_user.id, store_id_str, chosen_email,
    )
    return ProvisioningResult(
        user_id           = new_user.id,
        tenant_id         = new_tenant.id,
        email             = new_user.email,
        role              = new_user.role or "merchant",
        created_user      = True,
        created_tenant    = True,
        linked_existing   = False,
        filled_gap        = False,
        set_password_token = token,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────
def _insert_user(db: Session, *, email: str, tenant_id: int):
    """Insert a User row with a random unguessable bcrypt hash.

    The random ``password_hash`` is a placeholder ONLY — the merchant
    will set their real password through the welcome-email
    ``/set-password`` link. Until then they CANNOT log in via local
    auth (which is the desired behaviour: OAuth-only access until they
    explicitly opt in to local credentials).
    """
    from models import User  # noqa: PLC0415
    from core.auth import hash_password  # noqa: PLC0415

    user = User(
        username      = (email.split("@", 1)[0] or "merchant")[:64],
        email         = email,
        password_hash = hash_password(_secrets.token_urlsafe(24)),
        role          = "merchant",
        tenant_id     = tenant_id,
        is_active     = True,
    )
    db.add(user)
    db.flush()
    return user


def _build_unique_tenant_name(db: Session, store_name: str, store_id: str) -> str:
    """Pick a tenant name that won't collide with the unique constraint.

    Strategy: ``"{store_name}-{store_id}"`` first (Salla store_ids are
    integers and very unlikely to collide), then append a 6-char random
    hex if even that exists.
    """
    from models import Tenant  # noqa: PLC0415

    base = store_name or "متجر"
    candidate = f"{base}-{store_id}" if store_id else base

    if db.query(Tenant).filter(Tenant.name == candidate).first() is None:
        return candidate
    return f"{candidate}-{_secrets.token_hex(3)}"
