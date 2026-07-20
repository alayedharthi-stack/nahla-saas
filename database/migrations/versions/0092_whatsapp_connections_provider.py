"""0092 — add whatsapp_connections.provider (A1-Validate branch).

Sibling to ``0093`` on the ``0089`` integration-bootstrap branch. Both apply the
same bounded ``String`` / non-null / ``meta`` default contract for
``WhatsAppConnection.provider``.

Forward-only column add/backfill. Downgrade is intentionally a schema no-op
because sibling ``0093`` owns the same physical column contract and may remain
applied. Dropping or narrowing either sibling could violate the other revision
or destroy legitimate provider values.
"""
from __future__ import annotations

from whatsapp_connections_provider_helpers import ensure_whatsapp_connections_provider_column

revision = "0092"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_whatsapp_connections_provider_column()


def downgrade() -> None:
    # Forward-only shared schema contract. Never drop provider:
    # sibling 0093 may still be present and rows may rely on the column.
    pass
