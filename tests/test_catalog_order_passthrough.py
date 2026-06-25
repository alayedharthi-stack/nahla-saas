"""
tests/test_catalog_order_passthrough.py
───────────────────────────────────────
Locks the contract that WhatsApp catalog-order messages
(``type="order"``) do NOT get dropped — the normalizer unpacks
the order metadata into a brain-facing Arabic text so the bot
treats the catalog submission as a buying intent and asks for
whatever is missing (name / city / address / payment).

Production reproducer (June 2026): merchant screenshot showed a
customer's WhatsApp catalog order (header "طلب عبر الكتالوج",
"عنصر 1", "SAR 69.00") followed by silence from the bot. Before
this fix the inbound webhook fell through to the
``INBOUND_IGNORED_UNSUPPORTED`` branch because the normalizer
didn't recognise ``msg_type="order"``.

Surgical-fix scope (per merchant):
  * No new intent layer.
  * No new outbound templates / canned replies.
  * No dependency on Meta catalog import approval.
  * Just transform available metadata into a structured text on
    the standard ``normalized_type="text"`` path.

Invariants under test
─────────────────────
1.  ``msg_type="order"`` returns ``should_process=True`` (the
    webhook router's allow-list lets it through).
2.  ``normalized_type="text"`` so the message rides the standard
    text path — no router-side changes needed.
3.  The brain-facing text begins with ``[طلب كتالوج من العميل]``
    and contains item count, total + currency, SKU, and the
    Arabic framing line that tells the LLM to treat the message
    as a buying intent.
4.  ``metadata["source_type"]="catalog_order"`` is preserved for
    telemetry; raw ``product_items`` echoed for audit.
5.  Edge cases: missing prices, multiple items, customer note on
    the order block — all handled without raising.
6.  ``[CATALOG_MESSAGE_TRACE]`` log line emitted with the agreed
    fixed-shape fields and ``final_route=brain``.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _run(coro):
    """Run an async helper in a fresh event loop.

    Using ``asyncio.run`` (or an explicit fresh loop) rather than
    ``asyncio.get_event_loop`` keeps the suite robust to test
    pollution — earlier media tests in the same pytest session may
    have closed the default loop, which would otherwise make every
    catalog test raise ``RuntimeError: Event loop is closed``."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _normalize(message: Dict[str, Any]):
    from modules.ai.media.normalizer import normalize_whatsapp_inbound
    return _run(
        normalize_whatsapp_inbound(
            db=MagicMock(),
            wa_conn=MagicMock(),
            tenant_id=33,
            message=message,
        )
    )


# ── Production screenshot reproducer ─────────────────────────────


