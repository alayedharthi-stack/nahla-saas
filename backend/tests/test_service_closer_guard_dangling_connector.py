"""Regression: dangling connector after service-closer strip (2026-07-27 tenant 1)."""
from __future__ import annotations

import os
import re
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.postprocess.service_closer_guard import (  # noqa: E402
    apply_service_closer_guard,
)

_FORBIDDEN_CLOSER_TAIL = "إذا تحتاج مساعدة في شيء معين، خبرني وأنا هنا للمساعدة!"
_FIRST_SENTENCE = "للأسف، ما أقدر أطلع على طلباتك السابقة."

_DANGLING_CONNECTOR_END_RE = re.compile(
    r"(?:لكن|ولكن|و|أو|لذلك|ثم)[\s،,.!؟…]*$"
)


def _assert_no_dangling_connector(reply: str) -> None:
    assert not _DANGLING_CONNECTOR_END_RE.search(reply.rstrip()), (
        f"reply still ends with dangling connector: {reply!r}"
    )


def _assert_forbidden_closer_absent(reply: str) -> None:
    assert "أنا هنا للمساعدة" not in reply
    assert "إذا تحتاج مساعدة" not in reply


class TestDanglingConnectorAfterStrip:
    def test_c1_lakin_variant_strips_dangling_clause(self) -> None:
        raw = f"{_FIRST_SENTENCE} لكن {_FORBIDDEN_CLOSER_TAIL}"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert result.reply == _FIRST_SENTENCE
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)

    def test_c2_walakin_variant_strips_dangling_clause(self) -> None:
        raw = f"{_FIRST_SENTENCE} ولكن {_FORBIDDEN_CLOSER_TAIL}"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert result.reply == _FIRST_SENTENCE
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)

    def test_c3_mid_sentence_lakin_unchanged(self) -> None:
        raw = "الطلب جاهز لكن التوصيل يتأخر يومين."
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is False
        assert result.reply == raw
        assert "لكن" in result.reply

    def test_c4_multi_sentence_preserves_earlier_sentences(self) -> None:
        first = "الطلب مسجل في النظام."
        second = "التوصيل خلال يومين عمل."
        raw = f"{first} {second} لكن {_FORBIDDEN_CLOSER_TAIL}"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert result.reply == f"{first} {second}"
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)

    def test_c5_only_forbidden_closer_returns_string(self) -> None:
        raw = "إذا تحتاج أي مساعدة، خبرني وأنا هنا للمساعدة!"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert isinstance(result.reply, str)
        assert result.reply == ""

    # EM review 2026-07-27: single dangling-connector pass was insufficient —
    # stripping «لكن» can expose another connector (e.g. «و») still at the tail.
    def test_c6_waw_lakin_spelling_strips_both_connectors(self) -> None:
        raw = (
            "الطلب مسجل عندنا و لكن "
            f"{_FORBIDDEN_CLOSER_TAIL}"
        )
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert result.reply == "الطلب مسجل عندنا"
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)
        assert not result.reply.rstrip(" ،,.!؟…").endswith("و")
        assert not result.reply.rstrip(" ،,.!؟…").endswith("لكن")

    def test_c7_lidhalika_lakin_chain_strips_to_prior_sentence(self) -> None:
        raw = f"الطلب مسجل. لذلك. لكن {_FORBIDDEN_CLOSER_TAIL}"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert result.reply == "الطلب مسجل."
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)
        assert not result.reply.rstrip(" ،,.!؟…").endswith("لذلك")
        assert not result.reply.rstrip(" ،,.!؟…").endswith("لكن")

    def test_c8_stacked_connectors_terminates_without_dangling_tail(self) -> None:
        raw = f"الطلب مسجل. و لذلك و لكن {_FORBIDDEN_CLOSER_TAIL}"
        result = apply_service_closer_guard(raw, tenant_id=1)

        assert result.stripped is True
        assert isinstance(result.reply, str)
        _assert_no_dangling_connector(result.reply)
        _assert_forbidden_closer_absent(result.reply)
