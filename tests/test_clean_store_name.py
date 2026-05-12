"""Regression: customer-visible store labels must strip platform slugs in parens."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.store_display import clean_store_name, display_store_name  # noqa: E402
from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt  # noqa: E402


def test_strips_trailing_username_in_parens():
    raw = "متجر آل عايد للعسل البلدي (turky.ayed)"
    assert clean_store_name(raw) == "متجر آل عايد للعسل البلدي"
    assert "(turky.ayed)" not in clean_store_name(raw)


def test_display_store_name_alias():
    assert display_store_name("A (shop.slug)") == "A"


def test_preserves_arabic_parenthetical():
    # Inner text is not ASCII slug — kept as-is
    raw = "متجر الأناقة (فرع شمال الرياض)"
    assert clean_store_name(raw) == raw


def test_strips_slug_with_hyphen():
    assert clean_store_name("متجر الورد (my-shop-name)") == "متجر الورد"


def test_strips_double_trailing_slug():
    assert clean_store_name("Brand (slug10) (slug.two)") == "Brand"


def test_nahla_persona_system_prompt_excludes_slug():
    sp = nahla_persona_system_prompt(
        store_name="متجر آل عايد 🐝 من آل عايد للعسل البلدي (turky.ayed)",
        store_context_text="",
    )
    assert "(turky.ayed)" not in sp
    assert "turky.ayed" not in sp
    assert "للعسل البلدي" in sp
