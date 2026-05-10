"""
Lock-in tests for the manual knowledge base injection in the tenant
prompt overlay.

Why these tests matter:
  * The product rule is "manual KB is non-authoritative for prices/inventory
    when Salla is connected". That rule must appear *verbatim* in the
    prompt — losing it would let the model quote a stale merchant-typed
    price instead of the canonical Salla price.
  * Empty knowledge_base must NOT add a section header (otherwise the
    LLM gets a misleading "اعتمد قاعدة المعرفة" instruction with no body).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from modules.ai.prompts.tenant_overlay import build_tenant_prompt_overlay


def test_empty_knowledge_base_does_not_add_section():
    out = build_tenant_prompt_overlay({
        "assistant_name": "نحلة",
        "manual_knowledge_base": "",
    })
    assert "قاعدة المعرفة" not in out


def test_whitespace_only_knowledge_base_does_not_add_section():
    out = build_tenant_prompt_overlay({
        "assistant_name": "نحلة",
        "manual_knowledge_base": "   \n  \t\n",
    })
    assert "قاعدة المعرفة" not in out


def test_non_empty_knowledge_base_adds_section_with_content():
    kb = "نشحن مجاناً للطلبات فوق 200 ريال. الضمان سنة كاملة."
    out = build_tenant_prompt_overlay({
        "assistant_name": "نحلة",
        "manual_knowledge_base": kb,
    })
    assert "قاعدة المعرفة" in out
    assert kb in out


def test_knowledge_base_carries_salla_precedence_rule():
    """The Salla-wins rule for prices/inventory must always be in the prompt."""
    out = build_tenant_prompt_overlay({
        "manual_knowledge_base": "أي محتوى",
    })
    assert "سلة" in out, "Salla precedence rule missing from KB block"
    assert "السعر" in out
    assert "merchant_context" in out


def test_knowledge_base_does_not_leak_into_other_sections_when_only_field():
    """If KB is the *only* AI setting set, the wrapper headers still appear."""
    out = build_tenant_prompt_overlay({
        "manual_knowledge_base": "محتوى المتجر",
    })
    assert "═══ إعدادات مساعد المتجر" in out
    assert "═══ نهاية إعدادات المتجر ═══" in out


def test_owner_instructions_and_knowledge_base_are_separate_sections():
    out = build_tenant_prompt_overlay({
        "owner_instructions":     "كن مهذّباً دائماً",
        "manual_knowledge_base":  "السعر يبدأ من 99 ريال",
    })
    # Both blocks render, but as separate sections — neither leaks into the other.
    owner_idx = out.index("تعليمات صاحب المتجر")
    kb_idx    = out.index("قاعدة المعرفة")
    assert owner_idx != kb_idx
    assert "كن مهذّباً دائماً"      in out
    assert "السعر يبدأ من 99 ريال" in out
