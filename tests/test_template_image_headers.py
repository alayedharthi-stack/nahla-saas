"""Image templates: real draft persistence, captured Meta calls, no live sends."""
import asyncio
import copy
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import create_engine, MetaData, JSON
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import JSONB

from models import WhatsAppTemplate
from routers import templates as router
from services import template_image_header as headers
from services import template_media_storage as storage
from services.campaign_dispatcher import _build_send_payload, validate_template_payload, PayloadValidationError

URL = 'https://media.example/template-headers/7/' + 'a' * 32 + '.png'
BODY = '﴿فِيهِ شِفَاءٌ لِلنَّاسِ﴾\nخصم 12% على جميع المنتجات\n21 إلى 25 سبتمبر 2026\n'
BUTTONS = [{'type': 'URL', 'text': 'تسوّق الآن', 'url': 'https://ayedhoney.com'}]


def image_bytes(fmt='PNG'):
    out = io.BytesIO()
    Image.new('RGB', (80, 40), 'green').save(out, format=fmt)
    return out.getvalue()


def components(url=URL):
    return [{'type': 'HEADER', 'format': 'IMAGE', 'example': {'header_url': url}},
            {'type': 'BODY', 'text': BODY}, {'type': 'BUTTONS', 'buttons': copy.deepcopy(BUTTONS)}]


@pytest.fixture
def db(monkeypatch):
    # Copy types into a private SQLite schema; never mutate shared PG models.
    engine = create_engine('sqlite://')
    meta = MetaData()
    table = WhatsAppTemplate.__table__.to_metadata(meta)
    for col in table.columns:
        if isinstance(col.type, JSONB):
            col.type = JSON()
    # SQLite does not enforce this absent external tenant FK in these unit tests.
    table.foreign_key_constraints.clear()
    for constraint in list(table.constraints):
        if constraint.__class__.__name__ == 'ForeignKeyConstraint':
            table.constraints.remove(constraint)
    table.create(engine)
    monkeypatch.setenv('NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL', 'https://media.example')
    monkeypatch.setattr(router, 'resolve_tenant_id', lambda request: 7)
    monkeypatch.setattr(router, 'get_or_create_tenant', lambda *_: None)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def draft(db, comps=None):
    tpl = WhatsAppTemplate(tenant_id=7, name='ayed_national_day_offer_twelve_percent',
                          language='ar', category='MARKETING', status='DRAFT',
                          components=comps or components()[1:], ai_generation_metadata={})
    db.add(tpl); db.commit()
    return tpl


def test_same_draft_save_reopen_replace_and_remove_preserves_approved_copy(db):
    tpl = draft(db)
    original_id = tpl.id
    result = asyncio.run(router.update_template(tpl.id, router.UpdateTemplateIn(components=components()), MagicMock(), db))
    db.expire_all()
    reread = db.get(WhatsAppTemplate, original_id)
    assert headers.header_url(reread.components) == URL
    assert result['id'] == original_id and reread.status == 'DRAFT'
    assert reread.components[1:] == components()[1:]
    next_url = URL.replace('a' * 32, 'b' * 32)
    asyncio.run(router.update_template(tpl.id, router.UpdateTemplateIn(components=components(next_url)), MagicMock(), db))
    db.expire_all()
    assert headers.header_url(db.get(WhatsAppTemplate, original_id).components) == next_url
    asyncio.run(router.update_template(tpl.id, router.UpdateTemplateIn(components=components()[1:]), MagicMock(), db))
    db.expire_all()
    assert db.get(WhatsAppTemplate, original_id).components == components()[1:]


@pytest.mark.parametrize('url', [URL.replace('/7/', '/8/'), URL + '/../x.png', 'http://127.0.0.1/image.png', ''])
def test_foreign_or_invalid_header_refused_without_draft_mutation(db, url):
    tpl = draft(db)
    original = copy.deepcopy(tpl.components)
    with pytest.raises(HTTPException) as error:
        asyncio.run(router.update_template(tpl.id, router.UpdateTemplateIn(components=components(url)), MagicMock(), db))
    assert error.value.status_code == 400
    db.expire_all()
    assert db.get(WhatsAppTemplate, tpl.id).components == original


def test_foreign_template_upload_refused_before_storage(db, monkeypatch):
    tpl = draft(db); tpl.tenant_id = 8; db.commit()
    put = MagicMock()
    monkeypatch.setattr(storage, 'upload_template_header_image', put)
    with pytest.raises(HTTPException) as error:
        asyncio.run(router.upload_template_header_image(tpl.id, MagicMock(), UploadFile(file=io.BytesIO(image_bytes())), db))
    assert error.value.status_code == 404
    put.assert_not_called()