class TestCatalogOrderScreenshotReproducer:
    """Locks the EXACT shape from the merchant's screenshot:
    1 item, SAR 69.00, no customer note, no SKU title — only
    metadata. The bot MUST reach the brain with a buying-intent
    text instead of staying silent."""

    SCREENSHOT_MESSAGE: Dict[str, Any] = {
        "from": "966537114421",
        "id":   "wamid.SCREENSHOT_TEST_001",
        "timestamp": "1747606800",
        "type": "order",
        "order": {
            "catalog_id": "1234567890",
            "product_items": [
                {
                    "product_retailer_id": "honey-samr-quarter",
                    "quantity": 1,
                    "item_price": 69.00,
                    "currency": "SAR",
                },
            ],
        },
    }

    def test_should_process_is_true(self):
        result = _normalize(self.SCREENSHOT_MESSAGE)
        assert result.should_process is True, (
            "catalog-order webhook is being dropped — the customer "
            "just placed an order and the bot stays silent"
        )

    def test_normalized_type_is_text(self):
        """Riding the standard text path means the webhook router
        needs ZERO changes — the existing allow-list
        ({'text','audio','image','document','video'}) accepts it."""
        result = _normalize(self.SCREENSHOT_MESSAGE)
        assert result.normalized_type == "text"

    def test_brain_text_contains_required_framing(self):
        result = _normalize(self.SCREENSHOT_MESSAGE)
        text = result.text
        assert text, "brain-facing text is empty"
        # Header + numeric facts + buying-intent framing.
        assert text.startswith("[طلب كتالوج من العميل]")
        assert "عدد أسطر الطلب: 1" in text
        assert "إجمالي الكمية: 1" in text
        assert "69" in text and "SAR" in text
        assert "honey-samr-quarter" in text
        assert "تعامل معه كنية شراء" in text
        # Must NOT contain raw English keys / JSON fragments that
        # would leak into the customer's view if the brain echoed
        # the prompt back.
        for forbidden in (
            "product_retailer_id",
            "item_price",
            '"quantity"',
            "catalog_id",
        ):
            assert forbidden not in text, (
                f"brain-facing text leaked raw API field {forbidden!r}: "
                f"{text!r}"
            )

    def test_metadata_preserves_telemetry_fields(self):
        result = _normalize(self.SCREENSHOT_MESSAGE)
        meta = result.metadata
        assert meta.get("source_type") == "catalog_order"
        assert meta.get("wa_message_id") == "wamid.SCREENSHOT_TEST_001"
        assert meta.get("item_count") == 1
        assert meta.get("line_items_count") == 1
        assert meta.get("total_quantity") == 1
        assert meta.get("total_price") == 69.0
        assert meta.get("currency") == "SAR"
        assert meta.get("product_skus") == ["honey-samr-quarter"]
        assert meta.get("catalog_id") == "1234567890"
        # Raw items echoed for audit reconstruction.
        assert isinstance(meta.get("product_items"), list)
        assert len(meta["product_items"]) == 1

    def test_emits_catalog_message_trace(self, caplog):
        # The normalizer module logs under ``nahla.ai.media`` (set
        # at module import time). Capture at root-level INFO so we
        # never miss the trace line on different log configs.
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self.SCREENSHOT_MESSAGE)
        traces = [
            r for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        ]
        assert traces, "no [CATALOG_MESSAGE_TRACE] log line emitted"
        msg = traces[-1].getMessage()
        assert "wamid=wamid.SCREENSHOT_TEST_001" in msg
        assert "line_items=1" in msg
        assert "total_qty=1" in msg
        assert "currency=SAR" in msg
        assert "final_route=brain" in msg


# ── Edge cases ────────────────────────────────────────────────────


class TestCatalogOrderEdgeCases:

    def test_multiple_items_sums_quantity_and_price(self):
        message = {
            "id":   "wamid.MULTI_001",
            "type": "order",
            "order": {
                "product_items": [
                    {
                        "product_retailer_id": "a",
                        "quantity": 2,
                        "item_price": 10.50,
                        "currency": "SAR",
                    },
                    {
                        "product_retailer_id": "b",
                        "quantity": 3,
                        "item_price": 20.00,
                        "currency": "SAR",
                    },
                ],
            },
        }
        result = _normalize(message)
        assert result.should_process is True
        assert result.metadata["line_items_count"] == 2
        assert result.metadata["total_quantity"] == 5
        assert result.metadata["item_count"] == 2
        # 2*10.50 + 3*20 = 81.00
        assert result.metadata["total_price"] == 81.0
        assert "81" in result.text and "SAR" in result.text
        assert result.metadata["product_skus"] == ["a", "b"]

    def test_customer_note_is_propagated(self):
        message = {
            "id":   "wamid.NOTE_001",
            "type": "order",
            "order": {
                "text": "ابغى توصيل سريع لو سمحت",
                "product_items": [
                    {
                        "product_retailer_id": "x",
                        "quantity": 1,
                        "item_price": 50,
                        "currency": "SAR",
                    },
                ],
            },
        }
        result = _normalize(message)
        assert "ابغى توصيل سريع لو سمحت" in result.text
        assert result.metadata["customer_note"] == "ابغى توصيل سريع لو سمحت"

    def test_missing_price_still_routes_to_brain(self):
        """Some catalog setups omit prices on free / quote-on-request
        items. The bot must still reach the brain — it can ask the
        customer for clarification rather than ignoring the order."""
        message = {
            "id":   "wamid.NOPRICE_001",
            "type": "order",
            "order": {
                "product_items": [
                    {
                        "product_retailer_id": "free-sample",
                        "quantity": 1,
                    },
                ],
            },
        }
        result = _normalize(message)
        assert result.should_process is True
        assert result.normalized_type == "text"
        # No "الإجمالي" line when price is unknown.
        assert "الإجمالي" not in result.text
        # But the buying-intent framing is still there.
        assert "تعامل معه كنية شراء" in result.text

    def test_empty_product_items_still_routes_to_brain(self):
        """Defensive: even a malformed / empty order block must not
        crash the normalizer or drop the customer's message."""
        message = {
            "id":   "wamid.EMPTY_001",
            "type": "order",
            "order": {"product_items": []},
        }
        result = _normalize(message)
        assert result.should_process is True
        assert result.normalized_type == "text"
        assert "[طلب كتالوج من العميل]" in result.text

    def test_string_quantity_and_price_are_coerced(self):
        """Some providers serialise numeric fields as strings —
        the normalizer must coerce defensively without raising."""
        message = {
            "id":   "wamid.STR_001",
            "type": "order",
            "order": {
                "product_items": [
                    {
                        "product_retailer_id": "y",
                        "quantity":   "2",
                        "item_price": "12.5",
                        "currency":   "SAR",
                    },
                ],
            },
        }
        result = _normalize(message)
        assert result.metadata["line_items_count"] == 1
        assert result.metadata["total_quantity"] == 2
        assert result.metadata["item_count"] == 1
        assert result.metadata["total_price"] == 25.0


