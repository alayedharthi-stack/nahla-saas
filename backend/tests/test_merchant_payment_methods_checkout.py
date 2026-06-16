"""Merchant payment method settings + WA checkout replies."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.merchant_payment_methods import (  # noqa: E402
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
    PAYMENT_METHOD_MOYASAR,
    build_payment_method_state_patch,
    moyasar_checkout_ready,
    parse_payment_method_from_text,
    resolve_merchant_payment_methods,
    validate_payment_method_choice,
)
from core.wa_checkout_reply import (  # noqa: E402
    build_checkout_payment_options_reply,
    compose_address_reply,
)


def _methods(**kwargs):
    base = dict(
        bank_transfer_enabled=True,
        cash_on_delivery_enabled=False,
        moyasar_enabled=False,
        moyasar_checkout_ready=False,
        manual_payment_enabled=False,
        available_methods=[PAYMENT_METHOD_BANK_TRANSFER],
    )
    base.update(kwargs)
    avail = kwargs.get("available_methods")
    if avail is None:
        avail = []
        if base["moyasar_checkout_ready"]:
            avail.append(PAYMENT_METHOD_MOYASAR)
        if base["bank_transfer_enabled"]:
            avail.append(PAYMENT_METHOD_BANK_TRANSFER)
        if base["cash_on_delivery_enabled"]:
            avail.append(PAYMENT_METHOD_CASH_ON_DELIVERY)
        base["available_methods"] = avail
    return SimpleNamespace(**base)


class TestPaymentMethodResolver:
    def test_bank_transfer_only(self) -> None:
        m = resolve_merchant_payment_methods(
            extra_metadata={"payment_methods": {"bank_transfer_enabled": True, "cash_on_delivery_enabled": False}},
        )
        assert m.available_methods == [PAYMENT_METHOD_BANK_TRANSFER]

    def test_cod_only(self) -> None:
        m = resolve_merchant_payment_methods(
            extra_metadata={"payment_methods": {"bank_transfer_enabled": False, "cash_on_delivery_enabled": True}},
        )
        assert m.available_methods == [PAYMENT_METHOD_CASH_ON_DELIVERY]

    def test_both_enabled(self) -> None:
        m = resolve_merchant_payment_methods(
            extra_metadata={
                "payment_methods": {
                    "bank_transfer_enabled": True,
                    "cash_on_delivery_enabled": True,
                },
            },
        )
        assert PAYMENT_METHOD_BANK_TRANSFER in m.available_methods
        assert PAYMENT_METHOD_CASH_ON_DELIVERY in m.available_methods

    def test_moyasar_not_ready_not_offered(self) -> None:
        m = resolve_merchant_payment_methods(
            extra_metadata={"payment_methods": {"moyasar_enabled": True}},
            moyasar_cfg={"enabled": True},
        )
        assert PAYMENT_METHOD_MOYASAR not in m.available_methods

    def test_moyasar_ready_when_configured(self) -> None:
        assert moyasar_checkout_ready({"enabled": True, "secret_key": "sk_test"})
        m = resolve_merchant_payment_methods(
            extra_metadata={"payment_methods": {"moyasar_enabled": True, "bank_transfer_enabled": False}},
            moyasar_cfg={"enabled": True, "secret_key": "sk_test"},
        )
        assert m.available_methods == [PAYMENT_METHOD_MOYASAR]

    def test_no_methods_enabled(self) -> None:
        m = resolve_merchant_payment_methods(
            extra_metadata={
                "payment_methods": {
                    "bank_transfer_enabled": False,
                    "cash_on_delivery_enabled": False,
                },
            },
            has_bank_kb=False,
        )
        assert m.available_methods == []


class TestPaymentMethodValidation:
    def test_reject_cod_when_disabled(self) -> None:
        msg = validate_payment_method_choice(
            PAYMENT_METHOD_CASH_ON_DELIVERY,
            _methods(bank_transfer_enabled=True, cash_on_delivery_enabled=False),
        )
        assert msg is not None
        assert "غير متاح" in msg
        assert "تحويل بنكي" in msg

    def test_reject_bank_when_disabled(self) -> None:
        msg = validate_payment_method_choice(
            PAYMENT_METHOD_BANK_TRANSFER,
            _methods(bank_transfer_enabled=False, cash_on_delivery_enabled=True),
        )
        assert msg is not None
        assert "التحويل البنكي غير متاح" in msg

    def test_no_methods_invented(self) -> None:
        methods = _methods(
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
            available_methods=[],
        )
        msg = validate_payment_method_choice(PAYMENT_METHOD_CASH_ON_DELIVERY, methods)
        assert msg is not None
        assert "غير متاح" in msg
        assert "المتاح الآن" not in msg or msg.endswith(".")


class TestCheckoutReply:
    def _complete_prep(self):
        return {
            "customer_first_name": "A",
            "customer_last_name": "B",
            "city": "مكة",
            "line_items": [{"title": "عسل سمر", "quantity": 1, "size": "1 كيلو"}],
            "total": "387",
            "free_shipping": True,
            "delivery_address_status": "accepted",
            "google_maps_url": "https://maps.google.com/?q=21,39",
            "delivery_location_lat": "21",
            "delivery_location_lng": "39",
        }

    def test_location_complete_bank_only(self) -> None:
        prep = self._complete_prep()
        reply = build_checkout_payment_options_reply(
            prep,
            brain_state={},
            line_items=prep["line_items"],
            payment_methods=_methods(),
        )
        assert "وصل الموقع وتم تسجيله" in reply
        assert "عسل سمر" in reply
        assert "387" in reply
        assert "تحويل بنكي" in reply
        assert "دفع عند الاستلام" not in reply

    def test_location_complete_cod_enabled(self) -> None:
        prep = self._complete_prep()
        reply = build_checkout_payment_options_reply(
            prep,
            brain_state={},
            line_items=prep["line_items"],
            payment_methods=_methods(
                bank_transfer_enabled=True,
                cash_on_delivery_enabled=True,
                available_methods=[PAYMENT_METHOD_BANK_TRANSFER, PAYMENT_METHOD_CASH_ON_DELIVERY],
            ),
        )
        assert "دفع عند الاستلام" in reply

    def test_incomplete_cart(self) -> None:
        prep = {
            "google_maps_url": "https://maps.google.com/?q=21,39",
            "delivery_location_lat": "21",
            "delivery_location_lng": "39",
        }
        reply = compose_address_reply(
            order_prep=prep,
            brain_state={},
            payment_methods=_methods(),
        )
        assert "المنتج" in reply or "الكمية" in reply

    def test_no_payment_methods_message(self) -> None:
        prep = self._complete_prep()
        reply = build_checkout_payment_options_reply(
            prep,
            brain_state={},
            line_items=prep["line_items"],
            payment_methods=_methods(
                bank_transfer_enabled=False,
                cash_on_delivery_enabled=False,
                available_methods=[],
            ),
        )
        assert "طرق الدفع غير مفعلة" in reply


class TestPaymentMethodParsing:
    def test_parse_cod(self) -> None:
        assert parse_payment_method_from_text("دفع عند الاستلام") == PAYMENT_METHOD_CASH_ON_DELIVERY

    def test_parse_bank(self) -> None:
        assert parse_payment_method_from_text("تحويل بنكي") == PAYMENT_METHOD_BANK_TRANSFER

    def test_bank_transfer_state_patch_not_paid(self) -> None:
        patch = build_payment_method_state_patch(PAYMENT_METHOD_BANK_TRANSFER)
        assert patch["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert patch["payment_confirmed"] is False
        assert patch["order_status"] == "pending_payment"

    def test_cod_state_patch_not_paid(self) -> None:
        patch = build_payment_method_state_patch(PAYMENT_METHOD_CASH_ON_DELIVERY)
        assert patch["payment_status"] == "cod_pending"
        assert patch["order_status"] == "cod_pending"