def test_asset_upload_does_not_create_or_mutate_draft_or_call_meta(db, monkeypatch):
    tpl = draft(db)
    before = copy.deepcopy(tpl.components)
    client = MagicMock()
    monkeypatch.setattr(storage, '_s3_client', lambda: client)
    meta = AsyncMock(side_effect=AssertionError('no Meta calls on upload'))
    monkeypatch.setattr(router, 'provider_submit_template', meta)
    result = asyncio.run(router.upload_draft_header_asset(MagicMock(), UploadFile(file=io.BytesIO(image_bytes()))))
    assert result['content_type'] == 'image/png'
    assert result['image_url'].startswith('https://media.example/template-headers/7/')
    assert client.put_object.call_args.kwargs['Body'] == image_bytes()
    db.expire_all()
    assert db.get(WhatsAppTemplate, tpl.id).components == before
    assert db.query(WhatsAppTemplate).count() == 1
    meta.assert_not_called()


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG'])
def test_valid_meta_image_type_is_preserved(fmt):
    raw = image_bytes(fmt)
    data, mime = storage.prepare_template_image(raw)
    assert data == raw
    assert mime == ('image/png' if fmt == 'PNG' else 'image/jpeg')


@pytest.mark.parametrize('raw,reason', [(b'', 'empty_file'), (b'x' * (5*1024*1024+1), 'file_too_large'),
    (b'not-image', 'unsupported_image_type'), (image_bytes('WEBP'), 'unsupported_image_type'),
    (image_bytes()[:25], 'invalid_image')])
def test_bad_image_never_reaches_storage(monkeypatch, raw, reason):
    client = MagicMock()
    monkeypatch.setattr(storage, '_s3_client', client)
    with pytest.raises(storage.CatalogMediaValidationError, match=reason):
        storage.upload_template_header_image(tenant_id=7, content=raw)
    client.assert_not_called()


def test_meta_submit_uses_resumable_sample_not_public_url_and_preserves_draft(monkeypatch):
    from core.commerce_lifecycle import order_confirmation_header_image_fetch as fetcher
    from core.commerce_lifecycle import order_confirmation_meta_header as meta_header
    from services.whatsapp_platform import token_manager
    original = components()
    fetch = AsyncMock(return_value=(image_bytes(), 'image/png'))
    token = AsyncMock(return_value=SimpleNamespace(token='mock-access-token'))
    upload = AsyncMock(return_value='4::sample-handle')
    submit = AsyncMock(return_value=({'id': 'approved-later'}, None))
    monkeypatch.setattr(fetcher, 'fetch_header_image_bytes_secure', fetch)
    monkeypatch.setattr(token_manager, 'get_token_for_operation', token)
    monkeypatch.setattr(meta_header.MetaResumableHeaderUploader, 'upload_template_header', upload)
    monkeypatch.setattr(router, 'provider_submit_template', submit)
    asyncio.run(router._submit_template_to_meta(db=MagicMock(), conn=MagicMock(), tenant_id=7,
        waba_id='test-waba', name='generic_shoe_offer', language='ar', category='UTILITY', components=original))
    payload = submit.call_args.kwargs['payload']
    assert payload['components'][0] == {'type':'HEADER','format':'IMAGE','example':{'header_handle':['4::sample-handle']}}
    assert payload['components'][1:] == components()[1:]
    assert original == components()
    assert upload.call_args.kwargs['mime_type'] == 'image/png'
    fetch.assert_awaited_once_with(URL)


def test_missing_image_blocks_meta_submission(monkeypatch):
    submit = AsyncMock()
    monkeypatch.setattr(router, 'provider_submit_template', submit)
    with pytest.raises(ValueError):
        asyncio.run(router._submit_template_to_meta(db=MagicMock(), conn=MagicMock(), tenant_id=7,
            waba_id='test', name='missing', language='ar', category='UTILITY', components=components('')))
    submit.assert_not_called()


def test_approved_sync_retains_sending_url_without_changing_meta_copy():
    received = components()
    received[0]['example'] = {'header_handle':['review-sample-not-send-media-id']}
    merged = headers.preserve_image_on_sync(components(), received)
    assert headers.header_url(merged) == URL
    assert 'header_url' not in received[0]['example']
    tpl = SimpleNamespace(name='generic_shoe_offer', language='ar', components=merged)
    payload = _build_send_payload(template=tpl, to_phone='966500000000', customer_name='Customer', store_name='Store')
    assert payload['template']['components'] == [{'type':'header','parameters':[{'type':'image','image':{'link':URL}}]}]
    assert validate_template_payload(tpl) == []


