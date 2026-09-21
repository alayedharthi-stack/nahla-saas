"""
core/webhook_security.py
────────────────────────
Unified webhook signature verification for every external provider Nahla
ingests (Meta WhatsApp Cloud API, Salla Communication app, Salla Sync OAuth
app, Zid, Moyasar, HyperPay).

Phase 1B introduces ONE library that every provider router must call so we
get a single, auditable verification surface and consistent telemetry. The
library returns a ``VerificationResult`` — it does NOT decide whether to
reject the request. That decision belongs to the router (so we can run in
"audit-mode" first: compute the signature, log the result, but still 200
the request) and to per-tenant feature flags.

Threat model
────────────
* Adversary knows the public webhook URL (Meta, Salla, Zid all publish
  them in app dashboards / docs).
* Adversary cannot read traffic between provider and Nahla (TLS-protected).
* Adversary can re-send any captured signed body indefinitely → see the
  separate ``check_replay`` helper, off by default until Phase 1B-5.

Verifiers all use ``hmac.compare_digest`` for constant-time comparison.

Public API
──────────
* ``VerificationResult``       — frozen dataclass returned by every verifier
* ``SignatureStatus``          — enum: VALID / INVALID / MISSING / SECRET_NOT_CONFIGURED
* ``verify_meta_signature``    — Meta ``X-Hub-Signature-256`` over raw body
* ``verify_salla_signature``   — Salla ``X-Salla-Signature`` over raw body
* ``verify_zid_signature``     — Zid ``X-Zid-Signature`` over raw body
* ``verify_moyasar_signature`` — Moyasar ``signature`` / ``x-moyasar-signature``
* ``verify_hyperpay_signature``— HyperPay ``X-Initialization-Vector`` + ``X-Authentication-Tag``
* ``check_replay``             — Redis-backed body-hash dedup (off by default)

NOTE: The library never raises HTTPException itself — that is the router's
job, gated by ``ENFORCE`` flags. Audit-mode handlers should call the
verifier, hand the result to ``core.webhook_audit.record_result``, and
return 200 regardless of status.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("nahla.webhook_security")


class SignatureStatus(str, Enum):
    """Outcome of a single signature verification attempt."""

    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"
    SECRET_NOT_CONFIGURED = "secret_not_configured"


@dataclass(frozen=True)
class VerificationResult:
    """Result of running a verifier — pure data, never raises.

    Routers consult ``status`` plus the per-provider ``ENFORCE`` flag to
    decide whether to reject the request. Always safe to log; ``detail``
    is the short human-readable reason, designed for log ingestion.
    """

    provider: str
    status: SignatureStatus
    detail: str = ""
    header_present: bool = False

    @property
    def is_valid(self) -> bool:
        return self.status == SignatureStatus.VALID

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "detail": self.detail,
            "header_present": self.header_present,
        }


# ── Internal HMAC helpers ─────────────────────────────────────────────────────


def _hmac_sha256_hex(secret: str | bytes, body: bytes) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, body or b"", hashlib.sha256).hexdigest()


def _safe_compare(a: str, b: str) -> bool:
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    try:
        return hmac.compare_digest(a, b)
    except Exception:  # noqa: silent-ok — compare_digest only fails on type mismatch which we treat as invalid
        return False


# ── Provider verifiers ────────────────────────────────────────────────────────


def verify_meta_signature(
    raw_body: bytes,
    header_value: Optional[str],
    *,
    secret: Optional[str],
) -> VerificationResult:
    """Verify Meta WhatsApp Cloud API ``X-Hub-Signature-256``.

    Header format from Meta is ``sha256=<hex>``. Meta computes the HMAC
    over the EXACT raw request body using the App Secret as the key.

    https://developers.facebook.com/docs/messenger-platform/webhooks/#validate-payloads
    """
    if not secret:
        return VerificationResult(
            provider="meta",
            status=SignatureStatus.SECRET_NOT_CONFIGURED,
            detail="META_APP_SECRET is empty",
            header_present=bool(header_value),
        )

    if not header_value:
        return VerificationResult(
            provider="meta",
            status=SignatureStatus.MISSING,
            detail="X-Hub-Signature-256 header missing",
            header_present=False,
        )

    # Meta's header is `sha256=<hex>` — strip the prefix before comparing.
    header = header_value.strip()
    if header.lower().startswith("sha256="):
        header_hex = header[len("sha256="):]
    else:
        header_hex = header

    expected_hex = _hmac_sha256_hex(secret, raw_body)
    if _safe_compare(expected_hex, header_hex):
        return VerificationResult(
            provider="meta",
            status=SignatureStatus.VALID,
            detail="X-Hub-Signature-256 matched META_APP_SECRET",
            header_present=True,
        )

    return VerificationResult(
        provider="meta",
        status=SignatureStatus.INVALID,
        detail="X-Hub-Signature-256 did not match META_APP_SECRET",
        header_present=True,
    )


def verify_salla_signature(
    raw_body: bytes,
    header_value: Optional[str],
    *,
    secret: Optional[str],
    provider_label: str = "salla",
) -> VerificationResult:
    """Verify Salla webhook ``X-Salla-Signature``.

    Uses the same algorithm regardless of which Salla app secret is in
    play (Communication / Sync OAuth) — the caller passes the right
    secret; we just verify it. ``provider_label`` lets callers tag the
    audit row with ``salla`` vs ``salla_oauth``.
    """
    if not secret:
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.SECRET_NOT_CONFIGURED,
            detail="Salla webhook secret is empty",
            header_present=bool(header_value),
        )

    if not header_value:
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.MISSING,
            detail="X-Salla-Signature header missing",
            header_present=False,
        )

    expected_hex = _hmac_sha256_hex(secret, raw_body)
    if _safe_compare(expected_hex, header_value.strip()):
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.VALID,
            detail="X-Salla-Signature matched configured secret",
            header_present=True,
        )

    return VerificationResult(
        provider=provider_label,
        status=SignatureStatus.INVALID,
        detail="X-Salla-Signature did not match configured secret",
        header_present=True,
    )


def verify_zid_signature(
    raw_body: bytes,
    header_value: Optional[str],
    *,
    secret: Optional[str],
) -> VerificationResult:
    """Verify Zid webhook ``X-Zid-Signature``."""
    if not secret:
        return VerificationResult(
            provider="zid",
            status=SignatureStatus.SECRET_NOT_CONFIGURED,
            detail="ZID_WEBHOOK_SECRET is empty",
            header_present=bool(header_value),
        )

    if not header_value:
        return VerificationResult(
            provider="zid",
            status=SignatureStatus.MISSING,
            detail="X-Zid-Signature header missing",
            header_present=False,
        )

    expected_hex = _hmac_sha256_hex(secret, raw_body)
    if _safe_compare(expected_hex, header_value.strip()):
        return VerificationResult(
            provider="zid",
            status=SignatureStatus.VALID,
            detail="X-Zid-Signature matched ZID_WEBHOOK_SECRET",
            header_present=True,
        )

    return VerificationResult(
        provider="zid",
        status=SignatureStatus.INVALID,
        detail="X-Zid-Signature did not match ZID_WEBHOOK_SECRET",
        header_present=True,
    )


def verify_moyasar_signature(
    raw_body: bytes,
    header_value: Optional[str],
    *,
    secret: Optional[str],
    provider_label: str = "moyasar",
) -> VerificationResult:
    """Verify Moyasar webhook signature (HMAC-SHA256 over raw body).

    Moyasar passes the per-tenant secret either in the per-tenant config or
    in ``MOYASAR_WEBHOOK_SECRET``. Caller resolves the right secret first
    and passes it here.
    """
    if not secret:
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.SECRET_NOT_CONFIGURED,
            detail="No Moyasar webhook secret resolved for this request",
            header_present=bool(header_value),
        )

    if not header_value:
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.MISSING,
            detail="Moyasar signature header missing",
            header_present=False,
        )

    expected_hex = _hmac_sha256_hex(secret, raw_body)
    if _safe_compare(expected_hex, header_value.strip()):
        return VerificationResult(
            provider=provider_label,
            status=SignatureStatus.VALID,
            detail="Moyasar signature matched configured secret",
            header_present=True,
        )

    return VerificationResult(
        provider=provider_label,
        status=SignatureStatus.INVALID,
        detail="Moyasar signature did not match configured secret",
        header_present=True,
    )


def verify_hyperpay_signature(
    iv: Optional[bytes],
    raw_body: bytes,
    header_value: Optional[str],
    *,
    secret: Optional[str],
) -> VerificationResult:
    """Verify HyperPay webhook ``X-Authentication-Tag``.

    HyperPay signs ``IV || body`` with the configured webhook secret.
    The IV arrives in ``X-Initialization-Vector`` (hex). When the IV
    header is missing we surface that as ``MISSING`` even if the auth
    tag is present, because they are inseparable.
    """
    if not secret:
        return VerificationResult(
            provider="hyperpay",
            status=SignatureStatus.SECRET_NOT_CONFIGURED,
            detail="HYPERPAY_WEBHOOK_SECRET is empty",
            header_present=bool(header_value),
        )

    if not header_value or not iv:
        return VerificationResult(
            provider="hyperpay",
            status=SignatureStatus.MISSING,
            detail="X-Authentication-Tag or X-Initialization-Vector missing",
            header_present=bool(header_value),
        )

    expected_hex = hmac.new(
        secret.encode("utf-8") if isinstance(secret, str) else secret,
        (iv or b"") + (raw_body or b""),
        hashlib.sha256,
    ).hexdigest()

    if _safe_compare(expected_hex, header_value.strip()):
        return VerificationResult(
            provider="hyperpay",
            status=SignatureStatus.VALID,
            detail="HyperPay auth tag matched HYPERPAY_WEBHOOK_SECRET",
            header_present=True,
        )

    return VerificationResult(
        provider="hyperpay",
        status=SignatureStatus.INVALID,
        detail="HyperPay auth tag did not match HYPERPAY_WEBHOOK_SECRET",
        header_present=True,
    )


# ── Replay protection (opt-in) ────────────────────────────────────────────────


def _body_fingerprint(provider: str, raw_body: bytes) -> str:
    """SHA-256 of provider tag + raw body. Stable across workers."""
    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b"|")
    h.update(raw_body or b"")
    return h.hexdigest()


def check_replay(
    provider: str,
    raw_body: bytes,
    *,
    ttl_seconds: int = 86400,
) -> bool:
    """Return ``True`` iff this body was seen before within ``ttl_seconds``.

    Implementation: Redis ``SET key value NX EX ttl``. First-seen returns
    False (not a replay); duplicate returns True. When Redis is absent
    we conservatively return ``False`` (cannot detect replays without
    shared state). This guarantees we never reject a legitimate webhook
    just because a Redis hiccup forgot the nonce.

    Caller decides what to do with a True result — typically raise a
    409 only when ``WEBHOOK_REPLAY_PROTECTION_ENABLED`` is true.
    """
    from core.redis_client import get_redis  # noqa: PLC0415

    r = get_redis()
    if r is None:
        return False

    key = f"webhook:nonce:{provider}:{_body_fingerprint(provider, raw_body)}"
    try:
        # SET ... NX EX returns truthy on successful set (= first time we
        # see this body) and None on existing key (= replay).
        ok = r.set(key, "1", nx=True, ex=int(max(60, ttl_seconds)))
        if ok:
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — replay protection must never break webhooks
        logger.warning(
            "[webhook_security] redis SET NX failed for replay check (%s): %s — treating as not-replay",
            provider, exc,
        )
        return False


@dataclass(frozen=True)
class ReplayVerdict:
    """Whether to reject, and whether *this* request holds the nonce.

    ``claimed`` is what makes a refusal recoverable. The first request through
    claims the nonce with ``SET NX``; every later copy of the same body finds
    it. If the claimant then fails to accept the work and answers a retryable
    status, the provider's redelivery is the *same* body — and without giving
    the nonce back it would be dropped as a replay, which looks to the provider
    exactly like success. Only the request that holds the claim may release it,
    and it proves it holds it by the exact value it wrote (``claim``).

    A nonce also carries **how far the request that claimed it got**. A claim is
    an acquisition, not an acceptance: a process can die between the two, and a
    claim it left behind must never answer the retry as if the work were done.
    So the value is ``claimed:<token>:<epoch>`` until the route has finished
    deciding, and ``completed:<token>`` after. A later request that finds a
    claim still open within the in-flight lease is a concurrent duplicate and
    is answered retryable (``in_flight``); one that finds a claim older than
    the lease takes it over as a first attempt; only ``completed`` is a replay.
    """

    reject: bool
    claimed: bool = False
    key: str = ""
    token: str = ""
    claim: str = ""
    in_flight: bool = False


# How long a claim may stay open before it is an orphan rather than a request
# still deciding. The route's own work between claiming and completing is a
# signature check, a parse and a handful of database writes; a claim older than
# this belongs to a process that never finished, and the provider's retry may
# take it over. A concurrent duplicate inside the lease is answered retryable
# rather than acknowledged, so the outcome is never decided on its behalf.
REPLAY_IN_FLIGHT_LEASE_SECONDS = 60

_NONCE_CLAIMED = "claimed"
_NONCE_COMPLETED = "completed"

# Compare-and-set and compare-and-delete, atomic on the server. The first line
# of each script names it so a test double can recognise which one it is asked
# to run without interpreting Lua.
_NONCE_CAS_SET = """-- nahla:nonce_cas_set
local current = redis.call('GET', KEYS[1])
if current ~= ARGV[1] then return 0 end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = tonumber(ARGV[3]) end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
return 1"""
_NONCE_CAS_DEL = """-- nahla:nonce_cas_del
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0"""


def _claim_value(token: str, moment: Optional[float] = None) -> str:
    import time as _time  # noqa: PLC0415

    return f"{_NONCE_CLAIMED}:{token}:{int(moment if moment is not None else _time.time())}"


def _completed_value(token: str) -> str:
    return f"{_NONCE_COMPLETED}:{token}"


def _decoded(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _claim_age(value: str, now: float) -> Optional[float]:
    """Seconds since an open claim was written, or ``None`` for anything else.

    Only ``claimed:<token>:<epoch>`` is an open claim. A completed marker is a
    replay. Anything else — the ``"1"`` an earlier deployment wrote when a
    request *took* the nonce, or a value this code cannot read — is an
    **ambiguous acquisition**: see :func:`_is_completed` and the takeover in
    :func:`evaluate_replay_claim`.
    """
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != _NONCE_CLAIMED:
        return None
    try:
        return max(0.0, now - float(int(parts[2])))
    except ValueError:
        return None


def _is_completed(value: str) -> bool:
    """Whether the nonce says a request with this body finished deciding."""
    return value.startswith(f"{_NONCE_COMPLETED}:")


def _cas(client: Any, script: str, key: str, *args: Any) -> bool:
    try:
        return bool(int(client.eval(script, 1, key, *args) or 0))
    except Exception as exc:  # noqa: BLE001 — the TTL is the backstop
        logger.warning("[webhook_security] nonce compare-and-set failed: %s", exc)
        return False


def replay_nonce_key(provider: str, raw_body: bytes) -> str:
    """The exact key ``check_replay`` uses. One definition, two callers."""
    return f"webhook:nonce:{provider}:{_body_fingerprint(provider, raw_body)}"


def evaluate_replay_claim(
    provider: str,
    raw_body: bytes,
    *,
    tenant_id: Optional[int] = None,
    request_meta: Optional[dict] = None,
    ttl_seconds: int = 86400,
) -> ReplayVerdict:
    """:func:`evaluate_replay`, plus whether this request claimed the nonce.

    Same protection, same flags, same audit row. The difference is that the
    caller can hand the nonce back with :func:`release_replay_nonce` when it
    turns out it could not accept the work.
    """
    from core.config import (  # noqa: PLC0415
        WEBHOOK_REPLAY_PROTECTION_ENABLED,
        WEBHOOK_REPLAY_REJECT_ENABLED,
    )
    if not WEBHOOK_REPLAY_PROTECTION_ENABLED:
        return ReplayVerdict(reject=False, claimed=False)

    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    from core.redis_client import get_redis  # noqa: PLC0415

    key = replay_nonce_key(provider, raw_body)
    client = get_redis()
    if client is None:
        # No shared state: nothing can be claimed and nothing is rejected, the
        # same as ``check_replay``. There is nothing to release either.
        return ReplayVerdict(reject=False, claimed=False, key=key)

    ttl = int(max(60, ttl_seconds))
    token = _uuid.uuid4().hex
    claim = _claim_value(token)
    try:
        if client.set(key, claim, nx=True, ex=ttl):
            return ReplayVerdict(reject=False, claimed=True, key=key, token=token, claim=claim)
        current = _decoded(client.get(key))
    except Exception as exc:  # noqa: BLE001 — replay protection must never break webhooks
        logger.warning("[webhook_security] redis nonce claim failed for %s: %s — treating as "
                       "not-replay", provider, exc)
        return ReplayVerdict(reject=False, claimed=False, key=key)

    reject = bool(WEBHOOK_REPLAY_REJECT_ENABLED)
    age = _claim_age(current, _time.time())
    orphaned = age is not None and age > REPLAY_IN_FLIGHT_LEASE_SECONDS
    # A value that is neither an open claim nor a completed marker was written
    # by the earlier code (``"1"``) — or by nothing this code can read. It says
    # a request *took* the nonce; it cannot say whether that request lived
    # long enough to make anything durable. Reading it as "completed" turned a
    # crash between claim and record into a ``200 replay`` for a message
    # nothing held, once the pilot's durable check was off. So it is an
    # ambiguous acquisition and is taken over exactly like an orphaned claim:
    # the request is a first attempt, and the durable records and the dedup
    # boundaries decide, per message, what was already done.
    ambiguous = age is None and not _is_completed(current)
    if orphaned or ambiguous:
        # This request takes the nonce over, exactly once — the compare-and-set
        # fails for a second retry racing it, which is then a concurrent
        # duplicate of *this* one and is answered in flight.
        if _cas(client, _NONCE_CAS_SET, key, current, claim, ttl):
            if orphaned:
                logger.warning("[webhook_security] orphaned replay nonce taken over provider=%s "
                               "age=%ss — treating the request as a first attempt", provider,
                               int(age))
            else:
                logger.warning("[webhook_security] replay nonce written by an earlier "
                               "deployment taken over provider=%s value=%r — it cannot say how "
                               "far that request got; treating the request as a first attempt",
                               provider, current[:32])
            return ReplayVerdict(reject=False, claimed=True, key=key, token=token, claim=claim)
        age = 0.0

    try:
        from core.webhook_audit import record_replay  # noqa: PLC0415
        record_replay(provider, tenant_id=tenant_id, request_meta=request_meta or {})
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning("[webhook_security] replay audit record failed: %s", exc)

    if age is not None:
        # A claim still open inside the lease: another request with this body
        # is deciding right now. Its outcome is not known yet, so nothing is
        # acknowledged on its behalf — the copy is answered retryable.
        return ReplayVerdict(reject=reject, claimed=False, key=key, in_flight=True)
    return ReplayVerdict(reject=reject, claimed=False, key=key)


def mark_replay_completed(verdict: ReplayVerdict) -> bool:
    """Record that the request holding this claim has finished deciding.

    Called once the route has made its acknowledgement durable — the pilot's
    obligations recorded, processing scheduled — so that from here on a copy of
    the body is a replay and nothing else. Only the holder of the claim may
    mark it; a claim that was taken over in the meantime is left to its new
    holder. Never raises: an unmarked claim ages into an orphan, which delays a
    retry rather than losing one.
    """
    if not verdict.claimed or not verdict.key or not verdict.claim:
        return False
    try:
        from core.redis_client import get_redis  # noqa: PLC0415

        client = get_redis()
        if client is None:
            return False
        return _cas(client, _NONCE_CAS_SET, verdict.key, verdict.claim,
                    _completed_value(verdict.token), 86400)
    except Exception as exc:  # noqa: BLE001 — the TTL is the backstop
        logger.warning("[webhook_security] could not mark replay nonce completed: %s", exc)
        return False


def release_replay_nonce(verdict: ReplayVerdict) -> bool:
    """Give back a nonce this request claimed but could not accept.

    Called on exactly one path: the request is answered with a retryable status
    because the work was not made durable. Giving the nonce back is what keeps
    the provider's identical retry a real retry instead of a silent drop.

    A verdict that did not claim anything releases nothing — a concurrent
    duplicate must still be a duplicate. Never raises: a nonce that cannot be
    released expires on its own TTL, which delays a retry rather than losing it.
    """
    if not verdict.claimed or not verdict.key or not verdict.claim:
        return False
    try:
        from core.redis_client import get_redis  # noqa: PLC0415

        client = get_redis()
        if client is None:
            return False
        # Compare-and-delete: the key goes only if it still holds *this*
        # request's claim. A claim another request took over, or a completed
        # marker, is never deleted from under its owner.
        return _cas(client, _NONCE_CAS_DEL, verdict.key, verdict.claim)
    except Exception as exc:  # noqa: BLE001 — the TTL is the backstop
        logger.warning("[webhook_security] could not release replay nonce: %s", exc)
        return False


def evaluate_replay(
    provider: str,
    raw_body: bytes,
    *,
    tenant_id: Optional[int] = None,
    request_meta: Optional[dict] = None,
    ttl_seconds: int = 86400,
) -> bool:
    """Phase 1B-5 entry point: combine flag check + replay detection +
    audit recording, return whether the caller should REJECT.

    Behaviour:
      * ``WEBHOOK_REPLAY_PROTECTION_ENABLED=false`` → returns ``False``,
        no Redis lookup, no audit row.
      * ``WEBHOOK_REPLAY_PROTECTION_ENABLED=true``  → runs ``check_replay``;
        on hit, always records an audit "replay" event so the dashboard
        can chart legitimate retry rate. Returns ``True`` only when the
        SECOND flag ``WEBHOOK_REPLAY_REJECT_ENABLED`` is also true.

    Always safe to call — never raises. Routers do::

        if evaluate_replay("salla", raw_body, tenant_id=tid, request_meta=meta):
            return JSONResponse({"status": "ignored", "reason": "replay"}, status_code=200)
    """
    # Local import keeps the module free of FastAPI / config import cycles
    # when this library is unit-tested in isolation.
    from core.config import (  # noqa: PLC0415
        WEBHOOK_REPLAY_PROTECTION_ENABLED,
        WEBHOOK_REPLAY_REJECT_ENABLED,
    )
    if not WEBHOOK_REPLAY_PROTECTION_ENABLED:
        return False

    if not check_replay(provider, raw_body, ttl_seconds=ttl_seconds):
        return False

    try:
        from core.webhook_audit import record_replay  # noqa: PLC0415
        record_replay(provider, tenant_id=tenant_id, request_meta=request_meta or {})
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning("[webhook_security] replay audit record failed: %s", exc)

    return bool(WEBHOOK_REPLAY_REJECT_ENABLED)
