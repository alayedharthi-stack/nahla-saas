"""
backend/services/media_resolver.py
──────────────────────────────────
Stable namespaced-key resolver for the AI media library.

This module is the *only* place that knows how to translate a
canonical key (e.g. ``payment_rajhi_barcode``) into a concrete
``AIMediaItem`` row for a given tenant. Two callers use it:

  1. **The chat marker extractor**
     When Claude emits ``[MEDIA_KEY:payment_rajhi_barcode]`` in its
     reply, ``extract_media_key_markers`` finds the matching row
     and returns the attachment payload the WhatsApp webhook
     dispatches via ``_send_media_message``.

  2. **The deterministic post-LLM safety net**
     When the customer's message clearly names a payment method
     ("أرسل لي باركود الراجحي") but Claude failed to emit a
     marker, ``resolve_for_query`` runs the registry's
     ``find_key_for_query`` heuristic and tries to find the
     asset anyway — mirroring the existing
     ``find_best_payment_asset`` pattern in ``core/ai_libraries.py``.

Why a new module instead of stuffing this into ai_libraries.py?
───────────────────────────────────────────────────────────────
``ai_libraries.py`` already does **id-based** resolution
(``[MEDIA:42]``), relevance ranking, prompt formatting, validation
— it's the right place for those concerns. The new **key-based**
contract is a separate concern with its own data model
(``media_key`` column + registry) and its own LLM marker syntax.
Keeping them apart makes the resolver trivially testable in
isolation and lets us deprecate either side later without
disturbing the other.

Resolution contract
───────────────────
* ``resolve_by_key(db, tenant_id, key)`` — exact match on
  ``media_key``. Returns ``None`` when the merchant hasn't
  uploaded an asset for this key yet. The caller then decides
  whether to fall back to the registry's ``fallback_text``.

* ``resolve_for_query(db, tenant_id, query)`` — best-effort:
  registry trigger matching → ``resolve_by_key``. Returns
  ``(resolution, key_hit)`` so the caller can log the inferred
  key even when no asset exists.

* ``available_keys_for_tenant(db, tenant_id)`` — the list of
  keys for which this tenant has at least one active asset.
  Used to scope the prompt-side block of available keys so the
  LLM doesn't emit markers for assets that don't exist.

All three are tenant-scoped and ``is_active``-aware. Inactive
rows are invisible to the resolver, even by exact key — the
merchant's "off switch" must be honoured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services import media_key_registry as registry

logger = logging.getLogger("nahla.media_resolver")


# ──────────────────────────────────────────────────────────────────
# Resolution DTO
# ──────────────────────────────────────────────────────────────────


@dataclass
class MediaResolution:
    """Everything the WhatsApp sender needs to dispatch one media
    attachment.

    Mirrors the shape ``extract_media_markers`` already returns in
    ``core/ai_libraries.py`` so the webhook can iterate over a
    homogeneous list of attachments regardless of marker source
    (``[MEDIA:<id>]`` or ``[MEDIA_KEY:<key>]``).
    """
    id: int
    tenant_id: int
    media_key: Optional[str]
    title: str
    media_type: str
    file_url: str
    mime_type: Optional[str]
    storage_kind: Optional[str]
    storage_path: Optional[str]
    file_size_bytes: Optional[int]
    # The registry entry that *requested* this asset, if any.
    # Surfaced so logs + analytics can answer "how often did
    # `payment_rajhi_barcode` resolve vs fall back to text?"
    requested_key: Optional[str] = None
    # The deterministic ``fallback_text`` from the registry — only
    # populated when the caller asks for it. The webhook uses this
    # field when no asset exists for the requested key.
    fallback_text: Optional[str] = None

    def to_attachment(self) -> Dict[str, Any]:
        """Convert to the same dict shape ``extract_media_markers``
        emits (so the webhook's attachment loop doesn't need a
        branch for marker source)."""
        out = asdict(self)
        # ``fallback_text`` + ``requested_key`` are diagnostic
        # metadata — strip from the attachment dict so the webhook
        # send path doesn't accidentally try to use them.
        out.pop("fallback_text", None)
        out.pop("requested_key", None)
        return out


# ──────────────────────────────────────────────────────────────────
# Exact key lookup
# ──────────────────────────────────────────────────────────────────


def resolve_by_key(
    db: Session,
    tenant_id: int,
    key: str,
    *,
    include_fallback_text: bool = False,
) -> Optional[MediaResolution]:
    """Find the active media row whose ``media_key`` matches.

    Returns ``None`` when:
      * the merchant never uploaded an asset for this key,
      * the only row exists but is ``is_active=False``,
      * the tenant_id doesn't match (tenant isolation — never
        return a row from another tenant even if the key exists
        elsewhere).

    ``include_fallback_text=True`` populates ``fallback_text``
    from the registry, so the webhook can use that as the
    "soft failure" message when no asset is found.
    """
    if not key:
        return None
    key = key.strip().lower()
    if not key:
        return None

    # Lazy import — models.py is heavy and imports SQLAlchemy /
    # JSONB stuff that we don't want at module-import time when
    # this module is used from the test harness.
    from models import AIMediaItem  # noqa: PLC0415

    row = (
        db.query(AIMediaItem)
        .filter(
            AIMediaItem.tenant_id == tenant_id,
            AIMediaItem.media_key == key,
            AIMediaItem.is_active.is_(True),
        )
        .order_by(AIMediaItem.priority.asc(), AIMediaItem.id.desc())
        .first()
    )
    if row is None:
        if include_fallback_text:
            mk = registry.get(key)
            if mk and mk.fallback_text:
                logger.info(
                    "media_resolver | tenant=%s key=%s no_asset_using_fallback",
                    tenant_id, key,
                )
        return None

    res = MediaResolution(
        id=int(row.id),
        tenant_id=int(row.tenant_id),
        media_key=row.media_key,
        title=row.title,
        media_type=row.media_type,
        file_url=row.file_url,
        mime_type=row.mime_type,
        storage_kind=row.storage_kind,
        storage_path=row.storage_path,
        file_size_bytes=(
            int(row.file_size_bytes) if row.file_size_bytes is not None else None
        ),
        requested_key=key,
    )
    if include_fallback_text:
        mk = registry.get(key)
        if mk:
            res.fallback_text = mk.fallback_text
    return res


# ──────────────────────────────────────────────────────────────────
# Heuristic lookup (post-LLM safety net)
# ──────────────────────────────────────────────────────────────────


def resolve_for_query(
    db: Session,
    tenant_id: int,
    query: str,
) -> Tuple[Optional[MediaResolution], Optional[str]]:
    """Best-effort: pick the registry key whose triggers match
    ``query``, then resolve it.

    Returns ``(resolution, inferred_key)``:
      * ``(MediaResolution, "payment_rajhi_barcode")`` — happy path,
        the customer mentioned a specific bank trigger AND the
        merchant has the asset uploaded.
      * ``(None,            "payment_rajhi_barcode")`` — key was
        inferred but the merchant hasn't uploaded that asset.
        The caller can use ``registry.get(key).fallback_text``.
      * ``(MediaResolution, "payment_rajhi_barcode")`` via the
        generic-noun fallback — the customer typed a BARE generic
        noun ("QR" / "باركود" / "رمز الدفع") without naming a
        bank AND the merchant uploaded exactly ONE payment
        barcode for this tenant. See
        :func:`resolve_generic_payment_barcode` for the rules.
      * ``(None,            None)`` — nothing matched at all.

    The deliberate three-way return lets the conversation pipeline
    tell apart "we don't know what the customer wants" from "we
    know but the asset is missing" — two very different log /
    UX paths.
    """
    inferred = registry.find_key_for_query(query)
    if inferred:
        res = resolve_by_key(db, tenant_id, inferred, include_fallback_text=True)
        return res, inferred

    # Generic-noun fallback (May 2026 #21) — runs ONLY when no
    # specific bank trigger matched. The fallback is tenant-aware
    # (single-asset disambiguation) so it stays here in the
    # resolver layer; the registry helpers remain pure.
    return resolve_generic_payment_barcode(db, tenant_id, query)


# ──────────────────────────────────────────────────────────────────
# Generic payment-barcode fallback (tenant-aware)
# ──────────────────────────────────────────────────────────────────


# Media-key prefixes that count as a "payment barcode the merchant
# would attach when the customer asks for the QR/code". Keep in
# lock-step with the payment family in
# :mod:`services.media_key_registry.REGISTRY`. A new payment-rail
# slug (e.g. ``payment_urpay_qr``) is automatically picked up by
# the prefix match — no edit needed here.
_PAYMENT_KEY_LIKE_PATTERNS: Tuple[str, ...] = (
    "payment_%_barcode",
    "payment_%_qr",
)


def resolve_generic_payment_barcode(
    db: Session,
    tenant_id: int,
    query: str,
) -> Tuple[Optional[MediaResolution], Optional[str]]:
    """Fallback for "the customer said 'باركود' without naming a bank".

    The customer message must:
      * mention one of the generic payment-barcode nouns
        (``QR`` / ``باركود`` / ``كيو آر`` / ``رمز الدفع`` / ...),
      * NOT mention a specific bank trigger.

    AND the tenant must have **exactly one** active media row with
    a ``media_key`` in the ``payment_*_barcode`` / ``payment_*_qr``
    family. When both conditions hold we attach that single asset.
    When the tenant has zero or two+ payment barcodes uploaded we
    bail — the LLM / safety net can then ship a clarifying line
    instead of guessing the wrong bank.

    The same ``(resolution, key)`` shape as
    :func:`resolve_for_query` so callers don't need to branch.
    """
    if not registry.is_generic_payment_barcode_query(query):
        return None, None

    # Lazy import — see note in ``resolve_by_key``. Models.py is
    # heavy and we don't want it loaded at module-import time when
    # this helper runs in test harnesses.
    from models import AIMediaItem  # noqa: PLC0415
    from sqlalchemy import or_  # noqa: PLC0415

    q = (
        db.query(AIMediaItem)
        .filter(
            AIMediaItem.tenant_id == tenant_id,
            AIMediaItem.is_active.is_(True),
            or_(
                AIMediaItem.media_key.like("payment_%_barcode"),
                AIMediaItem.media_key.like("payment_%_qr"),
            ),
        )
        .order_by(AIMediaItem.id.asc())
    )
    rows = q.limit(2).all()  # we only need to know if it's 0, 1, or >=2
    if len(rows) != 1:
        if rows:
            logger.info(
                "media_resolver | tenant=%s generic_payment_fallback "
                "ambiguous active_barcodes>=2 — bailing",
                tenant_id,
            )
        return None, None

    row = rows[0]
    res = MediaResolution(
        id=int(row.id),
        tenant_id=int(row.tenant_id),
        media_key=row.media_key,
        title=row.title,
        media_type=row.media_type,
        file_url=row.file_url,
        mime_type=row.mime_type,
        storage_kind=row.storage_kind,
        storage_path=row.storage_path,
        file_size_bytes=(
            int(row.file_size_bytes) if row.file_size_bytes is not None else None
        ),
        requested_key=row.media_key,
    )
    mk = registry.get(row.media_key or "")
    if mk:
        res.fallback_text = mk.fallback_text
    logger.info(
        "media_resolver | tenant=%s generic_payment_fallback "
        "fired media_key=%s media_id=%s",
        tenant_id, row.media_key, row.id,
    )
    return res, row.media_key


# ──────────────────────────────────────────────────────────────────
# Marker extraction — ``[MEDIA_KEY:<slug>]`` in chat replies
# ──────────────────────────────────────────────────────────────────

import re as _re

# Mirror the ``[MEDIA:<id>]`` regex style in ``core/ai_libraries.py``.
# Allow ASCII letters / digits / underscore / hyphen in the slug
# (matches the registry slugs) and an optional ``|hint`` suffix
# the LLM may append for clarity (ignored by the parser).
_MEDIA_KEY_MARKER_RE = _re.compile(
    r"\[MEDIA_KEY:\s*([a-zA-Z0-9_\-]{1,64})(?:\s*\|[^\]]*)?\]",
    _re.IGNORECASE,
)


def extract_media_key_markers(
    db: Session,
    tenant_id: int,
    reply_text: str,
    *,
    max_attachments: int = 2,
) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """Strip ``[MEDIA_KEY:<slug>]`` tokens from ``reply_text``.

    Returns ``(cleaned_text, attachments, missing_keys)``:

      * ``cleaned_text``  — the same string with every marker
        removed and stranded blank lines collapsed.
      * ``attachments``   — same dict shape as
        ``core/ai_libraries.extract_media_markers`` so the
        webhook's existing attachment loop handles both flavours.
      * ``missing_keys``  — keys the LLM emitted that have NO
        active asset for this tenant. The caller can append the
        registry's ``fallback_text`` to ``cleaned_text`` so the
        conversation stays useful even without the media.

    Order: attachments are returned in the order the LLM emitted
    them. Duplicates within the reply are deduped (same key cited
    twice ships the file once). Cap at ``max_attachments`` matches
    the legacy ``[MEDIA:<id>]`` extractor — protects the customer
    from a hallucination that emits dozens of markers.
    """
    text = reply_text or ""
    if not text or "[MEDIA_KEY:" not in text.upper():
        return text, [], []

    matches = list(_MEDIA_KEY_MARKER_RE.finditer(text))
    if not matches:
        return text, [], []

    seen_keys: List[str] = []
    for m in matches:
        key = (m.group(1) or "").strip().lower()
        if not key:
            continue
        if key not in seen_keys:
            seen_keys.append(key)
        if len(seen_keys) >= max_attachments:
            break

    attachments: List[Dict[str, Any]] = []
    missing: List[str] = []
    for key in seen_keys:
        res = resolve_by_key(db, tenant_id, key, include_fallback_text=True)
        if res is None:
            missing.append(key)
            logger.info(
                "media_resolver | tenant=%s key=%s marker_dropped reason=no_active_asset",
                tenant_id, key,
            )
            continue
        attachments.append(res.to_attachment())

    # Strip every marker from the text (whether it resolved or not
    # — the customer should never see ``[MEDIA_KEY:...]`` verbatim).
    cleaned = _MEDIA_KEY_MARKER_RE.sub("", text)
    # Collapse runs of blank lines that the removal opened up.
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return cleaned, attachments, missing


# ──────────────────────────────────────────────────────────────────
# Prompt-side: list available keys for this tenant
# ──────────────────────────────────────────────────────────────────


def available_keys_for_tenant(
    db: Session, tenant_id: int,
) -> List[str]:
    """Return every registry key for which this tenant has at
    least one **active** asset uploaded.

    Used by the prompt builder to inject only the keys the LLM
    can actually use — emitting ``[MEDIA_KEY:payment_alahli_barcode]``
    when the merchant only uploaded the Rajhi barcode would
    silently fail.
    """
    from models import AIMediaItem  # noqa: PLC0415

    rows = (
        db.query(AIMediaItem.media_key)
        .filter(
            AIMediaItem.tenant_id == tenant_id,
            AIMediaItem.is_active.is_(True),
            AIMediaItem.media_key.isnot(None),
        )
        .distinct()
        .all()
    )
    keys = [r[0] for r in rows if r[0]]
    # Restrict to registry-known keys. Anything else is junk a
    # merchant typed by hand and we won't surface it to the LLM.
    return [k for k in keys if registry.is_valid_key(k)]


__all__ = [
    "MediaResolution",
    "resolve_by_key",
    "resolve_for_query",
    "resolve_generic_payment_barcode",
    "extract_media_key_markers",
    "available_keys_for_tenant",
]