def test_campaign_missing_image_has_clear_preflight_and_never_builds_silent_text_only_send():
    tpl = SimpleNamespace(name='missing', language='ar', components=components(''))
    assert validate_template_payload(tpl) == [headers.MISSING_IMAGE]
    with pytest.raises(PayloadValidationError, match=headers.MISSING_IMAGE):
        _build_send_payload(template=tpl, to_phone='966500000000', customer_name='Customer', store_name='Store')


def test_upload_failure_keeps_existing_draft_and_reports_error(db, monkeypatch):
    tpl = draft(db, components())
    old = copy.deepcopy(tpl.components)
    def unavailable(**_):
        raise storage.CatalogMediaStorageError('unavailable')
    monkeypatch.setattr(storage, 'upload_template_header_image', unavailable)
    with pytest.raises(HTTPException) as error:
        asyncio.run(router.upload_draft_header_asset(MagicMock(), UploadFile(file=io.BytesIO(image_bytes()))))
    assert error.value.status_code == 503
    db.expire_all()
    assert db.get(WhatsAppTemplate, tpl.id).components == old


def test_text_header_does_not_inherit_old_image_sample(db):
    tpl = draft(db, components())
    text_components = [{'type':'HEADER','format':'TEXT','text':'Generic sale'}, *components()[1:]]
    asyncio.run(router.update_template(tpl.id, router.UpdateTemplateIn(components=text_components), MagicMock(), db))
    db.expire_all()
    assert db.get(WhatsAppTemplate, tpl.id).components == text_components


def test_resumable_upload_contract_uses_sample_handle_not_media_id(monkeypatch):
    from core.commerce_lifecycle import order_confirmation_meta_header as module
    client = MagicMock()
    client.post = AsyncMock(side_effect=[MagicMock(json=lambda:{'id':'upload:session'}), MagicMock(json=lambda:{'h':'4::sample'})])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **_: context)
    monkeypatch.setattr(module, 'META_APP_ID', 'test-app')
    handle = asyncio.run(module.MetaResumableHeaderUploader().upload_template_header(
        access_token='test-token', image_bytes=image_bytes(), mime_type='image/png'))
    assert handle == '4::sample'
    first, second = client.post.call_args_list
    assert first.args[0].endswith('/test-app/uploads')
    assert first.kwargs['params']['file_type'] == 'image/png'
    assert first.kwargs['params']['file_length'] == str(len(image_bytes()))
    assert second.kwargs['headers']['file_offset'] == '0'
    assert second.kwargs['content'] == image_bytes()


def test_create_image_template_is_a_local_draft_only(db, monkeypatch):
    from models import WhatsAppConnection
    original_query = db.query
    def query(model, *args, **kwargs):
        if model is WhatsAppConnection:
            result = MagicMock()
            result.filter.return_value.first.return_value = None
            return result
        return original_query(model, *args, **kwargs)
    monkeypatch.setattr(db, 'query', query)
    monkeypatch.setattr(router, 'get_or_create_settings', lambda *_: SimpleNamespace(whatsapp_settings={}))
    submit = AsyncMock(side_effect=AssertionError('saving a draft must not submit'))
    monkeypatch.setattr(router, '_submit_template_to_meta', submit)
    result = asyncio.run(router.create_template(router.CreateTemplateIn(
        name='generic_shoe_offer', category='MARKETING', components=components(), auto_submit=False), MagicMock(), db))
    db.expire_all()
    saved = db.get(WhatsAppTemplate, result['id'])
    assert saved.status == 'DRAFT' and saved.meta_template_id is None
    assert saved.components == components()
    submit.assert_not_called()


def test_approved_template_image_upload_refused_before_storage(db, monkeypatch):
    tpl = draft(db, components()); tpl.status = 'APPROVED'; db.commit()
    store = MagicMock()
    monkeypatch.setattr(storage, 'upload_template_header_image', store)
    with pytest.raises(HTTPException) as error:
        asyncio.run(router.upload_template_header_image(tpl.id, MagicMock(), UploadFile(file=io.BytesIO(image_bytes())), db))
    assert error.value.status_code == 409
    store.assert_not_called()
