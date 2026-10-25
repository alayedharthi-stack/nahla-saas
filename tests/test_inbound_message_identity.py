"""A customer resend is a new turn; only provider identity establishes retries."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.ai.brain.commerce.inbound_fragment_guard import (
    evaluate_duplicate_fragment_turn,
    reset_fragment_cache_for_tests,
)
from backend.tests.test_trusted_context_shadow_wireup import (
    _merchant_handler_convo,
    _merchant_handler_db,
    _merchant_handler_patch_ctx,
)


@pytest.fixture(autouse=True)
def _clear_legacy_fragment_cache():
    reset_fragment_cache_for_tests()
    yield
    reset_fragment_cache_for_tests()


@pytest.mark.parametrize("inbound", ["وش منتجاتكم ؟", "Do you carry running shoes?"])
@pytest.mark.parametrize("first_delivery_succeeded", [False, True])
def test_customer_resend_reaches_brain(inbound, first_delivery_succeeded):
    from routers.whatsapp_webhook import _handle_merchant_message

    convo = _merchant_handler_convo()
    db = _merchant_handler_db()
    send = AsyncMock(side_effect=[first_delivery_succeeded, True, True])
    # Distinct model candidates make this a pre-Brain ownership test, independent
    # of outbound similarity handling. Network, persistence and Brain are seams;
    # the actual merchant handler and fragment evaluator execute unchanged.
    replies = [
        "We have clothing in our catalog.",
        "Our running shoes are available.",
        "The catalog includes fragrances too.",
    ]
    with _merchant_handler_patch_ctx(
        convo=convo, shadow_mock=MagicMock(return_value=None), whatsapp_send_mock=send,
    ) as (brain, _state), patch(
        "modules.ai.brain.commerce.inbound_fragment_guard.evaluate_duplicate_fragment_turn",
        new=evaluate_duplicate_fragment_turn,
    ):
        brain.return_value.process = AsyncMock(side_effect=[
            {"reply": reply, "buttons": []} for reply in replies
        ])
        for mid in ("wamid.customer.first", "wamid.customer.resend", "wamid.customer.third"):
            asyncio.run(_handle_merchant_message(
                phone_id="PH1", to="966500000099", tenant_id=1,
                db=db, text=inbound, wa_msg_id=mid,
            ))

    assert brain.return_value.process.await_count == 3
    assert [call.kwargs["message"] for call in brain.return_value.process.await_args_list] == [inbound] * 3
    assert [call.kwargs["text"] for call in send.await_args_list] == replies
