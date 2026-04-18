# Merchant Brain — Technical Changelog

> سجل تقني تفصيلي بكل تغيير في الكود.
> للصورة الكاملة والأولويات → [`merchant_brain_roadmap.md`](merchant_brain_roadmap.md)

---

## 2026-04-18

### Phase 1 — Foundation

**ملفات جديدة:**
- `backend/modules/ai/brain/types.py` — Intent, MerchantConversationState, CommerceFacts, BrainContext, Decision, ActionResult
- `backend/modules/ai/brain/protocols.py` — Protocol interfaces: IntentClassifier, StateStore, FactsLoader, DecisionMaker, PolicyGate, ActionExecutor, Composer, MemoryUpdater
- `backend/modules/ai/brain/__init__.py`
- `backend/modules/ai/brain/intent/rules.py` — 8 intents بـ regex + threshold 0.82–0.95. أنماط لـ: greeting, ask_product, ask_price, start_order, pay_now, ask_shipping, hesitation, track_order, talk_to_human
- `backend/modules/ai/brain/intent/slot_extractor.py` — async Haiku call، يُعيد product_query/price_range/quantity/order_id/intent_hint
- `backend/modules/ai/brain/intent/classifier.py` — DefaultIntentClassifier. RULES_ONLY_THRESHOLD = 0.85
- `backend/modules/ai/brain/state/stages.py` — 7 constants: discovery, exploring, deciding, ordering, checkout, complete, support
- `backend/modules/ai/brain/state/store.py` — DefaultStateStore. يبحث عبر Customer.normalized_phone → Conversation.customer_id (مُصلَح بعد اكتشاف bug في extra_metadata lookup)
- `backend/modules/ai/brain/facts/commerce_facts.py` — DefaultFactsLoader. يحمّل Phase 2 rich facts فوراً
- `backend/modules/ai/brain/decision/actions.py` — 10 ACTION_* constants (Phase 1 + Phase 2)
- `backend/modules/ai/brain/decision/engine.py` — DefaultDecisionEngine. 8+ قواعد بالترتيب: handoff → payment → track → greet → order → search → hesitation → LLM
- `backend/modules/ai/brain/decision/policy.py` — PassThroughPolicyGate + RealPolicyGate (4 قواعد)
- `backend/modules/ai/brain/execution/search.py` — ProductSearchHandler عبر CatalogContextBuilder
- `backend/modules/ai/brain/execution/orders.py` — DraftOrderHandler + TrackOrderHandler
- `backend/modules/ai/brain/execution/executor.py` — DefaultActionExecutor: dispatcher لـ 10 handlers
- `backend/modules/ai/brain/compose/templates.py` — Arabic templates: greeting, product_results, no_products, draft_order_created, order_intent_captured, payment_link, order_status, no_orders, coupon_offer, clarify, narrow_choices, handoff, generic_fallback
- `backend/modules/ai/brain/compose/responder.py` — DefaultComposer. عند ACTION_LLM_REPLY يستدعي generate_orchestrate_response القديم
- `backend/modules/ai/brain/memory/updater.py` — DefaultMemoryUpdater Phase 2: ConversationTrace + ProductAffinity + PriceSensitivity + ConversationHistorySummary (كل 5 turns عبر Haiku)
- `backend/modules/ai/brain/pipeline.py` — MerchantBrain.process() + BrainTurnTrace JSON log + build_default_brain() + get_brain() singleton
- `tests/test_merchant_brain.py` — 21 unit test: TestIntentRules, TestDecisionEngine, TestComposerTemplates, TestExecutor, TestBrainPipeline

**ملفات مُعدَّلة:**
- `backend/core/config.py` — أضيف: MERCHANT_BRAIN_ENABLED (global), MERCHANT_BRAIN_TENANT_IDS (per-tenant set)
- `backend/routers/whatsapp_webhook.py` — أضيف import MERCHANT_BRAIN_TENANT_IDS، غُيّر check إلى `_brain_active = MERCHANT_BRAIN_ENABLED or tenant_id in MERCHANT_BRAIN_TENANT_IDS`، BrainTurnTrace log مُحسَّن

### Phase 2 — Policy + Rich Facts

**تغييرات على ملفات موجودة:**
- `pipeline.py` — أضيف `import json`، `reason_before_policy` لـ policy_modified flag، BrainTurnTrace JSON كامل في نهاية كل turn
- `types.py` — CommerceFacts: أضيف in_stock_count, orderable, coupon_eligibility, top_products, integration_platform, within_working_hours
- `facts/commerce_facts.py` — أعيد كتابته كاملاً بـ Phase 2 rich facts + `_check_working_hours()`
- `decision/actions.py` — أضيف ACTION_CLARIFY + ACTION_NARROW
- `decision/engine.py` — أضيف orderable check في start_order، ClarificationFlow عند غياب product_query
- `decision/policy.py` — أعيد كتابته: PassThrough + RealPolicyGate مع 4 قواعد
- `decision/__init__.py` — export RealPolicyGate + ACTION_CLARIFY + ACTION_NARROW
- `execution/search.py` — أضيف suggest_narrow flag + after_search key في result.data
- `execution/executor.py` — أضيف _ClarifyHandler + _NarrowHandler، LLMReplyHandler يعيد policy_reason
- `compose/templates.py` — أضيف clarify() + narrow_choices()
- `compose/responder.py` — أضيف ACTION_CLARIFY + ACTION_NARROW cases، suggest_narrow في search response
- `memory/updater.py` — أعيد كتابته كاملاً: _write_trace + _bump_affinity + _nudge_price_sensitivity + _summarise
- `state/store.py` — أعيد كتابته: Customer.normalized_phone → Conversation، transition يعرف ACTION_CLARIFY + ACTION_NARROW
- `tests/test_merchant_brain.py` — تحديث `_make_facts()` بإضافة in_stock_count + orderable + integration_platform

### Bug Fix

- `decision/policy.py` — Working Hours gate: تراجع عن block للـ propose_draft_order/payment_link. الـ gate يُوقف الـ handoff فقط. السبب: المتجر الإلكتروني لا يحتاج حضور بشري لإنشاء طلب.

---

*القادم في Changelog: Phase 3 — Smart Composer + Signals Usage*
