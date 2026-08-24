
"""Graph-proven coexistence WABA + phone resolution.

Client/session hints and legacy provider metadata are untrusted. Adoption requires
full Graph proof: readable WABA, phone list, exact E.164 phone match, mandatory
owner portfolio proof, and no cross-tenant claim (checked at DB layer).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from utils.phone_utils import normalize_to_e164

logger = logging.getLogger("nahla.embedded_waba_resolution")

REAUTH_REQUIRED = "REAUTH_REQUIRED"
WABA_RESOLUTION_CONFLICT = "WABA_RESOLUTION_CONFLICT"
WRONG_PHONE = "WRONG_PHONE"
WRONG_BUSINESS_OWNER = "WRONG_BUSINESS_OWNER"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"

ALLOWED_OAUTH_MODES = frozenset({"cloud_api", "coexistence"})


@dataclass(frozen=True)
class VerifiedCoexistenceAssets:
    waba_id: str
    phone_number_id: str
    display_phone_number: str
    verified_name: Optional[str]
    ownership_type: Optional[str]
    owner_business_id: str
    canonical_phone_e164: str
    trusted_business_portfolio_id: str


class CoexistenceWabaResolutionError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _redact_id(value: Optional[str]) -> str:
    token = str(value or "").strip()
    if not token:
        return "∅"
    return f"#{hashlib.sha256(token.encode()).hexdigest()[:8]}"


def canonicalize_phone_e164(raw: Optional[str], *, default_region: str = "SA") -> Optional[str]:
    """Return strict E.164 or None when the number cannot be canonicalized."""
    if not raw:
        return None
    e164 = normalize_to_e164(str(raw).strip(), default_region=default_region)
    if not e164 or not e164.startswith("+"):
        return None
    digits = re.sub(r"\D+", "", e164)
    # Reject implausibly short international numbers after canonicalization.
    if len(digits) < 10:
        return None
    return e164


def phones_match_exact_e164(expected_raw: Optional[str], display_raw: Optional[str]) -> bool:
    expected = canonicalize_phone_e164(expected_raw)
    candidate = canonicalize_phone_e164(display_raw)
    if not expected or not candidate:
        return False
    return expected == candidate


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


def derive_trusted_business_portfolio_id(debug_info: Dict[str, Any]) -> Optional[str]:
    """Server-derived portfolio id from token debug only — never from client hints."""
    portfolios: List[str] = []
    for scope in debug_info.get("granular_scopes", []) or []:
        if scope.get("scope") != "business_management":
            continue
        for raw in scope.get("target_ids", []) or []:
            token = str(raw or "").strip()
            if token and token not in portfolios:
                portfolios.append(token)
    if len(portfolios) == 1:
        return portfolios[0]
    return None


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
    hinted_waba_id: Optional[str] = None,
    hinted_phone_number_id: Optional[str] = None,
) -> VerifiedCoexistenceAssets:
    """Resolve exactly one WABA + Meta phone_number_id with Graph proof only."""
    # Client hints are untrusted — log presence only for audit, never for selection.
    if str(hinted_waba_id or "").strip() or str(hinted_phone_number_id or "").strip():
        logger.info(
            "[CoexistenceResolve] ignoring client hints waba=%s phone=%s",
            _redact_id(hinted_waba_id),
            _redact_id(hinted_phone_number_id),
        )

    expected_e164 = canonicalize_phone_e164(expected_phone_number)
    if not expected_e164:
        raise CoexistenceWabaResolutionError(
            REAUTH_REQUIRED,
            "A verified merchant phone number is required before coexistence adoption.",
            http_status=400,
        )

    authorized = _authorized_waba_ids(debug_info)
    if not authorized:
        raise CoexistenceWabaResolutionError(
            REAUTH_REQUIRED,
            "Meta token does not authorize any WhatsApp Business Account. Reauthorize and select the existing account.",
            http_status=400,
        )

    portfolio_id = str(expected_business_portfolio_id or "").strip()
    if not portfolio_id:
        portfolio_id = derive_trusted_business_portfolio_id(debug_info) or ""
    if not portfolio_id:
        raise CoexistenceWabaResolutionError(
            WRONG_BUSINESS_OWNER,
            "Could not derive a trusted business portfolio from the Meta token.",
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
                "[CoexistenceResolve] waba unreadable id=%s code=%s",
                _redact_id(waba_id),
                waba_resp.get("code"),
            )
            continue
        waba_data = waba_resp["data"]
        owner_info = waba_data.get("owner_business_info") or {}
        owner_id = str(owner_info.get("id") or owner_info.get("business_id") or "").strip()
        if not owner_id:
            logger.warning(
                "[CoexistenceResolve] missing owner id=%s",
                _redact_id(waba_id),
            )
            continue
        if owner_id != portfolio_id:
            continue

        phones_resp = await _graph_get(
            graph_base,
            token,
            f"{waba_id}/phone_numbers",
            "id,display_phone_number,verified_name,code_verification_status",
        )
        if not phones_resp.get("ok"):
            logger.warning(
                "[CoexistenceResolve] phones unreadable id=%s code=%s",
                _redact_id(waba_id),
                phones_resp.get("code"),
            )
            continue
        for phone in phones_resp["data"].get("data") or []:
            display = str(phone.get("display_phone_number") or "")
            if not phones_match_exact_e164(expected_e164, display):
                continue
            phone_id = str(phone.get("id") or "").strip()
            if not phone_id:
                continue
            probe = await _graph_get(graph_base, token, phone_id, "id,display_phone_number")
            if not probe.get("ok"):
                continue
            canon = canonicalize_phone_e164(display)
            if not canon:
                continue
            verified.append(
                VerifiedCoexistenceAssets(
                    waba_id=str(waba_data.get("id") or waba_id),
                    phone_number_id=phone_id,
                    display_phone_number=display,
                    verified_name=phone.get("verified_name"),
                    ownership_type=waba_data.get("ownership_type"),
                    owner_business_id=owner_id,
                    canonical_phone_e164=canon,
                    trusted_business_portfolio_id=portfolio_id,
                )
            )

    if not verified:
        raise CoexistenceWabaResolutionError(
            WRONG_PHONE,
            "No Graph-verified phone on an authorized WABA matches the merchant phone.",
            http_status=400,
        )
    if len(verified) > 1:
        raise CoexistenceWabaResolutionError(
            WABA_RESOLUTION_CONFLICT,
            "Multiple WhatsApp Business Accounts match this phone. Contact support for reconciliation.",
            http_status=409,
        )
    return verified[0]


def assert_retry_claim_matches(
    claim: Dict[str, Any],
    verified: VerifiedCoexistenceAssets,
) -> None:
    """Re-prove stored claim on retry — metadata alone is not truth."""
    if str(claim.get("waba_id") or "") != verified.waba_id:
        raise CoexistenceWabaResolutionError(
            WABA_RESOLUTION_CONFLICT,
            "Stored coexistence claim does not match current Graph WABA proof.",
            http_status=409,
        )
    if str(claim.get("phone_number_id") or "") != verified.phone_number_id:
        raise CoexistenceWabaResolutionError(
            WABA_RESOLUTION_CONFLICT,
            "Stored coexistence claim does not match current Graph phone proof.",
            http_status=409,
        )
    stored_portfolio = str(claim.get("trusted_business_portfolio_id") or "").strip()
    if stored_portfolio and stored_portfolio != verified.trusted_business_portfolio_id:
        raise CoexistenceWabaResolutionError(
            WRONG_BUSINESS_OWNER,
            "Stored business portfolio does not match current Graph owner proof.",
            http_status=400,
        )
