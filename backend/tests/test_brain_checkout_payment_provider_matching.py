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
from core.tenant_payment_accounts import extract_ibans  # noqa: E402
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
        assert wrong.reason == "wrong_provider_substitution"
        assert "SA0380000000608010167519" not in wrong.reply
        assert "SA1111111111111111111111" in wrong.reply

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
        with patch(
            "modules.ai.order_flow_v2.payment.load_tenant_payment_accounts",
            return_value=SimpleNamespace(bank_brands=("alahli",), ibans=("SA0380000000608010167519",)),
        ):
            result = apply_payment_credential_guard(
                "حساب الأهلي للتحويل SA0380000000608010167519",
                db=MagicMock(),
                tenant_id=1,
                inbound_text="الراجحي",
                requested_bank="rajhi",
            )
        assert result.replaced
        assert result.reason == "wrong_provider_substitution"
        assert not extract_ibans(result.reply)

    @patch("modules.ai.brain.postprocess.payment_credential_guard._verified_ibans")
    @patch("modules.ai.brain.postprocess.payment_credential_guard._ibans_for_requested_brand")
    def test_compose_verified_bank_transfer_block_does_not_fallback_to_other_bank_when_requested_missing(
        self,
        brand_ibans_mock,
        verified_ibans_mock,
    ) -> None:
        brand_ibans_mock.return_value = ()
        verified_ibans_mock.return_value = ("SA0380000000608010167519",)
        with patch(
            "modules.ai.order_flow_v2.payment.load_tenant_payment_accounts",
            return_value=SimpleNamespace(bank_brands=("alahli",)),
        ):
            body = compose_verified_bank_transfer_block(
                MagicMock(),
                tenant_id=1,
                requested_bank="rajhi",
            )
        assert "غير مفعّلة" in body
        assert "الأهلي" in body
        assert not extract_ibans(body)
        verified_ibans_mock.assert_not_called()

    @patch("modules.ai.brain.postprocess.payment_credential_guard._ibans_for_requested_brand")
    def test_apply_payment_credential_guard_wrong_provider_strips_other_bank_iban(
        self,
        brand_ibans_mock,
    ) -> None:
        brand_ibans_mock.return_value = ()
        with patch(
            "modules.ai.order_flow_v2.payment.load_tenant_payment_accounts",
            return_value=SimpleNamespace(bank_brands=("alahli",), ibans=("SA0380000000608010167519",)),
        ):
            result = apply_payment_credential_guard(
                "حساب الأهلي SA0380000000608010167519",
                db=MagicMock(),
                tenant_id=1,
                inbound_text="الراجحي",
                requested_bank="rajhi",
            )
        assert result.replaced
        assert result.reason == "wrong_provider_substitution"
        assert not extract_ibans(result.reply)
        assert "غير مفعّلة" in result.reply

    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    def test_selected_rajhi_with_only_ahli_configured_returns_mismatch_without_credentials(
        self,
        methods_mock,
        accounts_mock,
    ) -> None:
        methods_mock.return_value = _methods()
        accounts_mock.return_value = SimpleNamespace(
            bank_brands=("alahli",),
            ibans=("SA0380000000608010167519",),
        )
        patch, chosen = apply_payment_method_selection(
            MagicMock(),
            tenant_id=1,
            message="الراجحي",
        )
        assert chosen is None
        assert patch and patch.get("order_flow_v2_payment_rejected")
        reply = build_payment_bank_mismatch_reply(
            MagicMock(),
            tenant_id=1,
            rejection_reason="requested_bank_not_enabled",
            requested_bank="rajhi",
        )
        assert "غير مفعّلة" in reply
        assert not extract_ibans(reply)

    @patch("modules.ai.brain.postprocess.payment_credential_guard._ibans_for_requested_brand")
    def test_selected_bank_unconfigured_does_not_emit_any_iban(
        self,
        brand_ibans_mock,
    ) -> None:
        brand_ibans_mock.return_value = ()
        with patch(
            "modules.ai.order_flow_v2.payment.load_tenant_payment_accounts",
            return_value=SimpleNamespace(bank_brands=("alahli",)),
        ):
            compose_body = compose_verified_bank_transfer_block(
                MagicMock(),
                tenant_id=1,
                requested_bank="rajhi",
            )
            guard_result = apply_payment_credential_guard(
                "الآيبان: SA0380000000608010167519",
                db=MagicMock(),
                tenant_id=1,
                inbound_text="الراجحي",
                requested_bank="rajhi",
            )
        assert not extract_ibans(compose_body)
        assert not extract_ibans(guard_result.reply)

    def test_no_requested_bank_lists_verified_accounts_when_multiple_configured(self) -> None:
        with patch(
            "modules.ai.brain.postprocess.payment_credential_guard._verified_ibans",
            return_value=("SA1111111111111111111111", "SA2222222222222222222222"),
        ):
            body = compose_verified_bank_transfer_block(
                MagicMock(),
                tenant_id=1,
                requested_bank="",
            )
        assert "SA1111111111111111111111" in body
        assert "SA2222222222222222222222" in body
