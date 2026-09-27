#!/usr/bin/env python3
"""Stage 1: the message-intent check's decision alone, on the real model (eval only).

No agent runs, nothing is sent, no production database is touched. For each
case the check (``core.commerce_runtime.message_intent``) sees the message, the
case's recent conversation and the store's own product titles, and records one
decision. The case table below is measurement only: it is never shown to the
check and nothing in the runtime imports it.

Hard gates (any miss stops the alternative):
  * the observed message, with and without its social context, is never
    ``store_request``;
  * every clear product or store request is ``store_request``;
  * a bare name is ``store_request`` where the store sells a product by it and
    is not where it does not.
Soft cases (social and personal messages, and held-out wording written after
the check's instructions were fixed) are reported, not gated.

Output: one ``INTENT_TURN=`` JSON line per call and one ``INTENT_SUMMARY=``.
"""
from __future__ import annotations

import collections
import dataclasses
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from budget import Budget  # noqa: E402

LABEL = os.environ.get("EVAL_LABEL", "intent")
SR, NC, AMB = "store_request", "non_commercial", "ambiguous"
NOT_SR = (NC, AMB)
SOCIAL = (("inbound", "ابداع روعه"), ("outbound", "الله يسعدك 🌷"))
PERFUME_ASKED = (("inbound", "عندكم عطر سلطان؟"), ("outbound", "إيه عندنا عطر سلطان بـ 220 ريال 🌹"))


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    group: str             # original | product | named | social | holdout_personal | holdout_product
    store: str             # "D" clothing, "E" perfume
    message: str
    accepted: Tuple[str, ...]
    hard: bool
    history: Tuple[Tuple[str, str], ...] = ()


CASES: Tuple[Case, ...] = (
    Case("o_after_social", "original", "D", "عيال محمد عندك", NOT_SR, True, SOCIAL),
    Case("o_fresh", "original", "D", "عيال محمد عندك", NOT_SR, True),
    Case("o_fresh_perfume", "original", "E", "عيال محمد عندك", NOT_SR, True),
    Case("n_sultan_sold", "named", "E", "سلطان عندك؟", (SR,), True),
    Case("n_sultan_not_sold", "named", "D", "سلطان عندك؟", NOT_SR, True),
    Case("n_perfume_sultan", "named", "E", "عطر سلطان عندك؟", (SR,), True),
    Case("p_blouse", "product", "D", "فيه بلوزه بيضا؟", (SR,), True),
    Case("p_black_dress", "product", "D", "عندكم فستان أسود؟", (SR,), True),
    Case("p_browse", "product", "D", "وش عندكم منتجات؟", (SR,), True),
    Case("p_incense", "product", "E", "عندكم بخور؟", (SR,), True),
    Case("p_watches", "product", "D", "عندكم ساعات؟", (SR,), True),
    Case("p_green_dress", "product", "D", "عندكم فستان أخضر؟", (SR,), True),
    Case("p_amber", "product", "E", "عندكم عطر عنبر؟", (SR,), True),
    Case("p_abayas", "product", "D", "عندكم عبايات؟", (SR,), True),
    Case("p_context_amber", "product", "E", "وعنبر؟", (SR,), True, PERFUME_ASKED),
    Case("p_delivery", "product", "D", "توصلون للرياض؟", (SR,), True),
    Case("p_gift_wrap", "product", "D", "عندكم تغليف هدايا؟", (SR,), True),
    Case("s_compliment", "social", "D", "ابداع روعه", NOT_SR, False),
    Case("s_dinner", "social", "D", "تبغون العشا الي البارح", NOT_SR, False),
    Case("s_addressing_owner", "social", "D", "خالد رد علي", NOT_SR, False),
    Case("s_arrived", "social", "E", "الجماعة وصلوا عندك؟", NOT_SR, False),
    Case("hp_shoes", "holdout_product", "D", "ابي جزمه رياضيه مقاس 42", (SR,), True),
    Case("hp_bag_colour", "holdout_product", "D", "الشنطه الجلد لونها ايش؟", (SR,), True),
    Case("hp_price", "holdout_product", "D", "بكم الفستان الوردي", (SR,), True),
    Case("hp_order", "holdout_product", "D", "طلبي متى يوصل؟", (SR,), True),
    Case("hp_gift_advice", "holdout_product", "E", "ابي هديه لزوجتي وش تنصحني", (SR,), True),
    Case("hp_oud", "holdout_product", "E", "دهن العود موجود؟", (SR,), True),
    Case("hn_phone_number", "holdout_personal", "D", "رقم ابو فهد عندك؟", NOT_SR, False),
    Case("hn_mother", "holdout_personal", "E", "امي تسأل عنك وتسلم عليك", NOT_SR, False),
    Case("hn_missed_call", "holdout_personal", "D", "وينك ما رديت على الجوال", NOT_SR, False),
    Case("hn_keys", "holdout_personal", "E", "المفتاح عندك؟", NOT_SR, False),
    Case("ha_this", "holdout_ambiguous", "D", "هذا عندكم؟", (SR, AMB), False),
)

