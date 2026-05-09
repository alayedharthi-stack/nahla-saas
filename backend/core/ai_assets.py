"""
core/ai_assets.py
─────────────────
Forward-compatible facade over ``core.ai_libraries``.

Today, "AI Assets" == "AI Media Library" + "Manual Coupons". Tomorrow we
will likely add other asset kinds (CTA cards, payment QR, catalogs, offer
banners, dynamic PDFs, voice notes, carousels). Each new kind will get
its own table, but we want callers (the prompt builder, the WhatsApp
webhook, future channel adapters) to depend on a *generic* asset
interface so adding a new kind doesn't ripple through the codebase.

This module is intentionally thin:

* No new tables. The user explicitly asked: "بدون migration الآن".
* No rename of ``ai_media_library``. Public DB schema stays stable.
* Re-exports the existing helpers under generic names so future asset
  kinds can be plugged in by extending the registry below.

When a new asset kind is introduced, add its lister + validator + sender
to the ``_REGISTRY`` and the rest of the pipeline will pick it up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from core import ai_libraries as _lib

__all__ = [
    "AssetKind",
    "AssetDescriptor",
    "list_all_assets_for_prompt",
    "validate_asset_for_send",
    "register_asset_kind",
]


# ── Asset kind registry ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AssetKind:
    """A registered asset family (media, coupon, future kinds…)."""

    name: str
    lister: Callable[..., List[Dict[str, Any]]]
    validator: Optional[Callable[..., Any]] = None


@dataclass(frozen=True)
class AssetDescriptor:
    """A single asset row, kind-tagged for downstream dispatch."""

    kind: str
    item: Dict[str, Any]


# Each asset kind contributes its rows under a stable key. The brain's
# prompt sees them as separate sections; the WhatsApp pipeline only
# dispatches kinds it knows how to ship today (currently: ``media``).
_REGISTRY: Dict[str, AssetKind] = {
    "media": AssetKind(
        name="media",
        lister=_lib.list_active_ai_media,
        validator=_lib.validate_media_for_send,
    ),
    "coupon": AssetKind(
        name="coupon",
        lister=_lib.list_active_manual_coupons,
        validator=None,  # coupons are text-only, no send-time gate needed
    ),
    # Future kinds will register themselves at import time, e.g.:
    # "cta_card":      AssetKind("cta_card",      list_active_cta_cards,      validate_cta_card),
    # "payment_qr":    AssetKind("payment_qr",    list_active_payment_qrs,    validate_payment_qr),
    # "catalog":       AssetKind("catalog",       list_active_catalogs,       validate_catalog),
    # "offer_banner":  AssetKind("offer_banner",  list_active_offer_banners,  None),
    # "voice_note":    AssetKind("voice_note",    list_active_voice_notes,    validate_voice_note),
    # "carousel":      AssetKind("carousel",      list_active_carousels,      validate_carousel),
}


def register_asset_kind(kind: AssetKind) -> None:
    """Plug a new asset family into the runtime. Idempotent."""
    _REGISTRY[kind.name] = kind


# ── Generic API ──────────────────────────────────────────────────────────────


def list_all_assets_for_prompt(
    db: Session,
    tenant_id: int,
    *,
    relevance_query: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return every registered asset family for the prompt builder.

    Result shape: ``{kind_name: [items, …], …}``. Empty lists are kept
    so the caller can rely on stable keys (``"media"`` / ``"coupon"``)
    without having to handle KeyError per call site.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for name, kind in _REGISTRY.items():
        try:
            out[name] = kind.lister(db, tenant_id, relevance_query=relevance_query)
        except TypeError:
            # A future lister might not accept ``relevance_query`` yet;
            # fall back to the positional-only signature gracefully.
            out[name] = kind.lister(db, tenant_id)
        except Exception:  # noqa: BLE001 — never let a bad lister sink the prompt
            out[name] = []
    return out


def validate_asset_for_send(
    kind: str,
    attachment: Dict[str, Any],
    *,
    expected_tenant_id: int,
    db: Optional[Session] = None,
):
    """Dispatch to the kind-specific validator, or accept by default
    when no validator is registered (e.g. coupons are text-only).
    """
    entry = _REGISTRY.get(kind)
    if entry is None or entry.validator is None:
        return True, None, attachment
    return entry.validator(attachment, expected_tenant_id=expected_tenant_id, db=db)
