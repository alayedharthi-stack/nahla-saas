"""Smoke test: backend.main imports and compiles."""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_backend_main_py_compile() -> None:
    subprocess.check_call([sys.executable, "-m", "py_compile", str(_REPO / "backend" / "main.py")])


def test_backend_main_import() -> None:
    module = importlib.import_module("backend.main")
    assert hasattr(module, "app")
