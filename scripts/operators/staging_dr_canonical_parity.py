"""Staging DR canonical parity evaluator (read-only, fail-closed)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from scripts.operators.staging_dr_canonical_parity_contract import (
    CONTRACT_JSON_FILENAME,
    export_contract,
    load_contract,
)


class ParityFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def default_contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "ops"
        / "staging_dr_executor"
        / "contracts"
        / CONTRACT_JSON_FILENAME
    )


def read_contract(path: Path | None = None) -> dict[str, Any]:
    contract_path = path or default_contract_path()
    try:
        parsed = json.loads(contract_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError
        return load_contract(parsed)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ParityFailure("contract_invalid") from exc


def write_contract(path: Path | None = None) -> Path:
    """Emit the closed contract JSON from Python source constants."""
    contract_path = path or default_contract_path()
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        json.dumps(export_contract(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return contract_path


def evaluate_observation(
    observation: Mapping[str, Any],
    *,
    contract_path: Path | None = None,
) -> dict[str, Any]:
    required = {
        "source_revision",
        "restore_revision",
        "source_table_count",
        "restore_table_count",
        "source_fingerprint_sha256",
        "restore_fingerprint_sha256",
    }
    if set(observation) != required:
        raise ParityFailure("observation_invalid")
    contract = read_contract(contract_path)
    for key in (
        "source_revision",
        "restore_revision",
        "source_fingerprint_sha256",
        "restore_fingerprint_sha256",
    ):
        value = observation[key]
        if not isinstance(value, str) or not value:
            raise ParityFailure("observation_invalid")
    for key in ("source_table_count", "restore_table_count"):
        value = observation[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ParityFailure("observation_invalid")
    result = evaluate_parity_from_contract(contract, observation)
    if not result["canonical_manifest_parity"]:
        raise ParityFailure("canonical_manifest_parity_failed")
    if not result["source_contract_eligible"]:
        raise ParityFailure("source_contract_ineligible")
    return result


def evaluate_parity_from_contract(
    contract: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    from scripts.operators.staging_dr_canonical_parity_contract import evaluate_parity

    return evaluate_parity(
        source_revision=observation["source_revision"],
        restore_revision=observation["restore_revision"],
        source_table_count=observation["source_table_count"],
        restore_table_count=observation["restore_table_count"],
        source_fingerprint_sha256=observation["source_fingerprint_sha256"],
        restore_fingerprint_sha256=observation["restore_fingerprint_sha256"],
        contract=contract,
    )


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["emit-contract"]:
            path = write_contract()
            _emit({"ok": True, "contract_path": str(path)})
            return 0
        if len(arguments) == 2 and arguments[0] == "evaluate":
            payload = json.loads(Path(arguments[1]).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ParityFailure("observation_invalid")
            _emit({"ok": True, "parity": evaluate_observation(payload)})
            return 0
        raise ParityFailure("command_invalid")
    except ParityFailure as exc:
        _emit({"ok": False, "code": exc.code})
        return 2
    except BaseException:
        _emit({"ok": False, "code": "parity_failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
