#!/usr/bin/env bash
# Starts a private PostgreSQL in this container and runs the evaluation once.
set -euo pipefail
if [ "${EVAL_CONFIRM:-}" = "RUN_KEY_PROBE" ]; then
  /opt/venv/bin/python -u /app/ops/commerce_runtime_eval/key_probe.py || true
  sleep 10
  exit 0
fi
case "${EVAL_CONFIRM:-}" in
  RUN_OFFSEND_EVAL) SCRIPT=run_eval.py ;;
  *) echo '{"status": "idle", "reason": "EVAL_CONFIRM names no run"}'; exit 0 ;;
esac
# The evaluation key shares its organization's spend limit with production:
# no run without its own hard cap (enforced again inside the script).
case "${EVAL_BUDGET_USD:-}" in
  ''|*[!0-9.]*) echo '{"status": "refused", "reason": "EVAL_BUDGET_USD must be a positive number"}'; exit 0 ;;
esac
unset DATABASE_URL
export PGDATA=/tmp/evalpg
mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
su postgres -c "/usr/lib/postgresql/16/bin/initdb -D $PGDATA -A trust -U postgres -E UTF8 --locale=C" > /dev/null
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1 -p 5432' -w -l /tmp/evalpg.log start" > /dev/null
export NAHLA_EVAL_ADMIN_DSN="postgresql://postgres@127.0.0.1:5432/postgres"
cd /app
status=0
/opt/venv/bin/python -u "ops/commerce_runtime_eval/$SCRIPT" 2>/tmp/eval.err || status=$?
echo "{\"status\": \"eval_exit\", \"code\": $status}"
grep -E "Traceback|Error" /tmp/eval.err | tail -5 || true
sleep 10
