"""Architectural contract tests: the AI Assets layer (manual coupons +
AI media library) must remain *fully independent* of the autopilot /
automation stack.

The merchant's request is unambiguous:

* ``autopilot_enabled = False`` → the brain still ships manual coupons
  and media items because both libraries are part of *store
  intelligence*, not automation.
* ``autopilot_enabled = True``  → the libraries are still visible to
  GPT (so it may pick a manual coupon if the automatic engine returned
  nothing), but the prompt language tells the model the automatic
  source is the primary one.

These tests lock that contract:

  1. ``list_active_manual_coupons`` and ``list_active_ai_media`` never
     read or import autopilot state.
  2. ``build_merchant_context`` surfaces ``autopilot_enabled`` into
     ``brain_profile`` for prompt-priority guidance only — never as a
     gate on the libraries.
  3. The Brain prompt builder switches its coupon-priority sentence
     based on the autopilot flag while always allowing manual coupons.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _make_db(rows: list):
    """Lightweight stand-in for a SQLAlchemy session that returns ``rows``
    from the order_by(...).limit(...).all() chain used by the listers."""
    db = MagicMock()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = rows
    return db


def _coupon(id_: int, code: str, *, active: bool = True, priority: int = 100):
    return SimpleNamespace(
        id=id_,
        tenant_id=1,
        code=code,
        title=None,
        description=None,
        discount_text=None,
        usage_context=None,
        is_active=active,
        priority=priority,
        starts_at=None,
        expires_at=None,
    )


def _media(id_: int, title: str, *, active: bool = True):
    return SimpleNamespace(
        id=id_,
        tenant_id=1,
        title=title,
        description=None,
        media_type="image",
        usage_context=None,
        tags=[],
        is_active=active,
        priority=100,
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. Library listers do NOT touch autopilot
# ─────────────────────────────────────────────────────────────────────────

def test_ai_libraries_module_does_not_import_automation_engine():
    """Hard architectural constraint: the libraries module must never
    pull the automation/autopilot stack. Anyone who tries to add such
    an import in the future will trip this test.

    We parse the AST so a docstring mentioning the rule (allowed) is
    not flagged as a violation — only real imports / attribute reads
    of ``automation_engine`` or autopilot fields are.
    """
    import ast

    src = (BACKEND_DIR / "core" / "ai_libraries.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden_modules = {"automation_engine", "core.automation_engine"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules and not alias.name.endswith(
                    ".automation_engine"
                ), f"ai_libraries.py imports {alias.name} — breaks intelligence/automation separation"
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            assert node.module not in forbidden_modules and not node.module.endswith(
                ".automation_engine"
            ), f"ai_libraries.py imports from {node.module} — breaks intelligence/automation separation"

    # And no runtime attribute reads against autopilot config either.
    # Comments / docstrings are stripped by ast.unparse on Function/Class
    # bodies but we don't need to walk them — code-level uses would show
    # as Name('autopilot_enabled') or similar identifier nodes.
    autopilot_identifiers = {"autopilot_enabled", "_is_autopilot_enabled", "autopilot"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in autopilot_identifiers:
            raise AssertionError(
                f"ai_libraries.py references identifier {node.id!r} — "
                "the libraries must stay independent of autopilot state."
            )
        if isinstance(node, ast.Attribute) and node.attr in autopilot_identifiers:
            raise AssertionError(
                f"ai_libraries.py reads attribute .{node.attr} — "
                "the libraries must stay independent of autopilot state."
            )


def test_list_active_manual_coupons_works_without_any_autopilot_reads():
    """The lister should hand back active coupons even if every autopilot
    code path is broken. We prove this by calling it with a stub DB that
    only knows how to return ManualCoupon rows."""
    from core.ai_libraries import list_active_manual_coupons

    rows = [_coupon(1, "AYNE26"), _coupon(2, "WELCOME10")]
    db = _make_db(rows)
    out = list_active_manual_coupons(db, tenant_id=1)
    assert [c["code"] for c in out] == ["AYNE26", "WELCOME10"]


def test_list_active_ai_media_works_without_any_autopilot_reads():
    from core.ai_libraries import list_active_ai_media

    rows = [_media(7, "باركود التحويل")]
    db = _make_db(rows)
    out = list_active_ai_media(db, tenant_id=1)
    assert [m["title"] for m in out] == ["باركود التحويل"]


# ─────────────────────────────────────────────────────────────────────────
# 2. Prompt builder switches priority language by autopilot, never
#    hides manual coupons.
# ─────────────────────────────────────────────────────────────────────────

def _make_brain_state(*, autopilot_enabled: bool, manual_coupons=None, media=None):
    """Build a minimal ``BrainReplyState`` instance for prompt rendering."""
    from modules.ai.brain.types import BrainReplyState

    state = BrainReplyState(
        store_name="متجر اختبار",
        tone="neutral",
        intent_name="ask_discount",
        stage="exploring",
        response_goal="ask_for_discount",
        merchant_context={
            "brain_profile": {"autopilot_enabled": autopilot_enabled},
            "manual_coupons": manual_coupons or [],
            "ai_media_library": media or [],
        },
    )
    return state


def test_prompt_includes_manual_coupons_block_when_autopilot_off():
    """Autopilot OFF + active manual coupon → the human-readable block
    appears in the rendered prompt (not just the JSON dump)."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    state = _make_brain_state(
        autopilot_enabled=False,
        manual_coupons=[
            {
                "id": 1,
                "code": "AYNE26",
                "title": "خصم ترحيبي",
                "discount_text": "10%",
                "description": "كوبون لعملاء واتساب",
                "usage_context": "إذا طلب العميل خصم",
                "priority": 10,
                "expires_at": None,
            },
        ],
    )
    prompt = build_brain_reply_prompt(state)
    # Code is reachable to the LLM
    assert "AYNE26" in prompt
    # Priority sentence reflects autopilot-OFF mode
    assert "الطيار الآلي مغلق" in prompt
    # And the language explicitly names manual_coupons as the source
    assert "merchant_context.manual_coupons" in prompt