# ── Negative test: order type must NOT be dropped ─────────────────


class TestCatalogOrderNotDropped:
    """Belt-and-suspenders against future regressions: the
    webhook router drops anything whose ``normalized_type`` is
    NOT in {'text','audio','image','document','video'}. By
    locking ``normalized_type="text"`` here we guarantee the
    catalog order reaches the brain without router changes."""

    def test_normalized_type_in_router_allowlist(self):
        result = _normalize({
            "id":   "wamid.LOCK_001",
            "type": "order",
            "order": {
                "product_items": [
                    {
                        "product_retailer_id": "z",
                        "quantity": 1,
                        "item_price": 1,
                        "currency": "SAR",
                    },
                ],
            },
        })
        # Mirror the literal set used at routers/whatsapp_webhook.py
        # so a future allow-list change forces an explicit update
        # here too.
        ROUTER_ALLOWLIST = {"text", "audio", "image", "document", "video"}
        assert result.normalized_type in ROUTER_ALLOWLIST, (
            f"normalized_type={result.normalized_type!r} would be "
            f"dropped by the webhook router as INBOUND_IGNORED_"
            f"UNSUPPORTED — catalog orders are silenced again"
        )


# ── Product-name extraction (June 2026) ─────────────────────────────
#
# Background: the merchant captured a screenshot where the bot said
# "ما وصلني اسم المنتج بالضبط" even though WhatsApp's catalog card on
# the customer's phone clearly displayed the product name. Meta's
# documented order shape only carries ``product_retailer_id``, but
# many BSPs / 360dialog payloads decorate ``product_items`` with a
# human-readable label. When that label IS in the payload we MUST
# forward it to the brain so the LLM uses the real name instead of
# guessing.


