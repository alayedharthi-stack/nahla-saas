"""
services/product_readiness.py
─────────────────────────────
Product Readiness Engine — answers the question:

    "Is this product ready to be published / used on channel X?"

For every product × every registered channel, returns a structured
verdict the dashboard can render as a colored badge + a per-field
warning list. Used by:

  * The Product Studio detail drawer (live preview while typing).
  * The Product Studio grid (per-row "X channels ready" badge).
  * The product listing endpoint (filtering: "show only WhatsApp-
    ready products" / "show products missing Meta requirements").
  * The future export jobs (Phase 3 Meta publish, Phase 4 Google
    publish) — both gate on ``ChannelReadiness.ready`` before
    queueing a row for outbound sync.

Design
──────
The engine is intentionally **pure**: it takes a product (or a
plain dict draft) + a ``ChannelSpec`` and returns a JSON-friendly
DTO. No DB session, no HTTP, no logging side-effects. That makes
it:

  * Cheap enough to call on every keystroke through the preview
    endpoint (the dashboard POSTs the in-flight form to
    ``/readiness/preview`` debounced at ~250ms).
  * Testable without fixtures — pin a few in-memory products,
    assert the verdict matches the spec.

Single source of truth
──────────────────────
The engine ONLY reads from :func:`channel_specs.extract_field` for
field values. It NEVER hard-codes a column name. That's the
guarantee that when Phase 2 promotes JSONB fields to top-level
columns, the engine doesn't change — only ``extract_field`` does.

Output shape — stable JSON contract for the dashboard:

    {
      "channel":        "meta_catalog",
      "label_ar":       "Meta Catalog",
      "icon_key":       "meta",
      "enabled":        true,
      "ready":          false,
      "score_pct":      75,
      "blocking_count": 1,
      "warnings_count": 2,
      "fields": [
        {
          "field":     "title",
          "label_ar":  "العنوان",
          "state":     "ok",            # ok | warn | error | missing
          "count":     96,
          "limit":     200,
          "soft_at":   170,
          "message":   "",
          "rationale": "Meta يحدّ العنوان بـ 200 حرف..."
        },
        ...
      ]
    }
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.channel_specs import (
    ChannelSpec,
    FieldConstraint,
    all_specs,
    extract_field,
    get_spec,
)


# ─────────────────────────────────────────────────────────────────────────────
# Status enum (string-typed, JSON-friendly)
# ─────────────────────────────────────────────────────────────────────────────

STATE_OK      = "ok"        # passes all checks
STATE_WARN    = "warn"      # passes hard checks, soft threshold hit
STATE_ERROR   = "error"     # hard fail (over limit, regex mismatch, etc.)
STATE_MISSING = "missing"   # field required by channel but empty

# Per-field score weights when computing ``score_pct``. ``ok`` = 1.0,
# ``warn`` = 0.7, ``error`` / ``missing`` for required = 0, for
# optional = 0.5 (the merchant gets credit for the optional field
# being optional even when blank — but not full credit because they
# could improve their catalog by filling it).
_SCORE_WEIGHT = {
    STATE_OK:      1.00,
    STATE_WARN:    0.70,
    STATE_ERROR:   0.00,   # only used for required fields
    STATE_MISSING: 0.00,   # only used for required fields
}


# ─────────────────────────────────────────────────────────────────────────────
# DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FieldStatus:
    """Per-field verdict from the readiness engine.

    Keep this JSON-friendly — the dashboard renders this dict
    directly into the live counter / warning panel.
    """
    field:     str
    label_ar:  str
    state:     str
    count:     Optional[int] = None      # current length (for string fields)
    limit:     Optional[int] = None      # hard max
    soft_at:   Optional[int] = None      # warn threshold (= floor(soft_pct * limit))
    message:   str = ""
    rationale: str = ""
    required:  bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field":     self.field,
            "label_ar":  self.label_ar,
            "state":     self.state,
            "count":     self.count,
            "limit":     self.limit,
            "soft_at":   self.soft_at,
            "message":   self.message,
            "rationale": self.rationale,
            "required":  self.required,
        }


@dataclass
class ChannelReadiness:
    """Channel-wide verdict — aggregates field statuses."""
    channel:        str
    label_ar:       str
    icon_key:       str
    enabled:        bool
    ready:          bool
    score_pct:      int
    blocking_count: int
    warnings_count: int
    fields:         List[FieldStatus] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel":        self.channel,
            "label_ar":       self.label_ar,
            "icon_key":       self.icon_key,
            "enabled":        self.enabled,
            "ready":          self.ready,
            "score_pct":      self.score_pct,
            "blocking_count": self.blocking_count,
            "warnings_count": self.warnings_count,
            "fields":         [fs.to_dict() for fs in self.fields],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Field-level computation
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_str(v: Any) -> str:
    """Coerce a raw field value into the form the validator expects.

    Returns an empty string for None / empty / whitespace — so the
    "missing" check is a simple ``not s`` test downstream. Numbers
    get coerced to their string repr so a price stored as ``"95"``
    or ``95`` both behave consistently.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        # Avoid the ``isinstance(v, int)`` trap — booleans are ints in
        # Python and we don't want True coerced to "True".
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x not in (None, ""))
    return str(v).strip()


