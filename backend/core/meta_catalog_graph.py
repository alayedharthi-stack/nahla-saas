"""Catalog-only Graph version selection; shared WhatsApp configuration is unchanged.

An empty override preserves the existing shared version. A nonempty malformed
override fails before a catalog request rather than silently using that fallback.
This validates URL syntax only, not Meta's version lifecycle or app permissions.
"""
from __future__ import annotations

import os
import re
from typing import Mapping, Optional

CATALOG_GRAPH_VERSION_ENV = "META_CATALOG_GRAPH_API_VERSION"
_VERSION = re.compile(r"v[1-9][0-9]*\.[0-9]+")


class CatalogGraphVersionError(ValueError):
    """The explicitly configured catalog version is not a version path segment."""


def catalog_graph_api_version(env: Optional[Mapping[str, str]] = None) -> str:
    values = os.environ if env is None else env
    override = str(values.get(CATALOG_GRAPH_VERSION_ENV) or "").strip()
    if override:
        if _VERSION.fullmatch(override) is None:
            raise CatalogGraphVersionError("catalog_graph_version_invalid")
        return override
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    return str(META_GRAPH_API_VERSION or "v20.0")