class TestCatalogOrderProductNameExtraction:

    def _build(self, items):
        return {
            "id":   "wamid.NAME_001",
            "type": "order",
            "order": {
                "catalog_id": "cat-99",
                "product_items": items,
            },
        }

    def test_name_field_per_item_is_forwarded_to_brain(self):
        result = _normalize(self._build([
            {
                "product_retailer_id": "honey-samr-quarter",
                "name":     "ربع كيلو سمر",
                "quantity": 1,
                "item_price": 79,
                "currency": "SAR",
            },
        ]))
        assert "اسم المنتج: ربع كيلو سمر" in result.text, (
            f"product name missing from brain text: {result.text!r}"
        )
        assert result.metadata.get("product_names") == ["ربع كيلو سمر"]

    def test_title_field_is_used_when_name_missing(self):
        result = _normalize(self._build([
            {
                "product_retailer_id": "x",
                "title":    "بروبوليس بالعسل 250غ",
                "quantity": 1,
                "item_price": 69,
                "currency": "SAR",
            },
        ]))
        assert "اسم المنتج: بروبوليس بالعسل 250غ" in result.text
        assert result.metadata["product_names"] == ["بروبوليس بالعسل 250غ"]

    def test_retailer_name_field_is_recognised(self):
        result = _normalize(self._build([
            {
                "product_retailer_id": "x",
                "retailer_name": "كريم سدر",
                "quantity": 1,
                "item_price": 50,
                "currency": "SAR",
            },
        ]))
        assert "اسم المنتج: كريم سدر" in result.text

    def test_nested_product_subobject_is_unwrapped(self):
        result = _normalize(self._build([
            {
                "product_retailer_id": "x",
                "product": {"name": "عسل طلح فاخر"},
                "quantity": 1,
                "item_price": 200,
                "currency": "SAR",
            },
        ]))
        assert "اسم المنتج: عسل طلح فاخر" in result.text

    def test_multiple_items_join_distinct_names(self):
        result = _normalize(self._build([
            {
                "product_retailer_id": "a",
                "name": "ربع سمر",
                "quantity": 1, "item_price": 79, "currency": "SAR",
            },
            {
                "product_retailer_id": "b",
                "name": "ربع طلح",
                "quantity": 1, "item_price": 126, "currency": "SAR",
            },
        ]))
        assert "اسم المنتج: ربع سمر + ربع طلح" in result.text
        assert result.metadata["product_names"] == ["ربع سمر", "ربع طلح"]

    def test_top_level_product_name_is_used_as_fallback(self):
        message = {
            "id": "wamid.TOP_001", "type": "order",
            "order": {
                "catalog_id":   "cat-99",
                "product_name": "كريم نحلة",
                "product_items": [
                    {
                        "product_retailer_id": "x",
                        "quantity": 1, "item_price": 30, "currency": "SAR",
                    },
                ],
            },
        }
        result = _normalize(message)
        assert "اسم المنتج: كريم نحلة" in result.text
        assert result.metadata["product_names"] == ["كريم نحلة"]

    def test_missing_name_does_not_emit_empty_line(self):
        """If no name is anywhere in the payload, the framed text
        must NOT contain a stray ``اسم المنتج:`` line — that would
        leak as garbage to the LLM."""
        result = _normalize(self._build([
            {
                "product_retailer_id": "x",
                "quantity": 1, "item_price": 30, "currency": "SAR",
            },
        ]))
        assert "اسم المنتج:" not in result.text
        assert result.metadata["product_names"] == []

    def test_trace_log_uses_real_name_when_available(self, caplog):
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self._build([
                {
                    "product_retailer_id": "honey-samr-quarter",
                    "name":     "ربع كيلو سمر",
                    "quantity": 1, "item_price": 79, "currency": "SAR",
                },
            ]))
        msg = next(
            r.getMessage() for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        )
        assert "product_name=ربع كيلو سمر" in msg, (
            "trace log must surface the real label when present, not the SKU"
        )

    def test_blank_name_strings_are_ignored(self):
        """Whitespace-only names must not produce an empty ``اسم
        المنتج:`` line."""
        result = _normalize(self._build([
            {
                "product_retailer_id": "x",
                "name":     "   ",
                "title":    "",
                "quantity": 1, "item_price": 30, "currency": "SAR",
            },
        ]))
        assert "اسم المنتج:" not in result.text


