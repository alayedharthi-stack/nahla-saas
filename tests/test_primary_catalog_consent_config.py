"""Primary catalog consent is an explicit, isolated, default-off deployment mode.

Configuration tests only: no database, Meta request, token or entitlement state
is created. Stored-authorization expiry remains covered by the consent suite.
"""
from __future__ import annotations

import secrets

import pytest
from cryptography.fernet import Fernet

from core import meta_catalog_consent_config as config
from core.review_environment import MarkerReading

TENANT = 7
CATALOG = "880000000000001"
BUSINESS = "770000000000001"
REVIEW_FLAG = "NAHLA_CATALOG_REVIEW_ENV"
REVIEW_REDIRECT = f"https://api.catalog-review.example.test{config.CALLBACK_PATH}"
REVIEW_DASHBOARD = "https://catalog-review.example.test"


@pytest.fixture()
def primary_env():
    return {
        config.ENABLED_ENV: "1",
        config.PRIMARY_ENABLED_ENV: "1",
        config.CONFIG_ID_ENV: "400000000000001",
        config.REDIRECT_URI_ENV: config.PRIMARY_REDIRECT_URI,
        config.APPROVED_ASSETS_ENV: f"{TENANT}:{CATALOG}:{BUSINESS}",
        "DASHBOARD_URL": config.PRIMARY_DASHBOARD_URL,
        "META_APP_ID": "600000000000001",
        "META_APP_SECRET": secrets.token_urlsafe(32),
        config.ENCRYPTION_KEY_ENV: Fernet.generate_key().decode(),
    }


@pytest.fixture()
def review_env(primary_env):
    env = dict(primary_env)
    env.pop(config.PRIMARY_ENABLED_ENV)
    env.update({
        REVIEW_FLAG: "1",
        config.REDIRECT_URI_ENV: REVIEW_REDIRECT,
        "DASHBOARD_URL": REVIEW_DASHBOARD,
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": "postgresql://review_user@postgres-catalog-review.railway.internal/railway",
    })
    return env


def _no_marker_read(_url):
    raise AssertionError("This mode must not read the review database marker")


def _marked_review(_url):
    return MarkerReading("catalog-review", "catalog-review", "railway")


def test_default_is_disabled():
    result = config.evaluate_consent_availability({}, marker_reader=_no_marker_read)
    assert not result.available
    assert result.reason == config.R_DISABLED
    assert not config.runtime_consent_enabled({})


