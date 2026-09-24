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
import hashlib
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
        "authorized_content_sha256": ["0" * 64],
        "base_content_sha256": hashlib.sha256(b"# original\nVALUE = 0\n").hexdigest(),
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
        return guard._scope_exceptions_on_base(base)
    finally:
        guard._HERE = original


def test_no_registry_on_base_grants_nothing(repo) -> None:
    """The strict direction: absent means protected, never open."""
    root, _run = repo
    assert _read(root) == {}


def test_an_exception_on_base_excuses_its_path_only_for_the_approved_content(repo) -> None:
    """A grant names a path *and* the content that path must have once the
    approved change is made. The path alone was a month-long licence to edit
    the file for any reason, which is not what an owner approves."""
    import hashlib  # noqa: PLC0415

    root, run = repo
    body = "# the change the owner approved\nVALUE = 1\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    target = root / GUARDED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _write_registry(root, {"exceptions": [
        _valid_entry(authorized_content_sha256=[digest], base_content_sha256=digest)]})
    run("git", "add", "-A")
    run("git", "commit", "-qm", "owner exception")

    granted = _read(root)
    assert set(granted) == {GUARDED_PATH}
    assert granted[GUARDED_PATH]["exception_id"] == "EX-TEST"

    # Change the file to anything else and the same grant stops applying.
    run("git", "checkout", "-qb", "feature")
    target.write_text("# a different edit\nVALUE = 2\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "different committed edit")
    assert _read(root) == {}


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
    """This repository's own registry, checked against the same rules the guard
    applies. It is empty today and that is the point: merging the mechanism
    grants nothing, because a grant needs a digest and a digest needs the
    implementation diff it authorises."""
    import datetime  # noqa: PLC0415

    payload = json.loads((REPO_ROOT / SCOPE_EXCEPTIONS_REL).read_text(encoding="utf-8"))
    for entry in payload["exceptions"]:
        for field in ("exception_id", "task_scope", "owner_approval_ref", "expires_at"):
            assert isinstance(entry.get(field), str) and entry[field].strip(), field
        datetime.datetime.strptime(entry["expires_at"], "%Y-%m-%d")
        assert entry["exact_file_scope"], entry["exception_id"]
        for path in entry["exact_file_scope"]:
            # A grant names a file that exists, or it is not reviewable.
            assert (REPO_ROOT / path).exists(), path
        digests = entry["authorized_content_sha256"]
        assert digests and all(len(d) == 64 for d in digests), entry["exception_id"]
        assert entry["owner_approval_ref"].startswith("https://"), entry["exception_id"]


def test_merging_this_registry_grants_nothing_today() -> None:
    """Stated as a test because the review caught the claim being wrong once.
    An earlier draft carried a live whole-file grant while the PR said merging
    granted nothing; it would have excused that path on every later branch."""
    payload = json.loads((REPO_ROOT / SCOPE_EXCEPTIONS_REL).read_text(encoding="utf-8"))
    assert payload["exceptions"] == []


def test_entries_awaiting_owner_approval_are_complete_and_approve_nothing() -> None:
    """A proposal names everything the owner decides on — the file, both blobs,
    the task, the expiry — and carries no approval of its own: the approval
    link is the owner's to add, and only when the entry moves into
    ``exceptions``. An entry cannot be proposed and granted at once."""
    import datetime  # noqa: PLC0415

    payload = json.loads((REPO_ROOT / SCOPE_EXCEPTIONS_REL).read_text(encoding="utf-8"))
    awaiting = (payload.get("_awaiting_owner_approval") or {}).get("entries") or []
    granted_ids = {entry["exception_id"] for entry in payload["exceptions"]}
    for entry in awaiting:
        assert entry["exception_id"] not in granted_ids, entry["exception_id"]
        assert entry.get("owner_approval_ref") is None, entry["exception_id"]
        for field in ("exception_id", "task_scope", "expires_at"):
            assert isinstance(entry.get(field), str) and entry[field].strip(), field
        datetime.datetime.strptime(entry["expires_at"], "%Y-%m-%d")
        assert len(entry["exact_file_scope"]) == 1
        assert (REPO_ROOT / entry["exact_file_scope"][0]).exists()
        for digest in [entry["base_content_sha256"], *entry["authorized_content_sha256"]]:
            assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        assert len(entry["authorized_content_sha256"]) == 1


def test_an_entry_awaiting_owner_approval_grants_nothing(tmp_path: Path) -> None:
    """Through the real guard: an entry for exactly this change, on BASE and
    complete in every field — even an approval link — but waiting rather than
    in ``exceptions``, leaves the path protected. The refusal is the scope
    violation itself, not a registry the guard could not read."""
    entry = _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])
    run = _guarded_repo(tmp_path, {"_awaiting_owner_approval": {"entries": [entry]},
                                   "exceptions": []})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(AssertionError) as refused:
        _run_guard(tmp_path)
    assert not isinstance(refused.value, ScopeExceptionRegistryMalformed)


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