DRESS_ASKED = (("inbound", "عندكم فستان وردي؟"), ("outbound", "إيه عندنا فستان وردي بـ 179 ريال 🌸"))

# Set b: where the check could cost a real shopper. Written after set a's
# results, to probe the known limits: a store with more products than the check
# is shown (G: 150 titles, «عطر سلطان» past the first 80), brand and
# perfume-house names, English, model codes, sizes, follow-ups. A product
# request judged non_commercial is the harm; ambiguous is accepted only where
# the name alone cannot show it is a product.
CASES_B: Tuple[Case, ...] = (
    Case("b_sultan_beyond_window", "harm", "G", "سلطان عندك؟", (SR, AMB), True),
    Case("b_perfume_sultan_beyond", "harm", "G", "عطر سلطان عندك؟", (SR,), True),
    Case("b_brand", "harm", "E", "عندكم شانيل؟", (SR,), True),
    Case("b_perfume_house", "harm", "E", "عبدالصمد القرشي عندكم؟", (SR, AMB), True),
    Case("b_english", "harm", "D", "Do you have a white blouse?", (SR,), True),
    Case("b_model_code", "harm", "D", "عندكم موديل 2291؟", (SR,), True),
    Case("b_followup_colour", "harm", "D", "والأسود؟", (SR,), True, DRESS_ASKED),
    Case("b_greeting_request", "harm", "D", "السلام عليكم عندكم مقاسات كبيرة؟", (SR,), True),
    Case("b_size_only", "harm", "D", "XL متوفر؟", (SR,), True),
    Case("b_order_complaint", "harm", "D", "الطلب وصل ناقص", (SR,), True),
    Case("b_gift_for_named", "harm", "E", "ابي عطر لأخوي محمد", (SR,), True),
    Case("b_original_after_product", "original", "D", "عيال محمد عندك", NOT_SR, True, DRESS_ASKED),
    Case("b_original_large_store", "original", "G", "عيال محمد عندك", NOT_SR, True),
    Case("b_compliment_large_store", "social", "G", "ابداع روعه", NOT_SR, False),
)

# Set c, after the owner's corrected criterion (2026-09-27): a bare name alone
# («سلطان عندك؟») is ambiguous and may be clarified even where the store sells
# «عطر سلطان», so those cases no longer gate. What gates is harm to a clear
# request: a named product with its kind past the check's first 80 titles, an
# order or delivery question, and a store whose catalogue could not be read.
BARE_NAME_SOLD = {"n_sultan_sold", "b_sultan_beyond_window"}
CASES_C: Tuple[Case, ...] = (
    Case("c_dress_named_beyond", "harm", "H", "فستان لولوة عندك؟", (SR,), True),
    Case("c_honey_type_beyond", "harm", "I", "عسل مانوكا عندك؟", (SR,), True),
    Case("c_honey_type_beyond_2", "harm", "I", "عندكم عسل مانوكا؟", (SR,), True),
    Case("c_perfume_named_beyond", "harm", "G", "عطر سلطان عندك؟", (SR,), True),
    Case("c_order_where", "harm", "D", "وين طلبي؟", (SR,), True),
    Case("c_order_number", "harm", "D", "رقم طلبي 4471 وش صار عليه؟", (SR,), True),
    Case("c_shipment", "harm", "E", "ابي اتابع شحنتي", (SR,), True),
    Case("c_delivery_fee", "harm", "D", "كم رسوم التوصيل؟", (SR,), True),
    Case("c_delivery_city", "harm", "E", "متى يوصل الطلب لجدة؟", (SR,), True),
    Case("c_unknown_catalogue_product", "harm", "U", "عطر سلطان عندك؟", (SR,), True),
    Case("c_unknown_catalogue_original", "original", "U", "عيال محمد عندك", NOT_SR, True),
)