@pytest.mark.parametrize("flag", ["", "0", "false", "off", "no", "primary"])
def test_primary_selector_never_enables_base_feature(primary_env, flag):
    primary_env[config.ENABLED_ENV] = flag
    assert config.evaluate_consent_availability(primary_env).reason == config.R_DISABLED
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("flag", [None, "", "0", "false", "off", "no", "production"])
def test_primary_hosts_never_imply_primary_opt_in(primary_env, flag):
    if flag is None:
        primary_env.pop(config.PRIMARY_ENABLED_ENV)
    else:
        primary_env[config.PRIMARY_ENABLED_ENV] = flag
    primary_env["ENVIRONMENT"] = "production"
    result = config.evaluate_consent_availability(primary_env, marker_reader=_no_marker_read)
    assert not result.available
    assert result.reason == config.R_REVIEW_ENV_REQUIRED
    assert config.canonical_redirect_uri(primary_env) is None
    assert config.dashboard_return_url(primary_env) is None
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on", " TRUE "])
def test_primary_opt_in_uses_fixed_origins_without_review_marker(primary_env, flag):
    primary_env[config.PRIMARY_ENABLED_ENV] = flag
    result = config.evaluate_consent_availability(primary_env, tenant_id=TENANT, marker_reader=_no_marker_read)
    assert result.available and result.reason is None
    assert result.approval == config.CatalogApproval(TENANT, CATALOG, BUSINESS)
    assert result.redirect_uri == config.PRIMARY_REDIRECT_URI
    assert result.dashboard_return_url == "https://app.nahlah.ai/catalog"
    assert config.runtime_consent_enabled(primary_env)
    assert config.REQUESTED_SCOPES == ("catalog_management", "business_management")


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_mixed_modes_fail_before_marker_read(primary_env, flag):
    primary_env[REVIEW_FLAG] = flag
    result = config.evaluate_consent_availability(primary_env, marker_reader=_no_marker_read)
    assert not result.available
    assert result.reason == config.R_DEPLOYMENT_MODE_CONFLICT
    assert config.canonical_redirect_uri(primary_env) is None
    assert config.dashboard_return_url(primary_env) is None
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("flag", ["", "0", "false", "off", "no"])
def test_explicitly_disabled_review_flag_is_not_mixed_mode(primary_env, flag):
    primary_env[REVIEW_FLAG] = flag
    assert config.evaluate_consent_availability(primary_env, marker_reader=_no_marker_read).available
    assert config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("redirect", [
    REVIEW_REDIRECT,
    f"http://api.nahlah.ai{config.CALLBACK_PATH}",
    f"https://app.nahlah.ai{config.CALLBACK_PATH}",
    f"https://nahlah.ai{config.CALLBACK_PATH}",
    f"https://api.nahlah.ai.evil.example{config.CALLBACK_PATH}",
    f"https://api.nahlah.ai.{config.CALLBACK_PATH}",
    f"https://API.NAHLAH.AI{config.CALLBACK_PATH}",
    f"https://api.nahlah.ai:443{config.CALLBACK_PATH}",
    f"https://api.nahlah.ai:8443{config.CALLBACK_PATH}",
    f"https://u:p@api.nahlah.ai{config.CALLBACK_PATH}",
    f"https://@api.nahlah.ai{config.CALLBACK_PATH}",
    f"{config.PRIMARY_REDIRECT_URI}/",
    f"{config.PRIMARY_REDIRECT_URI}?next=x",
    f"{config.PRIMARY_REDIRECT_URI}#x",
    "https://api.nahlah.ai/whatsapp/embedded/oauth/callback",
    "https://[invalid",
    "",
])
def test_primary_callback_is_exact_and_runtime_fails_closed(primary_env, redirect):
    primary_env[config.REDIRECT_URI_ENV] = redirect
    result = config.evaluate_consent_availability(primary_env)
    assert result.reason == config.R_REDIRECT_URI_INVALID
    assert config.canonical_redirect_uri(primary_env) is None
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("dashboard", [
    REVIEW_DASHBOARD,
    "http://app.nahlah.ai",
    "https://api.nahlah.ai",
    "https://nahlah.ai",
    "https://www.nahlah.ai",
    "https://app.nahlah.ai.evil.example",
    "https://app.nahlah.ai.",
    "https://APP.NAHLAH.AI",
    "https://app.nahlah.ai:443",
    "https://app.nahlah.ai:8443",
    "https://u:p@app.nahlah.ai",
    "https://@app.nahlah.ai",
    "https://app.nahlah.ai/",
    "https://app.nahlah.ai/catalog",
    "https://app.nahlah.ai?next=x",
    "https://app.nahlah.ai#x",
    "https://[invalid",
    "",
])
def test_primary_dashboard_is_exact_and_runtime_fails_closed(primary_env, dashboard):
    primary_env["DASHBOARD_URL"] = dashboard
    result = config.evaluate_consent_availability(primary_env)
    assert result.reason == config.R_DASHBOARD_URL_INVALID
    assert config.dashboard_return_url(primary_env) is None
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("name,value,reason", [
    ("META_APP_ID", "", config.R_APP_CREDENTIALS_MISSING),
    ("META_APP_SECRET", "", config.R_APP_CREDENTIALS_MISSING),
    (config.CONFIG_ID_ENV, "", config.R_CONFIG_ID_MISSING),
    (config.ENCRYPTION_KEY_ENV, "", config.R_ENCRYPTION_KEY_MISSING),
    (config.ENCRYPTION_KEY_ENV, "invalid", config.R_ENCRYPTION_KEY_INVALID),
    (config.APPROVED_ASSETS_ENV, f"{TENANT}:{CATALOG}", config.R_APPROVALS_INVALID),
    (config.APPROVED_ASSETS_ENV, f"{TENANT}:{CATALOG}:{BUSINESS},{TENANT}:{CATALOG}:{BUSINESS}",
     config.R_APPROVALS_INVALID),
])
def test_primary_preserves_shared_preconditions(primary_env, name, value, reason):
    primary_env[name] = value
    assert config.evaluate_consent_availability(primary_env, tenant_id=TENANT).reason == reason
    assert not config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("approved", ["", f"8:{CATALOG}:{BUSINESS}"])
