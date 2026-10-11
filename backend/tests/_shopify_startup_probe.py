"""Test-only execution of the exact source startup callback's DB tasks.

Does not import/mount the whole API or execute any provider worker. The two
database tasks run unchanged and concurrently on the supplied disposable DB.
All other created coroutines are closed before execution and reported.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import types
from urllib.parse import urlsplit


def install_network_guard() -> None:
    """Also imported by the subprocess sitecustomize, before application code."""
    if getattr(sys, "_nahla_startup_guard", False):
        return
    sys._nahla_startup_guard = True
    sys._nahla_startup_network_attempts = []
    allowed = {"127.0.0.1", "localhost", "shopify-gate-disposable"}

    def audit(event, arguments):
        host = None
        port = None
        if event == "socket.getaddrinfo":
            host, port = arguments[:2]
        elif event == "socket.connect":
            address = arguments[1]
            if not isinstance(address, tuple):
                sys._nahla_startup_network_attempts.append("socket_non_tcp_refused")
                raise RuntimeError("startup_network_refused")
            host, port = address[:2]
        if host is not None:
            if isinstance(host, bytes):
                host = host.decode("ascii", errors="replace")
            if host not in allowed or int(port or 0) not in (5432, 5433):
                sys._nahla_startup_network_attempts.append("provider_network_refused")
                raise RuntimeError("startup_network_refused")
        if event == "subprocess.Popen":
            argv = arguments[1]
            root = Path(os.environ["NAHLA_STARTUP_ROOT"])
            accepted = (
                [sys.executable, str(root / "scripts/cleanup_salla_duplicates.py"), "--execute"],
                [sys.executable, "-m", "alembic", "upgrade", "0093"],
                [sys.executable, "-m", "alembic", "stamp", "0016"],
            )
            if list(argv) not in accepted:
                raise RuntimeError("startup_subprocess_refused")
            child_env = arguments[3]
            if child_env is None or child_env.get("DATABASE_URL") != os.environ["DATABASE_URL"]:
                raise RuntimeError("startup_subprocess_target_refused")
    sys.addaudithook(audit)


class _TaskBoundary:
    def __init__(self, dispatch_only: bool):
        self.dispatch_only = dispatch_only
        self.tasks = {}
        self.withheld = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def create_task(self, coroutine):
        name = coroutine.cr_code.co_name
        if not self.dispatch_only and name in ("_bootstrap_db_schema_bg", "_migrate_background"):
            task = asyncio.create_task(coroutine)
            self.tasks[name] = task
            return task
        self.withheld.append(name)
        coroutine.close()
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        return completed


def run(root: Path, *, dispatch_only: bool, provider_canary: bool) -> dict:
    target = urlsplit(os.environ["DATABASE_URL"])
    if (target.scheme != "postgresql" or target.hostname != "127.0.0.1"
            or target.port != 5433 or target.username != "nahla"
            or target.password != "nahla_password" or target.query
            or target.fragment or not target.path.startswith("/startup_")):
        raise RuntimeError("disposable_startup_target_required")
    assert os.environ.get("NAHLA_DISABLE_SCHEDULERS") == "1"
    install_network_guard()
    try:
        socket.getaddrinfo("startup-provider-canary.invalid", 443)
    except RuntimeError as exc:
        assert str(exc) == "startup_network_refused"
    else:
        raise AssertionError("network_guard_canary_not_refused")
    assert sys._nahla_startup_network_attempts == ["provider_network_refused"]
    sys._nahla_startup_network_attempts.clear()

    for entry in reversed((root, root / "backend", root / "database")):
        sys.path.insert(0, str(entry))
    boundary = _TaskBoundary(dispatch_only)
    registrations = []
    provider_calls = []
    config = types.ModuleType("core.config")
    config.WA_TOKEN = "synthetic-provider-canary" if provider_canary else ""
    config.WA_PHONE_ID = "synthetic-phone" if provider_canary else ""
    config.WA_BUSINESS_ACCOUNT_ID = ""
    config.META_GRAPH_API_VERSION = "v23.0"
    sys.modules["core.config"] = config
    httpx = types.ModuleType("httpx")

    def forbidden_client(*args, **kwargs):
        provider_calls.append("http_client")
        raise AssertionError("provider_client_must_not_execute")
    httpx.AsyncClient = forbidden_client
    sys.modules["httpx"] = httpx
    runtime = types.ModuleType("core.runtime_perf")

    async def run_runtime_heartbeat():
        raise AssertionError("heartbeat_must_be_withheld")

    def schedule_with_delay(factory, *, name, delay_seconds):
        registrations.append(name)
        raise AssertionError("disabled_scheduler_registered")
    runtime.run_runtime_heartbeat = run_runtime_heartbeat
    runtime.schedule_with_delay = schedule_with_delay
    sys.modules["core.runtime_perf"] = runtime
    background = types.ModuleType("core.background_tasks")
    background.get_background_task = lambda name: None
    background.register_background_task = lambda name, task: registrations.append(name)
    sys.modules["core.background_tasks"] = background

    records = []
    logger = logging.getLogger("startup-proof")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    logger.addHandler(Capture())
    raw = (root / "backend/main.py").read_bytes()
    parsed = ast.parse(raw.decode("utf-8"))
    startup = next(n for n in parsed.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_startup")
    startup_hash = hashlib.sha256(ast.dump(startup, include_attributes=False).encode()).hexdigest()
    startup.decorator_list = []
    masked = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == "_masked_db_target")
    namespace = {"os": os, "sys": sys, "logger": logger, "asyncio": boundary,
                 "_REPO_ROOT": str(root), "_DATABASE_DIR": str(root / "database"),
                 "__name__": "startup_proof_source", "__file__": str(root / "backend/main.py")}
    exec(compile(ast.Module(body=[masked, startup], type_ignores=[]), str(root / "backend/main.py"), "exec"), namespace)

    subprocesses = []
    original_run = subprocess.run

    def actual_run(argv, *args, **kwargs):
        completed = original_run(argv, *args, **kwargs)
        operation = "cleanup" if "--execute" in argv else " ".join(argv[3:])
        subprocesses.append({"operation": operation, "returncode": completed.returncode})
        return completed
    subprocess.run = actual_run

    async def exercise():
        await namespace["on_startup"]()
        if not dispatch_only:
            assert set(boundary.tasks) == {"_bootstrap_db_schema_bg", "_migrate_background"}
            await asyncio.wait_for(asyncio.gather(*boundary.tasks.values()), timeout=180)
    try:
        asyncio.run(exercise())
    finally:
        subprocess.run = original_run
        session = sys.modules.get("database.session")
        if session is not None:
            session.engine.dispose()
    errors = [message for message in records if any(token in message for token in (
        "Startup migration skipped statement", "Step B failed", "Step C FAILED",
        "TIMEOUT", "safe_alters] skipped", "safe_alters] timeout", "dispatch skipped",
        "Failed to schedule bootstrap", "Bootstrap FAILED"))]
    return {
        "main_sha256": hashlib.sha256(raw).hexdigest(), "startup_ast_sha256": startup_hash,
        "db_tasks": sorted(boundary.tasks), "withheld_tasks": sorted(boundary.withheld),
        "scheduler_registrations": registrations, "provider_client_calls": provider_calls,
        "provider_network_attempts": len(sys._nahla_startup_network_attempts),
        "network_canary_refused": True, "subprocesses": subprocesses,
        "sql_or_bootstrap_errors": errors,
        "safe_alters_ready": any("Database tables ready." in line for line in records),
        "bootstrap_ready": any("Step C: alembic upgrade 0093 OK" in line for line in records),
        "lifespan_complete": any("[BOOT/lifespan] complete (schedulers DISABLED)" in line for line in records),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dispatch-only", action="store_true")
    parser.add_argument("--provider-canary", action="store_true")
    args = parser.parse_args()
    print("STARTUP_REPORT=" + json.dumps(run(args.root.resolve(), dispatch_only=args.dispatch_only,
                                           provider_canary=args.provider_canary), sort_keys=True))