def corrected(case: Case) -> Case:
    if case.name in BARE_NAME_SOLD:
        return dataclasses.replace(case, hard=False, accepted=(SR, AMB, NC))
    return case


SCENTS = ("ورد", "ياسمين", "عنبر", "مسك", "عود", "فانيلا", "لافندر", "صندل", "زعفران", "ليمون",
          "برتقال", "نعناع", "قرفة", "هيل", "جوري", "فل", "كادي", "ريحان", "توت", "خزامى")
KINDS = ("عطر", "بخور", "دهن", "معطر مفارش", "صابون", "شموع", "بودي لوشن")


def large_catalogue() -> List[str]:
    titles = [f"{kind} {scent}" for kind in KINDS for scent in SCENTS][:130]
    titles += [f"عطر {scent} 100ml" for scent in SCENTS]
    titles.insert(120, "عطر سلطان")
    return titles


CLOTHING = ("فستان", "بلوزة", "تنورة", "جاكيت", "عباية", "بنطلون", "قميص")
COLOURS = ("أسود", "أبيض", "أحمر", "أزرق", "أخضر", "وردي", "بيج", "كحلي", "رمادي", "بني",
           "فوشي", "عنابي", "زيتي", "سماوي", "ذهبي", "فضي", "بنفسجي", "خمري", "موف", "تركواز")
FOOD = ("عسل", "تمر", "قهوة", "شاي", "زيت", "بهارات", "مكسرات")
FOOD_TYPES = ("سمر", "طلح", "زهور", "جبلي", "سكري", "خلاص", "عربي", "تركي", "أخضر", "أسود",
              "زيتون", "سمسم", "مشكل", "كبسة", "لوز", "كاجو", "فستق", "بلدي", "ملكي", "فاخر")


def large_clothing() -> List[str]:
    titles = [f"{kind} {colour}" for kind in CLOTHING for colour in COLOURS]
    titles.insert(125, "فستان لولوة")
    return titles


def large_food() -> List[str]:
    titles = [f"{kind} {kind_type}" for kind in FOOD for kind_type in FOOD_TYPES]
    titles.insert(125, "عسل مانوكا")
    return titles


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def emit(prefix: str, payload) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if key:
        line = line.replace(key, "[redacted]")
    print(prefix + line, flush=True)


def _models() -> List[Tuple[str, int]]:
    out = []
    for part in os.environ.get("EVAL_INTENT_MODELS", "claude-haiku-4-5-20251001:5").split(","):
        name, _, reps = part.strip().partition(":")
        if name:
            out.append((name, max(1, min(int(reps or 1), 10))))
    return out


