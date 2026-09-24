"""The whole ordered result of one search, for the platform and never for the model.

The model reads a search through a bounded window: at most five products, the
ones the adapter puts in the tool result. That window is deliberate — five
grounded products keep the model's context small and its turn inside budget —
and nothing here widens it.

But a customer who asks what a merchant has may be owed more than five, and a
list that pages needs the rest in a form the platform can trust. Asking the
model to carry them is the wrong shape twice over: the model never saw them,
and ids a model repeats back are a claim, not a fact. So the search tool hands
the platform a second, typed thing beside the model's window — this — and the
model never sees it.

What makes it trustworthy
=========================
* **It is the same search.** Read by the same strategy, predicate and total
  order as the model's window (``CatalogContextBuilder._search_steps``), in the
  same tool call, so the window is its prefix. ``from_observation`` refuses one
  whose window is not.
* **It is scoped.** Tenant, namespace, the runtime conversation and the turn
  are stamped from the loop's trusted ``ToolScope`` — never from an argument —
  and ``from_observation`` refuses one bound to any other scope.
* **Its end is proven, not assumed.** ``complete`` is true only when the read
  got back fewer matches than it asked for. A read that stopped at its own cap
  says ``complete=False``; reaching the cap is not reaching the end, and nothing
  downstream may say "that is everything" on its strength.
* **It is typed and closed.** A frozen dataclass with validated fields, carried
  on ``ToolObservation.platform``: never serialised into a tool result, never
  copied into ``provider_observations``, never checkpointed. There is no
  untyped side list anywhere.

What it deliberately is not
===========================
Not evidence. Nothing in it is a fact a reply may state: it holds identities
and an order, no title, no price, no availability. Every value a customer later
reads about one of these products comes from a trusted catalogue read made at
the moment it is shown.
"""
from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Iterable, Optional, Sequence, Tuple

# The most identities one search hands the platform. A bound on memory, on the
# snapshot row and on how far a customer can page — never a claim about the
# catalogue. A search with more matches than this says ``complete=False``.
CANDIDATE_CAP = 50

# Closed reasons ``from_observation`` refuses for. Named so a refusal is
# auditable, and so a negative control can prove each guard is load-bearing.
NOT_A_SEARCH = "not_a_search_observation"
NO_CANDIDATES = "no_candidates_attached"
WRONG_SCOPE = "candidates_bound_to_another_scope"
WINDOW_NOT_PREFIX = "model_window_is_not_the_candidates_prefix"
USABLE = "usable"


def query_digest(query: Any) -> str:
    """A stable name for the query, so a stored browse can say which search it
    was without storing what the customer typed."""
    text = " ".join(str(query or "").split()).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _ids(values: Iterable[Any]) -> Tuple[int, ...]:
    out = []
    for value in values or ():
        if isinstance(value, bool):
            raise ValueError("a product id is an integer, not a boolean")
        number = int(value)
        if number <= 0:
            raise ValueError("a product id is positive")
        if number in out:
            raise ValueError("a search result names each product once")
        out.append(number)
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class SearchCandidates:
    """One search's matches, in its order, bound to the scope that ran it."""

    tenant_id: int
    namespace: str
    conversation_id: int          # the runtime conversation, as ``ToolScope`` carries it
    turn_id: int
    query_digest: str
    method: str                   # the catalogue strategy that matched
    product_ids: Tuple[int, ...]  # every match up to ``CANDIDATE_CAP``, in search order
    complete: bool                # proven: nothing matches beyond ``product_ids``
    window_ids: Tuple[int, ...]   # the products the model's tool result carried

    def __post_init__(self) -> None:
        for name in ("tenant_id", "conversation_id", "turn_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.namespace, str) or not self.namespace:
            raise ValueError("namespace is required")
        object.__setattr__(self, "product_ids", _ids(self.product_ids))
        object.__setattr__(self, "window_ids", _ids(self.window_ids))
        if len(self.product_ids) > CANDIDATE_CAP:
            raise ValueError("a search hands the platform at most CANDIDATE_CAP ids")
        if not isinstance(self.complete, bool):
            raise ValueError("complete is a proof, and a proof is a boolean")

    @property
    def extends_beyond_window(self) -> bool:
        """Whether this search matched products the model's window did not carry.

        True when the stored result is longer than the window, and also when it
        is not complete: a cap reached is a proof that more exist.
        """
        return len(self.product_ids) > len(self.window_ids) or not self.complete

    def bound_to(self, *, tenant_id: int, namespace: str, conversation_id: int,
                 turn_id: int) -> bool:
        return (self.tenant_id == int(tenant_id) and self.namespace == str(namespace)
                and self.conversation_id == int(conversation_id)
                and self.turn_id == int(turn_id))

    def window_is_prefix(self) -> bool:
        """Whether the model's window is exactly the first products of this result.

        As a set, not a sequence: the adapter lists orderable products before
        non-orderable ones, which reorders the window without changing which
        products it holds. What must hold is that the window contains no
        product outside the result's head, and is missing none of it.
        """
        head = self.product_ids[:len(self.window_ids)]
        return set(head) == set(self.window_ids)


def window_ids(result: Any) -> Tuple[int, ...]:
    """The product ids a model-visible search result carried, in its order."""
    if not isinstance(result, dict):
        return ()
    out = []
    for item in result.get("products") or ():
        if not isinstance(item, dict):
            continue
        try:
            product_id = int(item.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in out:
            out.append(product_id)
    return tuple(out)


def from_observation(observation: Any, *, tenant_id: int, namespace: str,
                     conversation_id: int, turn_id: int,
                     search_tool_names: Sequence[str]) -> Tuple[Optional[SearchCandidates], str]:
    """The candidates one observation carries, if the platform may use them.

    Every guard fails closed to ``(None, reason)``: an observation that is not
    a successful, untruncated search; one with nothing attached; one attached
    under another scope; and one whose model window is not the head of the
    stored result — the sign that the two did not come from one search.
    """
    if (not getattr(observation, "ok", False) or getattr(observation, "body_truncated", False)
            or str(getattr(observation, "tool_name", "") or "") not in set(search_tool_names)):
        return None, NOT_A_SEARCH
    candidates = getattr(observation, "platform", None)
    if not isinstance(candidates, SearchCandidates):
        return None, NO_CANDIDATES
    if not candidates.bound_to(tenant_id=tenant_id, namespace=namespace,
                               conversation_id=conversation_id, turn_id=turn_id):
        return None, WRONG_SCOPE
    if (candidates.window_ids != window_ids(getattr(observation, "result", None))
            or not candidates.window_is_prefix()):
        return None, WINDOW_NOT_PREFIX
    return candidates, USABLE


__all__ = [
    "CANDIDATE_CAP", "NOT_A_SEARCH", "NO_CANDIDATES", "SearchCandidates", "USABLE",
    "WINDOW_NOT_PREFIX", "WRONG_SCOPE", "from_observation", "query_digest", "window_ids",
]
