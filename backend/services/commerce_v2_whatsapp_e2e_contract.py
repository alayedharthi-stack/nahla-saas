"""Fail-closed contract for Work-operated real WhatsApp Commerce V2 acceptance."""
from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping


CONTRACT_VERSION = "commerce_v2_real_whatsapp_e2e_v1"
ONLY_LIVE_TENANT_ID = 1
ACCOUNT_ALIASES = ("A", "B", "C")
SERVICE_TIERS = ("auto", "fast")
READ_ONLY_TOOLS = frozenset(
    {
        "search_products",
        "get_product_details",
        "search_merchant_knowledge",
        "search_product_knowledge",
        "resolve_customer_order",
        "get_order_details",
        "get_order_shipment",
    }
)
WRITE_TOOL_MARKERS = frozenset(
    {"create", "update", "cancel", "refund", "pay", "confirm", "mutate", "write"}
)
_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


@dataclass(frozen=True)
class CorpusTurn:
    case_id: str
    account_alias: str
    account_turn_index: int
    wave_id: int
    sequence_id: str
    sequence_position: int
    category: str
    inbound_text: str
    expected_tools: tuple[str, ...]
    expected_outcome: str
    requested_service_tier: str
    common_turn: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _assert_read_only_tools(values: list[Any]) -> tuple[str, ...]:
    tools = tuple(str(value) for value in values)
    if any(tool not in READ_ONLY_TOOLS for tool in tools):
        raise ValueError("corpus_contains_unknown_or_write_tool")
    if any(any(marker in tool.lower() for marker in WRITE_TOOL_MARKERS) for tool in tools):
        raise ValueError("corpus_contains_write_tool")
    return tools


def validate_test_owned_accounts(accounts: Mapping[str, str]) -> dict[str, str]:
    """Validate three distinct controlled numbers, returning aliases to fingerprints only."""
    if set(accounts) != set(ACCOUNT_ALIASES):
        raise ValueError("three_test_account_aliases_required")
    normalized: dict[str, str] = {}
    for alias, raw in accounts.items():
        phone = re.sub(r"[\s()-]", "", str(raw or ""))
        if not _PHONE_RE.fullmatch(phone):
            raise ValueError(f"test_account_invalid:{alias}")
        normalized[alias] = phone.lstrip("+")
    if len(set(normalized.values())) != len(ACCOUNT_ALIASES):
        raise ValueError("test_accounts_must_be_distinct")
    # Callers retain the numbers in their local driver. Evidence uses aliases only.
    return {alias: f"confirmed:{len(value)}digits" for alias, value in normalized.items()}


def load_corpus(path: Path, *, seed: int) -> list[CorpusTurn]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("corpus_contract_version_invalid")
    if payload.get("tenant_id") != ONLY_LIVE_TENANT_ID:
        raise ValueError("corpus_tenant_must_be_1")
    rng = random.Random(int(seed))
    by_account: dict[str, list[dict[str, Any]]] = {}
    for account in payload.get("accounts") or []:
        alias = str(account.get("alias") or "")
        if alias not in ACCOUNT_ALIASES or alias in by_account:
            raise ValueError("corpus_account_alias_invalid")
        blocks: list[dict[str, Any]] = []
        for segment_index, segment in enumerate(account.get("segments") or [], start=1):
            repeat = int(segment.get("repeat") or 0)
            steps = list(segment.get("steps") or [])
            if repeat <= 0 or not steps:
                raise ValueError("corpus_segment_invalid")
            for repetition in range(1, repeat + 1):
                sequence_id = f"{alias}-S{segment_index:02d}-R{repetition:02d}"
                sequence: list[dict[str, Any]] = []
                for position, step in enumerate(steps, start=1):
                    variants = [str(value).strip() for value in step.get("variants") or []]
                    variants = [value for value in variants if value]
                    if not variants:
                        raise ValueError("corpus_turn_variants_missing")
                    sequence.append(
                        {
                            "sequence_id": sequence_id,
                            "sequence_position": position,
                            "category": str(segment.get("category") or "").strip(),
                            "inbound_text": rng.choice(variants),
                            "expected_tools": _assert_read_only_tools(
                                list(step.get("expected_tools") or [])
                            ),
                            "expected_outcome": str(
                                step.get("expected_outcome") or "grounded_reply"
                            ),
                            "common_turn": bool(step.get("common_turn", True)),
                        }
                    )
                blocks.append({"sequence": sequence})
        rng.shuffle(blocks)
        by_account[alias] = [turn for block in blocks for turn in block["sequence"]]

    if set(by_account) != set(ACCOUNT_ALIASES):
        raise ValueError("corpus_requires_accounts_a_b_c")
    if any(len(turns) != 60 for turns in by_account.values()):
        raise ValueError("corpus_requires_60_turns_per_account")

    corpus: list[CorpusTurn] = []
    for wave_index in range(60):
        for alias_index, alias in enumerate(ACCOUNT_ALIASES):
            raw = by_account[alias][wave_index]
            # Stratified 50/50 AUTO vs FAST within every account.
            tier = SERVICE_TIERS[(wave_index + alias_index + seed) % 2]
            corpus.append(
                CorpusTurn(
                    case_id=f"{alias}-{wave_index + 1:03d}",
                    account_alias=alias,
                    account_turn_index=wave_index + 1,
                    wave_id=wave_index + 1,
                    requested_service_tier=tier,
                    **raw,
                )
            )
    if len(corpus) != 180 or len({turn.case_id for turn in corpus}) != 180:
        raise ValueError("corpus_requires_180_unique_turns")
    return corpus


def render_controlled_test_data(
    corpus: list[CorpusTurn],
    *,
    test_order_number: str,
) -> list[CorpusTurn]:
    order_number = str(test_order_number or "").strip()
    if not order_number or len(order_number) > 64:
        raise ValueError("controlled_test_order_number_required")
    return [
        replace(
            turn,
            inbound_text=turn.inbound_text.replace("{TEST_ORDER_NUMBER}", order_number),
        )
        for turn in corpus
    ]


__all__ = [
    "ACCOUNT_ALIASES",
    "CONTRACT_VERSION",
    "CorpusTurn",
    "ONLY_LIVE_TENANT_ID",
    "READ_ONLY_TOOLS",
    "load_corpus",
    "render_controlled_test_data",
    "validate_test_owned_accounts",
]
