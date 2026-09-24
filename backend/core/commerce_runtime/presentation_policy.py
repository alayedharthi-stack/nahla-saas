"""Which shape a reply takes, decided from structured provenance alone.

Content is the agent's; presentation is the platform's. The model writes the
answer and may *offer* a shape for it; this module decides which shape the
customer actually gets, and it decides from things the turn established —
the tools that ran, the products they returned, a verified tap, a delivered
card — never from the customer's words and never from the model's prose.

Nothing here reads customer language. There is no keyword table, no regex over
anything a customer wrote, no product or category name, and no vertical: a
clothing merchant and a honey merchant reach the same decision from the same
structured inputs. The model's text is not touched, at all, on any path.

Provenance comes from the registry's own declarations
=====================================================
Each observation carries the tool that produced it, and each tool declares what
kind of result it returns (``ToolDefinition.result_kind``). That declaration is
the provenance:

* ``product``      — ``get_product_details``: one product, read deliberately
* ``product_list`` — ``search_products``: candidates
* everything else  — orders, shipments, addresses, promotions, knowledge

A product that appears *incidentally*, as the line item inside an order summary,
is a fact the answer may state. It is not a product the customer asked to
browse, so it never becomes a card or a row on its own. Reading the class off
the declaration rather than off a tool's name is what keeps that true when a
tool is added.

Precedence
==========
First match wins:

1. a **verified fresh product-row selection** → hydrate → card;
2. a valid model-requested shape, consistent with evidence and policy;
3. a focused product from a deliberate ``get_product_details`` → card, subject
   to recent-card suppression when there is no new selection;
4. multiple candidates and no single focus → list;
5. otherwise → text.

**The tap is first, and that is the whole point of it.** A tap on a row this
conversation sent is the strongest structured product selection the channel
gives us — stronger than a model request, which is a suggestion, and stronger
than suppression, which is a guess about repetition. Letting an unrelated
selector in the same reply turn an explicit choice into a different list would
answer a question the customer did not ask.

Only one structured fact returns such a turn to multi-product selection: the
model asks for a selector that **itself offers the tapped product back**, among
others. That is the agent deliberately presenting the customer's own choice as
one of several — a comparison the selection is part of — and it is read off the
requested product ids, never off anything anyone wrote. A selector that does not
carry the tapped product is about something else and does not cancel the card;
its options still reach the customer as lines under the model's own sentence, so
nothing it meant to offer is lost.

Hydration is the tools' own contract, not a copy of it
======================================================
Step 2 must not depend on the model happening to call ``get_product_details``:
a customer-visible guarantee cannot be conditional on a non-deterministic
choice. So the platform reads the product itself — by running the *same* tool
through the *same* registry the model's own calls go through
(``hydration_request``). Tenant isolation, the product-identity guard, the
merchant's own catalogue read now, and the evidence reference all come with it
because it **is** the contract. No business logic is restated here.

A hydration that returns nothing, or a product whose image or link cannot be
proven, fails closed to text with a named reason. A card is never built from a
guess.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import reply_choices as rc

# The registry's declared result kinds this module gives meaning to. Any other
# kind is incidental by construction — including one added tomorrow.
FOCUS_KIND = "product"
CANDIDATE_KIND = "product_list"

# The tool the platform runs to hydrate a tapped product. It is named once,
# here, and resolved through the registry like any other call.
HYDRATION_TOOL = "get_product_details"
# The correlation id hydration is issued under. It is the platform's own call,
# not the provider's, and says so.
HYDRATION_CALL_ID = "platform:hydrate_tapped_product"

# Shapes, closed.
SHAPE_CARD = "card"
SHAPE_LIST = "list"
SHAPE_TEXT = "text"

# Reasons, closed, for the delivered payload and the pilot log.
MODEL_REQUESTED = "model_requested_shape"
TAP_SELECTED = "verified_product_row_tap"
TAP_HYDRATION_UNAVAILABLE = "tap_hydration_unavailable"
TAP_PRODUCT_NOT_FOUND = "tap_product_not_found"
FOCUSED_PRODUCT = "focused_product_read"
RECENT_CARD_SUPPRESSED = "recent_card_already_sent"
MULTIPLE_CANDIDATES = "multiple_candidates_no_focus"
NO_PRODUCT_FOCUS = "no_product_focus"
# The model's selector offers the tapped product back among others, so the turn
# is a multi-product selection again — established from the requested ids.
SELECTION_REOFFERED = "tapped_product_reoffered_among_others"


@dataclasses.dataclass(frozen=True)
class PresentationContext:
    """Structured facts about this conversation the policy is allowed to use.

    Both are established before the loop runs, by the code that already proves
    them, and neither reaches the model: they are presentation state, not
    commerce truth, and the answer must not be written differently because of
    them.

    ``tapped_product_id`` is a row tap **already verified** — the row was sent
    to this conversation, inside the browsing lapse, and the product was re-read
    in the merchant's catalogue. ``last_card_product_id`` is the product of the
    most recent card this conversation actually *delivered*: a send the provider
    accepted and identified, never a payload that merely carried one.
    """

    tapped_product_id: Optional[int] = None
    last_card_product_id: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class Shape:
    """The shape this reply will take, and why."""

    kind: str
    reason: str
    # The card's product, when the platform determined one itself. ``None`` for
    # a model-requested shape: that request stands on its own field.
    product_id: Optional[int] = None
    # Whether the platform must read the product before the card can compose.
    hydrate: bool = False
    # Whether a selector the model requested must not be offered this turn. Set
    # only when the customer's own verified selection is being answered instead;
    # the options themselves are never withheld with it.
    withhold_selector: bool = False

    @property
    def determined_product_id(self) -> Optional[int]:
        """The product the platform is showing as a card, if it determined one."""
        return self.product_id if self.kind == SHAPE_CARD else None


def _result_kinds(definitions: Sequence[Any]) -> Dict[str, str]:
    """Tool name to declared result kind, from the registry's own definitions."""
    kinds: Dict[str, str] = {}
    for definition in definitions or ():
        name = str(getattr(definition, "name", "") or "")
        kind = str(getattr(definition, "result_kind", "") or "")
        if name and kind:
            kinds[name] = kind
    return kinds


