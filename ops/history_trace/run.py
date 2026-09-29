"""The trace service's saved start command names this path; run the current trace."""
import pathlib
import runpy

runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "readonly_trace" / "run.py"),
               run_name="__main__")
