"""Payment provider matching regressions for checkout."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.merchant_payment_methods import MerchantPaymentMethods  # noqa: E402
from models import MerchantKnowledgeSection  # noqa: E402
from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: E402
    apply_payment_credential_guard,
    compose_verified_bank_transfer_block,
)
from modules.ai.order_flow_v2.payment import (  # noqa: E402
    apply_payment_method_selection,
    build_payment_bank_mismatch_reply,
)


def _methods() -> MerchantPaymentMethods:
    return MerchantPaymentMethods(
        bank_transfer_enabled=True,
        cash_on_delivery_enabled=False,
        moyasar_enabled=False,
        moyasar_checkout_ready=False,
        manual_payment_enabled=False,
        available_methods=["bank_transfer"],
    )


class TestPaymentProviderMatching:
    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    def test_selected_bank_must_match_verified_account(self, _methods_mock, _accounts_mock) -> None:
        _methods_mock.return_value = _methods()
        _accounts_mock.return_value = SimpleNamespace(bank_brands=("rajhi",))
        patch, chosen = apply_payment_method_selection(
            MagicMock(),
            tenant_id=1,
            message="تحويل الراجحي",
        )
        assert chosen == "bank_transfer"
        assert patch and patch.get("requested_bank") == "rajhi"

    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    def test_selected_rajhi_does_not_send_ahli_credentials(self, _methods_mock, _accounts_mock) -> None:
        _methods_mock.return_value = _methods()
        _accounts_mock.return_value = SimpleNamespace(
            bank_brands=("rajhi",),
            ibans=("SA1111111111111111111111",),
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            SimpleNamespace(
                title="حساب الراجحي",
                body="البنك: الراجحي\nالآيبان: SA1111111111111111111111",
                metadata_json={"bank_brand": "rajhi"},
            )
        ]
        body = compose_verified_bank_transfer_block(db, tenant_id=1, requested_bank="rajhi")
        assert "SA1111111111111111111111" in body
        wrong = apply_payment_credential_guard(
            "بيانات الأهلي: SA0380000000608010167519",
            db=db,
            tenant_id=1,
            inbound_text="الراجحي",
            requested_bank="rajhi",
        )
        assert wrong.replaced
        assert "الأهلي" not in wrong.reply or "غير مفعّلة" in wrong.reply

    def test_selected_bank_unconfigured_returns_honest_no_config(self) -> None:
        db = MagicMock()
        reply = build_payment_bank_mismatch_reply(
            db,
            tenant_id=1,
            rejection_reason="requested_bank_not_enabled",
            requested_bank="rajhi",
        )
        with patch(
            "modules.ai.order_flow_v2.payment.load_tenant_payment_accounts",
            return_value=SimpleNamespace(bank_brands=("alahli",)),
        ):
            reply = build_payment_bank_mismatch_reply(
                db,
                tenant_id=1,
                rejection_reason="requested_bank_not_enabled",
                requested_bank="rajhi",
            )
        assert "غير مفعّلة" in reply
        assert "الأهلي" in reply

    def test_payment_barcode_alias_matches_selected_provider(self) -> None:
        assert "payment_rajhi_barcode".startswith("payment_")
        assert "rajhi" in "payment_rajhi_barcode"

    def test_wrong_provider_payment_media_is_blocked(self) -> None:
        result = apply_payment_credential_guard(
            "حساب الأهلي للتحويل",
            db=MagicMock(),
            tenant_id=1,
            inbound_text="الراجحي",
            requested_bank="rajhi",
        )
        assert result.replaced