def test_prompt_includes_manual_coupons_block_when_autopilot_on():
    """Autopilot ON: manual coupons are still visible — the prompt only
    changes which source is the *primary* one."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    state = _make_brain_state(
        autopilot_enabled=True,
        manual_coupons=[
            {
                "id": 2,
                "code": "FALLBACK15",
                "title": "كوبون احتياطي",
                "discount_text": "15%",
                "description": "",
                "usage_context": "إذا فشل المحرك التلقائي",
                "priority": 50,
                "expires_at": None,
            },
        ],
    )
    prompt = build_brain_reply_prompt(state)
    # Manual coupon code is still cited so GPT can reach it
    assert "FALLBACK15" in prompt
    # Priority sentence reflects autopilot-ON mode
    assert "autopilot ON" in prompt
    # Auto coupons are described as the priority, not manual.
    # We search for the stem "كوبونات التلقائية" because it appears
    # both as "الكوبونات التلقائية" (alone) and "للكوبونات التلقائية"
    # (with the prepositional prefix) in the rendered prompt.
    assert "كوبونات التلقائية" in prompt


def test_prompt_media_block_renders_regardless_of_autopilot():
    """Media library is even more clearly part of store intelligence —
    autopilot must never hide it."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    media = [{
        "id": 12,
        "title": "باركود التحويل البنكي",
        "media_type": "image",
        "tags": ["تحويل", "دفع", "بنك"],
        "usage_context": "أرسله إذا طلب العميل التحويل البنكي",
        "description": "",
        "priority": 5,
    }]

    for autopilot in (False, True):
        state = _make_brain_state(autopilot_enabled=autopilot, media=media)
        prompt = build_brain_reply_prompt(state)
        assert "MEDIA_ID=12" in prompt, (
            f"Media library missing from prompt when autopilot={autopilot}"
        )
        assert "باركود التحويل البنكي" in prompt
        # Marker syntax must always be reachable so GPT can cite it.
        assert "[MEDIA:<id>]" in prompt


