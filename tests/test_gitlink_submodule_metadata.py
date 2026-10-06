"""Gitlinks must have local metadata for actions/checkout cleanup."""
from __future__ import annotations

import subprocess
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]


def test_gitlinks_have_submodule_metadata_for_checkout_cleanup() -> None:
    """This is local-only: it neither clones nor initializes submodules."""
    gitlinks = subprocess.run(
        ["git", "ls-files", "--stage"],
        cwd=_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    paths = {
        line.split("\t", 1)[1]
        for line in gitlinks
        if line.startswith("160000 ") and "\t" in line
    }
    if not paths:
        return

    mappings = subprocess.run(
        ["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        cwd=_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    configured_paths = {line.rsplit(" ", 1)[1] for line in mappings}
    assert paths <= configured_paths
    for path in paths:
        name = next(
            line.split(" ", 1)[0].removesuffix(".path")
            for line in mappings
            if line.endswith(f" {path}")
        )
        url = subprocess.run(
            ["git", "config", "--file", ".gitmodules", "--get", f"{name}.url"],
            cwd=_REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert url.startswith("https://")

    subprocess.run(
        ["git", "submodule", "foreach", "--recursive", "true"],
        cwd=_REPO,
        check=True,
    )
