"""
tests/test_single_automation_engine.py
──────────────────────────────────────
Guardrail: Nahla must have ONE automation execution path.

This test statically inspects the backend tree and enforces two invariants:

  1. Nothing calls `_log_autopilot_event(...)` anywhere. That helper wrote a
     fake `AutomationEvent(processed=True)` row that made the dashboard look
     like a message was sent while `provider_send_message` was never invoked.
     It was deleted; this test prevents it from being reintroduced via a
     helpful-looking utility module or a copy-paste from git history.

  2. `provider_send_message(...)` is only called from a tiny allow-list:
       - backend/core/automation_engine.py    (the canonical automation path)
       - backend/services/whatsapp_platform/  (the provider module itself)
       - backend/routers/whatsapp_webhook.py  (conversational replies to the
         customer's inbound message — NOT an automation)

     Any new call-site means someone has re-opened a parallel execution path
     that bypasses AutomationExecution accounting.

The test walks the AST instead of grepping raw text, so comments, docstrings,
and string literals mentioning these names in documentation do not trip it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

# Paths that are allowed to call `provider_send_message`. Anything else is a
# regression: it means we've grown a second sending code path outside the
# unified automation engine.
PROVIDER_SEND_ALLOWLIST = {
    # The one place automations are allowed to send from.
    BACKEND_DIR / "core" / "automation_engine.py",
    # The provider implementation itself — this is where the function lives.
    BACKEND_DIR / "services" / "whatsapp_platform" / "service.py",
    # Conversational replies to incoming WhatsApp messages. Not an automation;
    # this path is synchronous with the webhook and is part of the chat-agent
    # loop, not the Event → Engine → Execution pipeline. Still funnels through
    # `provider_send_message` for a single-provider abstraction.
    BACKEND_DIR / "routers" / "whatsapp_webhook.py",
    # Cash-on-Delivery confirmation template — sent synchronously the moment
    # POST /api/v1/ai-sales/create-order accepts a COD order. This is a
    # transactional, per-request send (not a background marketing automation):
    # routing it through the engine would require seeding a SmartAutomation
    # row, fabricating an AutomationEvent for every COD order, and waiting
    # for the next 60-second engine cycle before the customer sees the
    # confirmation. Keeping it on the synchronous send path preserves the
    # "create order → customer immediately gets the tap-to-confirm template"
    # UX. The send itself is logged via observability.event_logger so it
    # remains observable, and the COD funnel does not affect SmartAutomation
    # metrics (which is correct — it's not a SmartAutomation).
    BACKEND_DIR / "services" / "cod_confirmation.py",
}

# Directories we never scan (tests, migrations, cached bytecode, vendored).
SKIP_DIR_NAMES = {"__pycache__", "migrations", "tests", ".venv", "node_modules"}


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path


def _call_names(tree: ast.AST):
    """Yield ('simple_name', node) for every Call whose func is a plain Name or Attribute."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            yield func.id, node
        elif isinstance(func, ast.Attribute):
            yield func.attr, node


@pytest.mark.parametrize("py_file", list(_iter_python_files(BACKEND_DIR)))
def test_no_log_autopilot_event_callers(py_file: Path) -> None:
    """`_log_autopilot_event` was deleted. No file may call it."""
    source = py_file.read_text(encoding="utf-8")
    # Fast path: if the literal identifier never appears at all, skip the
    # AST parse. Keeps the test matrix cheap even on a large backend tree.
    if "_log_autopilot_event" not in source:
        return
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:  # pragma: no cover - would fail elsewhere anyway
        pytest.fail(f"{py_file} does not parse as Python")

    offenders = [
        node for name, node in _call_names(tree) if name == "_log_autopilot_event"
    ]
    assert not offenders, (
        f"{py_file} calls the deleted `_log_autopilot_event`. "
        f"Use `emit_automation_event(tenant_id, AutomationTrigger.<X>.value, "
        f"customer_id, payload)` instead — that routes through the real engine."
    )

    # Also reject `def _log_autopilot_event(...)` — the function itself must
    # stay deleted. (An import-only reference in a comment/docstring won't
    # survive the ast.walk check.)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_log_autopilot_event", (
                f"{py_file} redefines `_log_autopilot_event`. "
                "This helper simulated sends without calling the provider; it "
                "must not be revived."
            )


def test_provider_send_message_has_single_execution_path() -> None:
    """
    Only files in PROVIDER_SEND_ALLOWLIST may call `provider_send_message`.

    Every other call-site would mean a parallel execution path that bypasses
    AutomationExecution accounting and inflates metrics.
    """
    offenders: list[tuple[Path, int]] = []

    for py_file in _iter_python_files(BACKEND_DIR):
        if py_file in PROVIDER_SEND_ALLOWLIST:
            continue
        source = py_file.read_text(encoding="utf-8")
        if "provider_send_message" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for name, node in _call_names(tree):
            if name == "provider_send_message":
                offenders.append((py_file, node.lineno))

    assert not offenders, (
        "provider_send_message must only be called from the unified engine.\n"
        "Offending call-sites:\n  "
        + "\n  ".join(f"{p}:{ln}" for p, ln in offenders)
        + "\n\nIf you need to send a WhatsApp message as part of an automation, "
        "emit an AutomationEvent and let automation_engine handle it. If this "
        "is a genuinely new non-automation send path, update "
        "PROVIDER_SEND_ALLOWLIST with a comment explaining why."
    )


def test_legacy_autopilot_jobs_are_deleted() -> None:
    """
    The four fake-send jobs (`_job_order_status_update`, `_job_predictive_reorder`,
    `_job_abandoned_cart`, `_job_inactive_customers`) must not exist as
    function definitions anywhere in the backend.
    """
    banned = {
        "_job_order_status_update",
        "_job_predictive_reorder",
        "_job_abandoned_cart",
        "_job_inactive_customers",
    }
    offenders: list[tuple[Path, str, int]] = []

    for py_file in _iter_python_files(BACKEND_DIR):
        source = py_file.read_text(encoding="utf-8")
        if not any(name in source for name in banned):
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in banned:
                    offenders.append((py_file, node.name, node.lineno))

    assert not offenders, (
        "Legacy fake-send autopilot jobs reappeared:\n  "
        + "\n  ".join(f"{p}:{ln} def {n}" for p, n, ln in offenders)
        + "\nThese jobs never called provider_send_message; they only wrote "
        "AutomationEvent log rows. Replace them with emit_automation_event(...)."
    )