def _products_in(result: Any) -> Tuple[int, ...]:
    """The catalogue product ids one tool result returned, in its own order."""
    if not isinstance(result, Mapping):
        return ()
    found: list = []
    items: list = []
    listed = result.get("products")
    if isinstance(listed, (list, tuple)):
        items.extend(listed)
    single = result.get("product")
    if single is not None:
        items.append(single)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            product_id = int(item.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in found:
            found.append(product_id)
    return tuple(found)


@dataclasses.dataclass(frozen=True)
class Provenance:
    """What this turn's reads say about products, by the class of the read."""

    focused: Tuple[int, ...]      # products read deliberately, one at a time
    candidates: Tuple[int, ...]   # every product a browse or a deliberate read returned

    @property
    def single_focus(self) -> Optional[int]:
        """The one product this turn focused on, or ``None`` if it focused on none.

        Two deliberate reads are a comparison, not a focus: the customer is
        weighing products, and one product's photo is not the answer to that.
        """
        return self.focused[0] if len(self.focused) == 1 else None


def provenance(observations: Sequence[Any], definitions: Sequence[Any]) -> Provenance:
    """Classify this turn's product reads by the kind their tool declares.

    A truncated or failed observation states nothing: its body is not there to
    be trusted, so it contributes neither a focus nor a candidate.
    """
    kinds = _result_kinds(definitions)
    focused: list = []
    candidates: list = []
    for obs in observations or ():
        if not getattr(obs, "ok", False) or getattr(obs, "body_truncated", False):
            continue
        kind = kinds.get(str(getattr(obs, "tool_name", "") or ""))
        if kind not in (FOCUS_KIND, CANDIDATE_KIND):
            continue        # incidental: a product inside an order is not a browse
        for product_id in _products_in(getattr(obs, "result", None)):
            if product_id not in candidates:
                candidates.append(product_id)
            if kind == FOCUS_KIND and product_id not in focused:
                focused.append(product_id)
    return Provenance(focused=tuple(focused), candidates=tuple(candidates))


def _selector_offers(draft: Any, product_id: int) -> bool:
    """Whether a selector the model requested offers this product back.

    Read from the requested product ids and nothing else. It is the one
    structured fact that can return a tapped turn to multi-product selection,
    so it must be exactly that fact and not an impression of one.
    """
    return int(product_id) in rc.requested_product_ids(draft)


def _model_requested(draft: Any) -> Optional[str]:
    """The shape the model asked for, if it asked for one.

    Read off the draft's own request fields. Whether that request is *valid* is
    not decided here — ``reply_choices`` and ``reply_card`` already refuse a
    request this turn's evidence does not support, and verification refused an
    unevidenced one before either ran.
    """
    payload = getattr(draft, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    if rc.REQUESTED_KEY in payload:
        return SHAPE_LIST
    from core.commerce_runtime import reply_card as rcard  # noqa: PLC0415

    # A card request that names no product is not a request for a shape — it is
    # the button wording, offered for whatever card the platform decides to
    # show. Treating it as a shape would let a word the model supplied stand in
    # front of the customer's own verified selection.
    return SHAPE_CARD if rcard.requested_product_id(draft) is not None else None


def decide(*, draft: Any, observations: Sequence[Any], definitions: Sequence[Any],
           presentation: Optional[PresentationContext] = None) -> Shape:
    """The shape this reply takes, from structured provenance alone.

    Called after verification, so every refusal here is a presentation limit
    and never a truth problem — the answer itself is already the model's and
    goes out whatever this returns.
    """
    ctx = presentation if presentation is not None else PresentationContext()
    requested = _model_requested(draft)

    # 1. A verified fresh product-row selection. The strongest structured
    #    product choice the channel gives us, so it comes first: the product is
    #    read by the platform rather than hoped for from the model, and no
    #    request of the model's turns it into a different list.
    tapped = ctx.tapped_product_id
    if tapped is not None and int(tapped) > 0:
        if not _selector_offers(draft, int(tapped)):
            return Shape(kind=SHAPE_CARD, reason=TAP_SELECTED, product_id=int(tapped),
                         hydrate=True, withhold_selector=requested == SHAPE_LIST)
        # The selector offers the tapped product back among others: the agent
        # is presenting the customer's own choice as one of several, which is a
        # multi-product selection again. Read from the requested ids alone.
        return Shape(kind=SHAPE_LIST, reason=SELECTION_REOFFERED, product_id=int(tapped))

    # 2. The model asked for a shape. It may legitimately decide this turn
    #    offers alternatives, and its request already passed verification. The
    #    composers below still refuse a request this turn's evidence will not
    #    support.
    if requested is not None:
        return Shape(kind=requested, reason=MODEL_REQUESTED)

    reads = provenance(observations, definitions)

    # 3. One product, read deliberately. A card unless this conversation has
    #    already delivered that same card and nothing new was selected — which
    #    step 2 would have caught.
    focus = reads.single_focus
    if focus is not None:
        last = ctx.last_card_product_id
        if last is not None and int(last) == int(focus):
            return Shape(kind=SHAPE_TEXT, reason=RECENT_CARD_SUPPRESSED, product_id=int(focus))
        return Shape(kind=SHAPE_CARD, reason=FOCUSED_PRODUCT, product_id=int(focus))

    # 4. Several products to choose between and no single focus.
    if len(reads.candidates) >= rc.MIN_CHOICES:
        return Shape(kind=SHAPE_LIST, reason=MULTIPLE_CANDIDATES)

    # 5. One candidate is not a selection, and none is not a browse. A single
    #    search result does not become a card by being alone.
    return Shape(kind=SHAPE_TEXT, reason=NO_PRODUCT_FOCUS)


def hydration_request(shape: Shape) -> Optional[Any]:
    """The tool request that reads a tapped product, or ``None`` if none is due.

    Deliberately an ordinary ``ToolRequest``: the platform runs it through the
    same registry, under the same scope and the same guards, as any call the
    model makes. There is no second path into the catalogue.
    """
    if not shape.hydrate or shape.product_id is None:
        return None
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415

    return ac.ToolRequest(call_id=HYDRATION_CALL_ID, tool_name=HYDRATION_TOOL,
                          arguments={"product_id": int(shape.product_id)})


# The ways a card the platform determined can fail closed before a card is even
# attempted. They are the merchant's facts, not the model's, so they are named
# on the delivered payload rather than left to a log line.
TAP_FAILED_CLOSED = frozenset({TAP_HYDRATION_UNAVAILABLE, TAP_PRODUCT_NOT_FOUND})


def withheld_reason(shape: Shape) -> str:
    """Why a card the platform meant to show is not there, or ``""``."""
    return shape.reason if shape.kind == SHAPE_TEXT and shape.reason in TAP_FAILED_CLOSED else ""


def already_read(shape: Shape, observations: Sequence[Any]) -> bool:
    """Whether this turn already holds the product the platform would read.

    A deliberate read the model made in this same turn went through the very
    same contract, so it is exactly as fresh: reading again would cost the
    merchant a query and prove nothing further. What must never happen is the
    opposite — the card depending on the model having made that call.
    """
    if shape.product_id is None:
        return False
    return int(shape.product_id) in rc.observed_products(observations)


def after_hydration(shape: Shape, observations: Sequence[Any]) -> Shape:
    """The shape once the platform's own read has come back.

    Fails closed: a read that did not succeed, or one whose product is no longer
    in the merchant's catalogue, yields text with a **named** reason rather than
    a card the turn cannot stand behind. The answer is unaffected either way —
    the model's own text goes out whichever this returns.
    """
    if not shape.hydrate or shape.product_id is None:
        return shape
    wanted = int(shape.product_id)
    if already_read(shape, observations):
        return dataclasses.replace(shape, hydrate=False)
    # The product is not among this turn's reads, so the platform's own call is
    # the one that has to account for it. Which way it failed is a different
    # fact to the merchant, so the two are never merged.
    for obs in reversed(list(observations or ())):
        if str(getattr(obs, "call_id", "") or "") != HYDRATION_CALL_ID:
            continue
        if not getattr(obs, "ok", False) or getattr(obs, "body_truncated", False):
            return Shape(kind=SHAPE_TEXT, reason=TAP_HYDRATION_UNAVAILABLE, product_id=wanted)
        return Shape(kind=SHAPE_TEXT, reason=TAP_PRODUCT_NOT_FOUND, product_id=wanted)
    return Shape(kind=SHAPE_TEXT, reason=TAP_HYDRATION_UNAVAILABLE, product_id=wanted)


__all__ = [
    "CANDIDATE_KIND", "FOCUSED_PRODUCT", "FOCUS_KIND", "HYDRATION_CALL_ID", "HYDRATION_TOOL",
    "MODEL_REQUESTED", "MULTIPLE_CANDIDATES", "NO_PRODUCT_FOCUS", "PresentationContext",
    "SELECTION_REOFFERED",
    "Provenance", "RECENT_CARD_SUPPRESSED", "SHAPE_CARD", "SHAPE_LIST", "SHAPE_TEXT", "Shape",
    "TAP_HYDRATION_UNAVAILABLE", "TAP_PRODUCT_NOT_FOUND", "TAP_SELECTED", "after_hydration",
    "already_read",
    "TAP_FAILED_CLOSED", "decide", "hydration_request", "provenance", "withheld_reason",
]
