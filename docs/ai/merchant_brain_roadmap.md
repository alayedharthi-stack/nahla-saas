# Nahla Merchant Brain — Roadmap & Living Status Document

> **هذا الملف هو مرجع الحقيقة الوحيد لمحرك ذكاء نحلة.**
> يُحدَّث مع كل مرحلة وكل تغيير جوهري.
> كل من يعمل على المحرك يجب أن يفتح هذا الملف أولاً.

---

## 1. Vision — الهدف النهائي

نحلة **ليست** chatbot يولّد نصاً بناءً على context.

نحلة يجب أن تكون **AI Sales Agent حقيقي** يعمل بهذا التسلسل:

```
رسالة العميل
    ↓
فهم النية (Intent)
    ↓
فهم حالة المحادثة (State)
    ↓
تحميل حقائق المتجر (Commerce Facts)
    ↓
فهم ملف العميل وسلوكه (Customer Signals)
    ↓
تطبيق قواعد السياسة (Policy)
    ↓
اتخاذ قرار (Decision)
    ↓
تنفيذ خطوة فعلية (Action Execution)
    ↓
اقتراح الخطوة التالية (Suggestion)
    ↓
صياغة الرد (Response Composer)
    ↓
حفظ ما تعلّمه (Memory Update)
```

**المعيار الذهبي:** إذا أرسل العميل "أبغى فستان أسود بأقل من 200 ريال"، يجب أن يبحث الذكاء في الكتالوج الحقيقي، يختار أقرب منتج، يتأكد أنه في المخزون، يقترح إنشاء طلب، وإذا رفض العميل يعرض كوبوناً — كل ذلك بدون أن يُلفّق معلومة واحدة.

---

## 2. Architecture Layers — الطبقات الكاملة

| # | Layer | الوصف | الملف |
|---|-------|-------|-------|
| 1 | **Message Intake** | استقبال الرسالة وتوجيهها للـ Brain | `routers/whatsapp_webhook.py` |
| 2 | **Intent Engine** | تصنيف نية العميل (rules + LLM) | `brain/intent/` |
| 3 | **Slot / Entity Extraction** | استخراج المنتج والسعر والكمية وحقول checkout | `brain/intent/slot_extractor.py` |
| 4 | **Conversation State Engine** | تتبع مرحلة المحادثة وتاريخها | `brain/state/` |
| 5 | **Commerce Facts Engine** | تحميل بيانات المتجر الحقيقية | `brain/facts/` |
| 6 | **Customer Profile / Signals** | ملف العميل وعلاماته السلوكية | `brain/memory/` + DB |
| 7 | **Policy Engine** | قواعد تحكم ما يُسمح وما يُمنع | `brain/decision/policy.py` |
| 8 | **Decision Engine** | القرار النهائي: أي action ينفّذ | `brain/decision/engine.py` |
| 9 | **Action / Execution Layer** | تنفيذ القرار (بحث/طلب/كوبون/…) | `brain/execution/` |
| 10 | **Response Composer** | صياغة الرد العربي النهائي | `brain/compose/` |
| 11 | **Suggestion Engine** | اقتراح next best action | `brain/memory/updater.py` (integrated) |
| 12 | **Memory Layer** | حفظ ما تعلّمه من هذا العميل | `brain/memory/updater.py` |
| 13 | **Analytics / Outcome Tracking** | هل أُكمل الطلب؟ هل نُقد الكوبون؟ | `brain/memory/outcome_tracker.py` ✅ |

---

## 3. Current Status Table — حالة كل طبقة

