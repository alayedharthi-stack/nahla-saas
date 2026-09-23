"""The scope guard's owner-exception mechanism: what it grants, and what it refuses.

The guard in ``test_trusted_context_shadow_wireup`` keeps one agent out of
another's files. An owner can lift it for a named file and a named task — and
the whole value of that depends on a branch being unable to grant itself the
lift it wants. So the registry is read from BASE with ``git show``, never from
the working tree, and these cases prove the properties that makes the grant
worth anything: BASE-only, exact-path-only, expiring, and fail-closed.

Same convention as ``intelligence_exceptions.json`` beside it, and the rule
``AGENTS.md`` states: owner exceptions cannot be created in the same PR as the
runtime change.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(_HERE).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.test_trusted_context_shadow_wireup import (  # noqa: E402
    SCOPE_EXCEPTIONS_REL,
    ScopeExceptionRegistryMalformed,
    _scope_exceptions_on_base,
)

GUARDED_PATH = "backend/services/coupon_generator.py"


def _valid_entry(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "exception_id": "EX-TEST",
        "exact_file_scope": [GUARDED_PATH],
        "task_scope": "a named piece of work",
        "owner_approval_ref": "https://example.test/approval",
        "expires_at": "2099-01-01",
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def repo(tmp_path: Path):
    """A throwaway git repository with a base branch and a working branch."""
    def run(*argv: str, **kw: Any):
        return subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, **kw)

    run("git", "init", "-q", "-b", "base")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    return tmp_path, run


def _write_registry(root: Path, payload: Any) -> None:
    target = root / SCOPE_EXCEPTIONS_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload if isinstance(payload, str)
                      else json.dumps(payload), encoding="utf-8")


def _read(root: Path, base: str = "base") -> dict:
    """Run the guard's own reader against this repository."""
    import tests.test_trusted_context_shadow_wireup as guard  # noqa: PLC0415

    original = guard._HERE
    guard._HERE = str(root / "backend" / "tests")
    try:
        return _scope_exceptions_on_base(base)
    finally:
        guard._HERE = original


def test_no_registry_on_base_grants_nothing(repo) -> None:
    """The strict direction: absent means protected, never open."""
    root, _run = repo
    assert _read(root) == {}


def test_an_exception_on_base_excuses_exactly_its_path(repo) -> None:
    root, run = repo
    _write_registry(root, {"exceptions": [_valid_entry()]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "owner exception")

    granted = _read(root)
    assert set(granted) == {GUARDED_PATH}
    assert granted[GUARDED_PATH]["exception_id"] == "EX-TEST"


def test_a_branch_cannot_grant_itself_the_exception_it_wants(repo) -> None:
    """The property the whole mechanism rests on. An exception that exists only
    on the working branch grants nothing: the reader looks at BASE."""
    root, run = repo
    run("git", "checkout", "-qb", "feature")
    _write_registry(root, {"exceptions": [_valid_entry()]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "self-granted exception")

    # Present on this branch...
    assert (root / SCOPE_EXCEPTIONS_REL).exists()
    # ...and worth nothing, because BASE does not carry it.
    assert _read(root) == {}


def test_an_uncommitted_registry_grants_nothing(repo) -> None:
    """Not even a working-tree file the reader could have been tempted to read."""
    root, _run = repo
    _write_registry(root, {"exceptions": [_valid_entry()]})
    assert _read(root) == {}


def test_a_lapsed_exception_restores_the_protection(repo) -> None:
    root, run = repo
    _write_registry(root, {"exceptions": [_valid_entry(expires_at="2020-01-01")]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "expired exception")
    assert _read(root) == {}


def test_a_registry_that_cannot_be_read_fails_closed(repo) -> None:
    """An unreadable grant is not an absent one: it is a governance record
    nobody can audit, and the guard refuses to guess which."""
    root, run = repo
    _write_registry(root, "{ not json")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "malformed registry")
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _read(root)


@pytest.mark.parametrize("missing", ["exception_id", "task_scope",
                                     "owner_approval_ref", "expires_at"])
def test_an_unauditable_entry_fails_closed(repo, missing: str) -> None:
    """Every field exists so a reader can later ask: which grant, for what work,
    approved where, until when. An entry that cannot answer is refused."""
    root, run = repo
    _write_registry(root, {"exceptions": [_valid_entry(**{missing: ""})]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "unauditable entry")
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _read(root)


def test_an_entry_without_an_exact_path_grants_nothing(repo) -> None:
    """Paths, never patterns: a grant that could not name its file could not be
    reviewed for what it opens."""
    root, run = repo
    _write_registry(root, {"exceptions": [_valid_entry(exact_file_scope=[])]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "scopeless entry")
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _read(root)


# ── The committed registry, read as the guard reads it ───────────────────────


def test_the_committed_registry_is_auditable_and_names_only_what_it_claims() -> None:
    """This repository's own registry, checked against the same rules."""
    payload = json.loads((REPO_ROOT / SCOPE_EXCEPTIONS_REL).read_text(encoding="utf-8"))
    entries = payload["exceptions"]
    assert entries, "an empty registry should be absent, not present"
    for entry in entries:
        for field in ("exception_id", "task_scope", "owner_approval_ref", "expires_at"):
            assert str(entry.get(field) or "").strip(), field
        assert entry["exact_file_scope"], entry["exception_id"]
        for path in entry["exact_file_scope"]:
            # A grant names a file that exists, or it is not reviewable.
            assert (REPO_ROOT / path).exists(), path
        assert entry["owner_approval_ref"].startswith("https://"), entry["exception_id"]


def test_the_guard_still_protects_every_path_no_exception_names() -> None:
    """The ban is narrowed by exact path and not lifted. Nothing the registry
    does not name may move, and the markers still cover the siblings."""
    import tests.test_trusted_context_shadow_wireup as guard  # noqa: PLC0415

    source = (REPO_ROOT / "backend" / "tests"
              / "test_trusted_context_shadow_wireup.py").read_text(encoding="utf-8")
    # The general prohibitions are still declared, both of them.
    assert '"backend/services/coupon_generator.py"' in source
    assert '"coupon_generator"' in source
    assert '"backend/routers/coupons.py"' in source
    assert '"promotion_engine"' in source
    # And nothing skips, or reads a branch name.
    assert "pytest.skip" not in source.split("def test_branch_diff_excludes")[1]
    assert "HEAD:" not in source

    granted = set(guard._scope_exceptions_on_base())
    assert "backend/routers/coupons.py" not in granted
    assert "backend/services/promotion_engine.py" not in granted
    assert "backend/core/store_knowledge.py" not in granted
