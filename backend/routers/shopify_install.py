"""Public, independently disabled install-first entry; no current Nahlah JWT required."""
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from core.database import get_db
from services.shopify_connection import install
from services.shopify_connection.install_config import CALLBACK, COOKIE, PREFIX, configuration, enabled
from services.shopify_connection.install_models import INSTALL_TABLES
from services.shopify_connection.oauth import ShopifyApi, verify_callback

router = APIRouter(tags=["Shopify install (dormant)"])
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _error(code="install_refused", status=400):
    return JSONResponse({"error": code}, status_code=status, headers=HEADERS)


def _storage_available(db):
    return all(inspect(db.get_bind()).has_table(t.name) for t in INSTALL_TABLES)


def _config(db, *, check_storage=True):
    if not enabled():
        return None, _error("not_found", 404)
    config = configuration()
    if not config or (check_storage and not _storage_available(db)):
        return None, _error("install_unavailable", 503)
    return config, None


def _api(config):
    return ShopifyApi(client_id=config.client_id, client_secret=config.client_secret, api_version=config.api_version)


def _same_origin(request, config):
    p = urlsplit(config.dashboard_complete_url)
    return request.headers.get("origin") == f"{p.scheme}://{p.netloc}"


async def _body(request, limit=2048):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise install.InstallRejected()
    return bytes(body)


@router.post(PREFIX + "/start")
async def start(request: Request, db: Session = Depends(get_db)):
    config, error = _config(db, check_storage=False)
    if error:
        return error
    if not _same_origin(request, config) or request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        return _error()
    try:
        data = parse_qs((await _body(request)).decode("ascii"), strict_parsing=True, max_num_fields=3)
        if set(data) != {"shop", "browser", "pilot_code"} or any(len(v) != 1 for v in data.values()):
            return _error()
        browser = data["browser"][0]
        install.validate_admission(shop=data["shop"][0], browser=browser, pilot_code=data["pilot_code"][0])
        if not _storage_available(db):
            return _error("install_unavailable", 503)
        url = install.begin(db, shop=data["shop"][0], browser=browser, config=config, pilot_code=data["pilot_code"][0])
        response = RedirectResponse(url, status_code=303, headers=HEADERS)
        # Top-level form navigation sets a first-party cookie even with separate dashboard/API sites.
        response.set_cookie(COOKIE, browser, max_age=install.FLOW_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
        return response
    except install.InstallRejected as exc:
        db.rollback()
        return _error(exc.code, 429 if exc.code in {"install_busy", "install_rate_limited"} else 400)
    except Exception:
        db.rollback()
        return _error()


@router.get(CALLBACK)
async def callback(request: Request, db: Session = Depends(get_db)):
    config, error = _config(db)
    if error:
        return error
    kind = None
    try:
        verified = verify_callback(request.url.query, secret=config.client_secret)
        kind, value = await install.callback(db, verified=verified, browser=request.cookies.get(COOKIE), config=config, api=_api(config))
        url = value if kind == "authorize" else config.dashboard_complete_url + "#install_handle=" + value
    except Exception:
        db.rollback()
        url = config.dashboard_complete_url + "#install_error=refused"
    response = RedirectResponse(url, status_code=303, headers=HEADERS)
    if kind == "ready":
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.post(PREFIX + "/redeem")
async def redeem(request: Request, db: Session = Depends(get_db)):
    config, error = _config(db)
    if error:
        return error
    if not _same_origin(request, config) or request.headers.get("content-type", "").split(";")[0] != "application/json":
        return _error()
    try:
        import json
        data = json.loads(await _body(request))
        if not isinstance(data, dict) or set(data) - {"handle", "browser", "totp"}:
            return _error()
        if not all(isinstance(data.get(k), str) for k in ("handle", "browser")):
            return _error()
        if data.get("totp") is not None and (not isinstance(data["totp"], str) or len(data["totp"]) > 6):
            return _error()
        result = await install.redeem(db, handle=data["handle"], browser=data["browser"], totp=data.get("totp"), config=config, api=_api(config))
        return JSONResponse(result, headers=HEADERS)
    except install.InstallRejected as exc:
        db.rollback()
        return _error(exc.code)
    except Exception:
        db.rollback()
        return _error()