| Layer | Status | Maturity | ما يعمل فعلاً | ما زال ناقصاً |
|-------|--------|----------|---------------|----------------|
| Message Intake | `production_ready` | high | توجيه per-tenant، feature flag، COD interception | — |
| Intent Engine (rules) | `production_ready` | high | 8+ intents: greeting/ask_product/ask_price/start_order/pay_now/shipping/hesitation/handoff/track + pick_list_item + who_are_you + general | — |
| Slot Extraction (LLM) | `phase2_done` | high | Haiku يستخرج product_query/price_range/quantity/city/name/address fields، compact JSON، JSON repair، regex-priority merge، max_tokens=350 | اختبار على محادثات إنتاجية حقيقية |
| Intent Classifier (hybrid) | `production_ready` | medium | rules أولاً → LLM للـ slots عند الحاجة | — |
| State Engine | `phase2_done` | high | 7 stages، last_search_candidates، order_prep، general_streak للـ auto-escalate | لا intent history per turn |
| Commerce Facts | `production_ready` | high | has_products/in_stock/orderable/top_products/coupon_eligibility/platform/working_hours/shipping/payment | — |
| Merchant Context (Store Knowledge) | `production_ready` | high | build_merchant_context(): منتجات + سياسات + دفع + شحن + FAQ + pages + product descriptions/variants + brain_profile | — |
| Merchant Knowledge UI | `production_ready` | high | تبويب "ذكاء المتجر": quality ring، منتجات، مستبعدات، سياسات، دفع، شحن، FAQ، pages، brain_profile | — |
| LLM Context (merchant_context) | `production_ready` | high | slim_merchant_ctx يدخل في كل LLM call + FAQ + context_verbosity A/B | — |
| Customer Signals | `phase2_done` | medium | ProductAffinity bump بعد search/order، PriceSensitivity | ProductAffinity لا تُقرأ في ترتيب النتائج |
| Policy Engine | `production_ready` | high | coupon_cap_hours (configurable)، working_hours، price_range، max_order_value، auto-escalate (general_streak)، **block list** | — |
| Decision Engine | `production_ready` | high | 10+ قواعد حتمية، rejected alternatives، numeric pick، orderable guard، **ACTION_NARROW لتصفية السعر** | — |
| Execution — Search | `production_ready` | high | CatalogContextBuilder + Arabic FTS + fallback to top products + suggest_narrow | — |
| Execution — Orders | `production_ready` | high | stateful checkout، DraftOrder، **TrackOrderHandler بمطابقة رقم الطلب** + Arabic status labels | — |
| Execution — FAQ | `production_ready` | high | ACTION_FAQ_REPLY لـ 4 topics + order_resume_hint | — |
| Execution — Coupon | `production_ready` | high | **OfferDecisionService ينتقي أذكى كوبون** بناءً على product_price/cart_total + block عربي مع discount% | — |
| Response Composer | `production_ready` | high | قوالب عربية، **3 variations لكل قالب عالي التكرار**، **dedup guard (أول 70 حرف)**، variant bump | — |
| Suggestion Engine | `production_ready` | medium | suggested_next_step + follow-up question بعد كل action + order_resume_hint | — |
| Memory — Trace | `production_ready` | high | ConversationTrace + BrainTurnTrace JSON log | — |
| Memory — Affinity | `phase2_done` | medium | ProductAffinity تُكتب بعد search/order | لا تُقرأ في ترتيب النتائج |
| Memory — Summary | `phase2_done` | medium | ConversationHistorySummary كل 5 turns عبر Haiku | — |
| Analytics / Outcomes | `production_ready` | high | **outcome_tracker.py**: order_confirmed + coupon_redeemed flags في ConversationTrace، Salla webhook ربط | — |
| Merchant Knowledge Management | `production_ready` | high | تعديل FAQ وسياسات مباشرة من /intelligence + PUT endpoint | — |
| Brain Analytics Dashboard | `production_ready` | medium | /intelligence/response-quality: conversion funnels، latency، top intents/actions، daily trends | — |
| Policy Config UI | `production_ready` | high | coupon_cap_hours، auto_escalate_after_n، max_order_value، context_verbosity من Dashboard | — |
| Block List | `production_ready` | high | PolicyGate._block_list() + UI في Intelligence.tsx + blocked_customers في brain_profile | — |
| Salla Pages Sync | `production_ready` | medium | fetch_store_pages() من Salla API → HTML cleanup → StoreKnowledgeSnapshot → merchant_context.pages | — |
| Product Descriptions/Variants | `production_ready` | high | HTML cleanup + truncation للوصف، _format_variants_for_llm() يُلخص المتاح | — |

