"""OrderFlowV2 checkout reference guard for generic line items."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.order_flow_v2.order_reference import (  # noqa: E402
    persist_checkout_draft_and_resolve_reference,
)


def test_persist_checkout_skips_sync_for_generic_only_prep() -> None:
    conv = SimpleNamespace(id=42, tenant_id=1)
    prep = {
        "line_items": [{"product_name": "منتج", "quantity": 1}],
        "customer_first_name": "أحمد",
    }
    with patch("services.nahla_order_bridge.sync_nahla_wa_order") as sync_mock:
        ref, patch_out = persist_checkout_draft_and_resolve_reference(
            MagicMock(),
            tenant_id=1,
            conversation=conv,
            brain_state={"order_prep": prep},
            order_prep=prep,
        )
    assert ref == ""
    assert patch_out == {}
    sync_mock.assert_not_called()
