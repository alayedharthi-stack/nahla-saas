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
test "${RAILWAY_DEPLOYMENT_ID:-}" = 36a2d33d-ab11-4666-b91c-a1a5ca7a1590
base=/recovery/clone-rehearsal-20261013
test ! -e "$base/rehearsal-result.json"
test "$(sha256sum "$base/clone_rehearsal.py" | cut -d' ' -f1)" = 0167efff6c69535fd50bd933dea324f7fbdcea268ef1b461020f17334b15bf39
timeout --signal=TERM --kill-after=15s 900s /tmp/nahla-clone-venv/bin/python -I -B "$base/clone_rehearsal.py" 2> "$base/rehearsal.stderr.private"
