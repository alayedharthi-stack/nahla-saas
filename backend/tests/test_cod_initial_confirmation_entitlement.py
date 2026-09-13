from types import SimpleNamespace

from core.automation_engine import _is_initial_cod_confirmation_order_update


def _event(*, event_type="order_cod_pending", message_type=None):
    payload = {}
    if message_type is not None:
        payload["message_type"] = message_type
    return SimpleNamespace(event_type=event_type, payload=payload)


def test_initial_cod_prompt_is_a_transactional_order_update():
    assert _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(message_type="initial_confirmation"),
    )


def test_cod_reminders_remain_growth_autopilot():
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(message_type="reminder"),
    )
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(),
    )


def test_unrelated_order_events_do_not_bypass_entitlements():
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(event_type="order_created", message_type="initial_confirmation"),
    )
    assert not _is_initial_cod_confirmation_order_update(
        "customer_winback",
        _event(message_type="initial_confirmation"),
    )
