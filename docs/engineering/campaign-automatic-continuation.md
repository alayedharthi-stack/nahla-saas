# Start-once campaign continuation (revised 27 September 2026)

Owner request: a merchant starts a campaign once; the platform completes the
remainder on its own whenever sending is possible, recovers from worker
deaths and restarts, honours manual stops, unsubscribes, offer expiry and
the provider's real restrictions, and never sends a customer a second copy.

This revision supersedes the 25 September "typed marketing-only breaker with
exponential cooldown". That design is the root cause of the incident below.

## Incident: campaign 35 / tenant 33 (24–27 September)

Evidence (production runtime logs, service `nahla-saas`):

| Time (UTC) | Observation |
| --- | --- |
| 24 Sep 09:50, 25 Sep 09:47 | paused `messaging_limit_reached` (TIER_250, 90% budget) — correct, resumed by the capacity scheduler |
| 24 Sep 13:53 → 27 Sep 17:59 | **12 pauses** `provider_throttling` / `post_accept marketing_blocked x25`, each after 74–179 accepted messages |
| 24 Sep 20:51–21:08 | six merchant clicks inside the 15-minute window, all refused (`provider_throttled`) |
| 27 Sep 17:59 | wait record attempt 8 → next automatic check 28 Sep 17:59 (24h); 2,941 accepted, 5,883 queued, 7 failed, 58 excluded |

### First divergence

`POST_ACCEPT_BREAKER_THRESHOLDS["marketing_blocked"] = 25` treated Meta error
**131049** as a *sender-scope* throttle. 131049 ("This message was not
delivered to maintain healthy ecosystem engagement") is Meta's **per-user**
marketing-message limit: the recipient has received too many marketing
templates from all businesses. It says nothing about our number, so about one
recipient in five in this audience produced it regardless of pace. Every
continuation therefore sent ~100–180 recipients, collected 25 receipts within
15 minutes, and stopped; the cooldown doubled each time (1h → 24h) because the
counter never reset after a productive run. Source of truth: Meta's error
catalogue (131049 vs 131048 "Spam rate limit hit", which *is* sender-scoped).

Contributing gaps:

* `active` campaigns whose worker died mid-run (17+ deploy restarts in three
  days) had no automatic continuation — only the merchant's click.
* The merchant's click was refused for 15 minutes after each pause, so the
  campaign looked like it "only moves when resume is pressed".

## Behaviour now

* **131049 is a per-recipient, final outcome** for this campaign: recorded as
  failed-after-accept with `marketing_blocked`, never retried, counted as
  `recipients_marketing_limit`, and **never** a reason to stop sending to
  anyone else. `RECIPIENT_SCOPED_POST_ACCEPT_CODES` documents this.
* **Sender-scope breakers stay**: 131048 spam (5 in 15 min) pauses for the
  merchant; a pre-accept per-minute limit (130429) backs off 5–60 minutes and
  continues on its own; the shared 24h capacity wait is unchanged (recheck
  every 15 minutes, tier upgrades discovered without a dashboard visit).
* **Stalled runs recover automatically** (`resume_stalled_campaigns`, every
  scheduler tick): an `active` campaign with queued recipients whose lease is
  expired by ≥90 s is re-dispatched through the same leased path (atomic
  claims, send guard, zombie resolution → `uncertain`, budget). Backoff 1, 2,
  4 … 30 minutes; after 8 recoveries it pauses as `stalled_repeatedly` for
  review. Wave campaigns get their orphaned `dispatching` wave back to
  `pending`. A merchant stop, a live worker or a paused status always wins.
* **Legacy 131049 pauses are not silently continued.** A campaign the retired
  breaker paused shows `needs_review` and continues only on the merchant's
  explicit resume (its offer may be stale). This is how campaign 35 appears
  after deploy; nothing is sent until the merchant reviews it.
* **Offer expiry** (`Campaign.offer_expires_at`): before each claim, and in
  the capacity/stall schedulers, an expired offer pauses the campaign as
  `offer_expired`; resume and dispatch-now are refused (409) until the
  merchant updates the offer or its date.
* **Content revisions** (`campaign_content_revisions`): `PUT
  /campaigns/{id}/content` edits template / variables / coupon / expiry as a
  new revision, refused while a worker is live or an attempt is in flight.
  Each attempt records `content_revision`; `GET /campaigns/{id}/revisions`
  reports sends per version. Edits reach only recipients not yet sent.
* **Consent re-read at send time**: block list, `is_unsubscribed`,
  `pending_unsubscribe` and the merchant opt-out are checked per queued row
  before the claim, not only at snapshot (a campaign can span days). The
  check fails closed: if the customer's current state cannot be read
  (refresh error) or the stored flags have an unexpected shape, that
  recipient gets no request in this attempt, stays queued with
  `error_code=consent_unreadable`, and the run ends paused as
  `consent_unreadable` (needs review); only a legacy `NULL` counts as "no
  flags". Unreadable consent is never permission to send.
* **Button taps** (`services/campaign_click_tracking.py`): a quick-reply tap
  quotes our wamid (`context.id`) and is counted once per attempt, only for
  attempts flagged `click_trackable` at send time. URL / copy-code buttons
  produce no event → "not available". Copies sent before tracking existed
  are matched but not counted (no retroactive attribution). Reads never imply
  clicks.
* **Statistics** (`GET /campaigns/stats`): scope all / running / one
  campaign × period today / week / month / year / all / custom, counted by
  each event's own time (`period_basis=event_time`). Unique recipients and
  message attempts are separate; WhatsApp acceptance is not reach.
* **Dashboard**: live campaigns first, full width, with name, message
  preview, one calm status sentence (next known check time or "not yet
  known"), a progress bar, primary counters (reached, read, tapped or "not
  available"), secondary counters (remaining, accepted-unconfirmed, failed
  final, excluded, recipient limit), "last updated" from the log with a note
  that receipts lag, and technical facts under "Diagnostic details". Red is
  reserved for states that need the merchant, with the reason and the action.

## Evidence and gates

`tests/test_campaign_marketing_continuation.py` (registered in the strict
PostgreSQL proof manifest) and `tests/test_campaign_throttle_state.py` cover:
131049 history and live receipts never stopping a run; spam still stopping
it; the legacy pause needing review; stalled recovery without duplicates
(accepted and uncertain copies untouched), stop/live-worker/too-recent
guards, backoff and give-up; offer expiry with refused resume, revision
save, continuation of the remainder only, per-revision send counts; expiry
reached during a capacity wait; consent re-read; taps counted once, never
retroactively, unavailable for URL buttons; event-time statistics by scope.

No model, prompt, persona or customer wording changed. Dashboard labels are
static merchant chrome.

## Design preview (mocked API, generic merchant)

* `assets/campaigns-live-cards-desktop.png` — live campaigns first: a
  reviewable legacy pause (red, with reason and action) and a capacity wait
  (calm, with the exact slot time).
* `assets/campaigns-stats-filters-desktop.png` — statistics filtered by
  campaign scope and period, counted by event time; taps show a real count
  only where quick-reply tracking exists.
* `assets/campaigns-live-cards-mobile.png` — the same card at phone width.
* `assets/campaigns-edit-content-dialog.png` — offer / expiry / content edit
  with preview and per-version send history.
