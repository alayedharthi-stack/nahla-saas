"""
verify_refactor.py
──────────────────
Post-refactor verification script.
Checks that:
  1. All router modules import without errors
  2. The FastAPI app loads (routes register)
  3. No duplicate route paths exist
  4. Route count is reasonable (>= 40)

Run from the backend/ directory:
  cd backend && python verify_refactor.py
"""
import os
import sys
import importlib

# ── Path setup ────────────────────────────────────────────────────────────────
_BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
_DATABASE_DIR = os.path.abspath(os.path.join(_BACKEND_DIR, "..", "database"))
for _p in (_BACKEND_DIR, _DATABASE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = "[PASS]"
FAIL = "[FAIL]"

results = []

# ── 1. Import each router ─────────────────────────────────────────────────────
ROUTERS = [
    "routers.health",
    "routers.admin",
    "routers.auth",
    "routers.settings",
    "routers.templates",
    "routers.campaigns",
    "routers.automations",
    "routers.intelligence",
    "routers.ai_sales",
    "routers.billing",
    "routers.webhooks",
    "routers.handoff",
    "routers.store_integration",
    "routers.salla_oauth",
    "routers.system",
    "routers.widget",
    "routers.tracking",
]

print("\n" + "="*60)
print("  Nahla SaaS Backend - Refactor Verification")
print("="*60 + "\n")

print("1. Router import checks:")
all_passed = True
for mod_name in ROUTERS:
    try:
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "router"), f"{mod_name} has no 'router' attribute"
        print(f"   {PASS}  {mod_name}")
        results.append((mod_name, True, None))
    except Exception as exc:
        print(f"   {FAIL}  {mod_name} — {exc}")
        results.append((mod_name, False, str(exc)))
        all_passed = False

# ── 2. Load main app ──────────────────────────────────────────────────────────
print("\n2. FastAPI app load:")
try:
    import main as _main_mod
    app = _main_mod.app
    print(f"   {PASS}  main.py loaded — app={app}")
except Exception as exc:
    print(f"   {FAIL}  main.py failed to load — {exc}")
    sys.exit(1)

# ── 3. Route inventory ────────────────────────────────────────────────────────
print("\n3. Route inventory:")
routes = [
    r for r in app.routes
    if hasattr(r, "methods") and hasattr(r, "path")
]

paths_by_method = {}
for r in routes:
    for method in r.methods:  # type: ignore[attr-defined]
        key = (method, r.path)
        paths_by_method.setdefault(key, []).append(r.name)

duplicates = {k: v for k, v in paths_by_method.items() if len(v) > 1}
total_routes = len(paths_by_method)

print(f"   Total routes registered: {total_routes}")

# ── 4. Duplicate check ────────────────────────────────────────────────────────
print("\n4. Duplicate route check:")
if duplicates:
    print(f"   {FAIL}  {len(duplicates)} duplicate route(s) found:")
    for (method, path), names in duplicates.items():
        print(f"        {method} {path} → {names}")
    all_passed = False
else:
    print(f"   {PASS}  0 duplicate routes")

# ── 5. Minimum route count ────────────────────────────────────────────────────
print("\n5. Minimum route count (>=40):")
if total_routes >= 40:
    print(f"   {PASS}  {total_routes} routes registered")
else:
    print(f"   {FAIL}  Only {total_routes} routes — expected >= 40")
    all_passed = False

# ── 6. main.py size ───────────────────────────────────────────────────────────
print("\n6. main.py line count (<=300):")
main_path = os.path.join(_BACKEND_DIR, "main.py")
with open(main_path) as f:
    line_count = sum(1 for _ in f)
if line_count <= 300:
    print(f"   {PASS}  main.py is {line_count} lines")
else:
    print(f"   {FAIL}  main.py is {line_count} lines — target <= 300")
    all_passed = False

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
if all_passed:
    print(f"  {PASS}  ALL CHECKS PASSED")
else:
    failed = [r[0] for r in results if not r[1]]
    print(f"  {FAIL}  SOME CHECKS FAILED")
    if failed:
        print(f"  Failed routers: {', '.join(failed)}")
print("="*60 + "\n")

sys.exit(0 if all_passed else 1)
