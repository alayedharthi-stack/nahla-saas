"""Shared serialization helpers for Layer 2 shadow contracts."""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping, TypeVar

T = TypeVar("T")


def filter_known_keys(data: Mapping[str, Any], allowed: FrozenSet[str]) -> Dict[str, Any]:
    """Drop unknown keys for forward-compatible deserialization."""
    return {key: data[key] for key in data if key in allowed}


def require_schema_version(data: Mapping[str, Any], *, expected: str = "1") -> None:
    version = data.get("schema_version")
    if version != expected:
        raise ValueError(f"unsupported schema_version: {version!r}")


__all__ = ["filter_known_keys", "require_schema_version"]