### مفتاح الحالات

| Status | المعنى |
|--------|--------|
| `not_started` | لم يُبدأ |
| `planned` | مُخطَّط ولم يُنفَّذ |
| `basic` | موجود لكن minimal implementation |
| `phase1_done` | اكتمل في Phase 1 |
| `phase2_done` | اكتمل في Phase 2 |
| `needs_upgrade` | يعمل لكن يحتاج تحسين جوهري |
| `production_ready` | جاهز للإنتاج بثقة |

---

## 4. Current Phase — المرحلة الحالية

### Phase 1 — Foundation ✅ مكتملة
> تاريخ الإكمال: 2026-04-18

**ما دخل ضمنها:**
- بنية المجلدات كاملة (`brain/intent|state|facts|decision|execution|compose|memory`)
- types.py + protocols.py (العقد بين الطبقات)
- IntentClassifier (rules + Haiku hybrid)
- StateStore (7 stages، Customer→Conversation lookup)
- DefaultDecisionEngine (8 قواعد حتمية)
- ActionExecutor مع كل handlers الأساسية
- DefaultComposer مع القوالب العربية
- DefaultMemoryUpdater (ConversationTrace)
- Feature flag: `MERCHANT_BRAIN_ENABLED` + `MERCHANT_BRAIN_TENANT_IDS`
- Webhook routing مع fallback للـ legacy pipeline
- 21 اختباراً

### Phase 2 — Policy + Rich Facts + Clarification ✅ مكتملة
> تاريخ الإكمال: 2026-04-18

**ما دخل ضمنها:**
- BrainTurnTrace JSON log في كل turn (searchable في Railway)
- Per-tenant activation (`MERCHANT_BRAIN_TENANT_IDS=1`)
- StateStore مُصلَح (يبحث عبر Customer.normalized_phone)
- CommerceFacts غنية: `in_stock_count`, `orderable`, `top_products`, `coupon_eligibility`, `integration_platform`, `within_working_hours`
- RealPolicyGate: 4 قواعد (coupon cap, working_hours للـ handoff فقط, price_range, auto-escalate)
- ClarificationFlow: `ACTION_CLARIFY` + `ACTION_NARROW`
- MemoryUpdater Phase 2: ProductAffinity + PriceSensitivity + ConversationHistorySummary

### Phase 2.5 — Merchant Context UI & Observability ✅ مكتملة
> تاريخ الإكمال: 2026-04-28

**ما دخل ضمنها:**
- `build_merchant_context()` محسّن: أضيف `pages` + logging تشخيصي مفصّل
- FAQ الآن مدرج في `slim_merchant_ctx` الذي يدخل في كل LLM call (مُحدود بـ 5 عناصر)
- `GET /intelligence/merchant-brain/knowledge` endpoint جديد
- تبويب "ذكاء المتجر" في `/intelligence` مع quality ring، كل أقسام المعرفة
- Types كاملة في `automations.ts`

### Phase 2.6 — Salla Pages API Integration ✅ مكتملة
> تاريخ الإكمال: 2026-04-28

**ما دخل ضمنها:**
- `fetch_store_pages()` يستدعي Salla API لجلب الصفحات الثابتة
- HTML cleanup + truncation آمن
- تخزين في `StoreKnowledgeSnapshot`
- pages تملأ `merchant_context` فعلياً بدل القائمة الفارغة
- عرض الصفحات في واجهة "ذكاء المتجر" بدل placeholder

### Phase 3.5 — Structured Checkout + Address Resolution ✅ مكتملة
> تاريخ الإكمال: 2026-04-18/28

**ما دخل ضمنها:**
- `OrderPreparationState` داخل `MerchantConversationState`
- جمع checkout fields خطوة بخطوة: الاسم، المدينة، العنوان
- قبول `short_address_code` أو `Google Maps URL` كمدخل عنوان
- resolver فعلي يدعم SPL National Address API
- product_options: يجمع خيارات المنتج قبل إنشاء الطلب
- fallback منظم مع salla_failure_count وescalation تلقائي

