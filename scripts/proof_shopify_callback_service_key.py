"""CI-only actual ASGI + disposable PostgreSQL proof for the exact GET exemption.

No lifespan, dependency overrides, fake routes or provider transports. Synthetic
settings are activated only inside this throw-away test process. Shopify tokens
are never exchanged: callback state/code acceptance and completion refusals are
proved before any provider call. The Docker network is internal, and every
Python network/process attempt is refused, including attempts caught by app code.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path('/app')
MODE = os.environ['CALLBACK_PROOF_MODE']
assert MODE in ('candidate', 'baseline')
assert sys.version_info[:2] == (3, 11)
assert os.environ['ENVIRONMENT'] == 'production'
assert os.environ['RAILWAY_GIT_COMMIT_SHA'] == os.environ['CALLBACK_SOURCE_SHA']
assert os.environ['API_SECRET_KEY'] == 'synthetic-service-key-for-asgi-proof'
assert os.environ['DATABASE_URL'] == 'postgresql://proof:proof@postgres:5432/shopify_callback_proof'
assert not os.environ.get('NAHLA_SKIP_PREFLIGHT')
for path in (ROOT, ROOT / 'backend', ROOT / 'database'):
    sys.path.insert(0, str(path))

ATTEMPTS = {'network': 0, 'subprocess': 0, 'unexpected_database': 0}
DB_CONNECTS = 0
REQUESTS = 0
CASES = []
REPORT = {'mode': MODE, 'source_sha': os.environ['CALLBACK_SOURCE_SHA'],
          'python': sys.version, 'lifespan_executed': False, 'cases': CASES}


def audit_guard(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        ATTEMPTS['network'] += 1
        raise RuntimeError('proof_network_refused')
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        ATTEMPTS['subprocess'] += 1
        raise RuntimeError('proof_subprocess_refused')


sys.addaudithook(audit_guard)
import sqlalchemy as sa  # noqa: E402


def connection_guard(dialect, record, cargs, cparams):
    global DB_CONNECTS
    if (dialect.name != 'postgresql' or cparams.get('host') != 'postgres'
            or str(cparams.get('port')) != '5432'
            or cparams.get('dbname') != 'shopify_callback_proof'
            or cparams.get('user') != 'proof' or cparams.get('password') != 'proof'):
        ATTEMPTS['unexpected_database'] += 1
        raise RuntimeError('proof_unexpected_database_refused')
    DB_CONNECTS += 1


sa.event.listen(sa.engine.interfaces.Dialect, 'do_connect', connection_guard)
THREADS_BEFORE = {thread.ident for thread in threading.enumerate()}


def passed(name):
    CASES.append({'name': name, 'status': 'passed'})
    print('CALLBACK_CASE', MODE, name, 'PASS', flush=True)


async def proof():
    import backend.main as main
    import httpx
    from core.auth import create_token, decode_token
    from services.shopify_connection import lifecycle
    from services.shopify_connection.actor import session_ref_hash
    from services.shopify_connection.config import evaluate_availability
    from services.shopify_connection.models import ShopifyConnection, ShopifyOAuthState
    from models import Base, Tenant, User
    from session import engine, SessionLocal

    assert Path(main.__file__).resolve() == ROOT / 'backend/main.py'
    assert not main._FASTAPI_APPLICATION.dependency_overrides
    callback = '/merchant/integrations/shopify/callback'
    prefix = '/merchant/integrations/shopify'
    routes = main._FASTAPI_APPLICATION.routes
    non_shopify = sorted((r.path, sorted(getattr(r, 'methods', None) or []),
                          getattr(getattr(r, 'endpoint', None), '__module__', ''), r.name)
                         for r in routes if 'shopify' not in r.path.lower())
    REPORT['non_shopify_routes_count'] = len(non_shopify)
    REPORT['non_shopify_routes_sha256'] = hashlib.sha256(json.dumps(non_shopify, sort_keys=True).encode()).hexdigest()
    assert len(non_shopify) == 643
    assert REPORT['non_shopify_routes_sha256'] == '17fed89fc8f585bfb49d1f24df15b4b961e4ae7678f2725421540e6702f4b10b'
    passed('actual_full_app_unchanged_non_shopify_routes')

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url='https://proof.invalid',
                                follow_redirects=False) as client:
        async def request(method, path, **kwargs):
            global REQUESTS
            REQUESTS += 1
            return await client.request(method, path, **kwargs)

        response = await request('GET', callback)
        REPORT['dormant_callback_status'] = response.status_code
        assert response.status_code == (404 if MODE == 'candidate' else 401)
        passed('configured_service_key_exact_get_reaches_dormant_handler' if MODE == 'candidate'
               else 'baseline_reproduces_exact_get_401')

        for method in ('HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
            assert (await request(method, callback)).status_code == 401
            response = await request(method, callback, headers={'X-Nahla-Key': os.environ['API_SECRET_KEY']})
            assert response.status_code == 405
        for path in (callback + '/', callback + '/extra', callback + '-other',
                     prefix + '/Callback', prefix + '//callback', callback + '%2Fextra'):
            for headers in ({}, {'X-Nahla-Key': os.environ['API_SECRET_KEY']}):
                assert (await request('GET', path, headers=headers)).status_code == 401
        # Existing global CORS/OPTIONS exception stays unchanged and mutates nothing.
        assert (await request('OPTIONS', callback)).status_code == 204
        passed('non_get_and_non_exact_paths_keep_both_gates')

        for path in (prefix + '/status', prefix + '/start', prefix + '/complete',
                     '/merchant/catalog/meta-consent/callback',
                     '/whatsapp/embedded/oauth/callback', '/oauth/salla/callback'):
            method = 'POST' if path.endswith(('/start', '/complete')) else 'GET'
            assert (await request(method, path)).status_code == 401
        passed('other_shopify_salla_meta_protection_unchanged')
        if MODE == 'baseline':
            assert DB_CONNECTS == 0
            return

        with engine.begin() as connection:
            version = int(connection.exec_driver_sql('SHOW server_version_num').scalar())
            assert version // 10000 == 16
            REPORT['postgres_server_version_num'] = version
            Base.metadata.create_all(connection, tables=[Tenant.__table__, User.__table__])
            migration_path = ROOT / 'database/migrations/versions/0121_shopify_connection_foundation.py'
            spec = importlib.util.spec_from_file_location('callback_proof_0121', migration_path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        with SessionLocal() as db:
            db.add_all([Tenant(id=701, name='Generic apparel merchant', is_active=True),
                        Tenant(id=702, name='Generic fragrance merchant', is_active=True)])
            db.flush()
            for uid, tenant in ((701, 701), (702, 702), (703, 701)):
                db.add(User(id=uid, username='proof-merchant-' + str(uid),
                            email='merchant-' + str(uid) + '@example.invalid',
                            tenant_id=tenant, role='merchant', is_active=True, email_verified=True))
            db.commit()
        passed('real_pg16_actual_0121_and_synthetic_merchant_fixture')

        secret = 'synthetic-shopify-client-secret-for-callback-proof'
        os.environ.update({
            'NAHLA_SHOPIFY_CONNECTION_ENABLED': '1', 'SHOPIFY_CLIENT_ID': 'synthetic-client-id',
            'SHOPIFY_CLIENT_SECRET': secret, 'SHOPIFY_OAUTH_REDIRECT_URI': 'https://proof.invalid' + callback,
            'SHOPIFY_TOKEN_ENC_KEY': base64.urlsafe_b64encode(b's' * 32).decode(),
            'DASHBOARD_URL': 'https://dashboard.invalid', 'SHOPIFY_ADMIN_API_VERSION': '2026-10',
        })
        assert evaluate_availability().available
        tokens = {uid: create_token('merchant-' + str(uid) + '@example.invalid', 'merchant', tenant, user_id=uid)
                  for uid, tenant in ((701, 701), (702, 702), (703, 701))}
        shop_counter = 0

        def state_row(raw_state):
            with SessionLocal() as db:
                return db.scalar(sa.select(ShopifyOAuthState).where(
                    ShopifyOAuthState.state_hash == lifecycle.state_hash(raw_state)))

        def signed_query(shop, state, **changes):
            params = {'code': 'synthetic-authorization-code', 'shop': shop,
                      'state': state, 'timestamp': str(int(time.time()))}
            params.update(changes)
            message = '&'.join(k + '=' + params[k] for k in sorted(params))
            params['hmac'] = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
            return urlencode(params)

        async def start(uid=701):
            nonlocal shop_counter
            shop_counter += 1
            shop = 'generic-proof-' + str(shop_counter) + '.myshopify.com'
            response = await request('POST', prefix + '/start', json={'shop': shop},
                                     headers={'Authorization': 'Bearer ' + tokens[uid], 'X-Tenant-ID': '702'})
            assert response.status_code == 200
            query = parse_qs(urlsplit(response.json()['authorize_url']).query)
            assert query['scope'] == ['read_products']
            raw_state = query['state'][0]
            row = state_row(raw_state)
            payload = decode_token(tokens[uid])
            assert row.tenant_id == payload['tenant_id'] and row.actor_user_id == uid
            assert row.session_ref_hash == session_ref_hash(payload['jti'])
            assert row.status == 'pending' and row.code_enc is None
            return shop, raw_state

        def assert_redirect(response, code):
            assert response.status_code == 302
            location = urlsplit(response.headers['location'])
            assert location.scheme == 'https' and location.netloc == 'dashboard.invalid'
            assert location.path == '/integrations/shopify/complete' and not location.query
            assert parse_qs(location.fragment) == {'shopify_connection': [code]}
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['referrer-policy'] == 'no-referrer'

        async def accept(uid=701):
            shop, raw_state = await start(uid)
            response = await request('GET', callback + '?' + signed_query(shop, raw_state))
            assert response.status_code == 302
            location = urlsplit(response.headers['location'])
            assert location.netloc == 'dashboard.invalid' and location.path == '/integrations/shopify/complete'
            handle = parse_qs(location.fragment)['shopify_handle'][0]
            row = state_row(raw_state)
            assert row.status == 'callback_received' and row.code_enc.startswith('sgcm1:')
            assert 'synthetic-authorization-code' not in row.code_enc
            assert row.completion_handle_hash == lifecycle.completion_handle_hash(handle)
            return shop, raw_state, handle

        # Positive route proof uses actual /start and callback with real state storage.
        shop, raw_state, handle = await accept()
        row = state_row(raw_state)
        encrypted_before = row.code_enc
        assert_redirect(await request('GET', callback + '?' + signed_query(shop, raw_state)), 'state_replayed')
        assert state_row(raw_state).code_enc == encrypted_before
        passed('authenticated_start_bound_nonce_signed_callback_encrypted_code_and_replay')

        shop, raw_state = await start()
        good = signed_query(shop, raw_state)
        params = parse_qs(good)
        malformed = [
            good.replace(params['hmac'][0], '0' * 64),
            urlencode({k: v[0] for k, v in params.items() if k != 'hmac'}),
            good + '&hmac=' + params['hmac'][0],
            signed_query(shop, raw_state, timestamp=str(int(time.time()) - 1000)),
            signed_query('https://evil.invalid', raw_state),
        ]
        for query in malformed:
            assert_redirect(await request('GET', callback + '?' + query), 'callback_invalid')
            pending = state_row(raw_state)
            assert pending.status == 'pending' and pending.code_enc is None
        passed('bad_missing_duplicate_hmac_stale_timestamp_bad_shop_fail_before_nonce_consumption')

        assert_redirect(await request('GET', callback + '?' + signed_query(shop, secrets.token_urlsafe(32))), 'invalid_state')
        passed('valid_hmac_unknown_state_rejected')

        for failure in ('state_expired', 'shop_mismatch', 'callback_mismatch', 'actor_revoked'):
            shop, raw_state = await start()
            query = signed_query(shop, raw_state)
            with SessionLocal() as db:
                row = db.scalar(sa.select(ShopifyOAuthState).where(ShopifyOAuthState.state_hash == lifecycle.state_hash(raw_state)))
                if failure == 'state_expired':
                    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                elif failure == 'shop_mismatch':
                    query = signed_query('different-proof.myshopify.com', raw_state)
                elif failure == 'callback_mismatch':
                    row.redirect_uri_fingerprint = '0' * 64
                else:
                    db.get(User, 701).is_active = False
                db.commit()
            assert_redirect(await request('GET', callback + '?' + query), failure)
            row = state_row(raw_state)
            assert row.status == 'failed' and row.failure_code == failure and row.code_enc is None
            if failure == 'actor_revoked':
                with SessionLocal() as db:
                    db.get(User, 701).is_active = True
                    db.commit()
        passed('expired_shop_redirect_actor_state_checks_still_enforced')

        shop, raw_state = await start()
        os.environ['SHOPIFY_CLIENT_SECRET'] = ''
        assert_redirect(await request('GET', callback + '?' + signed_query(shop, raw_state)), 'unavailable')
        assert state_row(raw_state).status == 'pending'
        os.environ['SHOPIFY_CLIENT_SECRET'] = secret
        passed('missing_secret_fails_closed_without_state_consumption')

        for mismatch in ('tenant', 'user', 'session'):
            shop, raw_state, handle = await accept()
            token = (tokens[702] if mismatch == 'tenant' else tokens[703] if mismatch == 'user'
                     else create_token('merchant-701@example.invalid', 'merchant', 701, user_id=701))
            response = await request('POST', prefix + '/complete', json={'handle': handle},
                                     headers={'Authorization': 'Bearer ' + token, 'X-Tenant-ID': '701'})
            assert response.status_code == 403
            assert response.json()['detail']['error'] == 'session_mismatch'
            row = state_row(raw_state)
            assert row.status == 'failed' and row.code_enc is None
        passed('cross_tenant_user_and_new_session_completion_refused_before_provider')

        os.environ['NAHLA_SHOPIFY_CONNECTION_ENABLED'] = '0'
        for uid in (701, 702):
            for method, path, body in (
                ('GET', prefix + '/status', None), ('POST', prefix + '/start', {'shop': 'proof.myshopify.com'}),
                ('POST', prefix + '/complete', {'handle': 'h' * 48}),
                ('POST', prefix + '/disconnect', {'shop': 'proof.myshopify.com'}),
                ('GET', prefix + '/catalog-import/status', None),
                ('POST', prefix + '/catalog-import/start', {'shop': 'proof.myshopify.com'})):
                response = await request(method, path, json=body,
                                         headers={'Authorization': 'Bearer ' + tokens[uid]})
                assert response.status_code == 404
        assert (await request('GET', callback)).status_code == 404
        with SessionLocal() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShopifyConnection)) == 0
        passed('flags_off_two_tenants_no_shop_binding_or_credentials')
        engine.dispose()


try:
    asyncio.run(proof())
    assert not any(ATTEMPTS.values()), ATTEMPTS
    extra_threads = [t.name for t in threading.enumerate() if t.ident not in THREADS_BEFORE]
    assert not any('shopify' in name.lower() for name in extra_threads)
    REPORT['status'] = 'passed'
    REPORT['additional_threads'] = extra_threads
except BaseException as exc:
    REPORT['status'] = 'failed'
    REPORT['error_type'] = type(exc).__name__
    traceback.print_exc()
finally:
    REPORT['refused_attempt_counts'] = ATTEMPTS
    REPORT['synthetic_db_connects'] = DB_CONNECTS
    REPORT['http_requests'] = REQUESTS
    print('CALLBACK_REPORT', json.dumps(REPORT, sort_keys=True), flush=True)
if REPORT['status'] != 'passed':
    raise SystemExit(1)