# ── Through the real guard, not the reader ───────────────────────────────────
#
# A reader that returns the right dictionary proves nothing if the guard built
# on it still lets a branch through. These drive
# ``test_branch_diff_excludes_other_agent_scope_paths`` itself against a real
# repository with a real base and a real feature branch, and assert what a
# reviewer actually cares about: does the guard pass, or does it fail.


import tests.test_trusted_context_shadow_wireup as guard  # noqa: E402


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


AUTHORISED_BODY = "# the change the owner approved\nVALUE = 1\n"
DIFFERENT_BODY = "# some other edit to the same file\nVALUE = 2\n"


def _guarded_repo(tmp_path: Path, registry: Any = None):
    """A repository whose ``base`` carries the registry, if one is given."""
    def run(*argv: str):
        return subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)

    run("git", "init", "-q", "-b", "base")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    target = tmp_path / GUARDED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# original\nVALUE = 0\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    if registry is not None:
        _write_registry(tmp_path, registry)
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "checkout", "-qb", "feature")
    return run


def _run_guard(root: Path, base: str = "base") -> None:
    """Invoke the real scope test against ``root``. Raises what it raises.

    ``_branch_diff_paths`` and the reader both take the base as a default
    argument, which Python binds once at definition, so rebinding the module
    constant would change nothing. The defaults themselves are swapped.
    """
    original_here = guard._HERE
    diff_defaults = guard._branch_diff_paths.__defaults__
    reader_defaults = guard._scope_exceptions_on_base.__defaults__
    guard._HERE = str(root / "backend" / "tests")
    guard._branch_diff_paths.__defaults__ = (base,)
    guard._scope_exceptions_on_base.__defaults__ = (base,)
    try:
        guard.test_branch_diff_excludes_other_agent_scope_paths()
    finally:
        guard._HERE = original_here
        guard._branch_diff_paths.__defaults__ = diff_defaults
        guard._scope_exceptions_on_base.__defaults__ = reader_defaults


def _edit_guarded_file(root: Path, run, body: str) -> None:
    (root / GUARDED_PATH).write_text(body, encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "edit the protected file")


