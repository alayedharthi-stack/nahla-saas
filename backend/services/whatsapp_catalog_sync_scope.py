"""
services/whatsapp_catalog_sync_scope.py
───────────────────────────────────────
Explicit write scope for the WhatsApp catalog publish path.

A limited trial must touch only the tenants and products named in
advance. When ``NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS`` is set, every
Graph-writing entry point (publish, retirement, reconnect product sync,
drains, reconcile) refuses any other tenant without changing its state;
``NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS`` narrows it further to listed
product ids (``<tenant>:<product>`` pairs, or bare ids when a single
tenant is in scope). Unset means platform-wide, as before.

The scope is read from the environment on every call so an operator can
widen or clear it without a restart of the stored state.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Set

TENANT_SCOPE_ENV = "NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS"
PRODUCT_SCOPE_ENV = "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"
SCOPE_BLOCKER_CODE = "sync_scope_excluded"


def _ids(raw: str) -> Set[int]:
    out: Set[int] = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def scoped_tenant_ids() -> Optional[Set[int]]:
    """Tenants allowed to write, or None when the scope is not set."""
    raw = os.environ.get(TENANT_SCOPE_ENV, "")
    if not str(raw).strip():
        return None
    return _ids(raw)


def scoped_product_ids() -> Optional[Dict[int, Set[int]]]:
    """``{tenant_id: {product_id, ...}}`` or None when products are not limited."""
    raw = str(os.environ.get(PRODUCT_SCOPE_ENV, "") or "").strip()
    if not raw:
        return None
    tenants = scoped_tenant_ids() or set()
    out: Dict[int, Set[int]] = {}
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            t_raw, p_raw = part.split(":", 1)
            try:
                tid, pid = int(t_raw), int(p_raw)
            except ValueError:
                continue
        else:
            if len(tenants) != 1:
                # A bare product id is only unambiguous with exactly one tenant in scope.
                continue
            try:
                tid, pid = next(iter(tenants)), int(part)
            except ValueError:
                continue
        out.setdefault(tid, set()).add(pid)
    return out


def scope_active() -> bool:
    return scoped_tenant_ids() is not None


def tenant_in_sync_scope(tenant_id: int) -> bool:
    allowed = scoped_tenant_ids()
    if allowed is None:
        return True
    try:
        return int(tenant_id) in allowed
    except (TypeError, ValueError):
        return False


def product_in_sync_scope(tenant_id: int, product_id: Optional[int]) -> bool:
    """True when writes for this product are allowed under the current scope."""
    if not tenant_in_sync_scope(tenant_id):
        return False
    products = scoped_product_ids()
    if products is None:
        return True
    if product_id is None:
        # Identity-only operations (ledger rows of deleted products) need a
        # product id to be matched; without one they stay out of a product-
        # limited trial.
        return False
    try:
        return int(product_id) in products.get(int(tenant_id), set())
    except (TypeError, ValueError):
        return False


def tenant_scope_status(tenant_id: int) -> Dict[str, object]:
    """The write scope as it applies to one tenant, for that tenant's own view.

    Never lists other tenants or their products: a merchant sees whether a
    limited scope is active, whether their store is in it, and, when products
    are limited, only their own allowed product ids.
    """
    tenants = scoped_tenant_ids()
    products = scoped_product_ids()
    try:
        tid = int(tenant_id)
    except (TypeError, ValueError):
        tid = None
    in_scope = tenant_in_sync_scope(tid) if tid is not None else False
    own_products = sorted(products.get(tid, set())) if (products is not None and tid is not None and in_scope) else []
    return {
        "active": tenants is not None,
        "tenant_in_scope": bool(in_scope),
        "products_limited": products is not None,
        "product_ids": own_products,
    }


def scope_description() -> Dict[str, object]:
    """The full write scope, every tenant and product. Operator use only;
    never returned to a merchant (see ``tenant_scope_status``)."""
    tenants = scoped_tenant_ids()
    products = scoped_product_ids()
    return {
        "active": tenants is not None,
        "tenant_ids": sorted(tenants) if tenants else [],
        "product_ids": {str(k): sorted(v) for k, v in (products or {}).items()},
        "tenant_env": TENANT_SCOPE_ENV,
        "product_env": PRODUCT_SCOPE_ENV,
    }


__all__ = [
    "PRODUCT_SCOPE_ENV",
    "SCOPE_BLOCKER_CODE",
    "TENANT_SCOPE_ENV",
    "product_in_sync_scope",
    "scope_active",
    "scope_description",
    "scoped_product_ids",
    "scoped_tenant_ids",
    "tenant_in_sync_scope",
    "tenant_scope_status",
]
