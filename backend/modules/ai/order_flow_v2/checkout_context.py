"""Bridge OrderContext saved-address truth into OrderFlowV2 deterministic replies."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.order_context_prefill import MODE_CONFIRM
from core.wa_order_lifecycle import has_accepted_delivery_address

from .missing_fields import compute_v2_missing_fields

logger = logging.getLogger("nahla.order_flow_v2.checkout_context")


@dataclass(frozen=True)
class AddressPresentation:
    """What a reply is ABOUT TO put in front of the customer.

    Reading context is not showing anything to anyone. This carries the
    revision a reply would describe and an opaque identity for THAT
    showing; only a proven successful send turns it into a recorded offer.

    ``offer_id`` is generated when the presentation is prepared and is
    embedded in the action ids the customer will tap. It is what makes an
    action answerable to one specific outbound message: a tap carrying a
    superseded ``offer_id`` names a showing that is no longer the live one,
    and is refused rather than applied to whatever the row holds now.
    """

    address_id: int = 0
    fingerprint: str = ""
    # THIS page of the inventory — what one message can actually carry.
    # A choice that is not on this page is not being shown, so it is not
    # in the receipt either; ``page``/``page_count`` say how the rest is
    # reached.
    choices: Tuple[Dict[str, Any], ...] = ()
    offer_id: str = ""
    page: int = 0
    page_count: int = 1

    @property
    def is_empty(self) -> bool:
        return not (self.address_id and self.fingerprint) and not self.choices


@dataclass(frozen=True)
class CheckoutReplyContext:
    missing_fields: List[str]
    field_modes: Dict[str, str]
    known_previous: Dict[str, str]
    identity_first_name: str = ""
    # Every durable address the customer may explicitly choose between.
    # Populated whatever the resolution, so several candidates are visible
    # and selectable instead of simply absent.
    address_choices: List[Dict[str, Any]] = field(default_factory=list)
    # Prepared, NOT recorded. See ``record_presented_address_offer``.
    presentation: AddressPresentation = field(default_factory=AddressPresentation)


def _shipping_context_dict(previous: Any) -> Dict[str, str]:
    """Project the saved address for the reply layer.

    ``short_address`` is the national SHORT address and stays its own key —
    it is never merged with, or rendered as, a postal code.
    """
    if previous is None:
        return {}
    return {
        "city": str(getattr(previous, "city", "") or "").strip(),
        "district": str(getattr(previous, "district", "") or "").strip(),
        "address_line": str(getattr(previous, "address_line", "") or "").strip(),
        "short_address": str(getattr(previous, "short_address", "") or "").strip(),
        "maps_url": str(getattr(previous, "maps_url", "") or "").strip(),
        "selection_state": (
            "selected"
            if bool(getattr(previous, "explicitly_selected", False))
            else "candidate"
        ),
        "sufficient": "true" if bool(getattr(previous, "sufficient", False)) else "false",
    }


# ── Offered-address lifecycle (R4) ──────────────────────────────────────
#
# Confirmation has to mean "yes, THAT address". Re-reading whatever the row
# holds at confirmation time and trusting its fresh fingerprint proves
# nothing: the row may have been refreshed between the offer and the reply,
# and the customer would be recorded as approving content they never saw.
# So the offer is recorded when it is made, and the confirmation is checked
# against it.
_OFFER_KEY = "address_offer"
_OFFER_SET_KEY = "address_offer_set"
_TURN_OPERATION_KEY = "address_operation"

# Namespace for the structured reply id that carries consent. A customer
# tapping this is an unambiguous, machine-checkable act: it names the exact
# address, it cannot be a question, and it cannot be produced by phrasing.
CONSENT_ACTION_PREFIX = "nahla_addr_select"


def _conversation_metadata(conversation: Any) -> Dict[str, Any]:
    raw = getattr(conversation, "extra_metadata", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _write_conversation_metadata(db: Any, conversation: Any, meta: Dict[str, Any]) -> bool:
    try:
        conversation.extra_metadata = meta
        db.add(conversation)
        return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — every caller treats a failed write as "not recorded", which refuses rather than allows
        return False


def new_offer_id() -> str:
    """An opaque identity for one showing of one address revision."""
    import uuid  # noqa: PLC0415

    return uuid.uuid4().hex[:16]


def offer_id_for_showing(
    *,
    tenant_id: Any,
    conversation: Any,
    turn_ref: str,
    choices: Any = (),
    address_id: Any = 0,
    fingerprint: str = "",
    page: int = 0,
) -> str:
    """The identity of THIS showing — stable for one inbound turn.

    A random identity per preparation looked safe and was not: preparing
    the same inbound turn twice produced two different payloads, so the
    outbound dedup could not recognise the redelivery as the same message
    and the customer could end up holding two live offers for one
    question. The identity is therefore derived from what actually makes
    a showing what it is — the scope, the turn being answered, the page,
    and the exact revisions on it. Re-preparing the same turn reproduces
    it; anything else changes it.

    It stays opaque: a digest of values the customer never sees, not a
    row id or a timestamp they could guess or replay into another scope.
    Without an inbound turn there is nothing stable to derive from, so a
    random identity is used rather than a predictable one.
    """
    import hashlib  # noqa: PLC0415

    turn = str(turn_ref or "").strip()
    if not turn:
        return new_offer_id()
    parts = [
        str(int(tenant_id or 0)),
        str(int(getattr(conversation, "id", 0) or 0)),
        str(int(getattr(conversation, "customer_id", 0) or 0)),
        turn,
        str(int(page or 0)),
        f"{int(address_id or 0)}:{str(fingerprint or '')}",
    ]
    for choice in choices or ():
        parts.append(
            f"{int((choice or {}).get('address_id') or 0)}"
            f":{str((choice or {}).get('content_fingerprint') or (choice or {}).get('fingerprint') or '')}"
        )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def consent_action_id(offer_id: Any, address_id: Any) -> str:
    """The structured reply id that selects this address FROM THIS showing.

    The offer id is in the payload, not merely the address id. Without it
    a button kept from an older message selected whatever that row held
    later — the customer's tap said "the address you showed me", and only
    the offer identity can tell which one that was.
    """
    ref = str(offer_id or "").strip()
    try:
        row_id = int(address_id)
    except (TypeError, ValueError):
        return ""
    if not ref or not row_id:
        return ""
    return f"{CONSENT_ACTION_PREFIX}:{ref}:{row_id}"


def address_choice_actions(presentation: Any) -> List[Dict[str, Any]]:
    """Structured choices in WhatsApp reply-button shape, or nothing.

    Platform-owned CTA payload built from trusted stored facts — the id is
    the offer/address identity, the title is drawn from the address's own
    fields. It composes no conversational prose.

    The shape is ``{"type": "reply", "reply": {"id", "title"}}`` because
    that is what the final wire sanitizer
    (``core.wa_link_buttons.whatsapp_reply_buttons_payload``) reads. A flat
    ``{"id", "title"}`` survives that sanitizer as an EMPTY button, which
    would reach the customer as an untappable choice.

    Every choice in ``presentation.choices`` gets an action, and the
    titles are made distinct from each other. Truncating to three here was
    how choice four became unreachable, and titling four Riyadh addresses
    "الرياض" was how the wire sanitizer's duplicate-title rule silently
    deleted three of them. The send boundary picks the surface that can
    carry the count (see ``address_choice_surface``).
    """
    if presentation is None or getattr(presentation, "is_empty", True):
        return []
    offer_id = str(getattr(presentation, "offer_id", "") or "")
    if not offer_id:
        return []

    choices = [dict(c) for c in (getattr(presentation, "choices", ()) or ())]
    if not choices and getattr(presentation, "address_id", 0):
        choices = [{"address_id": presentation.address_id}]

    rows: List[Dict[str, Any]] = []
    for choice, title, _description, _fallback in _labelled_choices(
        choices, _BUTTON_TITLE_LIMIT
    ):
        action_id = consent_action_id(offer_id, choice.get("address_id"))
        if not action_id:
            continue
        rows.append({"type": "reply", "reply": {"id": action_id, "title": title}})
    return rows


def address_choice_rows(presentation: Any) -> List[Dict[str, Any]]:
    """The same choices as interactive-list rows.

    A list row carries a description as well as a title, so the street
    that distinguishes two addresses in one city is visible to the
    customer instead of being compressed out of a 20-character button.
    """
    if presentation is None or getattr(presentation, "is_empty", True):
        return []
    offer_id = str(getattr(presentation, "offer_id", "") or "")
    if not offer_id:
        return []

    choices = [dict(c) for c in (getattr(presentation, "choices", ()) or ())]
    if not choices and getattr(presentation, "address_id", 0):
        choices = [{"address_id": presentation.address_id}]

    rows: List[Dict[str, Any]] = []
    for choice, title, description, _fallback in _labelled_choices(
        choices, _LIST_TITLE_LIMIT
    ):
        action_id = consent_action_id(offer_id, choice.get("address_id"))
        if not action_id:
            continue
        row: Dict[str, Any] = {"id": action_id, "title": title}
        if description:
            row["description"] = description
        rows.append(row)
    more = address_more_action_id(presentation)
    if more:
        rows.append({"id": more, "title": _MORE_ROW_TITLE})
    return rows


def address_choice_surface(presentation: Any) -> str:
    """``buttons``, ``list`` or ``none`` — which surface can carry this.

    Buttons are preferred when they can actually do the job: at most
    three choices, no paging, and labels that tell the addresses apart
    within a button's 20 characters. When the stored fields only differ
    past that budget the buttons would read "الرياض", "الرياض #2",
    "الرياض #3" — distinct enough for the provider, useless to the
    customer. A list row has a longer title AND a description, so the
    street that actually distinguishes them is visible.
    """
    actions = address_choice_actions(presentation)
    if not actions:
        return "none"
    if len(actions) > _BUTTON_SURFACE_LIMIT or address_more_action_id(presentation):
        return "list"
    if len(actions) > 1 and _needs_detail_surface(presentation):
        return "list"
    return "buttons"


def _needs_detail_surface(presentation: Any) -> bool:
    """True when a button label cannot distinguish these on its own."""
    choices = [dict(c) for c in (getattr(presentation, "choices", ()) or ())]
    if len(choices) < 2:
        return False
    on_buttons = _labelled_choices(choices, _BUTTON_TITLE_LIMIT)
    if any(fallback for _c, _t, _d, fallback in on_buttons):
        return True
    on_list = _labelled_choices(choices, _LIST_TITLE_LIMIT)
    return [t for _c, t, _d, _f in on_buttons] != [t for _c, t, _d, _f in on_list]


def address_more_action_id(presentation: Any) -> str:
    """The action that shows the NEXT page, when the inventory has one."""
    if presentation is None:
        return ""
    if int(getattr(presentation, "page_count", 1) or 1) <= 1:
        return ""
    offer_id = str(getattr(presentation, "offer_id", "") or "")
    if not offer_id:
        return ""
    page = int(getattr(presentation, "page", 0) or 0)
    page_count = int(getattr(presentation, "page_count", 1) or 1)
    return f"{PAGE_ACTION_PREFIX}:{offer_id}:{(page + 1) % page_count}"


def structured_page_request(inbound_metadata: Any) -> int:
    """The page a paging action asked for, or 0.

    Paging is how a customer with more addresses than one message can
    hold still reaches every one of them. It authorizes nothing: it only
    decides which page the next showing presents.
    """
    if not isinstance(inbound_metadata, dict):
        return 0
    for key in ("button_id", "list_reply_id", "interactive_reply_id", "button_provenance"):
        raw = str(inbound_metadata.get(key) or "").strip()
        if not raw.startswith(f"{PAGE_ACTION_PREFIX}:"):
            continue
        parts = raw.split(":")
        if len(parts) != 3:
            continue
        try:
            return max(0, int(parts[2]))
        except (TypeError, ValueError):
            continue
    return 0


# WhatsApp rejects a reply button whose title is empty or over 20
# characters, and rejects the whole payload on duplicate titles. An
# interactive list allows a longer title plus a description, and at most
# ten rows in total across every section.
_BUTTON_TITLE_LIMIT = 20
_BUTTON_SURFACE_LIMIT = 3
_LIST_TITLE_LIMIT = 24
_LIST_DESCRIPTION_LIMIT = 72
_LIST_ROW_LIMIT = 10
_BUTTON_TITLE_FALLBACK = "العنوان المحفوظ"
_MORE_ROW_TITLE = "عناوين أخرى"
PAGE_ACTION_PREFIX = "nahla_addr_page"


def address_choice_page(
    choices: List[Dict[str, Any]], page: int = 0
) -> Tuple[List[Dict[str, Any]], int, int]:
    """One page of the inventory, the page index, and how many pages.

    Up to ``_LIST_ROW_LIMIT`` choices fit in one showing. Beyond that the
    last row is spent on a paging action, so a page holds one fewer. The
    whole inventory stays reachable either way; nothing is dropped.
    """
    rows = [dict(c) for c in (choices or []) if c]
    if not rows:
        return [], 0, 1
    if len(rows) <= _LIST_ROW_LIMIT:
        return rows, 0, 1
    per_page = _LIST_ROW_LIMIT - 1
    page_count = (len(rows) + per_page - 1) // per_page
    index = max(0, int(page or 0)) % page_count
    start = index * per_page
    return rows[start:start + per_page], index, page_count


_ELISION = "…"


def _address_detail(choice: Dict[str, Any]) -> str:
    """Everything about this address BELOW the city line, in one string."""
    choice = choice or {}
    parts = [
        str(choice.get("district") or "").strip(),
        str(choice.get("address_line") or choice.get("street") or "").strip(),
        str(choice.get("short_address_code") or "").strip(),
    ]
    seen: set = set()
    kept: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            kept.append(part)
    return " · ".join(kept)


def _word_boundary_prefix(texts: List[str]) -> int:
    """Length of the longest shared opening these texts have in common.

    Cut back to a word boundary so a label never starts mid-word.
    """
    if len(texts) < 2:
        return 0
    shortest = min(len(t) for t in texts)
    common = 0
    while common < shortest and len({t[common] for t in texts}) == 1:
        common += 1
    if common <= 0:
        return 0
    head = texts[0][:common]
    cut = max(head.rfind(" "), head.rfind("·"), head.rfind("،"), head.rfind(","))
    return cut + 1 if cut > 0 else 0


def _distinguishing_details(choices: List[Dict[str, Any]], width: int) -> List[str]:
    """Each address's detail line, windowed onto what makes it DIFFERENT.

    Two addresses on the same long street differ only at the end —
    "…بوابة المجمع السكني مبنى 11 شقة 1" against "…مبنى 22 شقة 2". Taking
    the first N characters of each showed the customer the identical
    street twice and hid both buildings, so the rows were distinct to the
    provider and indistinguishable to the person choosing between them.

    So the window starts where they diverge, not where the text does.
    Any two texts that share a prefix differ at the character right after
    it, so a window opened there is distinguishing by construction. Groups
    that still collide (three addresses, two of which agree further) are
    re-cut against their own shared opening until nothing collides or no
    cut is left to make.
    """
    details = [_address_detail(c) for c in choices]
    shown = list(details)
    elided = [False] * len(details)

    def _render(index: int) -> str:
        text = shown[index]
        if elided[index]:
            text = f"{_ELISION} {text}".strip()
        return text[:width].strip()

    for _round in range(8):
        groups: Dict[str, List[int]] = {}
        for index in range(len(shown)):
            groups.setdefault(_render(index).casefold(), []).append(index)
        colliding = [idx for idx in groups.values() if len(idx) > 1]
        if not colliding:
            break
        progressed = False
        for indexes in colliding:
            texts = [shown[i] for i in indexes]
            if len(set(texts)) <= 1:
                # Genuinely the same detail — no window can separate them.
                continue
            cut = _word_boundary_prefix(texts)
            if cut <= 0:
                continue
            for i in indexes:
                remainder = shown[i][cut:].strip()
                if remainder and remainder != shown[i]:
                    shown[i] = remainder
                    elided[i] = True
                    progressed = True
        if not progressed:
            break

    return [
        (f"{_ELISION} {shown[i]}".strip() if elided[i] else shown[i])
        for i in range(len(shown))
    ]


def _labelled_choices(
    choices: List[Dict[str, Any]], limit: int
) -> List[Tuple[Dict[str, Any], str, str, bool]]:
    """Choices paired with an IDENTIFYING title and a describing line.

    Labelling is chosen for the SET, not per row. Escalating greedily —
    the first row keeps the bare city, later rows get a street — produced
    titles that were distinct to the provider and useless to the person
    choosing: "الرياض" beside "الرياض - مبنى 22" tells you what the
    second address is and leaves the first identified only by
    elimination. So one scheme covers every row:

      1. city (plus the national short code) — only when that alone
         already tells every row in this set apart;
      2. otherwise every row carries its own distinguishing detail, the
         part of the address that differs from the others here;
      3. otherwise the row id, for addresses whose stored text really is
         identical and which no label can separate.

    Uniqueness is judged on the same normalized key the wire sanitizer
    de-duplicates on, so a title that survives here survives there.
    """
    from core.product_button_label import normalize_button_title_key  # noqa: PLC0415

    rows = list(choices or [])
    if not rows:
        return []
    descriptions = _distinguishing_details(rows, _LIST_DESCRIPTION_LIMIT)
    title_details = _distinguishing_details(rows, limit)
    bases = [_base_label(choice) for choice in rows]

    def _render(scheme: str) -> List[str]:
        titles: List[str] = []
        for index, choice in enumerate(rows):
            base = bases[index]
            detail = title_details[index]
            if scheme == "base":
                title = base
            else:
                title = _join_within(base, detail, limit)
            if scheme == "id":
                title = _with_id_suffix(title or base, choice, limit)
            titles.append((title or _BUTTON_TITLE_FALLBACK)[:limit].strip())
        return titles

    chosen: List[str] = []
    scheme_used = "id"
    for scheme in ("base", "detail", "id"):
        titles = _render(scheme)
        keys = [normalize_button_title_key(t) for t in titles]
        if all(titles) and len(set(keys)) == len(keys):
            chosen, scheme_used = titles, scheme
            break
    if not chosen:
        chosen, scheme_used = _render("id"), "id"

    needs_detail = scheme_used != "base"
    return [
        (
            rows[index],
            chosen[index],
            descriptions[index][:_LIST_DESCRIPTION_LIMIT].strip(),
            needs_detail,
        )
        for index in range(len(rows))
    ]


def _base_label(choice: Dict[str, Any]) -> str:
    """City plus the national short code — the shared, shortest identity."""
    choice = choice or {}
    city = str(choice.get("city") or "").strip()
    code = str(choice.get("short_address_code") or "").strip()
    return " ".join(part for part in (city, code) if part).strip()


def _join_within(base: str, detail: str, limit: int) -> str:
    """``base - detail``, dropping the base rather than the detail.

    What distinguishes the address is the detail. A label trimmed to fit
    must keep that and lose the city it shares with everything else.
    """
    base = str(base or "").strip()
    detail = str(detail or "").strip()
    if not detail:
        return base
    joined = " - ".join(part for part in (base, detail) if part).strip()
    if len(joined) <= limit:
        return joined
    return detail[:limit].strip() or base


def _with_id_suffix(seed: str, choice: Dict[str, Any], limit: int) -> str:
    """``seed`` plus the row id, trimming the seed rather than the id."""
    address_id = str((choice or {}).get("address_id") or "").strip()
    if not address_id:
        return (seed or _BUTTON_TITLE_FALLBACK)[:limit]
    suffix = f" #{address_id}"
    head = (seed or _BUTTON_TITLE_FALLBACK)[: max(1, limit - len(suffix))].strip()
    return f"{head}{suffix}"[:limit]


def _choice_title(choice: Dict[str, Any]) -> str:
    """A short label for ONE address, from its own stored facts.

    Distinguishing a SET is ``_labelled_choices``' job — one label at a
    time cannot know what it has to differ from.
    """
    labelled = _labelled_choices([dict(choice or {})], _BUTTON_TITLE_LIMIT)
    return labelled[0][1] if labelled else _BUTTON_TITLE_FALLBACK



def structured_consent_action(inbound_metadata: Any) -> Optional[Tuple[str, int]]:
    """The (offer, address) a STRUCTURED customer action named, if any.

    Only an interactive reply id counts. Free text — however it is phrased,
    and whatever an intent detector makes of it — never reaches here, which
    is why a question can no longer become a durable selection.
    """
    if not isinstance(inbound_metadata, dict):
        return None
    for key in ("button_id", "list_reply_id", "interactive_reply_id", "button_provenance"):
        raw = str(inbound_metadata.get(key) or "").strip()
        if not raw.startswith(f"{CONSENT_ACTION_PREFIX}:"):
            continue
        parts = raw.split(":")
        if len(parts) != 3:
            # An id without an offer identity cannot say which showing it
            # answers, so it authorizes nothing.
            continue
        try:
            address_id = int(parts[2])
        except (TypeError, ValueError):
            continue
        if parts[1] and address_id:
            return parts[1], address_id
    return None


def record_turn_address_operation(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    attempt: Any,
    turn_ref: str,
) -> bool:
    """Let the WRITER publish what it just did, for this turn only.

    The guard boundary cannot be trusted to assemble this: it would then be
    the claimant vouching for itself. The selection owner records the
    operation it performed — address, revision and operation reference —
    stamped with the inbound turn it belongs to, and the boundary reads it
    back and verifies it against committed state.
    """
    if conversation is None or attempt is None or not getattr(attempt, "is_actionable", False):
        return False
    meta = _conversation_metadata(conversation)
    payload = dict(attempt.as_dict())
    payload["turn_ref"] = str(turn_ref or "")
    payload["conversation_id"] = int(getattr(conversation, "id", 0) or 0)
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    meta[_TURN_OPERATION_KEY] = payload
    return _write_conversation_metadata(db, conversation, meta)


def read_turn_address_operation(conversation: Any, *, turn_ref: str = "") -> Any:
    """The operation THIS turn performed, or nothing.

    Scoped three ways: same conversation, same inbound turn, and recent.
    An operation from an earlier turn is not this turn's, so it can never
    support this turn's claim.
    """
    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        NO_OPERATION,
        AddressOperationAttempt,
    )

    if conversation is None:
        return NO_OPERATION
    try:
        payload = _conversation_metadata(conversation).get(_TURN_OPERATION_KEY)
        if not isinstance(payload, dict):
            return NO_OPERATION
        conversation_id = int(getattr(conversation, "id", 0) or 0)
        if conversation_id and int(payload.get("conversation_id") or 0) != conversation_id:
            return NO_OPERATION
        if str(payload.get("turn_ref") or "") != str(turn_ref or ""):
            return NO_OPERATION
        recorded = _parse_iso(payload.get("recorded_at"))
        if recorded is None:
            return NO_OPERATION
        if (datetime.now(timezone.utc) - recorded).total_seconds() > _OPERATION_TTL_SECONDS:
            return NO_OPERATION
        return AddressOperationAttempt.from_dict(payload)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — unreadable persisted state proves no operation, which is the fail-closed reading; the caller must not treat it as support
        # Persisted metadata this reader cannot parse — a non-integer
        # conversation id, a truncated payload — is not evidence of an
        # operation. It used to raise here, and the boundary above caught
        # it and sent the unverified claim through untouched.
        logger.debug(
            "[ORDER_FLOW_V2] unreadable turn address operation turn_ref=%s",
            turn_ref,
            exc_info=True,
        )
        return NO_OPERATION


def read_turn_address_operation_for_conversation(
    db: Any,
    *,
    conversation_id: Any,
    turn_ref: str = "",
) -> Any:
    """Same read, from a boundary that holds only the conversation id."""
    from core.customer_address_persistence_evidence import NO_OPERATION  # noqa: PLC0415

    try:
        from models import Conversation  # noqa: PLC0415

        row = db.query(Conversation).filter_by(id=int(conversation_id)).first()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable conversation means no operation, which refuses the claim
        return NO_OPERATION
    return read_turn_address_operation(row, turn_ref=turn_ref)


_OPERATION_TTL_SECONDS = 600

# How long a showing stays answerable. Supersession is the primary
# lifecycle — the next showing replaces the live offer — but supersession
# alone left an offer answerable forever in a conversation that simply
# went quiet, and a button tapped weeks later would still approve the
# revision it named. A showing is a question asked in a conversation, so
# it expires the way a question does. The revision check still applies
# inside the window; this only bounds how long the question stands.
_OFFER_TTL_SECONDS = 7 * 24 * 60 * 60


def _parse_iso(raw: Any) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(str(raw or ""))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def turn_reference(inbound_metadata: Any) -> str:
    """The inbound message this turn is answering — its turn identity."""
    if not isinstance(inbound_metadata, dict):
        return ""
    for key in ("wa_message_id", "message_id", "inbound_message_id", "wamid"):
        value = str(inbound_metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def record_offered_address(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    previous: Any,
    delivery_ref: str = "",
    offer_id: str = "",
) -> None:
    """Remember which address revision was put in front of the customer.

    Called from the DELIVERY boundary, never from a context read. Reading
    the customer's addresses is not presentation: it happens on turns that
    send nothing about an address, and a later reply would then be checked
    against an "offer" nobody ever saw.

    An existing offer is never silently advanced either. A refresh that
    lands between the presentation and the customer's answer must make the
    answer refuse — replacing the offer with the newer revision would
    instead record approval of content that was never shown.
    """
    if conversation is None or previous is None:
        return
    address_id = getattr(previous, "address_id", None)
    fingerprint = str(getattr(previous, "content_fingerprint", "") or "")
    if not address_id or not fingerprint:
        return
    meta = _conversation_metadata(conversation)
    current = meta.get(_OFFER_KEY)
    offer = {
        "offer_id": str(offer_id or ""),
        "address_id": int(address_id),
        "fingerprint": fingerprint,
        "customer_id": int(getattr(conversation, "customer_id", 0) or 0),
        "conversation_id": int(getattr(conversation, "id", 0) or 0),
        "tenant_id": int(tenant_id),
        "offered_at": datetime.now(timezone.utc).isoformat(),
        "delivery_ref": str(delivery_ref or ""),
    }
    if isinstance(current, dict) and all(
        current.get(k) == offer[k]
        for k in ("offer_id", "address_id", "fingerprint", "customer_id", "tenant_id")
    ):
        return
    meta[_OFFER_KEY] = offer
    _write_conversation_metadata(db, conversation, meta)


def record_offered_address_set(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    candidates: Any,
    delivery_ref: str = "",
    offer_id: str = "",
) -> None:
    """Remember the set of addresses offered for an explicit choice.

    With several candidates there is deliberately no reusable default, so
    the only way forward is the customer naming one. Recording what was
    offered is what makes that naming bindable.
    """
    if conversation is None or not candidates:
        return
    offered = [
        {"address_id": int(c["address_id"]), "fingerprint": str(c["fingerprint"])}
        for c in candidates
        if c.get("address_id") and c.get("fingerprint")
    ]
    if not offered:
        return
    meta = _conversation_metadata(conversation)
    payload = {
        "offer_id": str(offer_id or ""),
        "customer_id": int(getattr(conversation, "customer_id", 0) or 0),
        "conversation_id": int(getattr(conversation, "id", 0) or 0),
        "tenant_id": int(tenant_id),
        "offered_at": datetime.now(timezone.utc).isoformat(),
        "delivery_ref": str(delivery_ref or ""),
        "addresses": offered,
    }
    existing = meta.get(_OFFER_SET_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("addresses") == offered
        and str(existing.get("offer_id") or "") == str(offer_id or "")
    ):
        return
    meta[_OFFER_SET_KEY] = payload
    _write_conversation_metadata(db, conversation, meta)


def record_presented_address_offer(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    presentation: Any,
    delivery_ref: str = "",
    duplicate_suppressed: bool = False,
    delivered_action_ids: Optional[Sequence[str]] = None,
) -> bool:
    """Record what a reply ACTUALLY reached the customer with.

    Called only from the successful-send boundary, with the OUTBOUND
    message identity. Producing a reply is not showing it: a reply that is
    blocked, suppressed, or fails in transport never happened as far as
    the customer is concerned, and recording a presentation for it would
    let a later tap approve something nobody saw.

    ``delivered_action_ids`` are the action ids present in the payload as
    it left, read back after every sanitizer had its say. The receipt is
    built from THAT, not from what the reply intended: the wire layer
    drops a choice whose title collides with an earlier one, and recording
    the intended set authorized addresses the customer was never able to
    see or tap. An id that is not in the delivered payload is not in the
    offer. Passing ``None`` means the caller could not observe the wire —
    then nothing is recorded, because an unverified showing is exactly the
    thing this function exists to refuse.

    ``duplicate_suppressed`` is a send that the outbound dedup answered
    with an earlier message's id. That proves the earlier message, not a
    new showing — so it may only reaffirm an identical offer that is
    already recorded, never create a new one.
    """
    if conversation is None or presentation is None or presentation.is_empty:
        return False
    if not str(delivery_ref or "").strip():
        # No outbound identity means no proof of which message carried it.
        return False
    offer_id = str(getattr(presentation, "offer_id", "") or "")
    if not offer_id:
        return False
    if duplicate_suppressed and not _offer_already_recorded(
        conversation, tenant_id=tenant_id, presentation=presentation,
    ):
        return False

    delivered = {str(a).strip() for a in (delivered_action_ids or ()) if str(a).strip()}
    if not delivered:
        return False

    def _was_delivered(address_id: Any) -> bool:
        return consent_action_id(offer_id, address_id) in delivered

    recorded = False
    if (
        presentation.address_id
        and presentation.fingerprint
        and _was_delivered(presentation.address_id)
    ):
        record_offered_address(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            previous=_PresentedAddress(
                address_id=int(presentation.address_id),
                content_fingerprint=str(presentation.fingerprint),
            ),
            delivery_ref=delivery_ref,
            offer_id=offer_id,
        )
        recorded = True
    shown = [
        dict(choice)
        for choice in (presentation.choices or ())
        if _was_delivered(choice.get("address_id"))
    ]
    if shown:
        record_offered_address_set(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            candidates=shown,
            delivery_ref=delivery_ref,
            offer_id=offer_id,
        )
        recorded = True
    return recorded


def delivered_address_action_ids(payload: Any) -> List[str]:
    """Every address action id present in an outbound payload as sent.

    Reads the payload back rather than trusting what was handed to the
    transport, so a choice a sanitizer removed cannot be recorded as
    shown. Both supported surfaces are covered: reply buttons and
    interactive-list rows.
    """
    if not isinstance(payload, dict):
        return []
    action = ((payload.get("interactive") or {}).get("action") or {})
    if not isinstance(action, dict):
        return []
    out: List[str] = []
    for button in action.get("buttons") or ():
        if not isinstance(button, dict):
            continue
        reply = button.get("reply") if isinstance(button.get("reply"), dict) else {}
        candidate = str(reply.get("id") or "").strip()
        if candidate.startswith(f"{CONSENT_ACTION_PREFIX}:"):
            out.append(candidate)
    for section in action.get("sections") or ():
        if not isinstance(section, dict):
            continue
        for row in section.get("rows") or ():
            if not isinstance(row, dict):
                continue
            candidate = str(row.get("id") or "").strip()
            if candidate.startswith(f"{CONSENT_ACTION_PREFIX}:"):
                out.append(candidate)
    return out


def _offer_already_recorded(
    conversation: Any,
    *,
    tenant_id: int,
    presentation: Any,
) -> bool:
    """True when this exact showing is already the recorded live offer."""
    meta = _conversation_metadata(conversation)
    offer_id = str(getattr(presentation, "offer_id", "") or "")
    single = meta.get(_OFFER_KEY)
    if isinstance(single, dict) and str(single.get("offer_id") or "") == offer_id:
        return True
    offer_set = meta.get(_OFFER_SET_KEY)
    if isinstance(offer_set, dict) and str(offer_set.get("offer_id") or "") == offer_id:
        return True
    return False


@dataclass(frozen=True)
class _PresentedAddress:
    """Minimal shape ``record_offered_address`` reads an offer from."""

    address_id: int
    content_fingerprint: str


def apply_explicit_address_selection(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    address_id: int,
    order_prep: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    offer_id: str = "",
) -> Dict[str, Any]:
    """Select one of the addresses this conversation offered, by id.

    The bounded structured selection path for the several-candidates case.
    It accepts only an address that was actually offered here, at the
    revision it was offered at, and returns the ordinary confirmed-address
    patch so checkout continues exactly as it does after any confirmation.
    """
    from core.customer_address_candidates import (  # noqa: PLC0415
        SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        resolve_customer_address_selection,
    )
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415
    from core.order_context_builder import _resolved_address_to_shipping_context  # noqa: PLC0415

    offered, offer_identity = _offered_revisions(
        tenant_id=int(tenant_id), conversation=conversation,
        offer_id=offer_id,
    )
    if int(address_id) not in offered:
        return {}
    # The selection's own idempotency identity comes from the ACTION the
    # customer took, not from when an offer happened to be stored. One
    # action redelivered is the same selection; a new tap is a new one.
    operation_ref = f"{offer_identity}:{turn_reference(inbound_metadata)}"
    customer_id = int(getattr(conversation, "customer_id", 0) or 0)
    # An address already accepted for this order does NOT end the choice.
    # Bailing out here was how "actually, send it to the other one" died:
    # the first tap accepted address A, and every later tap returned an
    # empty patch, so the order kept A while the customer was told to
    # choose. A tap is only refused when it names a showing that is not
    # live, or a revision that changed since it was shown — both checked
    # below. Changing an accepted address is a supported lifecycle.

    resolution = resolve_customer_address_selection(
        db, tenant_id=int(tenant_id), customer_id=customer_id,
    )
    chosen = next(
        (a for a in resolution.selectable if a.address_id == int(address_id)), None
    )
    if chosen is None or chosen.fingerprint != offered[int(address_id)]:
        # Not offered, or changed since it was offered.
        return {}

    previous = _resolved_address_to_shipping_context(chosen)
    attempt = _record_selection_for_confirmed_address(
        db,
        tenant_id=int(tenant_id),
        previous=previous,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=chosen.fingerprint,
        operation_ref=operation_ref,
    )
    if not getattr(attempt, "is_actionable", False):
        return {}
    # The writer publishes what it did, for this turn only. Nothing
    # downstream asserts on its own behalf that a save happened.
    record_turn_address_operation(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        attempt=attempt,
        turn_ref=turn_reference(inbound_metadata),
    )
    patch = _replace_order_delivery_address(previous)
    patch["customer_confirmed_previous_address"] = True
    patch["shipping_source"] = "customer_selected_address"
    return patch


def _replace_order_delivery_address(previous: Any) -> Dict[str, Any]:
    """The order's delivery address becomes THIS address and nothing else.

    ``_shipping_context_to_prep_patch`` writes the chosen address's own
    fields, including the empty ones — but the acceptance markers a
    previous address left behind (a pin, a maps URL, an accepted status)
    are not among them. Merged into retained order state they would keep
    asserting the earlier address, so a switch would carry the old
    coordinates under the new city. Every acceptance marker the new
    address does not itself supply is cleared.
    """
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

    patch = _shipping_context_to_prep_patch(previous)
    for key in _ACCEPTING_PREP_FIELDS:
        if key not in patch:
            patch[key] = None
    patch["address_candidate_only"] = False
    return patch


def _offered_revisions(
    *,
    tenant_id: int,
    conversation: Any,
    offer_id: str = "",
) -> Tuple[Dict[int, str], str]:
    """The revisions THIS showing presented, and its opaque identity.

    Both offer shapes count: a single address put forward for confirmation
    and a set put forward for a choice. A customer who was shown one
    address must be able to accept it structurally, not only choose from a
    list.

    When the action names an ``offer_id``, only offers carrying that exact
    id are considered. A superseded showing is not the one the customer
    answered, so a tap kept from it matches nothing — which is what stops
    an old button from approving a revision presented later.

    Scoped to tenant, customer AND conversation: an offer recorded in one
    conversation never authorizes an action arriving in another.
    """
    meta = _conversation_metadata(conversation)
    customer_id = int(getattr(conversation, "customer_id", 0) or 0)
    conversation_id = int(getattr(conversation, "id", 0) or 0)
    wanted = str(offer_id or "").strip()
    offered: Dict[int, str] = {}
    identity = ""

    def _scoped(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            if int(payload.get("tenant_id") or 0) != int(tenant_id):
                return False
            if customer_id and int(payload.get("customer_id") or 0) != customer_id:
                return False
            stored_conversation = int(payload.get("conversation_id") or 0)
            if conversation_id and stored_conversation and stored_conversation != conversation_id:
                return False
        except (TypeError, ValueError):
            return False
        stored_offer = str(payload.get("offer_id") or "")
        if wanted and stored_offer != wanted:
            return False
        if not stored_offer:
            # An offer with no identity predates this contract and cannot
            # say which message it was; it authorizes nothing.
            return False
        offered_at = _parse_iso(payload.get("offered_at"))
        if offered_at is None:
            # No showing time means nothing bounds it; fail closed.
            return False
        if (datetime.now(timezone.utc) - offered_at).total_seconds() > _OFFER_TTL_SECONDS:
            return False
        return True

    offer_set = meta.get(_OFFER_SET_KEY)
    if _scoped(offer_set):
        for entry in offer_set.get("addresses") or []:
            try:
                offered[int(entry["address_id"])] = str(entry["fingerprint"])
            except (KeyError, TypeError, ValueError):
                continue
        identity = str(offer_set.get("offer_id") or "")

    single = meta.get(_OFFER_KEY)
    if _scoped(single):
        try:
            offered.setdefault(
                int(single.get("address_id") or 0), str(single.get("fingerprint") or "")
            )
        except (TypeError, ValueError):
            pass
        identity = identity or str(single.get("offer_id") or "")
    offered.pop(0, None)
    return offered, identity


def apply_structured_address_consent(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    order_prep: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Durable selection from a STRUCTURED customer action, and only that.

    Consent about an address is a lifecycle event, not a reading of words.
    A structured reply id names the exact address the customer tapped: it
    cannot be a question, and no phrasing produces it. Free text still
    drives the turn's checkout exactly as before — it simply never writes
    a durable selection, which is the thing a "saved / adopted" claim
    would rest on.

    Idempotent: tapping the same choice twice selects once.
    """
    action = structured_consent_action(inbound_metadata)
    if action is None:
        return {}
    offer_id, address_id = action
    return apply_explicit_address_selection(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        address_id=int(address_id),
        order_prep=order_prep,
        inbound_metadata=inbound_metadata,
        offer_id=offer_id,
    )


