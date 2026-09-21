"""The replay nonce as a claim that becomes an acceptance, not a flag.

``core.webhook_security.evaluate_replay_claim`` writes ``claimed:<token>:<epoch>``
when a request takes the nonce and only ``mark_replay_completed`` turns it into
``completed:<token>``. These cases pin the state machine against the same
``NonceRedis`` double the route suites use (SET NX EX, GET, DEL and the two
compare-and-set scripts run through ``eval``), with the real module code.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.path.join(_ROOT, "backend"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core import webhook_security as security  # noqa: E402
from tests.commerce_reliability.runtime_support import NonceRedis  # noqa: E402

BODY = b'{"entry": [{"id": "WABA"}]}'
KEY = security.replay_nonce_key("meta", BODY)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> NonceRedis:
    import core.redis_client as redis_client
    from core import config as core_config

    nonce = NonceRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: nonce)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_PROTECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_REJECT_ENABLED", True, raising=False)
    return nonce


def claim_written(seconds_ago: float, token: str = "other") -> str:
    return f"claimed:{token}:{int(time.time() - seconds_ago)}"


def test_the_first_request_claims_and_holds_its_own_token(store: NonceRedis) -> None:
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (False, True, False)
    assert verdict.claim == store.keys[KEY]
    assert store.keys[KEY].startswith(f"claimed:{verdict.token}:")
    assert store.ttls[KEY] == 86400


def test_a_completed_nonce_is_a_replay(store: NonceRedis) -> None:
    store.keys[KEY] = "completed:other"
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (True, False, False)
    assert store.keys[KEY] == "completed:other"


def test_a_nonce_written_by_an_earlier_deployment_is_taken_over_not_acknowledged(
        store: NonceRedis) -> None:
    """``"1"`` is what the previous code wrote when a request *took* the nonce.
    It cannot say how far that request got — it may have died before anything
    was durable — so it is an ambiguous acquisition, never a completed one: the
    retry takes it over as a first attempt, and the durable records and the
    dedup boundaries decide per message what was already done."""
    store.keys[KEY] = "1"
    store.ttls[KEY] = 500
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (False, True, False)
    assert store.keys[KEY] == verdict.claim and verdict.claim.startswith("claimed:")
    assert store.evals == ["-- nahla:nonce_cas_set"]
    assert store.ttls[KEY] == 500                            # the key's own expiry is kept
    # Only the holder completes it, and from then on it is a real replay.
    assert security.mark_replay_completed(verdict) is True
    later = security.evaluate_replay_claim("meta", BODY)
    assert (later.reject, later.claimed, later.in_flight) == (True, False, False)


def test_an_unparseable_nonce_is_taken_over_like_a_legacy_one(store: NonceRedis) -> None:
    """Any value that is neither an open claim nor a completed marker says
    nothing about the request that wrote it; it is not acknowledged as a
    replay on that strength."""
    store.keys[KEY] = "claimed:garbage"
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed) == (False, True)
    assert store.keys[KEY] == verdict.claim


def test_a_legacy_nonce_taken_over_by_a_concurrent_copy_is_in_flight_for_the_loser(
        store: NonceRedis, monkeypatch) -> None:
    """Two retries find ``"1"`` at once. The compare-and-set lets exactly one
    take it over; the other sees a fresh claim and is in flight — nothing is
    acknowledged and nothing is dropped."""
    store.keys[KEY] = "1"
    real_eval = store.eval

    def _lose_the_race(script: str, numkeys: int, *args) -> int:
        # The other copy's compare-and-set lands first.
        store.keys[KEY] = claim_written(0.0, "winner")
        return real_eval(script, numkeys, *args)

    monkeypatch.setattr(store, "eval", _lose_the_race)
    loser = security.evaluate_replay_claim("meta", BODY)
    assert (loser.reject, loser.claimed, loser.in_flight) == (True, False, True)
    assert store.keys[KEY] == claim_written(0.0, "winner")


def test_an_open_claim_inside_the_lease_is_in_flight(store: NonceRedis) -> None:
    store.keys[KEY] = claim_written(2.0)
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (True, False, True)
    assert store.keys[KEY] == claim_written(2.0)            # untouched
    assert store.evals == []                                # nothing taken over


def test_an_open_claim_older_than_the_lease_is_taken_over_once(store: NonceRedis) -> None:
    stale = claim_written(security.REPLAY_IN_FLIGHT_LEASE_SECONDS + 1)
    store.keys[KEY] = stale
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (False, True, False)
    assert store.keys[KEY] == verdict.claim != stale
    assert store.evals == ["-- nahla:nonce_cas_set"]
    # A second retry racing the takeover finds a fresh claim: in flight.
    second = security.evaluate_replay_claim("meta", BODY)
    assert (second.reject, second.claimed, second.in_flight) == (True, False, True)


def test_only_the_holder_marks_the_claim_completed(store: NonceRedis) -> None:
    mine = security.evaluate_replay_claim("meta", BODY)
    stranger = security.ReplayVerdict(reject=False, claimed=True, key=KEY,
                                      token="stranger", claim="claimed:stranger:0")
    assert security.mark_replay_completed(stranger) is False
    assert store.keys[KEY] == mine.claim
    assert security.mark_replay_completed(mine) is True
    assert store.keys[KEY] == f"completed:{mine.token}"
    assert store.ttls[KEY] == 86400                        # the TTL is kept


def test_only_the_holder_releases_the_claim(store: NonceRedis) -> None:
    mine = security.evaluate_replay_claim("meta", BODY)
    stranger = security.ReplayVerdict(reject=False, claimed=True, key=KEY,
                                      token="stranger", claim="claimed:stranger:0")
    assert security.release_replay_nonce(stranger) is False
    assert KEY in store.keys
    assert security.release_replay_nonce(mine) is True
    assert KEY not in store.keys
    # And a completed marker is never deleted by a late release.
    store.keys[KEY] = f"completed:{mine.token}"
    assert security.release_replay_nonce(mine) is False
    assert store.keys[KEY] == f"completed:{mine.token}"


def test_an_unclaimed_verdict_neither_marks_nor_releases(store: NonceRedis) -> None:
    store.keys[KEY] = "completed:other"
    replay = security.evaluate_replay_claim("meta", BODY)
    assert replay.claimed is False
    assert security.mark_replay_completed(replay) is False
    assert security.release_replay_nonce(replay) is False
    assert store.keys[KEY] == "completed:other"


def test_without_redis_nothing_is_claimed_and_nothing_is_rejected(monkeypatch) -> None:
    import core.redis_client as redis_client
    from core import config as core_config

    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_PROTECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_REJECT_ENABLED", True, raising=False)
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed, verdict.in_flight) == (False, False, False)
    assert security.mark_replay_completed(verdict) is False
    assert security.release_replay_nonce(verdict) is False


def test_protection_off_claims_nothing(store: NonceRedis, monkeypatch) -> None:
    from core import config as core_config

    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_PROTECTION_ENABLED", False, raising=False)
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert (verdict.reject, verdict.claimed) == (False, False)
    assert store.keys == {}


def test_the_lease_is_measured_from_the_claim_not_from_the_key_ttl(store: NonceRedis) -> None:
    store.keys[KEY] = claim_written(security.REPLAY_IN_FLIGHT_LEASE_SECONDS - 1)
    store.ttls[KEY] = 5                                     # about to expire, still in flight
    verdict = security.evaluate_replay_claim("meta", BODY)
    assert verdict.in_flight is True
