"""Keep the publication owner alive until its Graph outcome is durable.

Call with a freshly loaded Product held FOR UPDATE, and keep that lock through
the retirement-ledger snapshot and delete. The publisher takes the same lock
before acquiring its lease. A source deletion raises into the existing durable
webhook retry queue; a manual deletion returns a retryable conflict.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable

from core.meta_catalog_membership import PUBLICATION_PROVENANCES
from services.meta_catalog_push import PENDING_PUBLICATIONS_KEY


class CatalogDeletionDeferred(RuntimeError):
    """Publication is still in flight or its successful POST is uncorroborated."""

    code = "catalog_deletion_deferred"
    retry_after_seconds = 60

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}:{reason}")


def assert_catalog_deletion_safe(
    product: Any, publication_identities: Iterable[Dict[str, Any]],
) -> None:
    """Refuse to erase a live lease or unresolved successful-create evidence.

    An expired lease is reclaimable by the publisher, but does not prove that
    its Graph request stopped. Never age out this guard or the pending records.
    A corroborated pending record may remain after a batch push: allow it only
    when the exact product/catalog/retailer/item is in the retirement snapshot.
    Pending metadata itself never grants permission to write to Graph.
    """
    if str(getattr(product, "sync_status", "") or "").strip().lower() == "syncing":
        raise CatalogDeletionDeferred("publication_in_flight")

    meta = getattr(product, "extra_metadata", None)
    sync_meta = meta.get("sync_meta") if isinstance(meta, dict) else None
    pending = sync_meta.get(PENDING_PUBLICATIONS_KEY) if isinstance(sync_meta, dict) else None
    if not pending:
        return
    if not isinstance(pending, dict):
        raise CatalogDeletionDeferred("publication_unresolved")

    def identity_key(record: Any) -> tuple | None:
        if not isinstance(record, dict):
            return None
        try:
            pid = int(record.get("product_id") or 0)
        except (TypeError, ValueError):
            return None
        parts = tuple(str(record.get(k) or "").strip() for k in (
            "catalog_id", "retailer_id", "meta_item_id",
        ))
        if pid != int(product.id) or not all(parts):
            return None
        return (pid, *parts)

    proven = {
        identity_key(identity)
        for identity in publication_identities
        if identity.get("publication_provenance") in PUBLICATION_PROVENANCES
    } - {None}
    if any(identity_key(record) not in proven for record in pending.values()):
        raise CatalogDeletionDeferred("publication_unresolved")
