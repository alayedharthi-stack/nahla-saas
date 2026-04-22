"""
campaign_wizard
───────────────
Backend services that power the new "smart" WhatsApp campaign creation
wizard introduced in this commit.

Shape (kept tiny on purpose so the router stays a thin orchestrator):

    goals.py        — fixed taxonomy of campaign goals (welcome, promotion,
                       reactivation, reorder, reminder, broadcast, custom)
                       plus the Meta categories each goal is allowed to use.

    segments.py     — named, reusable customer segments (all, new, vip,
                       dormant, lost, abandoned_cart, no_purchase_30/60/90,
                       …). Each segment exposes a SQLAlchemy-filter builder
                       so we can both COUNT and SAMPLE without duplicating
                       the WHERE clause.

    recommender.py  — given (goal, segment, language) score every approved
                       template the tenant owns and emit badges
                       ("الأفضل لهذه الحملة", "متوافق", "يحتاج مراجعة", …).

    test_send.py    — the single source of truth for sending a real test
                       message via provider_send_message, with mock variable
                       substitution when the merchant hasn't filled values
                       yet so the WhatsApp preview the merchant receives on
                       their phone always renders something sensible.

The wizard router is `backend/routers/campaign_wizard.py`.
"""
