"""``core.store_knowledge`` must not shadow the ``database`` package.

Production evidence (Phase 2.7A one-off operator job, deployment
903c0192): every AI usage ledger write failed with
``[AI_USAGE_LEDGER_WRITE_ERROR] ... err=ModuleNotFoundError``. The ledger
imports ``database.models`` lazily; ``core/store_knowledge.py`` had put its
own directory (``backend/core``) at ``sys.path[0]``, so in any process that
imports the catalog service before the ``database`` package is cached,
``import database`` resolves to ``backend/core/database.py`` (a module) and
``database.models`` no longer exists.

The API process masks this because ``main.py`` imports the ``database``
package first. Operator CLIs, workers and tests do not, so the check runs in
a fresh interpreter with the repo layout on ``PYTHONPATH`` exactly like the
production container (``/app:/app/backend:/app/database``).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import sys
import core.store_knowledge  # noqa: F401  — the catalog service under test

from database.models import AIUsageEvent  # noqa: F401  — the ledger's lazy import
from database.session import SessionLocal  # noqa: F401

core_dir = str(__import__("pathlib").Path(core.store_knowledge.__file__).resolve().parent)
assert core_dir not in sys.path, "backend/core must not be a top-level import root"

import secrets
assert "backend/core" not in str(secrets.__spec__.origin), (
    "stdlib secrets must not resolve to backend/core/secrets.py"
)
print("IMPORT_ISOLATION_OK")
"""


def test_store_knowledge_import_keeps_database_a_package(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]
        ),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'import_isolation.sqlite3'}",
        "NAHLA_SKIP_PREFLIGHT": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "IMPORT_ISOLATION_OK" in proc.stdout
