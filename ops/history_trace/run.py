"""The trace service's saved start command names this path; run the current one-shot job.

Now: the owner-approved COD auto-cancel correction (dry run unless the apply
token and the approved plan digest are both set; see that module)."""
import pathlib
import runpy

runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "cod_auto_cancel_revert" / "run.py"),
               run_name="__main__")