def read_offered_address(
    *,
    tenant_id: int,
    conversation: Any,
) -> Optional[Dict[str, Any]]:
    """The address revision this conversation last offered, if any."""
    offer = _conversation_metadata(conversation).get(_OFFER_KEY)
    if not isinstance(offer, dict):
        return None
    try:
        if int(offer.get("tenant_id") or 0) != int(tenant_id):
            return None
        conversation_customer = int(getattr(conversation, "customer_id", 0) or 0)
        if conversation_customer and int(offer.get("customer_id") or 0) != conversation_customer:
            return None
        if not int(offer.get("address_id") or 0) or not str(offer.get("fingerprint") or ""):
            return None
    except (TypeError, ValueError):
        return None
    return dict(offer)


def _record_selection_for_confirmed_address(
    db: Any,
    *,
    tenant_id: int,
    previous: Any,
    selection_source: str,
    expected_fingerprint: str = "",
    operation_ref: str = "",
) -> Any:
    """Persist the customer's explicit choice of THIS address revision.

    Bound to the exact revision that was just reviewed: if the stored
    content changed since it was read, nothing is written and the caller
    treats the address as unconfirmed. Idempotent — confirming the same
    revision twice writes once.

    Returns the ``AddressOperationAttempt`` describing what was written, or
    ``NO_OPERATION``. The WRITER produces it, so nothing downstream has to
    assert on its own behalf that an operation happened.
    """
    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        NO_OPERATION,
        AddressOperation,
        AddressOperationAttempt,
    )

    address_id = getattr(previous, "address_id", None)
    if not address_id:
        return NO_OPERATION
    try:
        from core.customer_address_candidates import (  # noqa: PLC0415
            record_explicit_address_selection,
        )
        from models import CustomerAddress  # noqa: PLC0415

        row = (
            db.query(CustomerAddress)
            .filter_by(tenant_id=int(tenant_id), id=int(address_id))
            .first()
        )
        if row is None or not getattr(row, "customer_id", None):
            return NO_OPERATION
        result = record_explicit_address_selection(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(row.customer_id),
            address_id=int(address_id),
            selection_source=selection_source,
            expected_fingerprint=(
                expected_fingerprint
                or str(getattr(previous, "content_fingerprint", "") or "")
            ),
            operation_ref=operation_ref,
        )
        if not result.selected:
            return NO_OPERATION
        return AddressOperationAttempt(
            operation=AddressOperation.ADOPT_SELECTION,
            tenant_id=int(tenant_id),
            customer_id=int(row.customer_id),
            address_id=int(address_id),
            fingerprint=str(result.fingerprint or ""),
            operation_ref=str(operation_ref or ""),
        )
    except Exception:  # noqa: BLE001
        return NO_OPERATION


