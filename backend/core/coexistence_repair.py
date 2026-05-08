"""One-off and startup repairs for coexistence integration JSON blobs."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.coexistence_client_id import sanitize_coexistence_client_id

logger = logging.getLogger("nahla-backend")


def repair_coexistence_placeholder_client_ids(db: Session) -> int:
    """NULL out bogus ``provider_details.client_id`` on coexistence rows.

    Returns number of rows mutated."""
    # Local import avoids circular imports at app bootstrap.
    from database.models import WhatsAppConnection  # noqa: PLC0415

    touched = 0
    q = db.query(WhatsAppConnection).filter(WhatsAppConnection.connection_type == "coexistence")
    for conn in q.all():
        meta = dict(conn.extra_metadata or {})
        pd = dict(meta.get("provider_details") or {})
        raw = pd.get("client_id")
        if raw is None:
            continue
        if sanitize_coexistence_client_id(raw) is None:
            pd["client_id"] = None
            meta["provider_details"] = pd
            conn.extra_metadata = meta
            flag_modified(conn, "extra_metadata")
            touched += 1
    if touched:
        db.commit()
        logger.warning("[coexistence_repair] cleared bogus client_id on %s whatsapp_connection row(s)", touched)
    return touched