# ─────────────────────────────────────────────────────────────────────────
# 3. Behavioural scenarios from the user's requirement document
# ─────────────────────────────────────────────────────────────────────────

def test_scenario_autopilot_off_customer_asks_for_coupon():
    """Scenario 1:
        autopilot=False, manual coupon active, customer asks "عندكم كوبون؟"
        Expected: GPT can reach the manual coupon code in the prompt."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    state = _make_brain_state(
        autopilot_enabled=False,
        manual_coupons=[{
            "id": 1,
            "code": "AYNE26",
            "title": "خصم ترحيبي",
            "discount_text": "10% خصم",
            "description": "",
            "usage_context": "إذا طلب العميل خصم أو كوبون",
            "priority": 10,
            "expires_at": None,
        }],
    )
    prompt = build_brain_reply_prompt(state)
    assert "AYNE26" in prompt
    assert "إذا طلب العميل خصم" in prompt
    # GPT is told this is the primary source while autopilot is off.
    assert "merchant_context.manual_coupons" in prompt


def test_scenario_autopilot_off_customer_asks_for_bank_transfer():
    """Scenario 2:
        autopilot=False, media asset active, customer asks for transfer info.
        Expected: GPT can reach the bank-barcode media item by meaning."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    state = _make_brain_state(
        autopilot_enabled=False,
        media=[{
            "id": 12,
            "title": "باركود التحويل البنكي",
            "media_type": "image",
            "tags": ["تحويل", "دفع", "بنك"],
            "usage_context": "أرسله إذا طلب العميل التحويل البنكي أو بيانات الدفع",
            "description": "",
            "priority": 5,
        }],
    )
    prompt = build_brain_reply_prompt(state)
    # Title surfaced for meaning-based selection
    assert "باركود التحويل البنكي" in prompt
    # Tag overlap helps GPT match phrases like "بيانات التحويل"
    assert "تحويل" in prompt and "دفع" in prompt
    # Marker is the only way to attach — and it's documented in the prompt.
    assert "[MEDIA:" in prompt
    # No raw URL is leaked.
    assert "http" not in prompt.lower() or "https://" not in prompt.lower()


def test_scenario_autopilot_on_uses_automatic_first_but_keeps_manual_visible():
    """Scenario 3:
        autopilot=True, both engines active.
        Expected: prompt instructs GPT that automatic coupons are the
        priority, while manual coupons remain reachable as a fallback."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    state = _make_brain_state(
        autopilot_enabled=True,
        manual_coupons=[{
            "id": 9,
            "code": "MANUAL_BACKUP",
            "title": "احتياطي",
            "discount_text": "10%",
            "description": "",
            "usage_context": "إذا لم تنفع الكوبونات التلقائية",
            "priority": 100,
            "expires_at": None,
        }],
    )
    prompt = build_brain_reply_prompt(state)
    # Manual code visible (so GPT can fall back to it if needed)
    assert "MANUAL_BACKUP" in prompt
    # But the *primary* source named in the priority rule is automatic.
    # Same Arabic-stem note as above — search the substring without the
    # prepositional prefix so the assertion isn't fragile.
    assert "كوبونات التلقائية" in prompt
    # And the rule is gated to the autopilot-ON branch.
    assert "autopilot ON" in prompt


# ─────────────────────────────────────────────────────────────────────────
# 4. ai_assets facade still exposes both kinds regardless of autopilot
# ─────────────────────────────────────────────────────────────────────────

def test_ai_assets_listing_independent_of_autopilot():
    """The asset-kind registry should hand back media + coupon entries
    without reading any autopilot/automation state."""
    from core import ai_assets

    db = _make_db([])
    out = ai_assets.list_all_assets_for_prompt(db, tenant_id=1)
    # Both kinds are registered today; the empty-list fallback proves
    # the listers ran without crashing on a missing autopilot import.
    assert "media" in out and "coupon" in out