def main() -> int:
    try:
        budget = Budget.from_env()
    except Exception as exc:  # noqa: BLE001
        emit("INTENT_SUMMARY=", {"label": LABEL, "status": "refused", "reason": str(exc)[:200]})
        return 2
    admin = os.environ.get("NAHLA_EVAL_ADMIN_DSN", "")
    probe = _load(APP_ROOT / "scripts/operators/commerce_runtime_synthetic_probe.py", "eval_probe")
    db_name, dsn = probe.create_database(admin)
    os.environ["DATABASE_URL"] = dsn   # usage ledger writes land in the disposable database
    records: List[Dict] = []
    status = "done"
    try:
        probe.migrate(dsn, "0111")
        probe.migrate(dsn, "0113")
        run_eval = _load(APP_ROOT / "ops/commerce_runtime_eval/run_eval.py", "eval_run")
        from core.commerce_runtime import message_intent as mi
        from modules.ai.orchestrator.providers.anthropic_provider import AnthropicProvider

        stores = {"D": ("متجر تجريبي عام D", [c[0] for c in run_eval.CATALOGUE_A]),
                  "E": ("متجر تجريبي عام E", [c[0] for c in run_eval.CATALOGUE_C]),
                  "G": ("متجر تجريبي عام G", large_catalogue()),
                  "H": ("متجر تجريبي عام H", large_clothing()),
                  "I": ("متجر تجريبي عام I", large_food()),
                  # A store whose catalogue could not be read: titles unknown.
                  "U": ("متجر تجريبي عام U", None)}
        wanted = os.environ.get("EVAL_INTENT_SET", "a")
        cases = tuple(corrected(c) for c in (CASES if "a" in wanted else ())
                      + (CASES_B if "b" in wanted else ()) + (CASES_C if "c" in wanted else ()))
        provider = AnthropicProvider()
        for model, reps in _models():
            for rep in range(1, reps + 1):
                for case in cases:
                    if budget.exhausted:
                        status = "budget_stop"
                        break
                    store_name, titles = stores[case.store]
                    history = [{"role": "user" if d == "inbound" else "assistant", "text": t}
                               for d, t in case.history]
                    result = mi.assess(message=case.message, history=history,
                                       store_name=store_name, product_titles=titles,
                                       provider=provider, model=model)
                    cost = budget.add(model, result.input_tokens, result.output_tokens)
                    record = {"label": LABEL, "case": case.name, "group": case.group,
                              "store": case.store, "message": case.message, "model": model,
                              "rep": rep, "decision": result.decision, "status": result.status,
                              "reason": result.reason, "latency_ms": result.latency_ms,
                              "input_tokens": result.input_tokens,
                              "output_tokens": result.output_tokens, "cost_usd": round(cost, 6),
                              "hard": case.hard, "accepted": list(case.accepted),
                              "correct": result.decision in case.accepted}
                    emit("INTENT_TURN=", record)
                    records.append(record)
    except Exception as exc:  # noqa: BLE001
        status = f"failed:{type(exc).__name__}:{str(exc)[:160]}"
    finally:
        try:
            probe.drop_database(admin, db_name)
        except Exception:  # noqa: BLE001
            pass
    by_model: Dict[str, Dict] = {}
    for model in {r["model"] for r in records}:
        mine = [r for r in records if r["model"] == model]
        cases = collections.OrderedDict()
        for r in mine:
            c = cases.setdefault(r["case"], {"n": 0, "correct": 0, "decisions": collections.Counter(),
                                             "hard": r["hard"], "group": r["group"]})
            c["n"] += 1
            c["correct"] += bool(r["correct"])
            c["decisions"][str(r["decision"])] += 1
        lat = sorted(r["latency_ms"] for r in mine if r["status"] == "ok")
        by_model[model] = {
            "hard_gate_pass": all(c["correct"] == c["n"] for c in cases.values() if c["hard"]),
            "hard_misses": {k: dict(v["decisions"]) for k, v in cases.items()
                            if v["hard"] and v["correct"] < v["n"]},
            "soft_accuracy": {k: f'{v["correct"]}/{v["n"]}' for k, v in cases.items() if not v["hard"]},
            "failures": sum(1 for r in mine if r["status"] != "ok"),
            "latency_ms_p50": statistics.median(lat) if lat else None,
            "latency_ms_p95": lat[int(0.95 * (len(lat) - 1))] if lat else None,
            "cost_usd": round(sum(r["cost_usd"] for r in mine), 4),
        }
    emit("INTENT_SUMMARY=", {"label": LABEL, "status": status, "spent_usd": round(budget.spent_usd, 4),
                             "budget_usd": budget.limit_usd, "calls": len(records),
                             "by_model": by_model})
    return 0 if status in ("done", "budget_stop") else 3


if __name__ == "__main__":
    raise SystemExit(main())
