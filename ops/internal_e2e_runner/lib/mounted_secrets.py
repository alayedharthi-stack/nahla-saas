"""Strict mounted-secret loading at the confined runner trust boundary."""
from __future__ import annotations

import sys
from pathlib import Path


class MountedSecretError(ValueError):
    """Mounted secret failed closed validation."""


def load_mounted_secret_bytes(raw: bytes) -> bytes:
    """Validate mounted secret bytes and return the terminator-stripped core."""
    if not raw:
        raise MountedSecretError("mounted_secret_empty")

    suffix_len = 0
    if raw.endswith(b"\r\n"):
        suffix_len = 2
    elif raw.endswith(b"\n"):
        suffix_len = 1
    elif raw.endswith(b"\r"):
        suffix_len = 1

    core = raw[:-suffix_len] if suffix_len else raw
    if not core:
        raise MountedSecretError("mounted_secret_empty")
    if suffix_len and (core.endswith(b"\n") or core.endswith(b"\r")):
        raise MountedSecretError("mounted_secret_multiple_terminators")

    if core[:1] in b" \t" or core[-1:] in b" \t":
        raise MountedSecretError("mounted_secret_whitespace_padding")

    if (core[:1] in b"\"'" and core[-1:] == core[:1]):
        raise MountedSecretError("mounted_secret_quote_wrapped")

    for byte in core:
        if byte > 127:
            raise MountedSecretError("mounted_secret_non_ascii")
        if byte < 32 or byte == 127:
            raise MountedSecretError("mounted_secret_control_byte")

    return core


def load_mounted_secret_file(path: str | Path) -> bytes:
    return load_mounted_secret_bytes(Path(path).read_bytes())


def emit_mounted_secret(path: str | Path) -> None:
    """Write validated secret bytes to stdout without a trailing newline."""
    try:
        secret = load_mounted_secret_file(path)
    except MountedSecretError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    sys.stdout.buffer.write(secret)
