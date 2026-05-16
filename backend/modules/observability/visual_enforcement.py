"""
modules/observability/visual_enforcement.py
───────────────────────────────────────────
Pure helpers for the WhatsApp webhook's [VISUAL_PRODUCT_ENFORCEMENT]
guard. Kept here (next to ``delivery_mode``) because the two modules
together own the contract "did the customer get what they asked for?".

The webhook owns the I/O — DB lookup, resolver call, attachment
append, log lines — and uses this module ONLY for the deterministic
parts that benefit from unit-tests:

  * :func:`pick_best_candidate_title`
      Choose the most likely product the customer is asking to see
      from the brain-state snapshot persisted on the conversation
      between turns. Falls back to the inbound text so the resolver
      can still fuzzy-match against the synced catalog.

  * :func:`has_visual_marker`
      Cheap check for already-emitted ``[PRODUCT:...]`` /
      ``[MEDIA_KEY:...]`` markers in the LLM reply, so we never
      double-attach when the LLM already did the right thing.

Design notes
────────────
* Pure functions. No DB, no HTTP, no logger calls. Every helper is
  deterministic given its inputs.
* Conservative candidate cascade — strongest signal first
  (``current_product_focus`` was set in the SAME conversation by an
  earlier resolved search) → recent search candidates → previous
  recommendations → raw inbound text. The text fallback hands the
  resolver a fuzzy query so e.g. "أبغى أشوف صورة لعسل السمر" still
  resolves to "عسل السمر" via the catalog's substring match.
* No side effects on the brain_state dict. The webhook decides
  whether and when to mutate state.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


# Source labels (closed enum) — kept short so they fit cleanly in the
# [VISUAL_PRODUCT_ENFORCEMENT] log lines without truncation.
SOURCE_FOCUS              = "current_product_focus"
SOURCE_LAST_SEARCH        = "last_search_candidates[0]"
SOURCE_LAST_RECOMMENDED   = "last_recommended_products[0]"
SOURCE_INBOUND_TEXT       = "inbound_text_fuzzy"
SOURCE_NONE               = "none"


def _first_nonempty_title(items: Any) -> str:
    """Return the first ``.title`` from a list of dicts, or ``""``.

    Defensive: tolerates ``None``, non-list inputs, and items that
    are not dicts. Whitespace-only titles are treated as empty.
    """
    if not isinstance(items, (list, tuple)):
        return ""
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if title:
            return title
    return ""


def pick_best_candidate_title(
    brain_state: Optional[Dict[str, Any]],
    inbound_text: str,
) -> Tuple[str, str]:
    """Return ``(candidate_title, source_label)`` for the enforcer.

    Cascade (first hit wins):
      1. ``brain_state['current_product_focus']['title']``
         — the customer is mid-conversation about ONE product
         (sales / checkout / clarification). The visual ask almost
         certainly refers to it.
      2. ``brain_state['last_search_candidates'][0]['title']``
         — the previous turn surfaced a small numbered list and the
         customer is now asking to see the top hit.
      3. ``brain_state['last_recommended_products'][0]['title']``
         — older / softer signal, still better than free-text.
      4. Trimmed ``inbound_text`` itself
         — the resolver's fuzzy match against the synced catalog
         turns "أبغى أشوف صورة لعسل السمر" into "عسل السمر".

    Returns ``("", SOURCE_NONE)`` only when ALL four are empty (very
    rare — would mean a brand-new conversation with no inbound text,
    which the webhook short-circuits earlier anyway).
    """
    bs = brain_state or {}

    focus = bs.get("current_product_focus")
    if isinstance(focus, dict):
        title = str(focus.get("title") or "").strip()
        if title:
            return title, SOURCE_FOCUS

    title = _first_nonempty_title(bs.get("last_search_candidates"))
    if title:
        return title, SOURCE_LAST_SEARCH

    title = _first_nonempty_title(bs.get("last_recommended_products"))
    if title:
        return title, SOURCE_LAST_RECOMMENDED

    fallback = (inbound_text or "").strip()
    if fallback:
        return fallback, SOURCE_INBOUND_TEXT

    return "", SOURCE_NONE


def has_visual_marker(reply_text: str) -> bool:
    """``True`` if the LLM reply already carries a product / media
    marker — in which case the enforcer MUST stay out of the way.

    Case-insensitive substring check. Cheap on purpose: the full
    extractor runs later in the webhook regardless; this is just
    the short-circuit so we don't double-attach.
    """
    if not reply_text:
        return False
    up = reply_text.upper()
    return ("[PRODUCT:" in up) or ("[MEDIA_KEY:" in up)


__all__ = [
    "SOURCE_FOCUS",
    "SOURCE_LAST_SEARCH",
    "SOURCE_LAST_RECOMMENDED",
    "SOURCE_INBOUND_TEXT",
    "SOURCE_NONE",
    "pick_best_candidate_title",
    "has_visual_marker",
]