### Phase 7 — Analytics / Outcome Tracking ✅ مكتملة
> تاريخ الإكمال: 2026-04-28

**ما دخل ضمنها:**
- `outcome_tracker.py`: `mark_order_confirmed()` + `mark_coupon_redeemed()`
- `order_confirmed` + `coupon_redeemed` flags في `ConversationTrace`
- Alembic migration للحقول الجديدة
- Salla webhook ربط (order.updated) → تحديث ConversationTrace
- اختبارات شاملة في `tests/test_outcome_tracker.py`

### Phase 9 — Knowledge Management UI ✅ مكتملة
> تاريخ الإكمال: 2026-04-28

**ما دخل ضمنها:**
- `PUT /intelligence/merchant-brain/knowledge` endpoint
- `flag_modified` لحفظ JSONB fields
- Intelligence.tsx: نماذج تعديل FAQ وسياسات تفاعلية

### Phase 10 — A/B Testing + Brain Analytics ✅ مكتملة
> تاريخ الإكمال: 2026-04-28

**ما دخل ضمنها:**
- `GET /intelligence/response-quality`: فانل تحويل، latency، top intents/actions، daily trends
- `BrainAnalyticsPanel` في Intelligence.tsx
- `context_verbosity` A/B setting (full/compact/minimal)

### Phase 11 — Merchant-Configurable Policy Rules ✅ مكتملة
> تاريخ الإكمال: 2026-05-06

**ما دخل ضمنها:**
- `coupon_cap_hours`, `auto_escalate_after_n`, `max_order_value`, `context_verbosity` في `DEFAULT_AI` وAISettings
- `brain_profile` يُمرّر هذه الإعدادات لـ `RealPolicyGate`
- PolicyGate: `_max_order_value()` + `_block_list()` + `_auto_escalate()` بـ general_streak حقيقي
- UI في Intelligence.tsx لكل الإعدادات السابقة
- **Block List** UI: إضافة/حذف أرقام محظورة من Dashboard

### Phase P0 Fix — SlotExtractor Complex Inputs ✅ مكتملة
> تاريخ الإكمال: 2026-05-06

**ما دخل ضمنها:**
- `max_tokens` رُفع من 200 إلى 350
- System prompt: compact JSON، لا حقول فارغة، multi-field support
- `_repair_json()`: إنقاذ JSON المقطوع جزئياً
- history context: آخر 2 turns فقط، حد 80 حرف لكل دور
- Regex-priority merge: `short_address_code`/`google_maps_url`/coordinates/email تأخذ أولوية على LLM
- 19 اختباراً في `tests/test_slot_extractor.py`

### Phase P1/P2 Improvements ✅ مكتملة
> تاريخ الإكمال: 2026-05-06

**ما دخل ضمنها:**
- **TrackOrderHandler**: مطابقة رقم طلب محدد، fuzzy match على reference_id، status_label_ar بالعربية، item_titles
- **suggest_coupon**: cart_total من current_product_focus → OfferDecisionService ينتقي الأذكى، coupon block عربي مع %خصم وصلاحية
- **general_streak**: حقل حقيقي في MerchantConversationState يزداد عند INTENT_GENERAL ويُعاد تعيينه عند أي intent آخر
- **ACTION_NARROW من DecisionEngine**: يُطلق مباشرة عند تصفية سعر ("أرخص", "أقل من X ريال") على `last_search_candidates` بدل full catalog search

---

## 5. Daily Progress Log

### 2026-05-06

**Phases P0/P1/P2/P3 — تحسينات شاملة:**

- `backend/modules/ai/brain/intent/slot_extractor.py`:
  - max_tokens=350، compact prompt، _repair_json()، history trim، regex-priority merge

- `backend/modules/ai/brain/decision/policy.py`:
  - _block_list() جديدة، _auto_escalate() بـ general_streak، _max_order_value()، _coupon_cap() configurable

