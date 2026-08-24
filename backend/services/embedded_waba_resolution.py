"""Resolve WABA id during Embedded Signup / coexistence exchange."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

import httpx

logger = logging.getLogger("nahla.embedded_waba_resolution")


def _digits(value: Optional[str]) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _waba_ids_from_debug(debug_info: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for scope in debug_info.get("granular_scopes", []) or []:
        if scope.get("scope") != "whatsapp_business_management":
            continue
        for raw in scope.get("target_ids", []) or []:
            token = str(raw or "").strip()
            if token and token not in out:
                out.append(token)
    return out


async def _can_read_waba(graph_base: str, token: str, waba_id: str) -> bool:
    try:
        resp = httpx.get(
            f"{graph_base}/{waba_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id"},
            timeout=15,
        )
        data = resp.json()
        return resp.status_code == 200 and "error" not in data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[EmbeddedWABA] probe failed waba=%s err=%s", waba_id, exc)
        return False


async def _phones_for_waba(graph_base: str, token: str, waba_id: str) -> List[dict]:
    try:
        resp = httpx.get(
            f"{graph_base}/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "fields": "id,display_phone_number,verified_name,code_verification_status",
            },
            timeout=15,
        )
        data = resp.json()
        if "error" in data:
            return []
        return list(data.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[EmbeddedWABA] phone list failed waba=%s err=%s", waba_id, exc)
        return []


async def resolve_embedded_waba_id(
    graph_base: str,
    token: str,
  debug_info: Dict[str, Any],
    *,
    hinted_waba_id: Optional[str] = None,
    hinted_phone_number_id: Optional[str] = None,
    known_phone_number: Optional[str] = None,
) -> str:
    """Prefer session hints and existing phone matches before first granular scope."""
    hinted_waba = str(hinted_waba_id or "").strip()
    hinted_phone = str(hinted_phone_number_id or "").strip()
    known_phone_digits = _digits(known_phone_number)
    authorized = _waba_ids_from_debug(debug_info)

    if hinted_waba:
        if await _can_read_waba(graph_base, token, hinted_waba):
            logger.info("[EmbeddedWABA] adopted session waba hint=%s", hinted_waba)
            return hinted_waba
        if hinted_waba in authorized:
            logger.info("[EmbeddedWABA] adopted authorized waba hint=%s", hinted_waba)
            return hinted_waba

    if hinted_phone:
        for waba_id in authorized:
            phones = await _phones_for_waba(graph_base, token, waba_id)
            if any(str(p.get("id") or "") == hinted_phone for p in phones):
                logger.info(
                    "[EmbeddedWABA] matched phone hint waba=%s phone_id=%s",
                    waba_id,
                    hinted_phone,
                )
                return waba_id

    if known_phone_digits:
        for waba_id in authorized:
            phones = await _phones_for_waba(graph_base, token, waba_id)
            for phone in phones:
                display = _digits(phone.get("display_phone_number"))
                if display and (
                    display == known_phone_digits
                    or display.endswith(known_phone_digits)
                    or known_phone_digits.endswith(display)
                ):
                    logger.info(
                        "[EmbeddedWABA] matched known phone waba=%s phone=%s",
                        waba_id,
                        phone.get("display_phone_number"),
                    )
                    return waba_id

    if authorized:
        logger.info("[EmbeddedWABA] fallback first authorized waba=%s", authorized[0])
        return authorized[0]

    raise ValueError("no_waba_resolved")
