#!/usr/bin/env bash
# Starts a private PostgreSQL in this container and runs the evaluation once.
set -euo pipefail
if [ "${EVAL_CONFIRM:-}" != "RUN_OFFSEND_EVAL" ]; then
  echo '{"status": "idle", "reason": "EVAL_CONFIRM is not RUN_OFFSEND_EVAL"}'
  exit 0
fi
unset DATABASE_URL
export PGDATA=/tmp/evalpg
mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
su postgres -c "/usr/lib/postgresql/16/bin/initdb -D $PGDATA -A trust -U postgres -E UTF8 --locale=C" > /dev/null
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1 -p 5432' -w -l /tmp/evalpg.log start" > /dev/null
export NAHLA_EVAL_ADMIN_DSN="postgresql://postgres@127.0.0.1:5432/postgres"
cd /app
status=0
/opt/venv/bin/python -u ops/commerce_runtime_eval/run_eval.py 2>/tmp/eval.err || status=$?
echo "{\"status\": \"eval_exit\", \"code\": $status}"
grep -E "Traceback|Error" /tmp/eval.err | tail -5 || true
sleep 10
