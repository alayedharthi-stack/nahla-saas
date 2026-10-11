"""CI-only full-module/HTTP proof; run inside the real Python 3.11 image.

No lifespan execution: the separately accepted real PostgreSQL callback proof
covers database startup. This proof exercises the actual exported ASGI app,
real middleware/router imports and resolved production dependencies. No route,
auth or database dependency is replaced. Docker --network none and the guards
below refuse provider traffic and database connections, including swallowed
attempts. Every credential is generated synthetic data, never a live value.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import logging
import os
from pathlib import Path
import sys
import threading
import traceback
from urllib.parse import urlencode


ROOT = Path('/app')
MODE = os.environ['PROOF_MODE']
assert MODE in ('candidate', 'accepted', 'legacy')
assert sys.version_info[:2] == (3, 11)
assert os.environ['ENVIRONMENT'] == 'production'
assert os.environ.get('NAHLA_SKIP_PREFLIGHT', '') != '1'
assert os.environ['DATABASE_URL'].startswith('postgresql://proof:proof@127.0.0.1:1/')
FLAGS = (
    'NAHLA_SHOPIFY_CONNECTION_ENABLED', 'NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED',
    'NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED',
    'NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED', 'NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED',
)
assert all(os.environ.get(flag) == '0' for flag in FLAGS)
assert all(not os.environ.get(name) for name in (
    'SHOPIFY_CLIENT_ID', 'SHOPIFY_CLIENT_SECRET', 'SHOPIFY_TOKEN_ENC_KEY',
    'WHATSAPP_TOKEN', 'PHONE_NUMBER_ID', 'WA_BUSINESS_ACCOUNT_ID', 'SENTRY_DSN',
    'SALLA_CLIENT_SECRET', 'MOYASAR_SECRET_KEY', 'REDIS_URL',
))
for path in (ROOT, ROOT / 'backend', ROOT / 'database'):
    sys.path.insert(0, str(path))

ATTEMPTS = {'network': [], 'subprocess': [], 'database': []}


def audit_guard(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        ATTEMPTS['network'].append(event)  # never record destinations/payloads
        raise RuntimeError('proof_network_refused')
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        frames = traceback.extract_stack(limit=16)
        ATTEMPTS['subprocess'].append({
            'event': event, 'executable_basename': Path(str(args[0])).name,
            'stdlib_processor_lookup': event == 'subprocess.Popen'
                and args[1] == ['uname', '-p']
                and any(f.filename == '/usr/local/lib/python3.11/platform.py'
                        and f.name == 'from_subprocess' for f in frames),
            'stack_functions': [Path(f.filename).name + ':' + f.name for f in frames],
        })
        raise RuntimeError('proof_subprocess_refused')


sys.addaudithook(audit_guard)
import sqlalchemy as sa  # noqa: E402


def deny_database(dialect, connection_record, cargs, cparams):
    ATTEMPTS['database'].append('do_connect')
    raise RuntimeError('proof_database_refused')


sa.event.listen(sa.engine.interfaces.Dialect, 'do_connect', deny_database)
THREADS_BEFORE = {t.ident for t in threading.enumerate()}
RESULTS = []
REPORT = {'mode': MODE, 'source_sha': os.environ['PROOF_SOURCE_SHA'],
          'python': sys.version, 'lifespan_executed': False, 'cases': RESULTS,
          'api_key_configured': bool(os.environ.get('API_SECRET_KEY'))}


def check(name, fn):
    fn()
    RESULTS.append({'name': name, 'status': 'passed'})
    print('ASGI_CASE', MODE, name, 'PASS', flush=True)


def proof():
    # A genuine import of the complete application, not an AST extraction.
    import backend.main as main
    import httpx
    from core.auth import create_token
    from core.log_redaction import install_log_redaction
    from core.observability_sentry import _before_send

    assert Path(main.__file__).resolve() == ROOT / 'backend/main.py'
    assert os.environ['RAILWAY_GIT_COMMIT_SHA'] == os.environ['PROOF_SOURCE_SHA']
    application = main._FASTAPI_APPLICATION
    assert not application.dependency_overrides
    paths = {route.path for route in application.routes}
    callback = '/merchant/integrations/shopify/callback'
    connection = '/merchant/integrations/shopify'
    protected = [
        ('GET', connection + '/status', None),
        ('POST', connection + '/start', {'shop': 'proof-shop.myshopify.com'}),
        ('POST', connection + '/complete', {'handle': 'h' * 48}),
        ('POST', connection + '/disconnect', {'shop': 'proof-shop.myshopify.com'}),
        ('GET', connection + '/catalog-import/status', None),
        ('POST', connection + '/catalog-import/start', {'shop': 'proof-shop.myshopify.com'}),
    ]
    webhooks = [
        '/webhooks/shopify/app-uninstalled', '/webhooks/shopify/privacy',
        '/webhooks/shopify/privacy/customers-data-request',
        '/webhooks/shopify/privacy/customers-redact', '/webhooks/shopify/privacy/shop-redact',
    ]
    expected = {callback, *(p for _, p, _ in protected), *webhooks}

    def import_graph():
        assert (expected <= paths) if MODE != 'legacy' else not (expected & paths)
        assert any('/salla' in p for p in paths)
        assert all(Path(sys.modules[n].__file__).resolve().is_relative_to(ROOT)
                   for n in sys.modules if n.startswith(('routers.shopify_', 'services.shopify_'))
                   and getattr(sys.modules[n], '__file__', None))
        non_shopify = sorted((r.path, sorted(getattr(r, 'methods', None) or []),
                              getattr(getattr(r, 'endpoint', None), '__module__', ''), r.name)
                             for r in application.routes if 'shopify' not in r.path.lower())
        REPORT['non_shopify_routes_sha256'] = hashlib.sha256(
            json.dumps(non_shopify, sort_keys=True).encode()).hexdigest()
        REPORT['non_shopify_routes_count'] = len(non_shopify)
        REPORT['shopify_route_count'] = len(expected & paths)

    check('full_module_and_router_graph', import_graph)

    if os.environ.get('PROOF_CALLBACK_PROBE') == '1':
        assert os.environ.get('API_SECRET_KEY') == 'synthetic-service-key-for-asgi-proof'

        async def callback_probe():
            transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=True)
            async with httpx.AsyncClient(transport=transport, base_url='https://proof.invalid') as client:
                response = await client.get(callback)
                REPORT['callback_status'] = response.status_code
                REPORT['callback_expected_status'] = 404 if MODE != 'legacy' else 401
                assert response.status_code == REPORT['callback_expected_status'], (
                    'browser_callback_api_key_gate', response.status_code, REPORT['callback_expected_status'])
                assert (await client.get(callback + '/extra')).status_code == 401

        check('configured_service_key_browser_callback', lambda: asyncio.run(callback_probe()))
        return

    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    logging.getLogger().addHandler(handler)
    install_log_redaction()  # supported installation for the new capture handler
    code, state, hmac = ('proof-code-9fc2', 'proof-state-43bd', 'proof-hmac-5a81')
    query = urlencode({'code': code, 'state': state, 'hmac': hmac,
                       'shop': 'proof-shop.myshopify.com', 'timestamp': '1'})
    url = 'https://proof.invalid' + callback + '?' + query

    async def requests():
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url='https://proof.invalid',
                                    follow_redirects=False) as client:
            for path in ('/alive', '/healthz', '/auth/ping'):
                assert (await client.get(path)).status_code == 200
            version = await client.get('/version')
            assert version.status_code == 200
            assert version.json()['git_sha'] == os.environ['PROOF_SOURCE_SHA']
            check('real_asgi_liveness', lambda: None)
            response = await client.get(callback + '?' + query)
            assert response.status_code == (404 if MODE != 'legacy' else 401)
            for suffix in ('/extra', '-other'):
                assert (await client.get(callback + suffix)).status_code == 401
            for method, path, body in protected:
                r = await client.request(method, path, json=body)
                assert r.status_code == 401
            check('callback_exact_public_and_protected_jwt', lambda: None)
            for tenant in (701, 702):
                token = create_token('proof@example.invalid', 'merchant', tenant, user_id=tenant)
                for method, path, body in protected:
                    r = await client.request(method, path, json=body,
                                             headers={'Authorization': 'Bearer ' + token,
                                                      'X-Tenant-ID': str(1403 - tenant)})
                    assert r.status_code == 404
                    assert r.headers.get('location') is None
                for path in webhooks:
                    r = await client.post(path, content=b'{}',
                                          headers={'Content-Type': 'application/json',
                                                   'X-Shopify-Hmac-Sha256': hmac})
                    assert r.status_code == 404
            check('two_signed_tenants_all_disabled_routes', lambda: None)

    asyncio.run(requests())

    def log_scrub():
        logging.getLogger('httpx').info('HTTP Request: GET %s', httpx.URL(url))
        logging.getLogger('uvicorn.access').info('GET %s', callback + '?' + query)
        rendered = logs.getvalue()
        assert code not in rendered and state not in rendered
        REPORT['hmac_log_scrubbed'] = hmac not in rendered
        if MODE != 'legacy':
            assert REPORT['hmac_log_scrubbed']
        assert 'REDACTED' in rendered
        # Do not emit captured logs or synthetic credential values into CI.

    check('installed_log_filters_actual_httpx_uvicorn_raw_asgi', log_scrub)

    def sentry_scrub():
        event = {'request': {'url': url, 'query_string': query.encode(),
                             'headers': {'X-Shopify-Hmac-Sha256': hmac, 'Referer': url}},
                 'exception': {'values': [{'type': 'ProofError', 'value': url,
                                           'stacktrace': {'frames': [{
                                               'module': 'services.shopify_connection.oauth',
                                               'vars': {'unlabelled': code}}]}}]}}
        scrubbed = _before_send(event, {})
        serialized = json.dumps(scrubbed)
        assert state not in serialized
        if MODE != 'legacy':
            assert all(value not in serialized for value in (code, hmac))
            webhook = _before_send({'request': {'url': 'https://proof.invalid' + webhooks[1],
                                     'headers': {'unsigned': 'caller-data'},
                                     'data': {'email': 'customer@example.invalid'}}}, {})
            assert 'caller-data' not in json.dumps(webhook)
            assert 'customer@example.invalid' not in json.dumps(webhook)
        REPORT['sentry_shopify_scrubbed'] = all(v not in serialized for v in (code, state, hmac))

    check('sentry_before_send_no_transport', sentry_scrub)

    def resolved_crypto():
        if MODE == 'legacy':
            # Same production dependency image, without claiming legacy Shopify code exists.
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            cipher = AESGCM(b'x' * 32)
            encrypted = cipher.encrypt(b'n' * 12, b'proof', b'aad')
            assert cipher.decrypt(b'n' * 12, encrypted, b'aad') == b'proof'
        else:
            from services.shopify_connection.crypto import TokenCipher, token_context, PURPOSE_ACCESS_TOKEN
            cipher = TokenCipher(b'x' * 32)
            context = token_context(PURPOSE_ACCESS_TOKEN, tenant_id=701,
                                    shop_domain='proof-shop.myshopify.com', generation=1)
            encrypted = cipher.encrypt('synthetic-offline-token', context)
            assert cipher.decrypt(encrypted, context) == 'synthetic-offline-token'

    check('production_resolved_encryption_library', resolved_crypto)

    def no_side_effects():
        assert not ATTEMPTS['network'] and not ATTEMPTS['database'], ATTEMPTS
        # Supply genuine non-secret source metadata, preventing the existing
        # health router's import-time git fallback. No subprocess is waived.
        assert not ATTEMPTS['subprocess'], ATTEMPTS
        extra = [t.name for t in threading.enumerate() if t.ident not in THREADS_BEFORE]
        assert not any('shopify' in name.lower() for name in extra), extra
        REPORT['additional_threads'] = extra
        REPORT['refused_attempt_counts'] = {k: len(v) for k, v in ATTEMPTS.items()}
        REPORT['refused_subprocess_details'] = ATTEMPTS['subprocess']
        REPORT['dependency_versions'] = {n: importlib.metadata.version(n) for n in (
            'fastapi', 'starlette', 'httpx', 'sqlalchemy', 'psycopg2-binary',
            'pydantic', 'cryptography', 'python-jose', 'sentry-sdk', 'openai-agents')}

    check('zero_provider_database_or_shopify_worker_attempts', no_side_effects)


try:
    proof()
except BaseException as exc:
    REPORT['status'] = 'failed'
    REPORT['error_type'] = type(exc).__name__
    REPORT['refused_attempt_counts'] = {k: len(v) for k, v in ATTEMPTS.items()}
    REPORT['refused_subprocess_details'] = ATTEMPTS['subprocess']
    print('ASGI_REPORT', json.dumps(REPORT, sort_keys=True), flush=True)
    traceback.print_exc()
    raise SystemExit(1)
else:
    REPORT['status'] = 'passed'
    print('ASGI_REPORT', json.dumps(REPORT, sort_keys=True), flush=True)
