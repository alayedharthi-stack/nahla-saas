"""Scoped index deployment never rewrites historical usage or broadens migrations."""
import sys
from unittest.mock import Mock
import pytest
import sqlalchemy as sa
from database.ai_usage_dedup_helpers import install_index
from scripts.operators import install_ai_usage_dedup_index as operator


def table():
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE ai_usage_events (provider TEXT NOT NULL, request_id TEXT, total_cost_usd NUMERIC)'))
        conn.execute(sa.text("INSERT INTO ai_usage_events VALUES ('anthropic', 'test-one', 0.0012), ('anthropic', NULL, 0.0012), ('anthropic', NULL, 0.0012)"))
    return engine


def test_index_install_is_idempotent_and_preserves_old_amounts():
    engine = table()
    with engine.begin() as conn:
        before = conn.execute(sa.text('SELECT * FROM ai_usage_events')).all()
        assert install_index(conn) == 'installed'
        assert install_index(conn) == 'already_installed'
        assert conn.execute(sa.text('SELECT * FROM ai_usage_events')).all() == before
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO ai_usage_events VALUES ('anthropic', 'test-one', 0.0015)"))


def test_wrong_existing_index_definition_blocks():
    engine = table()
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE INDEX uq_ai_usage_provider_request ON ai_usage_events (request_id)'))
        with pytest.raises(RuntimeError, match='definition requires review'):
            install_index(conn)


def test_scope_mismatch_never_connects(monkeypatch, capsys):
    connect = Mock(side_effect=AssertionError('must not connect'))
    monkeypatch.setattr(operator.psycopg2, 'connect', connect)
    monkeypatch.setattr(sys, 'argv', ['operator', '--apply', '--project', 'test-project', '--environment', 'test-env', '--service', 'test-service', '--tenant', '1', '--tenant', '33'])
    for key in ('RAILWAY_PROJECT_ID', 'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_SERVICE_ID'):
        monkeypatch.delenv(key, raising=False)
    assert operator.main() == 2
    assert 'deployment_scope_mismatch' in capsys.readouterr().out
    connect.assert_not_called()


def configure(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['operator', '--apply', '--project', 'test-project', '--environment', 'test-env', '--service', 'test-service', '--tenant', '1', '--tenant', '33'])
    for key, value in [('RAILWAY_PROJECT_ID', 'test-project'), ('RAILWAY_ENVIRONMENT_ID', 'test-env'), ('RAILWAY_SERVICE_ID', 'test-service'), ('DATABASE_URL', 'postgresql://example:private-test-value@localhost/test')]:
        monkeypatch.setenv(key, value)


def test_historical_duplicates_stop_before_any_write(monkeypatch, capsys):
    configure(monkeypatch)
    connection = Mock()
    engine = Mock(side_effect=AssertionError('no write connection allowed'))
    monkeypatch.setattr(operator.psycopg2, 'connect', lambda *a, **kw: connection)
    monkeypatch.setattr(operator, 'audit', lambda *a, **kw: {'duplicate_request_groups': 1})
    monkeypatch.setattr(operator.sa, 'create_engine', engine)
    assert operator.main() == 3
    out = capsys.readouterr().out
    assert 'historical_duplicates_require_review' in out
    assert 'private-test-value' not in out
    engine.assert_not_called()
    connection.close.assert_called_once()


def test_connection_errors_are_not_zero_and_do_not_expose_credentials(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(operator.psycopg2, 'connect', Mock(side_effect=RuntimeError('private-test-value')))
    assert operator.main() == 1
    out = capsys.readouterr().out
    assert '"status": "blocked"' in out
    assert 'private-test-value' not in out
