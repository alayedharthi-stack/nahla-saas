"""Pinned campaign-35 adoption runner. Dry-run unless exact scope is armed."""
import os
import runpy
import sys

path = "scripts/operators/campaign_adopt_marketing_wait.py"
sys.argv = [path, "--tenant-id", "33", "--campaign-id", "35",
            "--expected-paused-at", "2026-09-25T13:02:11.329622"]
if os.environ.get("NAHLA_CAMPAIGN_ADOPT_APPLY") == "tenant33-campaign35-20260925":
    sys.argv.append("--apply")
runpy.run_path(path, run_name="__main__")
