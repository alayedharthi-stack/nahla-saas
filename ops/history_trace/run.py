"""The trace service's saved start command names this path; run the job its token names.

READ_ONLY_STORE_STATUS_AND_SWEEP_V1 -> the read-only store-status + sweep check
(ops/readonly_trace); any other value -> the COD auto-cancel correction, which
itself stays idle unless its own dry-run or apply token is set."""
import os
import pathlib
import runpy

_OPS = pathlib.Path(__file__).resolve().parents[1]
_JOB = ("readonly_trace" if os.environ.get("NAHLA_HISTORY_TRACE_CONFIRM") == "READ_ONLY_STORE_STATUS_AND_SWEEP_V1"
        else "cod_auto_cancel_revert")
runpy.run_path(str(_OPS / _JOB / "run.py"), run_name="__main__")
