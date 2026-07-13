"""
Feature flag for external-store lifecycle shadow producer (PR 2C).

Default disabled — owned exclusively by the shadow producer call site.
"""
from __future__ import annotations

import os


def commerce_lifecycle_external_shadow_enabled() -> bool:
    val = str(
        os.environ.get("COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED", "false")
    ).strip().lower()
    return val in {"1", "true", "yes", "on"}


__all__ = ["commerce_lifecycle_external_shadow_enabled"]