- `backend/modules/ai/brain/state/store.py`:
  - general_streak counter في transition()

- `backend/modules/ai/brain/types.py`:
  - general_streak: int = 0 في MerchantConversationState

- `backend/modules/ai/brain/execution/orders.py`:
  - TrackOrderHandler: order_number fuzzy match، _ORDER_STATUS_AR، item_titles

- `backend/modules/ai/commerce/runtime.py`:
  - _tool_track_order: direct lookup + fuzzy match
  - _tool_apply_coupon: cart_total من payload

- `backend/modules/ai/brain/execution/executor.py`:
  - _SuggestCouponHandler: cart_total، richer coupon block

- `backend/modules/ai/brain/compose/templates.py`:
  - order_status: status_label_ar + item_titles

- `backend/modules/ai/brain/compose/responder.py`:
  - ACTION_TRACK_ORDER: passes status_label_ar + item_titles

- `backend/modules/ai/brain/decision/engine.py`:
  - Rule 3.9: ACTION_NARROW لتصفية السعر من last_search_candidates

- `backend/core/store_knowledge.py`:
  - blocked_customers في brain_profile، policy fields configurable

- `backend/core/tenant.py`:
  - DEFAULT_AI: coupon_cap_hours، auto_escalate_after_n، max_order_value، context_verbosity

- `dashboard/src/api/settings.ts`:
  - AISettings interface: policy fields + blocked_customers

- `dashboard/src/pages/Intelligence.tsx`:
  - Smart Automation Rules section، Blocked Customers UI

- `tests/test_slot_extractor.py`: 19 اختباراً جديداً

### 2026-04-28

**Phase 2.5 — Merchant Context UI & Observability:**

- `backend/core/store_knowledge.py`: pages + logging
- `backend/routers/intelligence.py`: GET /intelligence/merchant-brain/knowledge
- `dashboard/src/api/automations.ts`: 8 types جديدة
- `dashboard/src/pages/Intelligence.tsx`: MerchantKnowledgePanel كامل

### 2026-04-18

**Phase 1 + Phase 2 — تأسيس المحرك الكامل:**
- بُنيت بنية المجلدات الكاملة لـ `backend/modules/ai/brain/`
- types.py + protocols.py + intent + state + facts + decision + execution + compose + memory
- 21 اختباراً تمر كلها

---

## 6. Known Problems / Open Gaps

### P0 — حرجة (تؤثر على الإنتاج)

- [x] **LLM fallback يفقد سياق Brain** — ✅ حُلّ: `_llm_compose` يستخدم `BrainReplyState` مع `merchant_context` كاملاً
- [x] **SlotExtractor يفشل مع multi-field inputs** — ✅ حُلّ في 2026-05-06: max_tokens=350، compact output، _repair_json، regex-priority

### P1 — مهمة (تؤثر على الجودة)

- [x] **TrackOrderHandler يُعيد أحدث طلب فقط** — ✅ حُلّ: مطابقة order_number محدد + fuzzy match + Arabic status labels
- [x] **suggest_coupon يختار أول كوبون** — ✅ حُلّ: OfferDecisionService ينتقي الأذكى بناءً على cart_total + context
- [x] **Google Maps → short address محدود** — مُحسَّن جزئياً (resolver يدعم links + إحداثيات + regex)
- [ ] **ProductAffinity لا تُقرأ في ترتيب نتائج البحث** — تُكتب بعد كل search/order لكن لا تُستخدم في الـ ranking بعد

### P2 — تحسينات مرغوبة

- [x] **لا Suggestion Engine** — ✅ حُلّ: suggested_next_step + follow-up question بعد كل action
- [x] **لا dedup guard في Composer** — ✅ حُلّ: `_is_duplicate()` (أول 70 حرف) + variant bump
- [x] **قوالب الـ Composer لا تتغير** — ✅ حُلّ: 3 variations لـ greeting/product_results/no_products/handoff/narrow_choices/generic_fallback
- [x] **auto-escalate مبسّط** — ✅ حُلّ: general_streak حقيقي في MerchantConversationState
- [x] **لا Analytics / Outcome Tracking** — ✅ حُلّ: outcome_tracker.py + ConversationTrace flags
- [x] **لا block list** — ✅ حُلّ: PolicyGate._block_list() + UI في Dashboard

