"""0093 — add whatsapp_connections.provider (integration-bootstrap branch).

Sibling to ``0092`` on the ``0088`` A1-Validate branch. Normal bootstrap pins to
this revision so integration environments pick up the column without ``head``.

Forward-only column add/backfill. Downgrade is intentionally a schema no-op
because sibling ``0092`` owns the same physical column contract and may remain
applied. Dropping or narrowing either sibling could violate the other revision
or destroy legitimate provider values.
"""
from __future__ import annotations

from whatsapp_connections_provider_helpers import ensure_whatsapp_connections_provider_column

revision = "0093"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_whatsapp_connections_provider_column()


def downgrade() -> None:
    # Forward-only shared schema contract. Never drop provider:
    # sibling 0092 may still be present and rows may rely on the column.
    pass
