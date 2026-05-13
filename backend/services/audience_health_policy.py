"""
backend/services/audience_health_policy.py
──────────────────────────────────────────
**Ground rules** for Phase 4 (Audience Health / Preflight /
Audience Intent Classification).

This module is intentionally light on code — it's a single
``IntentLabel`` enum + a documented contract that every future
preflight/audience-health implementation MUST honour. Whoever
builds the actual classifier should import the labels from here
and follow the rules below verbatim.

Why a separate policy module
────────────────────────────
The user explicitly corrected the platform before Phase 4
started: **inactivity alone is NOT a quality risk.** A customer
who hasn't bought / read / replied in 6 months but whose phone is
still a valid, active WhatsApp account is a perfectly legitimate
target for marketing. Re-engagement / win-back campaigns are a
first-class use case of Nahla.

Without this module a well-meaning future engineer could read the
existing recommendation code and conclude that "cold audience"
should drag the Quality Score down. This module exists to make
that conclusion impossible to reach without consciously
violating a written policy.

The two failure modes we are guarding against
─────────────────────────────────────────────

  ❌ FAILURE MODE A: penalising legitimate re-engagement.

      Merchant runs a win-back campaign to dormant-but-valid
      customers. Phones are clean → delivery rate is 95% →
      a tiny share fail with ``not_on_whatsapp``. Our score
      drops because the engineer reasoned "long inactivity =
      low quality audience". WRONG: the merchant did exactly
      what the platform exists for, with clean phones, and got
      punished.

  ❌ FAILURE MODE B: hiding actual bad phones behind "inactive".

      Merchant has 30% of their audience with invalid phone
      formats. Engineer ships a banner that just says "your
      audience seems inactive — consider a stronger offer".
      WRONG: the audience isn't inactive, the phones are bad,
      and the merchant ends up nuking their Meta tier.

Both failure modes have the same root cause: conflating
``inactivity`` with ``phone quality``. They are orthogonal.

The orthogonal dimensions
─────────────────────────

Every customer can be located on a 2D grid:

                       PHONE QUALITY (Meta-relevant)
                       ──────────────────────────────
                       GOOD              BAD

ENGAGE-  ACTIVE        Active buyer      Risky recipient
MENT     (read/buy     (mainstream       (uses platform but
(NOT     <60d)         segment)          phone signal is bad)
Meta-    ─────────     ─────────────     ──────────────────
rele-    WARM          Warm customer     Risky recipient
vant)    (60–180d)     (good win-back    (same column = bad
                       target)           regardless of row)
         ─────────     ─────────────     ──────────────────
         COLD          Cold-but-valid    Unreachable
         (180d+)       (legitimate       (don't send; the
                       reactivation     phone, not the
                       target)           inactivity, is the
                                         problem)
         ─────────     ─────────────     ──────────────────
         NEVER         New /             Unreachable
         (no inbound)  unverified

* The **rows** describe engagement / conversion likelihood.
  Belong to: campaign-planning UI, expected-CTR predictions,
  ROI dashboards.
* The **columns** describe phone quality / Meta reputation risk.
  Belong to: Quality Score, Suppression Engine, send governor.

The platform must treat the two axes independently. Quality
features only consume the COLUMN. Audience-planning features
only consume the ROW. A campaign-launch preflight may surface
both — but it MUST visually separate them so the merchant sees
"these 1,200 customers are cold (engagement warning)" and
"these 40 customers have bad phones (quality risk)" as TWO
different lists, not one fused warning.

Intent labels (Phase 4 contract)
────────────────────────────────

The classifier produces ONE row-label and ONE column-label per
customer, never a fused single label. Surface them as a tuple in
every preflight payload.

Row labels (``EngagementIntent``):
    * ``ACTIVE``       — inbound or read/click ≤ 60 days.
    * ``WARM``         — inbound or read/click 60–180 days.
    * ``COLD``         — last positive signal > 180 days; still
                          reachable.
    * ``DORMANT_VALUE``— ``COLD`` AND lifetime order value above
                          merchant's P75 — surfaced as a
                          high-priority win-back segment.
    * ``NEVER``        — no inbound/engagement ever recorded.

Column labels (``PhoneQuality``):
    * ``CLEAN``        — no quality_risk failures in 180d, never
                          blocked, valid E.164.
    * ``DEGRADED``     — single quality_risk event but recoverable.
    * ``UNREACHABLE``  — repeated ``not_on_whatsapp`` /
                          ``invalid_phone`` / ``permanent_failure``.
    * ``RISKY``        — ``blocked_by_user`` or any active
                          ``CustomerSuppression`` row.

Hard rules for any Phase 4 code that consumes these labels
──────────────────────────────────────────────────────────

  1. The **Quality Score** MUST NOT read ``EngagementIntent`` at
     all. Not as a filter, not as a weight, not as a bonus.
     ``services/quality_score.py`` already documents this; see
     "What this score does NOT use".

  2. The **Suppression Engine** MUST NOT use ``COLD`` /
     ``DORMANT_VALUE`` as a trigger. Suppression is exclusively
     a phone-quality concern. (``services/delivery_quality.py``
     already complies.)

  3. The **pre-send governor** (when built) may surface BOTH
     dimensions to the merchant — but they must be displayed
     and audited separately:
        ``preflight.quality_risk_recipients`` (phone-quality)
        ``preflight.cold_audience_recipients`` (engagement)
     Never collapsed into a single "exclude these N customers"
     bucket. Letting the merchant proceed for one while
     filtering the other is the whole product feature.

  4. **Default UI copy** when a campaign targets a cold audience
     with clean phones:

        "هذه الحملة تستهدف عملاء غير متفاعلين منذ فترة.
         يُفضّل استخدام عرض قوي أو إعادة تنشيط تدريجية —
         ولا يوجد أي خطر على جودة رقمك في الإرسال لهم."

     Note the explicit "no quality risk" reassurance. This is
     the line in the sand: cold ≠ damaging.

  5. **Default UI copy** when a campaign targets clean phones
     PLUS some bad phones:

        "X رقم في جمهور هذه الحملة لن يستلموا الرسالة وقد
         يضرّون بتقييم رقمك (أرقام غير موجودة على واتساب /
         غير صالحة). يُنصح باستبعادهم قبل الإطلاق."

     Note we point at the BAD PHONES, not the cold customers.

Status
──────
This module is the **contract**. The classifier itself, the
preflight endpoint, and the dashboard preflight panel will live
in:

  * ``backend/services/audience_intent.py``       (classifier)
  * ``backend/routers/campaign_preflight.py``     (endpoint)
  * ``dashboard/src/pages/CampaignPreflight.tsx`` (UI)

Do not start any of those without first reading this file end to
end.
"""
from __future__ import annotations

from enum import Enum


class EngagementIntent(str, Enum):
    """Row dimension: engagement / conversion likelihood.

    **Not a quality signal.** See module docstring.
    """
    ACTIVE        = "active"          # inbound/read ≤ 60d
    WARM          = "warm"            # inbound/read 60–180d
    COLD          = "cold"            # >180d but reachable
    DORMANT_VALUE = "dormant_value"   # cold + lifetime value > P75
    NEVER         = "never"           # no positive signal ever


class PhoneQuality(str, Enum):
    """Column dimension: Meta reputation risk.

    **The only dimension** that feeds Quality Score / Suppression.
    """
    CLEAN       = "clean"          # no risk signal in 180d
    DEGRADED    = "degraded"       # one recoverable risk event
    UNREACHABLE = "unreachable"    # repeated hard failures
    RISKY       = "risky"          # blocked / actively suppressed


__all__ = ["EngagementIntent", "PhoneQuality"]