### P3 — تقنية (لا تؤثر على التشغيل)

- [x] **`ACTION_NARROW` لا تُطلق من DecisionEngine مباشرة** — ✅ حُلّ في 2026-05-06: Rule 3.9 تُطلق ACTION_NARROW عند تصفية السعر على last_search_candidates
- [ ] **StateStore لا يستخدم Redis كـ fallback** — إذا لم تُوجد Conversation row، لا تخزين مؤقت. غير ضروري حالياً.

---

## 7. Remaining Priorities — ما تبقّى

مرتّبة بحسب الأثر:

1. **تفعيل SPL address resolution في البيئة الإنتاجية** — حتى يصبح `short_address_code` و`Google Maps` auto-fill كاملاً.
2. **قراءة ProductAffinity في DecisionEngine** — ترتيب نتائج البحث بحسب affinity score بدل top_score فقط.
3. **StateStore Redis fallback** — تخزين مؤقت عند غياب Conversation row (P3، اختياري).

---

## 8. Definition of Done — متى يصبح Brain قوياً فعلاً؟

نعتبر Merchant Brain "production-ready" عندما تتحقق كل النقاط التالية:

### سلوك المحادثة
- [x] لا يُكرّر التحية في نفس الجلسة (dedup guard)
- [x] يتذكّر المنتج الذي تحدّث عنه العميل في الرسالة السابقة (state.current_product_focus)
- [x] يُعيد طرح الخيارات بصياغة مختلفة (3 template variations + variant bump)
- [x] لا يقترح كوبوناً في أول رسالة أو بدون منتج محدد (PolicyGate)
- [x] يسأل سؤالاً واحداً واضحاً عند غياب المعلومات (ACTION_CLARIFY)

### الأفعال الحقيقية
- [x] يبحث في الكتالوج الحقيقي بالعربية (CatalogContextBuilder + FTS)
- [x] يُنشئ draft order في Salla عند الطلب
- [x] يجمع الاسم والمدينة والرمز الوطني المختصر أو رابط الخرائط (OrderPreparationState)
- [ ] يملأ العنوان تلقائياً من short address / geocode عند توفر SPL API في الإنتاج
- [x] يُرسل payment link حقيقي
- [x] يُعيد رابط الدفع عند طلبه ثانيةً
- [x] يُتابع حالة طلب حقيقي بمطابقة رقم الطلب

### الذكاء والقرار
- [x] يكتشف نية شراء بدقة عالية (hybrid rules + LLM)
- [x] يُطبّق PolicyGate دون أن يُخطئ في السيناريوهات المعروفة
- [x] يُصعّد للإنسان عند الحاجة ولا يتجمّد في دوامة (general_streak auto-escalate)
- [x] يُحجب العميل المزعج (block list + immediate handoff)
- [x] يُصفّي قوائم المنتجات بناءً على تصفية السعر (ACTION_NARROW rule 3.9)

### الرقابة والشفافية
- [x] يُنتج BrainTurnTrace JSON لكل turn
- [x] ConversationTrace مكتوب في DB لكل رسالة
- [x] outcome tracking: order_confirmed + coupon_redeemed
- [x] لا hallucination: كل منتج أو سعر مذكور موجود في الكتالوج الحقيقي

### الثبات
- [x] 40+ اختبار يمر (21 brain + 19 slot_extractor + outcome_tracker + address_resolution)
- [x] الـ Legacy fallback يعمل عند فشل Brain
- [x] لا exception غير معالجة تُسكت الرد

---

## 9. File Map — خريطة الملفات

