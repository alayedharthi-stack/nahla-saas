import base64, hashlib, os, sys
import importlib.machinery
P = "[shopify-release-gate]"
R = "/app"
REL = "a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec"
IDS = {"RAILWAY_ENVIRONMENT_ID": "ede962ce-3042-4dae-94de-623837e83ed9", "RAILWAY_PROJECT_ID": "f0090862-0a40-4293-bd5d-e94df58762b5", "RAILWAY_SERVICE_ID": "686b36c5-a926-4e58-912a-5e9d13fbc2e7"}
LIBPQ = ["PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGPASSFILE", "PGCONNECT_TIMEOUT"]
FIXED = ["backend/__init__.py", "backend/payments/__init__.py", "backend/payments/models.py", "backend/services/shopify_catalog/config.py", "backend/services/shopify_connection/config.py", "backend/services/shopify_privacy/config.py", "database/alembic.ini", "requirements.txt", "scripts/operators/__init__.py", "scripts/operators/bootstrap_migration_contract.py", "scripts/operators/commerce_runtime_pilot_migration.py", "scripts/operators/commerce_runtime_pilot_migration_contract.py", "scripts/operators/shopify_release_gate.py", "scripts/operators/shopify_release_gate_contract.py", "scripts/operators/shopify_release_schema_expectations.json"]
ABSENT = ["scripts/__init__.py"]
N = 146
DIG = "31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174"
GSHA = "206eb16195a8afe3307f835c047553db6c3f917d9df8f5172a90cab3dee33157"
ASHA = "0c1ea28056cebd6cd0181f041b8ac79555df491e9955ce8d2da3c9700193470b"
ARGV = ["--root", "/app", "--phase", "S0", "--release-sha", "a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec", "--inventory-digest", "31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174", "--inventory-count", "146", "--approval-sha256", "0c1ea28056cebd6cd0181f041b8ac79555df491e9955ce8d2da3c9700193470b", "--expect-project", "f0090862-0a40-4293-bd5d-e94df58762b5", "--expect-environment", "ede962ce-3042-4dae-94de-623837e83ed9", "--expect-service", "686b36c5-a926-4e58-912a-5e9d13fbc2e7", "--expect-db", "postgres-ancu.railway.internal:5432/railway", "--expect-backup-service", "b77b3d27-47b0-4a3a-83fd-44def66a3a84", "--expect-backup-volume", "009bd0d5-85ed-4de4-99fc-94ea963c9d65"]


def fail(reason):
    print(P + " BOOTSTRAP FAIL reason=" + reason, flush=True)
    sys.exit(2)


if not os.path.isdir(os.path.join(R, "database")):
    fail("root_missing")
if os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip().lower() != REL:
    fail("release_sha_mismatch")
for name, value in IDS.items():
    if os.environ.get(name, "").strip() != value:
        fail("identity_mismatch")
if any(os.environ.get(name, "").strip() for name in LIBPQ):
    fail("libpq_environment_override")
for relative in ABSENT:
    if os.path.lexists(os.path.join(R, relative)):
        fail("initializer_unexpected")
paths = set(FIXED)
for dirpath, _dirnames, filenames in os.walk(os.path.join(R, "database")):
    for filename in filenames:
        if filename.endswith(".py"):
            paths.add(os.path.relpath(os.path.join(dirpath, filename), R).replace(os.sep, "/"))
if len(paths) != N:
    fail("inventory_count_mismatch")
digest = hashlib.sha256()
verified = {}
try:
    for relative in sorted(paths):
        absolute = os.path.abspath(os.path.join(R, relative))
        with open(absolute, "rb") as handle:
            data = handle.read()
        verified[absolute] = data
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
except OSError:
    fail("inventory_file_missing")
if digest.hexdigest() != DIG:
    fail("inventory_digest_mismatch")
gate_path = os.path.join(R, "scripts/operators/shopify_release_gate.py")
try:
    with open(gate_path, "rb") as handle:
        gate_sha = hashlib.sha256(handle.read()).hexdigest()
except OSError:
    fail("inventory_file_missing")
if gate_sha != GSHA:
    fail("gate_digest_mismatch")
payload = os.environ.get("NAHLA_SHOPIFY_RELEASE_APPROVAL_B64", "").strip()
if not payload:
    fail("approval_payload_missing")
try:
    raw = base64.b64decode(payload, validate=True)
except (ValueError, TypeError):
    fail("approval_payload_malformed")
if hashlib.sha256(raw).hexdigest() != ASHA:
    fail("approval_payload_pin_mismatch")
print(P + " BOOTSTRAP PASS gate_version=2 inventory_count=" + str(N), flush=True)

# Keep the verified bytes in this process. A source-only loader prevents a
# valid-looking, unpinned __pycache__ entry from replacing a verified .py file,
# including Alembic's direct SourceFileLoader use. The finder refuses other
# repository import origins (scripts.py, scripts.pyc, native extensions, etc.)
# before their loader runs. Installed dependencies retain their normal loader.
root = os.path.abspath(R)
directories = set([root])
for absolute in verified:
    parent = os.path.dirname(absolute)
    while parent != root and os.path.commonpath([root, parent]) == root:
        directories.add(parent)
        parent = os.path.dirname(parent)


def inside_repository(path):
    try:
        return os.path.commonpath([root, os.path.abspath(path)]) == root
    except ValueError:
        return False


original_get_code = importlib.machinery.SourceFileLoader.get_code


def verified_get_code(loader, fullname):
    absolute = os.path.abspath(loader.path)
    if inside_repository(absolute):
        data = verified.get(absolute)
        if data is None or not absolute.endswith(".py"):
            fail("repository_import_unverified")
        return compile(data, absolute, "exec", dont_inherit=True)
    return original_get_code(loader, fullname)


class RepositoryImportGate:
    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        owned_package = fullname.split(".", 1)[0] in ("scripts", "database")
        if owned_package and spec.origin and not inside_repository(spec.origin):
            fail("repository_import_unverified")
        if spec.origin and inside_repository(spec.origin):
            absolute = os.path.abspath(spec.origin)
            if absolute not in verified or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                fail("repository_import_unverified")
            return spec
        for location in spec.submodule_search_locations or ():
            if owned_package and not inside_repository(location):
                fail("repository_import_unverified")
            if inside_repository(location) and os.path.abspath(location) not in directories:
                fail("repository_import_unverified")
        return spec if spec.origin is None else None


importlib.machinery.SourceFileLoader.get_code = verified_get_code
sys.meta_path.insert(0, RepositoryImportGate())
sys.path.insert(0, root)
sys.argv = [gate_path] + ARGV
exec(compile(verified[os.path.abspath(gate_path)], gate_path, "exec"),
     {"__name__": "__main__", "__file__": gate_path})
