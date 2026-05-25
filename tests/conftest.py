"""
tests/conftest.py
─────────────────
Bootstrap sys.path so that every test file gets consistent module resolution:

  REPO_ROOT   – makes `database.models` importable as `from database.models import…`
  BACKEND_DIR – makes `backend` sub-packages importable without the `backend.` prefix
  DATABASE_DIR – makes `models` importable directly (used by billing, etc.)

We also force-import `observability` and `observability.event_logger` from
BACKEND_DIR *before* any test file is collected.  This is necessary because
`tests/test_ai_orchestrator_compat_shell.py` adds
`services/ai-orchestrator/` to sys.path[0] during collection, and that
service ships its own `observability/` package that lacks `event_logger`.
Once Python caches a module under a given name the wrong entry would shadow
the backend's logger for all subsequently-collected test files.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"

# Insert in reverse priority order so BACKEND_DIR ends up at position 0.
for p in reversed([str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

# Force-cache the *backend* copies of these packages before any test file
# that might inadvertently shadow them with a different observability package.
import observability        # noqa: E402, F401
import observability.event_logger  # noqa: E402, F401

# ── Prime ``database.models`` so it stays a package member even after a
# legacy ``from models import X`` runs (e.g. inside ``core.billing``).
# Without this, the first test that triggers ``brain.process()`` makes
# Python load ``database/models.py`` as the top-level ``models`` module
# (because ``DATABASE_DIR`` is on ``sys.path``), which then poisons
# ``from database.models import X`` for subsequent tests by demoting
# ``database`` to a non-package namespace. The fix pins both names in
# ``sys.modules`` up-front so neither resolution path can lose. Catches
# the production-grade bug surfaced by ``test_post_shipment_delivery_gate``
# when run after any pipeline-level relational test.
try:
    import database.models  # noqa: E402, F401
except Exception:  # noqa: BLE001
    # Tests that don't need this import still run — failure here only
    # means the conftest priming was a best-effort.
    pass