class TestCatalogTraceDiagnostics:
    """The merchant asked for richer ``[CATALOG_MESSAGE_TRACE]`` so a
    log grep instantly answers 'what did Meta send and what keys did
    we see?' when an SKU fails to resolve in production."""

    def _build(self, items):
        return {
            "id":   "wamid.TRACE_001",
            "type": "order",
            "order": {
                "catalog_id": "cat-99",
                "product_items": items,
            },
        }

    def test_trace_includes_raw_retailer_id(self, caplog):
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self._build([
                {
                    "product_retailer_id": "WA-EXT-101",
                    "quantity": 1, "item_price": 79, "currency": "SAR",
                },
            ]))
        msg = next(
            r.getMessage() for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        )
        assert "raw_retailer_id=WA-EXT-101" in msg

    def test_trace_lists_keys_present_on_first_item(self, caplog):
        """Future shape changes (BSP adds a new field) should be
        immediately visible without re-deploying."""
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self._build([
                {
                    "product_retailer_id": "x",
                    "name":       "كريم سم النحل",
                    "quantity":   1,
                    "item_price": 79,
                    "currency":   "SAR",
                },
            ]))
        msg = next(
            r.getMessage() for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        )
        # Keys are sorted alphabetically for stable diff'ing.
        assert "item_keys=currency,item_price,name,product_retailer_id,quantity" in msg

    def test_trace_records_product_names_count(self, caplog):
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self._build([
                {
                    "product_retailer_id": "a", "name": "ربع سمر",
                    "quantity": 1, "item_price": 79, "currency": "SAR",
                },
                {
                    "product_retailer_id": "b", "name": "ربع طلح",
                    "quantity": 1, "item_price": 126, "currency": "SAR",
                },
            ]))
        msg = next(
            r.getMessage() for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        )
        assert "product_names_count=2" in msg

    def test_trace_records_zero_names_when_payload_lacks_them(self, caplog):
        with caplog.at_level(logging.INFO, logger="nahla.ai.media"):
            _normalize(self._build([
                {
                    "product_retailer_id": "x",
                    "quantity": 1, "item_price": 79, "currency": "SAR",
                },
            ]))
        msg = next(
            r.getMessage() for r in caplog.records
            if "[CATALOG_MESSAGE_TRACE]" in r.getMessage()
        )
        assert "product_names_count=0" in msg


class TestCatalogFocusPinUsesPayloadName:
    """The ``_maybe_pin_catalog_focus`` helper must use the
    payload-supplied name as a fallback title when the merchant's
    catalog DB doesn't yet have a row for the SKU."""

    def test_pin_uses_payload_name_when_db_misses(self):
        from modules.ai.brain.pipeline import _maybe_pin_catalog_focus
        from modules.ai.brain.types import MerchantConversationState

        # Re-create the framed text the normalizer would produce
        # for an order whose item carries a ``name`` field.
        message = "\n".join([
            "[طلب كتالوج من العميل]",
            "عدد أسطر الطلب: 1",
            "إجمالي الكمية: 1",
            "الإجمالي: 79 SAR",
            "اسم المنتج: ربع كيلو سمر",
            "رمز المنتج (SKU): honey-samr-quarter",
            "ملاحظة: العميل أرسل طلبًا من كتالوج واتساب.",
        ])

        db = MagicMock()
        db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        state = MerchantConversationState()
        _maybe_pin_catalog_focus(db=db, tenant_id=1, message=message, state=state)

        assert state.current_product_focus, "focus must be pinned"
        assert state.current_product_focus["title"] == "ربع كيلو سمر", (
            f"payload name not used as title fallback: "
            f"{state.current_product_focus!r}"
        )
        assert state.current_product_focus["from_catalog_order"] is True

    def test_db_resolved_title_wins_over_payload_name(self):
        """Real catalog rows are the source of truth — the
        payload-supplied name is only a fallback."""
        from modules.ai.brain.pipeline import _maybe_pin_catalog_focus
        from modules.ai.brain.types import MerchantConversationState

        product = MagicMock()
        product.id = 11
        product.title = "ربع كيلو سمر — عبوة فاخرة"
        product.price = 79.0

        db = MagicMock()
        db.query.return_value.filter.return_value.filter.return_value.first.return_value = product

        message = "\n".join([
            "[طلب كتالوج من العميل]",
            "عدد أسطر الطلب: 1",
            "إجمالي الكمية: 1",
            "الإجمالي: 79 SAR",
            "اسم المنتج: ربع كيلو سمر",
            "رمز المنتج (SKU): honey-samr-quarter",
        ])

        state = MerchantConversationState()
        _maybe_pin_catalog_focus(db=db, tenant_id=1, message=message, state=state)

        assert state.current_product_focus["title"] == "ربع كيلو سمر — عبوة فاخرة", (
            "DB-resolved title must win over the payload-supplied name"
        )
