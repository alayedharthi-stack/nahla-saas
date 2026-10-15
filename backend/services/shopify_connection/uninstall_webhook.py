"""
Per-shop ``app/uninstalled`` subscription: the readiness evidence.

Shopify does not offer app-scoped (TOML) webhook subscriptions to an app on
the legacy install flow, so each shop's ``APP_UNINSTALLED`` subscription is
created through the Admin GraphQL API with that shop's offline token. A
connection is never ``active`` (and an install-first flow never ``ready``)
until this module has read back an exact subscription for it.

Exact means: topic ``APP_UNINSTALLED``, ``uri`` equal to
``https://<API host of the configured OAuth callback>/webhooks/shopify/app-uninstalled``,
format ``JSON``, and no ``includeFields`` / ``filter`` that could narrow or
suppress the delivery the uninstall handler parses.

One attempt (``ensure``) is bounded and never duplicates:

1. list the shop's ``APP_UNINSTALLED`` subscriptions, at most ``MAX_PAGES``
   pages — an exact one is the proof, nothing is created;
2. otherwise create exactly once;
3. always read back (also after a timeout, a 5xx, a throttle or
   ``userErrors`` from the create): only an exact subscription in the
   read-back list is proof. A create whose outcome is unknown is never
   repeated in the same attempt; the next attempt starts again at step 1.

Every call goes through the caller's ``guarded`` runner (the shop lease and
its deadline). Nothing here touches the database, logs a token, or deletes or
changes another subscription.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit

from services.shopify_connection.config import WEBHOOK_UNINSTALL_PATH
from services.shopify_connection.oauth import (
    WEBHOOK_FORMAT_JSON,
    WEBHOOK_TOPIC_APP_UNINSTALLED,
    ShopifyApiError,
    WebhookSubscription,
)

MAX_PAGES = 4

Guarded = Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]


class RegistrationFailed(Exception):
    """No exact subscription was read back. ``code`` is a fixed token."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebhookProof:
    subscription_id: str
    uri: str
    created: bool
    exact_count: int

    def detail(self, api_version: str) -> Dict[str, Any]:
        return {"subscription_id": self.subscription_id, "topic": WEBHOOK_TOPIC_APP_UNINSTALLED,
                "uri": self.uri, "format": WEBHOOK_FORMAT_JSON, "created": self.created,
                "exact_count": self.exact_count, "api_version": api_version}


def callback_uri(redirect_uri: str) -> str:
    """The uninstall endpoint on the same (validated) API host as the OAuth callback."""
    host = urlsplit(redirect_uri).hostname
    if urlsplit(redirect_uri).scheme != "https" or not host:
        raise RegistrationFailed("callback_uri_invalid")
    return f"https://{host}{WEBHOOK_UNINSTALL_PATH}"


def is_exact(sub: WebhookSubscription, uri: str) -> bool:
    return (sub.topic == WEBHOOK_TOPIC_APP_UNINSTALLED and sub.uri == uri and sub.format == WEBHOOK_FORMAT_JSON
            and not sub.include_fields and not sub.filter)


async def _exact(guarded: Guarded, api, *, shop_domain: str, access_token: str, uri: str):
    """(first exact subscription or None, exact count seen). Stops at the first exact page."""
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        page, after = await guarded(lambda cursor=after: api.uninstall_webhook_page(
            shop_domain=shop_domain, access_token=access_token, after=cursor))
        exact = [sub for sub in page if is_exact(sub, uri)]
        if exact:
            return exact[0], len(exact)
        if after is None:
            return None, 0
    # More subscriptions than one attempt may read: an exact one could be
    # further on, so creating could duplicate it. Fail closed.
    raise RegistrationFailed("too_many_subscriptions")


async def ensure(guarded: Guarded, api, *, shop_domain: str, access_token: str, uri: str) -> WebhookProof:
    """One bounded attempt. Raises ``RegistrationFailed`` or ``ShopifyApiError``
    (or the guard's own lease error) when no exact subscription is read back."""
    found, count = await _exact(guarded, api, shop_domain=shop_domain, access_token=access_token, uri=uri)
    if found is not None:
        return WebhookProof(found.id, uri, created=False, exact_count=count)
    create_error: Optional[ShopifyApiError] = None
    try:
        await guarded(lambda: api.create_uninstall_webhook(shop_domain=shop_domain, access_token=access_token,
                                                          uri=uri))
    except ShopifyApiError as exc:
        # Timeout, 5xx, throttle or userErrors: the subscription may exist
        # anyway. Never retry the create; read back instead.
        create_error = exc
    found, count = await _exact(guarded, api, shop_domain=shop_domain, access_token=access_token, uri=uri)
    if found is not None:
        return WebhookProof(found.id, uri, created=True, exact_count=count)
    raise RegistrationFailed(create_error.code if create_error is not None else "readback_missing")