def _check_field(fc: FieldConstraint, raw_value: Any) -> FieldStatus:
    """Compute the verdict for ONE field × ONE constraint."""
    label = fc.label_ar or fc.field
    s = _normalise_str(raw_value)
    soft_at: Optional[int] = None
    if fc.max_length and fc.soft_warn_at_pct:
        soft_at = int(fc.max_length * fc.soft_warn_at_pct)

    # ── 1. Missing handling ──────────────────────────────────────
    if not s:
        if fc.required:
            return FieldStatus(
                field=fc.field, label_ar=label,
                state=STATE_MISSING,
                count=0, limit=fc.max_length, soft_at=soft_at,
                message=f"{label}: مطلوب", rationale=fc.rationale_ar,
                required=True,
            )
        return FieldStatus(
            field=fc.field, label_ar=label,
            state=STATE_OK,
            count=0, limit=fc.max_length, soft_at=soft_at,
            message="", rationale=fc.rationale_ar,
            required=False,
        )

    count = len(s)

    # ── 2. Hard length / regex / enum checks ─────────────────────
    if fc.min_length is not None and count < fc.min_length:
        return FieldStatus(
            field=fc.field, label_ar=label, state=STATE_ERROR,
            count=count, limit=fc.max_length, soft_at=soft_at,
            message=f"{label}: أقل من {fc.min_length} حرف",
            rationale=fc.rationale_ar, required=fc.required,
        )

    if fc.max_length is not None and count > fc.max_length:
        return FieldStatus(
            field=fc.field, label_ar=label, state=STATE_ERROR,
            count=count, limit=fc.max_length, soft_at=soft_at,
            message=f"{label}: تجاوز الحد ({count}/{fc.max_length})",
            rationale=fc.rationale_ar, required=fc.required,
        )

    if fc.allowed_values is not None and s.lower() not in {a.lower() for a in fc.allowed_values}:
        return FieldStatus(
            field=fc.field, label_ar=label, state=STATE_ERROR,
            count=count, limit=fc.max_length, soft_at=soft_at,
            message=f"{label}: قيمة غير مقبولة",
            rationale=fc.rationale_ar, required=fc.required,
        )

    if fc.regex is not None and not re.fullmatch(fc.regex, s):
        return FieldStatus(
            field=fc.field, label_ar=label, state=STATE_ERROR,
            count=count, limit=fc.max_length, soft_at=soft_at,
            message=f"{label}: الشكل غير صحيح",
            rationale=fc.rationale_ar, required=fc.required,
        )

    # ── 3. Soft warning — close to max ───────────────────────────
    if soft_at is not None and count >= soft_at:
        return FieldStatus(
            field=fc.field, label_ar=label, state=STATE_WARN,
            count=count, limit=fc.max_length, soft_at=soft_at,
            message=f"{label}: قارب الحد ({count}/{fc.max_length})",
            rationale=fc.rationale_ar, required=fc.required,
        )

    # ── 4. All good ──────────────────────────────────────────────
    return FieldStatus(
        field=fc.field, label_ar=label, state=STATE_OK,
        count=count, limit=fc.max_length, soft_at=soft_at,
        message="", rationale=fc.rationale_ar, required=fc.required,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Channel-level aggregation
# ─────────────────────────────────────────────────────────────────────────────

def compute_readiness(product: Any, spec: ChannelSpec) -> ChannelReadiness:
    """Evaluate *product* against every constraint in *spec*.

    ``product`` can be any of:

    * A SQLAlchemy ``Product`` instance.
    * A plain dict shaped like the API payload (used by the preview
      endpoint when the merchant is mid-edit and the row isn't
      persisted yet).
    * A ``SimpleNamespace`` (test fixtures).

    Returns a :class:`ChannelReadiness` with one ``FieldStatus`` per
    field in the spec, plus aggregate counts:

    * ``ready``         — True only when zero blocking states.
    * ``blocking_count`` — number of required fields with state
      ``error`` or ``missing``.
    * ``warnings_count`` — number of fields with state ``warn`` OR
      optional fields with state ``error`` / ``missing``.
    * ``score_pct``     — weighted average (see ``_SCORE_WEIGHT``)
      scaled to 0-100. Used for the visual progress bar.
    """
    field_statuses: List[FieldStatus] = []
    for fc in spec.fields:
        value = extract_field(product, fc.field)
        field_statuses.append(_check_field(fc, value))

    blocking = 0
    warnings = 0
    score_sum = 0.0
    score_max = 0.0
    for fs in field_statuses:
        # Per-field weight in the score: required fields count 1.0,
        # optional fields count 0.5 — incentivising filling them but
        # not penalising the same as missing required.
        weight = 1.0 if fs.required else 0.5
        score_max += weight

        if fs.state == STATE_OK:
            score_sum += _SCORE_WEIGHT[STATE_OK] * weight
        elif fs.state == STATE_WARN:
            score_sum += _SCORE_WEIGHT[STATE_WARN] * weight
            warnings += 1
        elif fs.state in (STATE_ERROR, STATE_MISSING):
            if fs.required:
                blocking += 1
                # zero score
            else:
                warnings += 1
                score_sum += 0.50 * weight   # optional blank → half credit

    score_pct = int(round((score_sum / score_max) * 100)) if score_max else 0

    return ChannelReadiness(
        channel        = spec.channel,
        label_ar       = spec.label_ar,
        icon_key       = spec.icon_key,
        enabled        = spec.enabled,
        ready          = blocking == 0,
        score_pct      = score_pct,
        blocking_count = blocking,
        warnings_count = warnings,
        fields         = field_statuses,
    )


def compute_all(product: Any) -> List[ChannelReadiness]:
    """Run :func:`compute_readiness` for every registered channel.

    Iterates the registry's natural order so the dashboard renders
    badges consistently (WhatsApp → Meta → AI → Campaigns → Google).
    """
    return [compute_readiness(product, s) for s in all_specs()]


def compute_for_channel(product: Any, channel: str) -> Optional[ChannelReadiness]:
    spec = get_spec(channel)
    if spec is None:
        return None
    return compute_readiness(product, spec)


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate badge — for the grid (one-line summary per row)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProductBadge:
    """Compact summary the grid renders next to each row.

    The grid can't show 5 channel badges per row (would dwarf the
    product title), so it shows ONE pill summarising the picture:

        "جاهز 3/4 قنوات" → 3 of 4 enabled channels are ready
        "ناقص في 2 قنوات" → ready for 2, has blocking issues on 2
    """
    enabled_total:  int
    ready_count:    int
    warn_count:     int
    blocking_count: int
    score_pct:      int

    @property
    def level(self) -> str:
        """High-level severity for the badge color:

        * ``"green"``  — all enabled channels ready
        * ``"amber"``  — at least one channel has warnings
        * ``"red"``    — at least one enabled channel has blocking errors
        * ``"slate"``  — no channels enabled (shouldn't happen)
        """
        if self.enabled_total == 0:
            return "slate"
        if self.blocking_count > 0:
            return "red"
        if self.warn_count > 0:
            return "amber"
        return "green"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled_total":  self.enabled_total,
            "ready_count":    self.ready_count,
            "warn_count":     self.warn_count,
            "blocking_count": self.blocking_count,
            "score_pct":      self.score_pct,
            "level":          self.level,
        }


def compute_badge(product: Any) -> ProductBadge:
    """One-pill summary for the grid. Considers only ENABLED channels
    so Google (Phase 1: disabled) doesn't drag the score down."""
    readinesses = [r for r in compute_all(product) if r.enabled]
    total = len(readinesses)
    ready = sum(1 for r in readinesses if r.ready)
    warn  = sum(1 for r in readinesses if r.warnings_count > 0 and r.ready)
    block = sum(1 for r in readinesses if r.blocking_count > 0)
    score = int(round(sum(r.score_pct for r in readinesses) / total)) if total else 0
    return ProductBadge(
        enabled_total=total,
        ready_count=ready,
        warn_count=warn,
        blocking_count=block,
        score_pct=score,
    )


__all__ = [
    "ChannelReadiness",
    "FieldStatus",
    "ProductBadge",
    "STATE_ERROR",
    "STATE_MISSING",
    "STATE_OK",
    "STATE_WARN",
    "compute_all",
    "compute_badge",
    "compute_for_channel",
    "compute_readiness",
]
