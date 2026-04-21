"""
scripts/simulate_conversation_modes.py
──────────────────────────────────────
End-to-end simulator for the Conversation Mode Controller.

For each of the 5 real-chat scenarios below, this script invokes the
EXACT production functions used by the WhatsApp webhook
(`resolve_conversation_mode`, `render_identity_reply`,
`mode_prompt_overlay`) against a stand-in conversation row.

For each scenario it prints:
    - inbound text
    - prior mode
    - detected mode + reason + source
    - lease info (locked_until, transitioned, free_form_override)
    - the EXACT reply that would be sent to the customer (when the
      controller answers deterministically), OR the mode-aware system
      prompt overlay that would be injected into the AI prompt builder
      when the controller hands off to Brain / legacy.

Run from the repo root:

    python scripts/simulate_conversation_modes.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in reversed([str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

from modules.ai.routing.conversation_mode import (  # noqa: E402
    META_KEY,
    MODE_AUTOMATION_RECOVERY,
    MODE_CHECKOUT_ASSIST,
    MODE_IDENTITY_REPLY,
    MODE_LIVE_CHAT,
    MODE_POST_PURCHASE,
    MODE_SUPPORT_ESCALATION,
    RecoverySnapshot,
    mode_prompt_overlay,
    render_identity_reply,
    resolve_conversation_mode,
    save_lease,
)
import modules.ai.routing.conversation_mode as cm_mod  # noqa: E402


# ── Stand-ins ────────────────────────────────────────────────────────────────

class FakeConvo:
    def __init__(self, *, extra_metadata=None,
                 is_human_handoff=False, paused_by_human=False):
        self.id = 99
        self.extra_metadata = dict(extra_metadata or {})
        self.is_human_handoff = is_human_handoff
        self.paused_by_human = paused_by_human


class FakeDB:
    """Minimal DB. Recovery snapshot loader is patched per-scenario so we
    can deterministically simulate "active recovery"."""
    def add(self, *_a, **_k): pass
    def flush(self): pass
    def rollback(self): pass
    def query(self, *_a, **_k): return self
    def filter(self, *_a, **_k): return self
    def order_by(self, *_a, **_k): return self
    def first(self): return None
    def limit(self, *_a, **_k): return self
    def all(self): return []


# Stub the merchant assistant identity so identity replies show real
# personalized text rather than the unbranded fallback.
def _fake_assistant_name(_db, _tid): return "نحلة"
def _fake_store_name(_db, _tid):    return "متجر العميل"


cm_mod._load_assistant_name = _fake_assistant_name
cm_mod._load_store_name     = _fake_store_name


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_recovery_lease(convo: FakeConvo) -> None:
    """Seed the conversation as currently owned by automation_recovery
    (i.e. the customer just received a cart reminder)."""
    convo.extra_metadata[META_KEY] = {
        "mode":          MODE_AUTOMATION_RECOVERY,
        "previous_mode": "",
        "reason":        "automation reminder sent",
        "source":        "recovery_lineage_active",
        "changed_at":    datetime.now(timezone.utc).isoformat(),
        "locked_until":  "",
    }


def _patch_active_recovery():
    """Patch the loader so the scenario simulates an open recovery tree
    for this customer (step 2 of an abandoned-cart sequence)."""
    cm_mod.load_recovery_snapshot = lambda _db, *, tenant_id, customer_phone: \
        RecoverySnapshot(
            has_recovery=True, recovery_active=True,
            last_step_idx=2, last_step_at=datetime.now(timezone.utc).isoformat(),
            order_id=12345,
        )


def _patch_no_recovery():
    cm_mod.load_recovery_snapshot = lambda _db, *, tenant_id, customer_phone: \
        RecoverySnapshot()


def _print_scenario(num: int, title: str, inbound: str, decision, convo, *,
                    note: str = "") -> None:
    print()
    print("═" * 78)
    print(f"SCENARIO {num}: {title}")
    print("═" * 78)
    print(f"Inbound text       : {inbound!r}")
    print(f"Prior mode         : {decision.previous_mode}")
    print(f"Detected mode      : {decision.mode}")
    print(f"Reason             : {decision.reason}")
    print(f"Source             : {decision.source}")
    print(f"Transitioned       : {decision.transitioned}")
    print(f"Free-form override : {decision.free_form_override}")
    print(f"Recovery active    : {decision.recovery.recovery_active}")
    print(f"Lease.locked_until : {decision.lease.locked_until or '(none)'}")
    print(f"Lease.previous_mode: {decision.lease.previous_mode}")
    if note:
        print(f"Note               : {note}")


def _print_reply(label: str, body: str) -> None:
    print(f"\n── {label} ─────────────────────────────────────")
    for line in body.splitlines():
        print(f"   {line}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIOS
# ─────────────────────────────────────────────────────────────────────────────

def scenario_1_greeting_during_recovery():
    """cart recovery → customer says السلام عليكم"""
    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()
    inbound = "السلام عليكم"

    decision = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text=inbound,
    )
    save_lease(db, convo, decision.lease)
    _print_scenario(1, "cart recovery → customer says 'السلام عليكم'",
                    inbound, decision, convo)

    if decision.mode == MODE_IDENTITY_REPLY:
        reply = render_identity_reply(db, tenant_id=1,
                                      topic=decision.identity_topic)
        _print_reply("FINAL REPLY SENT TO CUSTOMER", reply)


def scenario_2_who_are_you_during_recovery():
    """cart recovery → customer says من أنت"""
    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()
    inbound = "من أنت"

    decision = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text=inbound,
    )
    save_lease(db, convo, decision.lease)
    _print_scenario(2, "cart recovery → customer says 'من أنت'",
                    inbound, decision, convo)

    if decision.mode == MODE_IDENTITY_REPLY:
        reply = render_identity_reply(db, tenant_id=1,
                                      topic=decision.identity_topic)
        _print_reply("FINAL REPLY SENT TO CUSTOMER", reply)


def scenario_3_freeform_product_question():
    """cart recovery → customer asks a product question in free-form"""
    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()
    inbound = "عندكم منتج آخر بنفس السعر؟"

    decision = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text=inbound,
    )
    save_lease(db, convo, decision.lease)
    _print_scenario(3, "cart recovery → free-form product question",
                    inbound, decision, convo,
                    note="Controller hands off to Brain/legacy AI; the "
                         "overlay below is what the legacy prompt builder "
                         "injects on top of the system prompt.")

    overlay = mode_prompt_overlay(decision)
    _print_reply("MODE-AWARE SYSTEM PROMPT OVERLAY (injected before AI call)",
                 overlay or "(none — Brain composes reply directly)")


def scenario_4_human_request_during_recovery():
    """cart recovery → customer asks for a human / employee"""
    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()
    inbound = "أبغى أتحدث مع موظف الآن"

    decision = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text=inbound,
    )
    save_lease(db, convo, decision.lease)
    _print_scenario(4, "cart recovery → asks for human / employee",
                    inbound, decision, convo,
                    note="Controller marks the conversation as support "
                         "escalation and locks a 30-min lease so automation "
                         "cannot reclaim ownership while the merchant is "
                         "responding.")

    overlay = mode_prompt_overlay(decision)
    _print_reply("MODE-AWARE SYSTEM PROMPT OVERLAY (handoff guidance)",
                 overlay or "(none)")


def scenario_5_lease_expires_after_inactivity():
    """5a) Greeting during recovery → live_chat lease acquired.
    5b) Simulate inactivity by ageing the lease past locked_until.
    5c) Next ambiguous message → controller falls back to normal mode
        resolution. With recovery still active, the conversation
        legitimately returns to automation_recovery."""
    print()
    print("═" * 78)
    print("SCENARIO 5: LIVE_CHAT lease expires after inactivity")
    print("═" * 78)

    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()

    # 5a — first turn: greeting acquires LIVE_CHAT lease
    first = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text="السلام عليكم",
    )
    save_lease(db, convo, first.lease)
    print("\n[step a] turn 1: customer says 'السلام عليكم'")
    print(f"   detected_mode  = {first.mode}")
    print(f"   lease_until    = {first.lease.locked_until}")
    print(f"   prior_mode     = {first.previous_mode}")

    # 5b — age the lease so it has expired
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    convo.extra_metadata[META_KEY]["locked_until"] = expired_at.isoformat()
    convo.extra_metadata[META_KEY]["mode"] = MODE_LIVE_CHAT
    print("\n[step b] simulate inactivity — ageing lease into the past")
    print(f"   stored locked_until = {convo.extra_metadata[META_KEY]['locked_until']} (expired)")

    # 5c — next inbound: an ambiguous reaction (no override signal,
    # not free-form intent). With recovery still active and lease
    # expired, controller correctly resumes automation_recovery.
    second = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text="[button:resume_cart]",
    )
    save_lease(db, convo, second.lease)
    print("\n[step c] turn 2 after inactivity: button payload (no free-form)")
    print(f"   detected_mode  = {second.mode}")
    print(f"   reason         = {second.reason}")
    print(f"   source         = {second.source}")
    print(f"   prior_mode     = {second.previous_mode}")
    print(f"   lease_until    = {second.lease.locked_until or '(none)'}")
    if second.mode == MODE_AUTOMATION_RECOVERY:
        print("   → automation_recovery legitimately resumes only AFTER "
              "lease expiry + no overriding signal.")

    # Bonus: a free-form text after the same expired lease still
    # immediately re-acquires LIVE_CHAT — the override path remains
    # responsive.
    convo.extra_metadata[META_KEY]["mode"] = MODE_AUTOMATION_RECOVERY
    convo.extra_metadata[META_KEY]["locked_until"] = ""
    third = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000", text="أبغى ألغي طلبي",
    )
    print("\n[step d] verification: free-form after expiry still wins")
    print(f"   detected_mode  = {third.mode}")
    print(f"   override       = {third.free_form_override}")
    print(f"   lease_until    = {third.lease.locked_until}")


def scenario_7_full_system_prompt_preview():
    """Show the EXACT system prompt that the legacy WhatsApp AI path
    composes for a free-form product question during recovery — the
    persona, the merchant store context, and the per-mode overlay all
    layered together. This is what the LLM actually sees."""
    print()
    print("═" * 78)
    print("SCENARIO 7: Full composed system prompt (legacy AI path)")
    print("═" * 78)

    from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt

    db, convo = FakeDB(), FakeConvo()
    _seed_recovery_lease(convo)
    _patch_active_recovery()

    decision = resolve_conversation_mode(
        db, tenant_id=1, convo=convo,
        customer_phone="+966500000000",
        text="عندكم منتج آخر بنفس السعر؟",
    )
    save_lease(db, convo, decision.lease)

    fake_store_context = (
        "- المنتج: قميص قطني — متوفر — السعر 99 ريال\n"
        "- المنتج: حذاء رياضي — متوفر — السعر 249 ريال\n"
        "- سياسة الإرجاع: خلال 7 أيام من الاستلام."
    )
    system_prompt = nahla_persona_system_prompt(
        store_name="متجر العميل",
        store_context_text=fake_store_context,
    )
    overlay = mode_prompt_overlay(decision)
    if overlay:
        system_prompt = f"{system_prompt}\n\n{overlay}"

    print()
    for line in system_prompt.splitlines():
        print(f"   {line}")
    print()


def scenario_6_identity_variants_demo():
    """Run the identity / greeting renderer multiple times to show that
    rotation actually produces different warm variants (no repeated
    canned line)."""
    print()
    print("═" * 78)
    print("SCENARIO 6: Variant rotation — greeting & identity")
    print("═" * 78)
    db = FakeDB()

    print("\n— Greeting (السلام عليكم) — 4 successive renders:")
    for i in range(1, 5):
        reply = render_identity_reply(db, tenant_id=1, topic="greeting")
        print(f"\n[render {i}]")
        for line in reply.splitlines():
            print(f"   {line}")

    print("\n— Identity (من أنت) — 3 successive renders:")
    for i in range(1, 4):
        reply = render_identity_reply(db, tenant_id=1, topic="identity")
        print(f"\n[render {i}]")
        for line in reply.splitlines():
            print(f"   {line}")


def main() -> int:
    print("Nahla — Conversation Mode Controller end-to-end simulation")
    print(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
    scenario_1_greeting_during_recovery()
    scenario_2_who_are_you_during_recovery()
    scenario_3_freeform_product_question()
    scenario_4_human_request_during_recovery()
    scenario_5_lease_expires_after_inactivity()
    scenario_6_identity_variants_demo()
    scenario_7_full_system_prompt_preview()
    print()
    print("═" * 78)
    print("All scenarios executed — see per-scenario blocks above.")
    print("═" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
