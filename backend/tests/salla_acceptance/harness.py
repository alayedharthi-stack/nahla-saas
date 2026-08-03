"""
Acceptance harness for Salla Merchant AI E2E tests.

Layer 1: direct ``CommerceToolRuntime`` + permission loader + handoff scrub helpers.
Layer 2: optional multi-turn via brain pipeline with mocked LLM (documented in metadata).

No real WhatsApp sends — ``OutboundCapture`` patches provider dispatch.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: E402
from modules.ai.commerce.permission_loader import load_tenant_commerce_permissions  # noqa: E402
from modules.ai.commerce.runtime import CommerceToolRuntime, ToolExecutionResult  # noqa: E402
from modules.ai.order_flow_v2.enforcement import resolve_order_flow_v2_operational  # noqa: E402

# Module-level acceptance report rows (appended by tests).
ACCEPTANCE_RESULTS: List[Dict[str, Any]] = []

RESULTS_PATH = _HERE / "ACCEPTANCE_RESULTS.json"

# Closed A–K authorization matrix from Salla Merchant AI acceptance authorization.
AUTHORIZED_MATRIX_IDS: Tuple[str, ...] = (
    "A1", "A2", "A3", "A4",
    "B1", "B2", "B3", "B4", "B5", "B6",
    "C1", "C2", "C3", "C4",
    "D1", "D2", "D3", "D4", "D5", "D6",
    "E1",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7",
    "G1", "G2", "G3", "G4", "G5",
    "H1", "H2", "H3", "H4", "H5",
    "I1", "I2", "I3", "I4", "I5", "I6",
    "J1",
    "K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10",
)

# Map harness sub-IDs to canonical matrix IDs where they extend a cell.
_MATRIX_ID_ALIASES: Dict[str, str] = {
    "C2b": "C1",
    "H1b": "H5",
}


@dataclass
class OutboundRecord:
    payload: Dict[str, Any]
    path: str = ""
    scrubbed: bool = False


class OutboundCapture:
    """Records outbound WhatsApp payloads; blocks real provider calls."""

    def __init__(self) -> None:
        self.sent: List[OutboundRecord] = []
        self.real_send_attempted = False

    async def _capture_post(self, *_args, **kwargs) -> Dict[str, Any]:
        self.real_send_attempted = True
        payload = dict(kwargs.get("json") or {})
        self.sent.append(OutboundRecord(payload=payload, path="provider_post_with_context"))
        return {"messages": [{"id": f"wamid.acceptance.{len(self.sent)}"}]}

    async def _capture_send(self, *args, **kwargs) -> Tuple[Dict[str, Any], Any]:
        self.real_send_attempted = True
        payload = dict(kwargs.get("payload") or kwargs.get("json") or {})
        if not payload and len(args) >= 2 and isinstance(args[1], dict):
            payload = dict(args[1])
        self.sent.append(OutboundRecord(payload=payload, path="provider_send_message"))
        return {"messages": [{"id": f"wamid.acceptance.{len(self.sent)}"}]}, MagicMock()

    @contextmanager
    def patch(self) -> Iterator["OutboundCapture"]:
        with patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=self._capture_post,
        ), patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=self._capture_send,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ):
            yield self

    def send_count(self) -> int:
        return len(self.sent)


@dataclass
class AcceptanceTurnResult:
    scenario_id: str
    inbound: str
    tenant_id: int
    conversation_id: Optional[int] = None
    intent: str = ""
    decision: str = ""
    tools: List[str] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    final_reply: str = ""
    guards: Dict[str, Any] = field(default_factory=dict)
    outbound: List[Dict[str, Any]] = field(default_factory=list)
    send_count: int = 0
    severity: str = "major"
    outcome: str = "unknown"
    layer: str = "layer1"
    llm_mocked: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() else asyncio.run(coro)


class AcceptanceHarness:
    """Run deterministic acceptance turns against synthetic tenants."""

    def __init__(self, db: Any, world: Any) -> None:
        self.db = db
        self.world = world
        self.outbound = OutboundCapture()

    def _runtime(
        self,
        tenant_id: int,
        *,
        customer_phone: str = "",
        customer_id: Optional[int] = None,
    ) -> CommerceToolRuntime:
        load = load_tenant_commerce_permissions(self.db, tenant_id)
        return CommerceToolRuntime(
            self.db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            customer_id=customer_id,
            permissions=load.permissions,
            permission_source=load.source,
        )

    async def execute_tool(
        self,
        tenant_id: int,
        tool_name: str,
        payload: Dict[str, Any],
        *,
        customer_phone: str = "",
        customer_id: Optional[int] = None,
    ) -> ToolExecutionResult:
        runtime = self._runtime(tenant_id, customer_phone=customer_phone, customer_id=customer_id)
        return await runtime.execute(tool_name, payload)

    async def layer1_turn(
        self,
        *,
        scenario_id: str,
        tenant_id: int,
        inbound: str,
        tool_name: str,
        tool_payload: Dict[str, Any],
        customer_phone: str = "",
        customer_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        severity: str = "critical",
        expected_predicate: Optional[Callable[[ToolExecutionResult], bool]] = None,
    ) -> AcceptanceTurnResult:
        result = await self.execute_tool(
            tenant_id,
            tool_name,
            tool_payload,
            customer_phone=customer_phone,
            customer_id=customer_id,
        )
        passed = expected_predicate(result) if expected_predicate else result.ok
        turn = AcceptanceTurnResult(
            scenario_id=scenario_id,
            inbound=inbound,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            tools=[tool_name],
            tool_results=[{"ok": result.ok, "error": result.error, "payload_keys": list((result.payload or {}).keys())}],
            sources=[result.audit.get("lookup", "") or result.audit.get("permission_source", "") or "runtime"],
            severity=severity,
            outcome="pass" if passed else "fail",
            layer="layer1",
            evidence={
                "audit": dict(result.audit or {}),
                "error": result.error,
                "payload_sample": _safe_sample(result.payload),
            },
        )
        return turn

    def scrub_outbound(
        self,
        body: str,
        *,
        tenant_id: int,
        recipient: str,
        handoff_truth_active: bool,
    ) -> Tuple[Dict[str, Any], bool]:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": body},
        }
        truth = MagicMock(active=handoff_truth_active, source="test", verify_failed=False)
        with patch("core.handoff_truth.resolve_handoff_truth_active", return_value=truth):
            out, scrubbed = sanitize_outbound_payload(
                payload,
                tenant_id=tenant_id,
                recipient=recipient,
                db=self.db,
            )
        return out, scrubbed

    def resolve_ofv2(
        self,
        tenant_id: int,
        *,
        conversation: Any,
        customer_phone: str = "966500100001",
        monkeypatch_env: Optional[Dict[str, str]] = None,
    ) -> Any:
        if monkeypatch_env:
            for key, val in monkeypatch_env.items():
                os.environ[key] = val
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=MagicMock(disabled=False, reason=""),
        ), patch("core.billing.has_billing_access", return_value=True):
            return resolve_order_flow_v2_operational(
                self.db,
                tenant_id=tenant_id,
                customer_phone=customer_phone,
                conversation=conversation,
            )

    def simulate_outbound_send(
        self,
        body: str,
        *,
        tenant_id: int,
        recipient: str,
        dedup: bool = True,
    ) -> int:
        """Send through capture + optional dedup; returns send count after attempt."""
        from core.outbound_dedup import check_outbound_send, clear_outbound_dedup, record_outbound_result

        if dedup:
            clear_outbound_dedup()
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": body},
        }
        sends = 0
        with self.outbound.patch():
            for _ in range(2 if dedup else 1):
                res = check_outbound_send(tenant_id=tenant_id, recipient=recipient, payload=payload)
                if not res.skip:
                    sends += 1
                    record_outbound_result(
                        tenant_id=tenant_id,
                        recipient=recipient,
                        payload=payload,
                        wamid=f"wamid.dedup.{sends}",
                        succeeded=True,
                    )
        return sends


def _safe_sample(payload: Any, *, max_len: int = 400) -> Any:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)[:max_len]
    return text[:max_len] if len(text) > max_len else payload


def record_acceptance(
    *,
    scenario_id: str,
    messages: Sequence[str],
    tenant: str,
    expected: str,
    actual: str,
    tools: Sequence[str],
    sources: Sequence[str],
    result: str,
    severity: str,
    evidence: Optional[Dict[str, Any]] = None,
    layer: str = "layer1",
    llm_mocked: bool = False,
) -> Dict[str, Any]:
    row = {
        "id": scenario_id,
        "messages": list(messages),
        "tenant": tenant,
        "expected": expected,
        "actual": actual,
        "tools": list(tools),
        "sources": list(sources),
        "result": result,
        "severity": severity,
        "evidence": evidence or {},
        "layer": layer,
        "llm_mocked": llm_mocked,
        "quality_score": "deferred_layer3_human" if llm_mocked else "n/a_layer1_deterministic",
    }
    ACCEPTANCE_RESULTS.append(row)
    return row


def record_turn(turn: AcceptanceTurnResult, *, tenant_name: str, expected: str) -> Dict[str, Any]:
    return record_acceptance(
        scenario_id=turn.scenario_id,
        messages=[turn.inbound],
        tenant=tenant_name,
        expected=expected,
        actual=turn.outcome,
        tools=turn.tools,
        sources=turn.sources,
        result=turn.outcome,
        severity=turn.severity,
        evidence=turn.evidence,
        layer=turn.layer,
        llm_mocked=turn.llm_mocked,
    )


def _normalize_matrix_id(scenario_id: str) -> str:
    sid = str(scenario_id or "").strip()
    return _MATRIX_ID_ALIASES.get(sid, sid)


def _matrix_coverage(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[str], float]:
    covered: set[str] = set()
    for row in rows:
        canonical = _normalize_matrix_id(str(row.get("id") or ""))
        if canonical in AUTHORIZED_MATRIX_IDS:
            covered.add(canonical)
    gaps = [mid for mid in AUTHORIZED_MATRIX_IDS if mid not in covered]
    pct = round((len(covered) / len(AUTHORIZED_MATRIX_IDS)) * 100, 1) if AUTHORIZED_MATRIX_IDS else 0.0
    return sorted(covered), gaps, pct


def write_acceptance_report(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or RESULTS_PATH
    rows = list(ACCEPTANCE_RESULTS)
    passed = sum(1 for r in rows if r.get("result") == "pass")
    failed = sum(1 for r in rows if r.get("result") == "fail")
    critical_failures = [
        r["id"] for r in rows if r.get("result") == "fail" and r.get("severity") == "critical"
    ]
    major_failures = [
        r["id"] for r in rows if r.get("result") == "fail" and r.get("severity") == "major"
    ]
    minor_failures = [
        r["id"] for r in rows if r.get("result") == "fail" and r.get("severity") == "minor"
    ]
    matrix_covered, matrix_gaps, matrix_pct = _matrix_coverage(rows)
    layer1_green = len(critical_failures) == 0 and len(major_failures) == 0
    summary = {
        "scenarios_total": len(rows),
        "passed": passed,
        "failed": failed,
        "critical_failures": critical_failures,
        "major_failures": major_failures,
        "minor_failures": minor_failures,
        "layers_completed": ["layer1"],
        "layers_not_run": [
            "layer2_simulated_whatsapp_e2e",
            "layer3_human_conversation_quality",
            "layer4_live_whatsapp",
        ],
        "matrix_ids_authorized": len(AUTHORIZED_MATRIX_IDS),
        "matrix_ids_covered": matrix_covered,
        "matrix_coverage_pct_estimate": matrix_pct,
        "gaps": matrix_gaps,
        "coverage_note": (
            "Layer 1 deterministic tool/state probes only. "
            f"{len(matrix_covered)}/{len(AUTHORIZED_MATRIX_IDS)} authorized A–K matrix IDs covered. "
            "Layer 2 simulated WhatsApp E2E and Layer 3 human conversation review not run. "
            "Readiness flags remain false until those layers pass acceptance criteria."
        ),
        "ready_for_internal_live_test": False,
        "ready_for_tenant1_pilot": False,
        "layer1_all_green": layer1_green,
        "blocking_defects": critical_failures + major_failures,
        "recommended_fix_packages": _suggest_fix_packages(rows),
        "recommended_next_action": _next_action(critical_failures, major_failures, matrix_gaps),
        "results": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def print_console_summary(summary: Dict[str, Any]) -> None:
    print(
        f"\n=== Salla Merchant AI Acceptance ===\n"
        f"Total: {summary['scenarios_total']} | "
        f"Passed: {summary['passed']} | Failed: {summary['failed']}\n"
        f"Critical failures: {summary['critical_failures'] or 'none'}\n"
        f"Major failures: {summary['major_failures'] or 'none'}\n"
        f"Report: {RESULTS_PATH}\n"
    )


def _suggest_fix_packages(rows: List[Dict[str, Any]]) -> List[str]:
    packages: List[str] = []
    for row in rows:
        if row.get("result") != "fail":
            continue
        sid = str(row.get("id") or "")
        if sid.startswith("I"):
            packages.append("fix-tenant-isolation")
        elif sid.startswith("F"):
            packages.append("fix-order-privacy")
        elif sid.startswith("G"):
            packages.append("fix-handoff-truth")
        elif sid.startswith("B"):
            packages.append("fix-catalog-stock-truth")
        elif sid.startswith("C"):
            packages.append("fix-discount-truth")
        elif sid.startswith("OFV2"):
            packages.append("fix-ofv2-rollout")
        elif sid.startswith("K"):
            packages.append("fix-commerce-permissions")
    return sorted(set(packages))


def _next_action(critical: List[str], major: List[str], gaps: List[str]) -> str:
    if critical:
        return "Fix critical failures before internal live test."
    if major:
        return "Review major failures; rerun acceptance suite."
    if gaps:
        return (
            "Layer 1 green on implemented probes; expand matrix coverage and run "
            "Layer 2 simulated E2E + Layer 3 human review before live test."
        )
    return "Run Layer 2 simulated WhatsApp E2E, then Layer 3 human conversation quality review."


__all__ = [
    "ACCEPTANCE_RESULTS",
    "AUTHORIZED_MATRIX_IDS",
    "AcceptanceHarness",
    "AcceptanceTurnResult",
    "OutboundCapture",
    "record_acceptance",
    "record_turn",
    "write_acceptance_report",
    "print_console_summary",
    "RESULTS_PATH",
]