# Fields that, on their own, make ``has_accepted_delivery_address`` true.
# An unselected candidate must never contribute them.
_ACCEPTING_PREP_FIELDS = (
    "short_address_code",
    "google_maps_url",
    "delivery_address_url",
    "latitude",
    "longitude",
    "delivery_location_lat",
    "delivery_location_lng",
    "delivery_address_status",
    "pending_delivery_location",
    "whatsapp_location",
)


def _candidate_context_only(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Strip everything that would mark the order's address as accepted."""
    out = {k: v for k, v in patch.items() if k not in _ACCEPTING_PREP_FIELDS}
    out.pop("shipping_source", None)
    out["address_candidate_only"] = True
    return out


def _identity_first_name(ctx: Any) -> str:
    identity = getattr(ctx, "identity", None)
    if identity is None:
        return ""
    first = str(getattr(identity, "first_name", "") or "").strip()
    if first:
        return first
    operational = str(getattr(identity, "operational_name", "") or "").strip()
    if operational:
        return operational.split()[0]
    return ""


def _engine_modes_to_v2(result: Any) -> Dict[str, str]:
    modes = dict(getattr(result, "missing_modes", None) or {})
    out: Dict[str, str] = {}
    if modes.get("name"):
        out["customer_name"] = str(modes["name"])
    if modes.get("city"):
        out["city"] = str(modes["city"])
    if modes.get("delivery_address"):
        out["delivery_address"] = str(modes["delivery_address"])
    return out


def _fallback_modes_from_known_previous(
    order_prep: Dict[str, Any],
    known_previous: Dict[str, str],
) -> Dict[str, str]:
    modes: Dict[str, str] = {}
    if known_previous.get("city") and not str(order_prep.get("city") or "").strip():
        modes["city"] = MODE_CONFIRM
    if (
        (known_previous.get("short_address") or known_previous.get("maps_url"))
        and not has_accepted_delivery_address(order_prep)
    ):
        modes["delivery_address"] = MODE_CONFIRM
    return modes


def load_checkout_reply_context(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> CheckoutReplyContext:
    """Load missing slots + confirm/ask modes from persisted customer/order context."""
    prep = dict(order_prep or {})
    bs = dict(brain_state or {})
    missing = compute_v2_missing_fields(
        prep,
        brain_state=bs,
        whatsapp_phone=customer_phone,
        db=db,
        tenant_id=tenant_id,
        conversation=conversation,
        inbound_metadata=inbound_metadata,
    )

    ctx = None
    known_previous: Dict[str, str] = {}
    address_choices: List[Dict[str, Any]] = []
    field_modes: Dict[str, str] = {}
    presentation = AddressPresentation()
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415
        from core.order_missing_fields_engine import resolve_flow_missing_fields  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            brain_state=bs,
            inbound_metadata=inbound_metadata,
            build_source="order_flow_v2_reply",
        )
        previous_ctx = getattr(ctx, "known_previous_address", None)
        known_previous = _shipping_context_dict(previous_ctx)
        address_choices = [dict(c) for c in getattr(ctx, "known_address_candidates", ()) or ()]
        # One showing carries one page. The customer may have more saved
        # addresses than any single WhatsApp message can hold; a paging
        # action moves between pages so none of them is unreachable.
        page_choices, page_index, page_count = address_choice_page(
            address_choices, structured_page_request(inbound_metadata),
        )
        # PREPARED, not recorded. Reading context is not presentation: this
        # runs on turns that never mention an address, and recording an
        # offer here made a later reply answerable to something nobody saw.
        # ``record_presented_address_offer`` records it when a reply that
        # actually presents it is delivered.
        presentation = AddressPresentation(
            address_id=int(getattr(previous_ctx, "address_id", 0) or 0),
            fingerprint=str(getattr(previous_ctx, "content_fingerprint", "") or ""),
            choices=tuple(dict(c) for c in page_choices),
            # One identity per SHOWING, not per preparation. It reaches
            # the customer inside the action ids, and is only persisted
            # if that exact message is proven sent — so a showing that
            # never left, or one that has since been superseded, matches
            # no later tap. Re-preparing the same inbound turn reproduces
            # the same identity, so a redelivery is recognisably the same
            # message instead of a second live offer.
            offer_id=offer_id_for_showing(
                tenant_id=tenant_id,
                conversation=conversation,
                turn_ref=turn_reference(inbound_metadata),
                choices=page_choices,
                address_id=int(getattr(previous_ctx, "address_id", 0) or 0),
                fingerprint=str(getattr(previous_ctx, "content_fingerprint", "") or ""),
                page=page_index,
            ),
            page=page_index,
            page_count=page_count,
        )
        _, engine_result = resolve_flow_missing_fields(
            prep,
            brain_state=bs,
            whatsapp_phone=customer_phone,
            db=db,
            tenant_id=tenant_id,
            conversation=conversation,
            inbound_metadata=inbound_metadata,
        )
        if engine_result is not None:
            missing = compute_v2_missing_fields(
                prep,
                brain_state=bs,
                whatsapp_phone=customer_phone,
                db=db,
                tenant_id=tenant_id,
                conversation=conversation,
                inbound_metadata=inbound_metadata,
            )
            field_modes = _engine_modes_to_v2(engine_result)
        elif known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)
    except Exception:  # noqa: BLE001
        if known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)

    first_name = _identity_first_name(ctx) if ctx is not None else ""
    return CheckoutReplyContext(
        missing_fields=list(missing),
        field_modes=field_modes,
        known_previous=known_previous,
        identity_first_name=first_name,
        address_choices=address_choices,
        presentation=presentation,
    )


