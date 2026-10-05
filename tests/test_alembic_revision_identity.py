"""Alembic revision identifiers are unique, resolvable and never reused.

Alembic does not refuse two scripts that declare the same ``revision``: it
warns ("Revision X is present more than once"), keeps one and drops the other
from the graph. A database stamped with that id would then be read as carrying
whichever migration survived, not the one it actually ran. Three open branches
once each declared ``0116`` (OTO connections off 0112, catalog channel
retirements off 0112, payments readiness off 0115); the id is retired so no
script may use it again, and a database that somehow carries it fails loudly
("Can't locate revision identified by '0116'") instead of being misread. That failure is the
intended stop: recovery of such a database is an owner decision with explicit
authorization, never inferred from table presence or absence, and no change to
``alembic_version`` is prescribed here.

Identity, ids and parents are checked by parsing the scripts, so they need no
database or app settings; the last test additionally loads the graph through
Alembic's ScriptDirectory (which imports the scripts).
"""
from __future__ import annotations

import ast
import os
import sys
import warnings
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from alembic.script import ScriptDirectory  # noqa: E402

from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    INTEGRATION_BOOTSTRAP_TARGET,
    repository_heads_expected,
)

VERSIONS = _REPO / "database" / "migrations" / "versions"

# Revision ids that were declared by more than one branch and withdrawn. Whether
# a database carries a retired id is a property of its current state: Alembic
# refuses such a database, and its recovery is an owner decision. Never reuse
# them.
RETIRED_REVISION_IDS = frozenset({"0116"})
# Where each former 0116 now sits. A wrong parent would leave the head set
# unchanged, so the links are pinned here explicitly.
RENUMBERED_PARENTS = {"0117": "0112", "0118": "0112", "0119": "0115"}


def _declared(path: Path) -> tuple[str | None, object]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
            found[target.id] = ast.literal_eval(value)
    return found.get("revision"), found.get("down_revision")  # type: ignore[return-value]


def _scripts() -> dict[Path, tuple[str | None, object]]:
    return {p: _declared(p) for p in sorted(VERSIONS.glob("*.py")) if p.name != "__init__.py"}


def _parents(down: object) -> tuple[str, ...]:
    if down is None:
        return ()
    if isinstance(down, str):
        return (down,)
    return tuple(down)  # type: ignore[arg-type]


def test_every_revision_id_is_declared_exactly_once() -> None:
    seen: dict[str, list[str]] = {}
    for path, (rev, _down) in _scripts().items():
        assert rev, f"{path.name} declares no revision"
        seen.setdefault(rev, []).append(path.name)
    duplicates = {rev: names for rev, names in seen.items() if len(names) > 1}
    assert duplicates == {}


def test_retired_revision_ids_are_never_reused() -> None:
    for path, (rev, down) in _scripts().items():
        assert rev not in RETIRED_REVISION_IDS, path.name
        assert not set(_parents(down)) & RETIRED_REVISION_IDS, path.name
        assert not path.name.startswith(tuple(f"{r}_" for r in RETIRED_REVISION_IDS)), path.name


def test_renumbered_revisions_keep_their_original_parents() -> None:
    declared = {rev: down for rev, down in _scripts().values()}
    present = {rev: declared[rev] for rev in RENUMBERED_PARENTS if rev in declared}
    assert present, "none of the renumbered revisions is in this checkout"
    for rev, down in present.items():
        assert down == RENUMBERED_PARENTS[rev], (rev, down)


def test_file_prefix_matches_revision_and_parents_exist() -> None:
    scripts = _scripts()
    known = {rev for rev, _ in scripts.values()}
    for path, (rev, down) in scripts.items():
        assert path.name.startswith(f"{rev}_"), path.name
        missing = set(_parents(down)) - known
        assert missing == set(), (path.name, missing)


def test_alembic_graph_loads_without_duplicate_warnings_and_keeps_bootstrap_target() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            script = ScriptDirectory(str(_REPO / "database" / "migrations"))
            revisions = list(script.walk_revisions())
            heads = script.get_heads()
    finally:
        os.chdir(prev_cwd)
    assert not [w for w in caught if "present more than once" in str(w.message)]
    assert len(revisions) == len(_scripts())
    assert repository_heads_expected(heads)
    assert INTEGRATION_BOOTSTRAP_TARGET == "0093"
    assert script.get_revision(INTEGRATION_BOOTSTRAP_TARGET) is not None
    assert not set(heads) & RETIRED_REVISION_IDS
