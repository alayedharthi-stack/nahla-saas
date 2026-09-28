"""The shape of what the model is told about values that are not references."""
from __future__ import annotations

from core.commerce_runtime import agent_contracts as ac


def test_only_the_values_that_are_not_references_are_named_bounded_and_quoted():
    detail = ac.malformed_evidence_detail(["catalog:product:1", "140", "x" * 500] + [str(i) for i in range(6)])
    assert "8 value(s)" in detail
    assert '"140"' in detail and "catalog:product:1" not in detail
    assert "x" * 61 not in detail                       # each shown value is clipped
    assert "and 3 more" in detail


def test_the_shape_check_still_refuses_everywhere_else_as_a_validation_error():
    import pytest
    from core.commerce_runtime import contracts as c

    with pytest.raises(c.ValidationError):
        ac.validate_evidence_ref("140")
    with pytest.raises(ac.MalformedEvidenceReference):
        ac.validate_evidence_ref("product 140")
    assert ac.validate_evidence_ref("catalog:product:140") == "catalog:product:140"