```
backend/modules/ai/brain/
├── __init__.py                  ← exports عامة
├── types.py                     ← عقود البيانات بين الطبقات (+ general_streak)
├── protocols.py                 ← Protocol interfaces لكل طبقة
├── pipeline.py                  ← MerchantBrain + BrainTurnTrace + get_brain()
│
├── intent/
│   ├── rules.py                 ← 8+ intents بـ regex عربي (0ms latency)
│   ├── slot_extractor.py        ← Haiku call، max_tokens=350، _repair_json، regex-priority
│   └── classifier.py           ← hybrid: rules-first → LLM fallback
│
├── state/
│   ├── stages.py                ← 7 stage constants
│   └── store.py                 ← load/save + general_streak transition
│
├── facts/
│   └── commerce_facts.py        ← RichFactsLoader (Phase 2)
│
├── decision/
│   ├── actions.py               ← 11 ACTION_* constants
│   ├── engine.py                ← DefaultDecisionEngine (12+ قاعدة) + Rule 3.9 ACTION_NARROW
│   └── policy.py                ← RealPolicyGate: block_list + working_hours + coupon_cap + price_range + max_order_value + auto_escalate
│
├── execution/
│   ├── search.py                ← ProductSearchHandler → CatalogContextBuilder + suggest_narrow
│   ├── orders.py                ← DraftOrderHandler + TrackOrderHandler (order_number match + Arabic status)
│   └── executor.py              ← dispatcher + _SuggestCouponHandler (OfferDecisionService)
│
├── compose/
│   ├── templates.py             ← Arabic reply templates (3 variations لـ 6 قوالب عالية التكرار)
│   └── responder.py             ← DefaultComposer + dedup guard + LLM fallback
│
└── memory/
    ├── updater.py               ← ConversationTrace + ProductAffinity + Summary
    └── outcome_tracker.py       ← order_confirmed + coupon_redeemed tracking

backend/core/
├── store_knowledge.py           ← build_merchant_context() + pages + brain_profile + blocked_customers
├── tenant.py                    ← DEFAULT_AI: coupon_cap_hours, auto_escalate_after_n, max_order_value, context_verbosity
└── config.py                    ← MERCHANT_BRAIN_ENABLED, MERCHANT_BRAIN_TENANT_IDS

backend/routers/
└── intelligence.py              ← GET/PUT /intelligence/merchant-brain/knowledge + /response-quality

backend/store_integration/
└── salla_pages.py               ← fetch_store_pages() + HTML cleanup

dashboard/src/
├── api/
│   ├── automations.ts           ← MerchantKnowledge types + updateMerchantKnowledge + blocked_customers
│   └── settings.ts              ← AISettings: policy fields
└── pages/
    └── Intelligence.tsx         ← MerchantKnowledgePanel + AISettingsPanel + BrainAnalyticsPanel + Blocked Customers UI

tests/
├── test_merchant_brain.py       ← 21 اختباراً
├── test_slot_extractor.py       ← 19 اختباراً (P0 fix coverage)
├── test_outcome_tracker.py      ← outcome tracking
└── test_address_resolution.py   ← address parsing + SPL
```

---

## 10. How to Use This Document

### كل يوم قبل البدء
1. افتح هذا الملف
2. اقرأ **Known Problems** — ما لم يُحلّ بعد
3. اقرأ **Remaining Priorities** — ما هو الأهم اليوم
4. ابدأ من أعلى القائمة

### كل يوم بعد الانتهاء
1. أضف entry جديدة في **Daily Progress Log** بتاريخ اليوم
2. حدّث **Current Status Table** إذا تغيّرت حالة أي طبقة
3. انقل الـ gaps التي حُلّت من **Known Problems** (علّم بـ [x])
4. حدّث **Current Phase** إذا انتهت مرحلة أو بدأت مرحلة جديدة

### قبل بناء feature جديدة
1. تأكد أنها مُدرجة في **Remaining Priorities**
2. تأكد أنها لا تتعارض مع **Known Problems** حالية
3. حدّث **Architecture Layers** إذا أضفت طبقة جديدة

---

*آخر تحديث: 2026-05-06 — اكتمال Phase P0/P1/P2/P3: SlotExtractor، TrackOrder، suggest_coupon، general_streak، block list، ACTION_NARROW من DecisionEngine*
