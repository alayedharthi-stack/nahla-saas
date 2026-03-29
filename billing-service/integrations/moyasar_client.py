from typing import Any, Dict, Optional


class MoyasarClient:
    def __init__(self, api_key: str, sandbox: bool = True):
        self.api_key = api_key
        self.sandbox = sandbox
        self.base_url = "https://api.moyasar.com" if not sandbox else "https://api.moyasar.com"

    def create_payment(self, amount_sar: int, description: Optional[str] = None, callback_url: Optional[str] = None) -> Dict[str, Any]:
        return {
            "gateway": "moyasar",
            "amount_sar": amount_sar,
            "currency": "SAR",
            "description": description,
            "callback_url": callback_url,
            "status": "pending",
            "supported": True,
        }

    def verify_payment(self, transaction_id: str) -> Dict[str, Any]:
        return {
            "gateway": "moyasar",
            "transaction_id": transaction_id,
            "status": "paid",
            "verified": True,
        }

    def build_charge_payload(self, amount_sar: int, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "amount": amount_sar,
            "currency": "SAR",
            "description": description,
            "metadata": metadata or {},
            "payment_method": "credit_card",
        }
