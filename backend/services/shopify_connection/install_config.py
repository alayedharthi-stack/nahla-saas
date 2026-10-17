"""Independent dormant gate, built on the existing connection configuration."""
import os
from dataclasses import replace
from urllib.parse import urlsplit

from services.shopify_connection.config import evaluate_availability, flag_enabled

PREFIX = "/merchant/integrations/shopify/install"
CALLBACK = PREFIX + "/callback"
DASHBOARD_PATH = "/app/shopify"
COOKIE = "__Host-nahla-shopify-install"
PUBLIC_REQUESTS = frozenset({("POST", PREFIX + "/start"), ("GET", CALLBACK), ("POST", PREFIX + "/redeem")})


def enabled(env=None):
    env = os.environ if env is None else env
    return flag_enabled(env) and str(env.get("NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}


def configuration(env=None):
    env = os.environ if env is None else env
    if not enabled(env):
        return None
    available = evaluate_availability(env)
    if not available.available:
        return None
    config = available.config
    # Same API origin as the existing reviewed callback; exact independent allowlist URI.
    origin = urlsplit(config.redirect_uri)
    redirect = f"https://{origin.hostname}{CALLBACK}"
    if env.get("SHOPIFY_INSTALL_REDIRECT_URI") != redirect:
        return None
    dashboard = urlsplit(config.dashboard_complete_url)
    return replace(config, redirect_uri=redirect, dashboard_complete_url=f"https://{dashboard.hostname}{DASHBOARD_PATH}")
