"""Shared fake of Shopify's shop-scoped webhook subscriptions (tests only).

Mixed into the PostgreSQL suites' token fakes, which provide ``lock``,
``calls`` and ``valid_access`` (access token -> shop). No network.
"""
from services.shopify_connection.oauth import ShopifyApiError, WebhookSubscription


class FakeWebhookSubscriptions:
    """Shop-scoped webhook subscriptions of this app (Admin GraphQL model).

    * ``webhookSubscriptions(topics: [APP_UNINSTALLED])`` pages through the
      shop's subscriptions; ``webhookSubscriptionCreate`` adds one, refusing an
      address already taken for the topic (``userErrors``).
    * Every call needs a currently valid access token of that shop.
    * Uninstalling the app deletes the shop's subscriptions.

    Hooks: ``list_errors`` / ``create_errors`` are consumed one per call
    (raise instead of answering); ``create_lost_response`` performs the create
    and then raises (a response lost after success).
    """

    page_size = 2

    def _webhook_state(self):
        if not hasattr(self, "_webhooks"):
            self._webhooks, self._next_webhook_id = {}, 1
            self.list_errors, self.create_errors = [], []
            self.create_lost_response = None
            self.list_hook = None
            self.create_hook = None
        return self._webhooks

    def _webhook_auth(self, shop_domain, access_token):
        if self.valid_access.get(access_token) != shop_domain:
            raise ShopifyApiError("rejected", "credentials_rejected")

    def add_webhook(self, shop, *, uri, fmt="JSON", topic="APP_UNINSTALLED", include_fields=(), flt=""):
        with self.lock:
            hooks = self._webhook_state().setdefault(shop, [])
            sub = WebhookSubscription(f"gid://shopify/WebhookSubscription/{self._next_webhook_id}", topic,
                                      uri, fmt, tuple(include_fields), flt)
            self._next_webhook_id += 1
            hooks.append(sub)
            return sub

    def webhooks(self, shop):
        with self.lock:
            return list(self._webhook_state().get(shop, []))

    async def uninstall_webhook_page(self, *, shop_domain, access_token, after=None):
        with self.lock:
            self._webhook_state()
            self.calls.append(("webhook_list", shop_domain))
            error = self.list_errors.pop(0) if self.list_errors else None
        if self.list_hook:
            self.list_hook(shop_domain)
        if error:
            raise error
        with self.lock:
            self._webhook_auth(shop_domain, access_token)
            hooks = [h for h in self._webhook_state().get(shop_domain, []) if h.topic == "APP_UNINSTALLED"]
            start = int(after) if after else 0
            page = hooks[start:start + self.page_size]
            more = start + self.page_size < len(hooks)
            return page, (str(start + self.page_size) if more else None)

    async def create_uninstall_webhook(self, *, shop_domain, access_token, uri):
        with self.lock:
            self._webhook_state()
            self.calls.append(("webhook_create", shop_domain))
            error = self.create_errors.pop(0) if self.create_errors else None
        if self.create_hook:
            self.create_hook(shop_domain)
        if error:
            raise error
        with self.lock:
            self._webhook_auth(shop_domain, access_token)
            if any(h.uri == uri and h.topic == "APP_UNINSTALLED" for h in self._webhook_state().get(shop_domain, [])):
                raise ShopifyApiError("permanent", "webhook_user_errors")   # address already taken
        sub = self.add_webhook(shop_domain, uri=uri)
        lost, self.create_lost_response = self.create_lost_response, None
        if lost:
            raise lost
        return sub

    def uninstall_webhooks(self, shop):
        with self.lock:
            self._webhook_state().pop(shop, None)
