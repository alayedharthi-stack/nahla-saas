"""Lock that the new catalog router is wired into the FastAPI app
so a future ``include_router`` refactor cannot silently drop the
``/merchant/catalog/*`` and ``/admin/catalog/*`` surfaces."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")


def test_main_module_registers_catalog_routers() -> None:
    """We don't boot the full FastAPI app (env-heavy). Instead we
    parse main.py text — the include_router lines for catalog must
    be present alongside the imports. This is brittle to formatting
    on purpose: if someone removes them by accident the diff is
    obvious."""
    main_src = (_BACKEND_ROOT / "main.py").read_text(encoding="utf-8")
    assert "_merchant_catalog_router" in main_src
    assert "_admin_catalog_router" in main_src
    assert "from routers.catalog" in main_src
    assert "app.include_router(_merchant_catalog_router)" in main_src
    assert "app.include_router(_admin_catalog_router)" in main_src


def test_expected_route_paths_exist_on_routers() -> None:
    """The dashboard hard-codes these paths — if they shift the
    UI breaks silently. Pin them here."""
    from routers.catalog import admin_router, merchant_router

    merchant_paths = {r.path for r in merchant_router.routes}
    admin_paths = {r.path for r in admin_router.routes}

    assert "/merchant/catalog/status" in merchant_paths
    assert "/merchant/catalog/config" in merchant_paths
    assert "/merchant/catalog/test-send" in merchant_paths

    assert "/admin/catalog/status" in admin_paths
    assert "/admin/catalog/audit" in admin_paths
    assert "/admin/catalog/config" in admin_paths
    assert "/admin/catalog/test-send" in admin_paths
