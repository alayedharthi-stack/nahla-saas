set -euo pipefail
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${CLONE_REHEARSAL_NONCE:-}" = 20261013T0924-no-source
for key in SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGPASSWORD SRC_PGDATABASE; do
 test "${!key:-}" = __REVOKED_NO_SOURCE_ACCESS__
 unset "$key"
done
test -z "${DATABASE_URL:-}"
set -euo pipefail
umask 077
phase=tools
trap 'printf "NAHLA_RECOVERY {\"kind\":\"clone_runtime_install\",\"status\":\"BLOCKED\",\"phase\":\"%s\"}\n" "$phase"' ERR
test "$(uname -m)" = x86_64
test "${RAILWAY_DEPLOYMENT_ID:-}" = 36a2d33d-ab11-4666-b91c-a1a5ca7a1590
log=/recovery/operator/clone-runtime-install.private.log
timeout 180 apt-get install -y --no-install-recommends curl ca-certificates > "$log" 2>&1
mkdir -m 700 /tmp/nahla-clone-toolchain
cd /tmp/nahla-clone-toolchain
phase=uv_download
curl --fail --silent --show-error --location --max-time 90 --proto '=https' --tlsv1.2 https://github.com/astral-sh/uv/releases/download/0.13.0/uv-x86_64-unknown-linux-gnu.tar.gz -o uv.tar.gz >> "$log" 2>&1
curl --fail --silent --show-error --location --max-time 60 --proto '=https' --tlsv1.2 https://github.com/astral-sh/uv/releases/download/0.13.0/uv-x86_64-unknown-linux-gnu.tar.gz.sha256 -o uv.sha256 >> "$log" 2>&1
expected="$(awk '{print $1}' uv.sha256)"
test "$(sha256sum uv.tar.gz | cut -d' ' -f1)" = "$expected"
tar -xzf uv.tar.gz
export UV_PYTHON_INSTALL_DIR=/tmp/nahla-clone-python
export UV_CACHE_DIR=/tmp/nahla-clone-uv-cache
export UV_PYTHON_BIN_DIR=/tmp/nahla-clone-python-bin
export UV_NO_MODIFY_PATH=1
uv=/tmp/nahla-clone-toolchain/uv-x86_64-unknown-linux-gnu/uv
phase=python_install
timeout 180 "$uv" python install 3.11 >> "$log" 2>&1
"$uv" venv --python 3.11 /tmp/nahla-clone-venv >> "$log" 2>&1
phase=pinned_dependencies
timeout 180 "$uv" pip install --python /tmp/nahla-clone-venv/bin/python --index-url https://pypi.org/simple 'alembic==1.13.1' 'sqlalchemy==2.0.30' 'psycopg2-binary==2.9.9' >> "$log" 2>&1
/tmp/nahla-clone-venv/bin/python -I -B -c 'import importlib.metadata,json,sys; print("NAHLA_RECOVERY "+json.dumps({"kind":"clone_runtime_install","status":"PASS","python":sys.version.split()[0],"pins":{n:importlib.metadata.version(n) for n in ("alembic","sqlalchemy","psycopg2-binary")},"ephemeral_toolchain":True}))'