def test_guard_passes_for_the_authorised_change(tmp_path: Path) -> None:
    """The one thing the grant is for."""
    run = _guarded_repo(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    _run_guard(tmp_path)                              # does not raise


def test_guard_refuses_a_different_change_to_the_same_file(tmp_path: Path) -> None:
    """The hole the review found. BASE-only reading stops a branch writing
    itself a grant; it does nothing about a branch reusing one that exists.
    The digest is what attaches the grant to the change it was approved for."""
    run = _guarded_repo(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, DIFFERENT_BODY)
    with pytest.raises(AssertionError):
        _run_guard(tmp_path)


def test_guard_refuses_a_malformed_expiry(tmp_path: Path) -> None:
    """``"not-a-date"`` sorted after every real date, so it read as "not
    expired" and made the grant permanent. A date the guard cannot parse is a
    date it will not honour."""
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        expires_at="not-a-date",
        authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


@pytest.mark.parametrize("bad", [20261023, None, ["2026-10-23"], "2026-13-01", "23/10/2026", ""])
def test_guard_refuses_every_expiry_that_is_not_an_iso_date(tmp_path: Path, bad: Any) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        expires_at=bad, authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


def test_guard_refuses_an_expired_grant(tmp_path: Path) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        expires_at="2020-01-01",
        authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(AssertionError):
        _run_guard(tmp_path)


def test_guard_refuses_a_grant_that_exists_only_on_the_feature_branch(tmp_path: Path) -> None:
    """The property the mechanism rests on, asserted through the guard."""
    run = _guarded_repo(tmp_path, registry=None)
    _write_registry(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(AssertionError):
        _run_guard(tmp_path)


def test_guard_refuses_a_protected_sibling_the_grant_does_not_name(tmp_path: Path) -> None:
    """Grants name paths, never patterns: the marker scan still covers every
    sibling, including a test file whose name merely contains the marker."""
    run = _guarded_repo(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    for sibling in ("backend/routers/coupons.py",
                    "backend/tests/test_coupon_generator_extra.py"):
        path = tmp_path / sibling
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# unrelated\n", encoding="utf-8")
        run("git", "add", "-A")
        run("git", "commit", "-qm", f"touch {sibling}")
        with pytest.raises(AssertionError):
            _run_guard(tmp_path)
        path.unlink()
        run("git", "add", "-A")
        run("git", "commit", "-qm", f"drop {sibling}")


def test_guard_refuses_a_malformed_registry_rather_than_reading_past_it(tmp_path: Path) -> None:
    run = _guarded_repo(tmp_path, "{ not json")
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


def test_guard_refuses_a_grant_with_no_authorized_content(tmp_path: Path) -> None:
    """An entry that names no digest is a whole-file grant, which is what the
    review refused. It fails closed rather than falling back to path-only."""
    entry = _valid_entry()
    entry.pop("authorized_content_sha256", None)
    run = _guarded_repo(tmp_path, {"exceptions": [entry]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


@pytest.mark.parametrize("bad", ["deadbeef", "z" * 64, 12345, []])
def test_guard_refuses_a_digest_that_is_not_a_sha256(tmp_path: Path, bad: Any) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=bad)]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


def test_guard_still_fails_when_the_base_diff_cannot_be_read(tmp_path: Path) -> None:
    """Unchanged, and worth re-asserting beside the grant: a guard that cannot
    see has proved nothing, and must not report PASS for it."""
    run = _guarded_repo(tmp_path, {"exceptions": [
        _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(guard.ScopeDiffUnavailable):
        _run_guard(tmp_path, base="origin/no-such-base")


def test_uncommitted_approved_content_cannot_hide_unapproved_head(tmp_path: Path) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        base_content_sha256=_sha256("# original\nVALUE = 0\n"),
        authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, DIFFERENT_BODY)
    # The tested delta is committed. A later local write must not change the verdict.
    (tmp_path / GUARDED_PATH).write_text(AUTHORISED_BODY, encoding="utf-8")
    with pytest.raises(AssertionError):
        _run_guard(tmp_path)


def test_grant_does_not_authorize_reverting_a_changed_base(tmp_path: Path) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        base_content_sha256=_sha256("# original\nVALUE = 0\n"),
        authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    run("git", "checkout", "-q", "base")
    _edit_guarded_file(tmp_path, run, "# later independent base change\nVALUE = 3\n")
    run("git", "checkout", "-qb", "later-feature")
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(AssertionError):
        _run_guard(tmp_path)


@pytest.mark.parametrize("overrides", [
    {"base_content_sha256": None},
    {"base_content_sha256": "bad"},
    {"authorized_content_sha256": ["0" * 64, "1" * 64]},
    {"exact_file_scope": [GUARDED_PATH, "backend/services/promotion_engine.py"]},
    {"exact_file_scope": ["../coupon_generator.py"]},
])
def test_guard_requires_one_exact_before_after_pair(tmp_path: Path, overrides: dict) -> None:
    entry = _valid_entry(authorized_content_sha256=[_sha256(AUTHORISED_BODY)])
    entry.update(overrides)
    run = _guarded_repo(tmp_path, {"exceptions": [entry]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    with pytest.raises(ScopeExceptionRegistryMalformed):
        _run_guard(tmp_path)


def test_committed_approved_head_does_not_depend_on_working_tree(tmp_path: Path) -> None:
    run = _guarded_repo(tmp_path, {"exceptions": [_valid_entry(
        authorized_content_sha256=[_sha256(AUTHORISED_BODY)])]})
    _edit_guarded_file(tmp_path, run, AUTHORISED_BODY)
    (tmp_path / GUARDED_PATH).write_text(DIFFERENT_BODY, encoding="utf-8")
    _run_guard(tmp_path)
