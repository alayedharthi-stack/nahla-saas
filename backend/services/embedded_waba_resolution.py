"""Graph-proven coexistence WABA + phone resolution.

Client/session hints and legacy provider metadata are untrusted. Adoption requires
full Graph proof: readable WABA, phone list, normalized phone match, optional owner
portfolio match, and no cross-tenant claim (checked separately at DB layer).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("nahla.embedded_waba_resolution")

REAUTH_REQUIRED = "REAUTH_REQUIRED"
WABA_RESOLUTION_CONFLICT = "WABA_RESOLUTION_CONFLICT"
WRONG_PHONE = "WRONG_PHONE"
WRONG_BUSINESS_OWNER = "WRONG_BUSINESS_OWNER"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


@dataclass(frozen=True)
class VerifiedCoexistenceAssets:
    waba_id: str
    phone_number_id: str
    display_phone_number: str
    verified_name: Optional[str]
    ownership_type: Optional[str]
    owner_business_id: Optional[str]


class CoexistenceWabaResolutionError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _digits(value: Optional[str]) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _expected_phone_variants(expected_digits: str) -> set[str]:
    variants = {expected_digits}
    if expected_digits.startswith("0") and len(expected_digits) > 1:
        variants.add(expected_digits[1:])
    return variants


def _phones_match(expected_digits: str, display: Optional[str]) -> bool:
    if not expected_digits:
        return False
    candidate = _digits(display)
    if not candidate:
        return False
    for variant in _expected_phone_variants(expected_digits):
        if (
            candidate == variant
            or candidate.endswith(variant)
            or variant.endswith(candidate)
        ):
            return True
    return False


def _authorized_waba_ids(debug_info: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for scope in debug_info.get("granular_scopes", []) or []:
        if scope.get("scope") != "whatsapp_business_management":
            continue
        for raw in scope.get("target_ids", []) or []:
            token = str(raw or "").strip()
            if token and token not in out:
                out.append(token)
    return out


def _business_portfolio_ids(debug_info: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for scope in debug_info.get("granular_scopes", []) or []:
        if scope.get("scope") != "business_management":
            continue
        for raw in scope.get("target_ids", []) or []:
            token = str(raw or "").strip()
            if token and token not in out:
                out.append(token)
    return out


async def _graph_get(
    graph_base: str,
    token: str,
    node: str,
    fields: str,
) -> Dict[str, Any]:
    resp = httpx.get(
        f"{graph_base}/{node}",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": fields},
        timeout=20,
    )
    data = resp.json()
    if "error" in data:
        err = data.get("error") or {}
        return {
            "ok": False,
            "status": resp.status_code,
            "code": err.get("code"),
            "message": err.get("message") or f"HTTP {resp.status_code}",
        }
    return {"ok": True, "status": resp.status_code, "data": data}


async def resolve_coexistence_assets_from_graph(
    graph_base: str,
    token: str,
    debug_info: Dict[str, Any],
    *,
    expected_phone_number: Optional[str] = None,
    expected_business_portfolio_id: Optional[str] = None,
) -> VerifiedCoexistenceAssets:
    """Resolve exactly one WABA + Meta phone_number_id with Graph proof only."""
    expected_digits = _digits(expected_phone_number)
    if not expected_digits:
        raise CoexistenceWabaResolutionError(
            REAUTH_REQUIRED,
            "A verified merchant phone number is required before coexistence adoption.",
            http_status=400,
        )

    portfolio_hint = str(expected_business_portfolio_id or "").strip()
    if not portfolio_hint:
        portfolios = _business_portfolio_ids(debug_info)
        if len(portfolios) == 1:
            portfolio_hint = portfolios[0]

    authorized = _authorized_waba_ids(debug_info)
    if not authorized:
        raise CoexistenceWabaResolutionError(
            REAUTH_REQUIRED,
            "Meta token does not authorize any WhatsApp Business Account. Reauthorize and select the existing account.",
            http_status=400,
        )

    verified: List[VerifiedCoexistenceAssets] = []
    for waba_id in authorized:
        waba_resp = await _graph_get(
            graph_base,
            token,
            waba_id,
            "id,name,ownership_type,owner_business_info",
        )
        if not waba_resp.get("ok"):
            logger.warning(
                "[CoexistenceResolve] waba unreadable waba=%s code=%s",
                waba_id,
                waba_resp.get("code"),
            )
            continue
        waba_data = waba_resp["data"]
        owner_info = waba_data.get("owner_business_info") or {}
        owner_id = str(owner_info.get("id") or owner_info.get("business_id") or "").strip() or None
        if portfolio_hint and owner_id and owner_id != portfolio_hint:
            continue

        phones_resp = await _graph_get(
            graph_base,
            token,
            f"{waba_id}/phone_numbers",
            "id,display_phone_number,verified_name,code_verification_status",
        )
        if not phones_resp.get("ok"):
            logger.warning(
                "[CoexistenceResolve] phones unreadable waba=%s code=%s",
                waba_id,
                phones_resp.get("code"),
            )
            continue
        for phone in phones_resp["data"].get("data") or []:
            if not _phones_match(expected_digits, phone.get("display_phone_number")):
                continue
            phone_id = str(phone.get("id") or "").strip()
            if not phone_id:
                continue
            probe = await _graph_get(graph_base, token, phone_id, "id,display_phone_number")
            if not probe.get("ok"):
                continue
            verified.append(
                VerifiedCoexistenceAssets(
                    waba_id=str(waba_data.get("id") or waba_id),
                    phone_number_id=phone_id,
                    display_phone_number=str(phone.get("display_phone_number") or ""),
                    verified_name=phone.get("verified_name"),
                    ownership_type=waba_data.get("ownership_type"),
                    owner_business_id=owner_id,
                )
            )

    if not verified:
        if portfolio_hint and authorized:
            raise CoexistenceWabaResolutionError(
                WRONG_PHONE,
                "No Graph-verified phone on the authorized WABA matches the merchant phone.",
                http_status=400,
            )
        raise CoexistenceWabaResolutionError(
            REAUTH_REQUIRED,
            "Could not verify an existing WhatsApp Business Account and phone. Reauthorize with Meta.",
            http_status=400,
        )
    if len(verified) > 1:
        raise CoexistenceWabaResolutionError(
            WABA_RESOLUTION_CONFLICT,
            "Multiple WhatsApp Business Accounts match this phone. Contact support for reconciliation.",
            http_status=409,
        )
    one = verified[0]
    if portfolio_hint and one.owner_business_id and one.owner_business_id != portfolio_hint:
        raise CoexistenceWabaResolutionError(
            WRONG_BUSINESS_OWNER,
            "WhatsApp Business Account owner does not match the verified business portfolio.",
            http_status=400,
        )
    return one
