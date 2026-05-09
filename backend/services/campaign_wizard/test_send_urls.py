"""
campaign_wizard.test_send_urls
──────────────────────────────
URL-button resolution **for the wizard's test-send path only**.

Why this module exists separately
─────────────────────────────────
Production cart-recovery automations (`core/automation_engine.py`) bind
the dynamic URL-button suffix to the real cart event payload — there is
NO fallback chain there, on purpose, because firing a recovery message
that links to a random product page would actively hurt conversion.

Test-send has the opposite constraint:

  * The merchant is on Step 6 of the wizard, on a Salla **demo / sandbox**
    store, with no real abandoned cart.
  * Salla sandbox cart URLs frequently 404 or land on a maintenance page,
    which makes a perfectly-valid template look broken to the merchant
    and tanks the wizard's conversion to "Launch".
  * The point of test-send is to validate the template's rendering and
    the WhatsApp delivery path — not to verify the merchant's checkout
    funnel.

So this resolver:

  1. Walks an explicit fallback chain
     (`cart_url → checkout_url → product_url → order_url → store_url`).
  2. If nothing usable is in the merchant_vars, derives a working
     storefront URL from the tenant's `domain` and a sensible demo path.
  3. As a last resort, returns a hard-coded public placeholder so Meta
     still gets a non-empty parameter and the test message is delivered.

It is **never imported** from any production send path.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Ordered fallback list — first non-empty entry from this chain wins.
# Mirrors `_URL_SLOT_PRECEDENCE` in automation_engine but with a wider
# net (adds `order_url` because the merchant brief explicitly listed
# it, and reorders so cart_url stays the primary intent for a recovery
# template even though we'll soft-fall when it's missing).
TEST_URL_FALLBACK_CHAIN: Tuple[str, ...] = (
    "cart_url",
    "checkout_url",
    "product_url",
    "order_url",
    "tracking_url",
    "payment_url",
    "store_url",
)

# Keys we accept inside `merchant_vars` to mean "the URL for slot X".
# Frontend has historically used both bare names (`cart_url`) and the
# bracketed Meta placeholder form (`{{1}}`); we honour both so the test
# button works regardless of which form the wizard's Step 4 emitted.
_MERCHANT_VAR_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cart_url":     ("cart_url",     "{{cart_url}}",     "url",  "{{url}}"),
    "checkout_url": ("checkout_url", "{{checkout_url}}", "checkout"),
    "product_url":  ("product_url",  "{{product_url}}",  "product"),
    "order_url":    ("order_url",    "{{order_url}}",    "order"),
    "tracking_url": ("tracking_url", "{{tracking_url}}", "tracking"),
    "payment_url":  ("payment_url",  "{{payment_url}}",  "payment"),
    "store_url":    ("store_url",    "{{store_url}}",    "store",  "{{store}}"),
}

# Hard fallback when the merchant supplied nothing AND the tenant has
# no domain configured. Public, obviously-fake, never serves real
# customer traffic — only here so Meta always receives a non-empty
# button parameter and the test message reaches the merchant's phone.
DEMO_FALLBACK_URL = "https://demo.nahlah.ai/preview/test"


def _first_non_empty(merchant_vars: Dict[str, str], keys: Tuple[str, ...]) -> str:
    if not isinstance(merchant_vars, dict):
        return ""
    for k in keys:
        v = merchant_vars.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _looks_like_url(value: str) -> bool:
    """Tight heuristic: only treat strings that genuinely look like URLs
    as candidates for the button. Prevents a free-text body var that
    happened to be reused under a `*_url` key from being injected as a
    button param."""
    if not value:
        return False
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def resolve_test_button_url(
    merchant_vars: Optional[Dict[str, str]] = None,
    *,
    store_domain_hint: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Pick the best URL to wire into a dynamic URL-button **for test-send
    only**. Returns ``(resolved_url, source_label)`` where
    ``source_label`` is either the matched fallback slot
    (``"cart_url"`` / ``"checkout_url"`` / …), ``"store_domain_hint"``,
    or ``"demo_fallback"`` — useful for logging.

    Rules:
      * Walk ``TEST_URL_FALLBACK_CHAIN`` and return the first
        merchant-provided value that looks like a real URL.
      * If nothing matches, derive ``https://{store_domain_hint}/`` so
        the button at least lands on the merchant's storefront.
      * Last resort: ``DEMO_FALLBACK_URL`` so Meta validation passes.
    """
    vars_ = merchant_vars or {}

    for slot in TEST_URL_FALLBACK_CHAIN:
        aliases = _MERCHANT_VAR_ALIASES.get(slot, (slot,))
        candidate = _first_non_empty(vars_, aliases)
        if _looks_like_url(candidate):
            return candidate, slot

    if store_domain_hint:
        domain = store_domain_hint.strip().lstrip("@")
        if domain:
            # Accept either a bare hostname ("mystore.salla.sa") or a
            # full URL ("https://mystore.salla.sa/extras"). Both should
            # collapse to a working storefront root.
            parsed = urlparse(domain if "://" in domain else f"https://{domain}")
            if parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}/", "store_domain_hint"

    return DEMO_FALLBACK_URL, "demo_fallback"


def extract_button_suffix(button_url_template: str, full_url: str) -> str:
    """
    Convert a resolved ``full_url`` into the dynamic suffix Meta expects
    for the ``{{1}}`` placeholder of a URL-button template.

    Examples:
      template ``https://store.salla.sa/{{1}}``,
      full ``https://store.salla.sa/products/test-item``
        → ``products/test-item``

      template ``https://example.com/{{1}}``,
      full ``https://other.example/cart/abc``
        → ``cart/abc``  (cross-domain fallback: path+query+fragment)

    Mirrors `_extract_button_url_suffix` in automation_engine but is
    re-implemented here so the test-send module never imports the
    heavy automation graph (which pulls Stripe / Salla SDKs at module
    load time).
    """
    if not full_url:
        return ""

    placeholder = "{{1}}"
    pos = (button_url_template or "").find(placeholder)

    # If the template uses a raw "{{1}}" placeholder, prefer
    # prefix-stripping when the resolved URL shares the registered base.
    if pos >= 0:
        base = button_url_template[:pos]
        if base and full_url.startswith(base):
            tail = full_url[len(base):]
            if tail:
                return tail

    # Fallback: synthesise the suffix from the URL's path/query/fragment.
    try:
        parsed = urlparse(full_url)
    except Exception:
        return full_url.lstrip("/")

    path = (parsed.path or "").lstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    suffix = f"{path}{query}{fragment}"

    if not suffix:
        # ``https://store.salla.sa`` with no path — Meta still needs
        # *something*. A literal "/" produces a working absolute link
        # against the registered base.
        return "/"
    return suffix
