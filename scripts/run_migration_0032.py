"""Run Alembic migration 0032 directly."""
import os, sys, subprocess

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
db_dir    = os.path.join(repo_root, "database")

result = subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    cwd=db_dir,
    env=os.environ.copy(),
    capture_output=False,
)
sys.exit(result.returncode)