def load_identity_first_name(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
) -> str:
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            build_source="order_flow_v2_greeting",
        )
        return _identity_first_name(ctx)
    except Exception:  # noqa: BLE001
        return ""


def apply_previous_address_confirmation(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Promote saved customer address when the customer confirms previous/on-file address."""
    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import detect_edit_intent_facts  # noqa: PLC0415
    from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: PLC0415

    text = str(message or "").strip()
    if not text:
        return {}
    edit = detect_edit_intent_facts(text, order_prep)
    if not (edit.previous_address_confirmed or is_address_on_file_claim(text)):
        return {}

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message=text,
        build_source="order_flow_v2_address_claim",
    )
    if ctx.known_previous_address is None:
        return {}

    if edit.previous_address_confirmed:
        from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

        if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
            return {}
        if has_accepted_delivery_address(dict(order_prep or {})):
            return {}
        previous = ctx.known_previous_address
        # Free text continues this turn's checkout exactly as it always
        # has. What it deliberately does NOT do is write a durable
        # selection.
        #
        # A phrase detector cannot tell consent from an inquiry: "هل عنواني
        # محفوظ عندكم؟" and "نفس العنوان السابق" both read as
        # ``previous_address_confirmed``. Widening or narrowing that
        # detector would be customer-intent regex repair, which the
        # intelligence policy forbids and which would fail on the next
        # phrasing anyway. So the durable act needs a durable signal:
        # ``apply_structured_address_consent`` writes the selection when
        # the customer takes a structured action naming the address they
        # were shown. Until then this turn holds the address in order
        # state, and no reply may claim it was saved or adopted.
        # CONTEXT ONLY. The words cannot tell an inquiry from consent —
        # "هل عنواني محفوظ عندكم؟" and "نفس العنوان السابق" both read as
        # ``previous_address_confirmed`` — so this path must not mark the
        # order's delivery address accepted either. It carries the known
        # fields forward so the reply can ask about them, and the accepted
        # state waits for the structured action.
        patch = _candidate_context_only(_shipping_context_to_prep_patch(previous))
        patch["address_selection_durable"] = False
        patch["address_reference_detected"] = True
        return patch

    # On-file claim without explicit confirm phrase — reply layer confirms; do not auto-apply.
    return {}


def apply_delivery_continuation_address_patch(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Accept saved / evidenced address when customer asks delivery to their address."""
    from modules.ai.order_flow_v2.slot_ownership import promote_address_evidence_patch  # noqa: PLC0415

    prep = dict(order_prep or {})
    patch = promote_address_evidence_patch(prep)
    merged = {**prep, **patch}
    if has_accepted_delivery_address(merged):
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = patch.get("shipping_source") or "delivery_continuation"
        return patch

    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message="",
        build_source="order_flow_v2_delivery_continuation",
    )
    previous = ctx.known_previous_address
    if previous is None:
        return patch
    if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
        return patch

    saved = _shipping_context_to_prep_patch(previous)
    if not bool(getattr(previous, "explicitly_selected", False)):
        # An imported candidate the customer has never selected is offered,
        # never adopted. Withholding only the confirmation flag is not
        # enough: copying the locating artefacts alone already makes
        # ``has_accepted_delivery_address`` true, so the order would be
        # treated as having an accepted delivery address the customer never
        # chose. Carry only the context fields, which re-ask nothing the
        # candidate already answers, and leave acceptance to an explicit
        # selection.
        saved = _candidate_context_only(saved)
        patch.update(saved)
        return patch

    saved["customer_confirmed_previous_address"] = True
    saved["shipping_source"] = "delivery_continuation_saved_address"
    patch.update(saved)
    return patch
