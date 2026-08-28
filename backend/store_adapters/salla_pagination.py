"""Pagination result types for Salla adapter list endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class SallaPaginatedFetchIncomplete(Exception):
    """Raised when a multi-page Salla fetch did not complete successfully."""

    def __init__(
        self,
        *,
        partial: bool,
        items: List[Dict[str, Any]],
        pages_fetched: int = 0,
        failure_class: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> None:
        self.partial = partial
        self.items = items
        self.pages_fetched = pages_fetched
        self.failure_class = failure_class
        self.http_status = http_status
        kind = "partial_pagination" if partial else "fetch_failed"
        super().__init__(kind)

    @classmethod
    def from_result(cls, result: Dict[str, Any]) -> "SallaPaginatedFetchIncomplete":
        return cls(
            partial=bool(result.get("partial")),
            items=list(result.get("items") or []),
            pages_fetched=int(result.get("pages_fetched") or 0),
            failure_class=result.get("failure_class"),
            http_status=result.get("http_status"),
        )