def test_primary_never_approves_tenant_implicitly(primary_env, approved):
    primary_env[config.APPROVED_ASSETS_ENV] = approved
    result = config.evaluate_consent_availability(primary_env, tenant_id=TENANT)
    assert not result.available
    assert result.reason == config.R_TENANT_NOT_APPROVED


def test_review_mode_still_requires_persisted_matching_marker(review_env, monkeypatch):
    calls = []

    def read_marker(url):
        calls.append(url)
        return _marked_review(url)

    result = config.evaluate_consent_availability(review_env, tenant_id=TENANT, marker_reader=read_marker)
    assert result.available
    assert calls == [review_env["DATABASE_URL"]]
    assert result.redirect_uri == REVIEW_REDIRECT
    assert result.dashboard_return_url == f"{REVIEW_DASHBOARD}/catalog"
    monkeypatch.setattr("core.review_environment.read_database_marker", read_marker)
    assert config.runtime_consent_enabled(review_env)
    assert calls == [review_env["DATABASE_URL"], review_env["DATABASE_URL"]]


@pytest.mark.parametrize("reading", [
    MarkerReading(None, None, "railway"),
    MarkerReading("catalog-review", None, "railway"),
    MarkerReading("catalog-review", "wrong", "railway"),
    MarkerReading("wrong", "catalog-review", "railway"),
    MarkerReading("catalog-review", "catalog-review", "wrong_database"),
    "catalog-review",
])
def test_review_marker_failure_remains_fail_closed(review_env, reading, monkeypatch):
    result = config.evaluate_consent_availability(review_env, marker_reader=lambda _url: reading)
    assert not result.available
    assert result.reason == config.R_REVIEW_ENV_UNVERIFIED
    monkeypatch.setattr("core.review_environment.read_database_marker", lambda _url: reading)
    assert not config.runtime_consent_enabled(review_env)


def test_primary_flag_cannot_rescue_unverified_review(review_env):
    review_env[config.PRIMARY_ENABLED_ENV] = "1"
    review_env[config.REDIRECT_URI_ENV] = config.PRIMARY_REDIRECT_URI
    review_env["DASHBOARD_URL"] = config.PRIMARY_DASHBOARD_URL
    result = config.evaluate_consent_availability(review_env, marker_reader=_no_marker_read)
    assert result.reason == config.R_DEPLOYMENT_MODE_CONFLICT
    assert not config.runtime_consent_enabled(review_env)


@pytest.mark.parametrize("host", ["nahlah.ai", "www.nahlah.ai", "app.nahlah.ai", "api.nahlah.ai"])
@pytest.mark.parametrize("suffix", ["", "."])
def test_review_helpers_never_allow_primary_hosts(review_env, host, suffix):
    review_env[config.REDIRECT_URI_ENV] = f"https://{host}{suffix}{config.CALLBACK_PATH}"
    review_env["DASHBOARD_URL"] = f"https://{host}{suffix}"
    assert config.canonical_redirect_uri(review_env) is None
    assert config.dashboard_return_url(review_env) is None
    result = config.evaluate_consent_availability(review_env, marker_reader=_marked_review)
    assert not result.available


@pytest.mark.parametrize("url", ["https://[invalid", "https://[not-an-ip]", "https://example.test:bad"])
def test_malformed_review_urls_fail_without_raising(review_env, url):
    review_env[config.REDIRECT_URI_ENV] = url
    review_env["DASHBOARD_URL"] = url
    assert config.canonical_redirect_uri(review_env) is None
    assert config.dashboard_return_url(review_env) is None


def test_primary_runtime_gate_never_reads_review_database_marker(primary_env, monkeypatch):
    monkeypatch.setattr("core.review_environment.read_database_marker", _no_marker_read)
    assert config.runtime_consent_enabled(primary_env)


@pytest.mark.parametrize("name,value", [
    ("RAILWAY_PROJECT_NAME", "wrong-project"),
    ("RAILWAY_ENVIRONMENT_NAME", "production"),
    ("DATABASE_URL", "postgresql://review_user@postgres-staging.railway.internal/railway"),
    ("DATABASE_URL", "postgresql://review_user@postgres-catalog-review.railway.internal/railway?host=other"),
])
def test_review_runtime_revalidates_static_isolation(review_env, monkeypatch, name, value):
    review_env[name] = value
    monkeypatch.setattr("core.review_environment.read_database_marker", _marked_review)
    assert not config.runtime_consent_enabled(review_env)
