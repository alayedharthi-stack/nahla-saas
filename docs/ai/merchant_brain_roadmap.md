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
| 11 | **Suggestion Engine** | اقتراح next best action | *لم يُبنَ بعد* |
| 12 | **Memory Layer** | حفظ ما تعلّمه من هذا العميل | `brain/memory/updater.py` |
| 13 | **Analytics / Outcome Tracking** | هل أُكمل الطلب؟ هل نُقد الكوبون؟ | *لم يُبنَ بعد* |

---

## 3. Current Status Table — حالة كل طبقة

| Layer | Status | Maturity | ما يعمل فعلاً | ما زال ناقصاً |
|-------|--------|----------|---------------|----------------|
| Message Intake | `phase1_done` | high | توجيه per-tenant، feature flag، COD interception | — |
| Intent Engine (rules) | `phase1_done` | high | 8 intents: greeting/ask_product/ask_price/start_order/pay_now/shipping/hesitation/handoff/track | تغطية أشمل للجمل العربية المركّبة |
| Slot Extraction (LLM) | `basic` | medium | يستدعي Haiku عند confidence < 0.85، ويستخرج product_query/price_range/quantity + city/name/short_address_code/google_maps_url | ما زال يحتاج قياساً على رسائل عربية حقيقية وتحسيناً لبعض العبارات المركّبة |
| Intent Classifier (hybrid) | `phase1_done` | medium | rules أولاً → LLM للـ slots عند الحاجة | لا يُعيد تصحيح النتيجة عند تعارض rules/LLM |
| State Engine | `phase2_done` | medium | 7 stages، يُحمّل/يحفظ عبر Customer→Conversation، transitions منطقية، ويخزّن `order_prep` المنظم | لا يُخزّن intent history per turn، streak detection مبسّط |
| Commerce Facts | `phase2_done` | medium | has_products/in_stock_count/orderable/top_products[5]/coupon_eligibility/platform/working_hours | لا يُحمّل منتجات مطابقة للـ query الحالية مسبقاً |
| Customer Signals | `basic` | low | ProductAffinity bump بعد search/order، PriceSensitivity nudge عند hesitation | لا يُقرأ في القرار بعد، ملف العميل لا يُحقن في Composer |
| Policy Engine | `phase2_done` | medium | coupon cap 24h، working_hours للـ handoff فقط، price_range gate، auto-escalate | لا block list للعملاء، لا frequency cap للطلبات، لا قواعد merchant-configurable |
| Decision Engine | `phase2_done` | medium | 8+ قواعد حتمية، clarify عند غياب product_query، orderable check | لا confidence scoring مقارن، لا multi-signal weighting |
| Execution — Search | `phase1_done` | high | CatalogContextBuilder + Arabic FTS + fallback to top products، narrow flag عند > 3 نتائج | — |
| Execution — Orders | `phase2_done` | medium | تجهيز طلب stateful: يجمع الاسم/المدينة/الرمز المختصر أو رابط الخرائط، ثم ينشئ draft order حقيقياً | ما يزال Google Maps → short code بحاجة تحسين أعمق عند غياب SPL API |
| Execution — Other | `phase1_done` | medium | greet/handoff/clarify/narrow/suggest_coupon/payment_link كلها مبنية | suggest_coupon يختار أول كوبون فقط |
| Response Composer | `phase1_done` | medium | قوالب عربية لكل action، narrow_choices، LLM fallback للـ general | لا dedup guard، لا variations للقوالب المتكررة |
| Suggestion Engine | `phase2_done` | medium | ينتج `suggested_next_step` وfollow-up question بعد كل action | يحتاج نضجاً أكبر في checkout/handoff وبعض المسارات الغامضة |
| Memory — Trace | `phase2_done` | high | ConversationTrace بعد كل turn مع كل تفاصيل القرار | — |
| Memory — Affinity | `phase2_done` | medium | ProductAffinity rows تُكتب بعد search/order | لا تُقرأ في القرار أو الـ Composer بعد |
| Memory — Summary | `phase2_done` | medium | ConversationHistorySummary كل 5 turns عبر Haiku | لا تُحقن في LLM fallback context بعد |
| Analytics / Outcomes | `not_started` | — | — | لم يُبنَ. لا tracking للطلبات المكتملة أو الكوبونات المُستردة. |

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
- StateStore مُصلَح (يبحث عبر Customer.normalized_phone بدل extra_metadata)
- CommerceFacts غنية: `in_stock_count`, `orderable`, `top_products`, `coupon_eligibility`, `integration_platform`, `within_working_hours`
- RealPolicyGate: 4 قواعد (coupon cap, working_hours للـ handoff فقط, price_range, auto-escalate)
- ClarificationFlow: `ACTION_CLARIFY` + `ACTION_NARROW`
- MemoryUpdater Phase 2: ProductAffinity + PriceSensitivity + ConversationHistorySummary

### Phase 3 — Smart Composer + Signals Usage (قادمة)

**ما سيدخل ضمنها:**
- SmartComposer dedup guard (لا تكرار نفس قائمة المنتجات)
- Template variations (3 نسخ من كل قالب، تتناوب)
- حقن ConversationHistorySummary في LLM fallback context
- قراءة ProductAffinity في DecisionEngine لتحسين الترتيب
- تحسين TrackOrderHandler ليطابق order_id محدد
- بناء Suggestion Engine أولي (next_best_action بعد كل turn)

### Phase 3.5 — Structured Checkout + Address Resolution 🚧

**ما دخل ضمنها الآن:**
- `OrderPreparationState` داخل `MerchantConversationState`
- جمع checkout fields خطوة بخطوة: الاسم الأول، اسم العائلة، المدينة
- قبول `short_address_code` أو `Google Maps URL` كمدخل عنوان
- resolver فعلي يدعم SPL National Address API عند توفر `SPL_NATIONAL_ADDRESS_API_KEY`
- fallback منظم: إذا لم تتوفر كل البيانات، يسأل الذكاء سؤالاً واحداً واضحاً بدلاً من إنشاء طلب ناقص

**ما بقي منها:**
- تحسين Google Maps → short address/code extraction
- رفع مفتاح SPL في البيئة الإنتاجية/التجريبية لتفعيل auto-fill الكامل
- دعم multi-item basket وحقول عنوان أكثر ثراءً لاحقاً

### Phase 4 — Memory + Learning (مستقبلية)

**ما سيدخل ضمنها:**
- Analytics / Outcome Tracking (هل أُكمل الطلب؟ هل نُقد الكوبون؟)
- PriceSensitivity يُقرأ في DecisionEngine لتخصيص عروض الكوبون
- A/B testing على القوالب والـ decisions
- Merchant-configurable policy rules من الـ Dashboard
- Multi-product basket (اقتراح أكثر من منتج)

---

## 5. Daily Progress Log

### 2026-04-18

**Phase 1 — Foundation:**
- بُنيت بنية المجلدات الكاملة لـ `backend/modules/ai/brain/`
- `types.py`: Intent, MerchantConversationState, CommerceFacts, BrainContext, Decision, ActionResult
- `protocols.py`: Protocol interfaces لكل طبقة (IntentClassifier, StateStore, FactsLoader, …)
- `intent/rules.py`: 8 intents بـ regex عربي، threshold 0.82–0.95
- `intent/slot_extractor.py`: Haiku call للـ slots عند confidence < 0.85
- `intent/classifier.py`: hybrid — rules أولاً، LLM للـ slots عند الحاجة
- `state/stages.py`: 7 stages (discovery → exploring → deciding → ordering → checkout → complete → support)
- `state/store.py`: load/save عبر Customer.normalized_phone → Conversation.customer_id
- `facts/commerce_facts.py`: Phase 2 rich facts فوراً
- `decision/actions.py`: 10 constants (Phase 1 + Phase 2: clarify, narrow)
- `decision/engine.py`: 8+ قواعد حتمية بدون LLM
- `decision/policy.py`: PassThroughPolicyGate + RealPolicyGate
- `execution/search.py`: CatalogContextBuilder + Arabic FTS + narrow flag
- `execution/orders.py`: create_draft_order + TrackOrderHandler
- `execution/executor.py`: dispatcher لكل handlers
- `compose/templates.py`: قوالب عربية لكل action + clarify + narrow_choices
- `compose/responder.py`: DefaultComposer مع LLM fallback
- `memory/updater.py`: ConversationTrace + ProductAffinity + PriceSensitivity + ConversationHistorySummary
- `pipeline.py`: MerchantBrain + BrainTurnTrace JSON logging + get_brain() singleton
- `tests/test_merchant_brain.py`: 21 اختباراً تمر كلها

**Phase 2 — همّش في نفس اليوم:**
- BrainTurnTrace JSON log شامل لكل turn
- `MERCHANT_BRAIN_TENANT_IDS` في config.py
- Webhook routing مُحدَّث بـ per-tenant check
- StateStore مُصلَح (Customer → Conversation)
- CommerceFacts: in_stock_count + orderable + top_products + coupon_eligibility + platform
- RealPolicyGate: working_hours (للـ handoff فقط) + coupon cap 24h + price_range gate + auto-escalate
- ClarificationFlow: ACTION_CLARIFY + ACTION_NARROW في DecisionEngine + Executor + Composer
- MemoryUpdater Phase 2 كامل

**Policy fix:**
- صُحِّح Working Hours gate: لا يُوقف الطلبات، يُوقف الـ handoff فقط (المتجر أونلاين لا يحتاج أحداً حاضراً)

**Structured checkout / order prep:**
- إضافة `OrderPreparationState` لحفظ بيانات تجهيز الطلب داخل state
- تحديث `slot_extractor.py` لاستخراج `city`, `customer_name`, `short_address_code`, `google_maps_url` وبعض حقول العنوان
- تنفيذ `services/address_resolution.py` لدعم SPL National Address API + تحليل deterministic للرمز المختصر وروابط الخرائط والإحداثيات
- تحويل `DraftOrderHandler` من "أنشئ طلباً ناقصاً فوراً" إلى مسار stateful يجمع البيانات الناقصة ثم ينشئ draft order
- قبول `city + short address code` كحد أدنى عملي، مع ملء الحقول تلقائياً إذا كانت SPL API مفعلة
- دعم Google Maps link كمدخل عنوان بديل يُستخدم في التحضير والـ notes مع محاولة geocode عند توفر المفتاح
- إضافة اختبارات تغطي order preparation وaddress signal extraction

---

## 6. Known Problems / Open Gaps

### P0 — حرجة (تؤثر على الإنتاج)

- [ ] **LLM fallback يفقد سياق Brain** — عند `ACTION_LLM_REPLY`، يستدعي `generate_orchestrate_response` القديم الذي لا يعرف شيئاً عن stage/product_focus/policy_reason. يجب حقن Brain context داخله.
- [ ] **SlotExtractor غير مختبر على رسائل حقيقية** — الـ Haiku call للـ slots لم يُختبر بعد على محادثات المتجر التجريبي. قد تكون جودة الاستخراج ضعيفة لبعض الجمل العربية.

### P1 — مهمة (تؤثر على الجودة)

- [ ] **ConversationHistorySummary لا تُحقن** — يكتبها كل 5 turns لكن لا أحد يقرأها. يجب حقنها في LLM fallback كـ "customer context".
- [ ] **ProductAffinity لا تُقرأ** — تُكتب بعد كل search/order لكن لم تُستخدم في الترتيب أو القرار بعد.
- [ ] **TrackOrderHandler يُعيد أحدث طلب** — لا يُطابق رقم طلب محدد إذا ذكره العميل.
- [ ] **suggest_coupon يختار أول كوبون** — لا ينتقي الكوبون الأذكى (مناسب للسعر، للعميل، للمنتج).
- [ ] **Google Maps → short address ما زال محدوداً** — المسار الحالي يستفيد من الرابط/الإحداثيات، لكن الاستخراج الكامل للرمز المختصر يحتاج SPL API مفعلة وتحسين parsing إضافي.

### P2 — تحسينات مرغوبة

- [ ] **لا Suggestion Engine** — الـ Brain لا يقترح الخطوة التالية بعد كل turn (e.g. "هل تريد رؤية المزيد؟" / "أبغى أطلب؟")
- [ ] **لا dedup guard في Composer** — إذا بحث العميل عن نفس الشيء مرتين، يحصل على نفس الرد بالضبط.
- [ ] **قوالب الـ Composer لا تتغير** — greeting/search/product_results نفسها دائماً. يجب 3 variations لكل قالب.
- [ ] **auto-escalate مبسّط** — يعتمد على `state.turn >= 3` وليس على streak حقيقي من GENERAL intents.
- [ ] **لا Analytics / Outcome Tracking** — لا نعرف هل أكمل العميل الطلب فعلاً في Salla، ولا هل نُقد الكوبون.
- [ ] **لا block list** — PolicyGate لا يستطيع إيقاف عميل مزعج.

### P3 — تقنية (لا تؤثر على التشغيل)

- [ ] **StateStore لا يستخدم Redis كـ fallback** — إذا لم تُوجد Conversation row بعد، لا يُوجد تخزين مؤقت.
- [ ] **`Action.NARROW` لا تُطلق من DecisionEngine بشكل مباشر** — تُطلق فقط من Composer عند `suggest_narrow=True` من نتيجة Search. يجب أن يكون القرار في DecisionEngine لا Composer.

---

## 7. Next Priorities — أولويات Phase 3

مرتّبة بحسب الأثر:

1. **حقن Brain state في LLM fallback** — أهم شيء لأن `ACTION_LLM_REPLY` هو catch-all لكل ما لم يُمسك بقاعدة. يجب أن يعرف LLM ما الـ stage والـ product_focus والـ policy_reason.
2. **حقن ConversationHistorySummary في LLM context** — يُعطي LLM ذاكرة حقيقية عن العميل.
3. **تفعيل SPL address resolution في البيئة** — حتى يصبح `short_address_code` و`Google Maps` auto-fill فعلياً في المتجر التجريبي/الإنتاجي.
4. **تحسين Google Maps parsing** — دعم أوسع لروابط الخرائط واستخراج short code/structured address بدقة أعلى.
5. **Template dedup + variations** — لا تكرار وإضافة 3 صياغات لكل قالب.
6. **قراءة ProductAffinity في DecisionEngine** — ترتيب نتائج البحث بحسب affinity score.
7. **تحسين auto-escalate** — تتبع streak حقيقي للـ GENERAL intents عبر history.
8. **Analytics / Outcome Tracking** — webhook من Salla عند تأكيد الطلب → تحديث ConversationTrace.
9. **Merchant-configurable policy** — إعدادات من Dashboard: ساعات العمل، coupon frequency cap، max_order_value.

---

## 8. Definition of Done — متى يصبح Brain قوياً فعلاً؟

نعتبر Merchant Brain "production-ready" عندما تتحقق كل النقاط التالية:

### سلوك المحادثة
- [ ] لا يُكرّر التحية في نفس الجلسة
- [ ] يتذكّر المنتج الذي تحدّث عنه العميل في الرسالة السابقة
- [ ] يُعيد طرح الخيارات بصياغة مختلفة لا بنسخة طبق الأصل
- [ ] لا يقترح كوبوناً في أول رسالة أو بدون منتج محدد
- [ ] يسأل سؤالاً واحداً واضحاً عند غياب المعلومات

### الأفعال الحقيقية
- [ ] يبحث في الكتالوج الحقيقي بالعربية
- [ ] يُنشئ draft order في Salla عند الطلب
- [ ] يجمع الاسم والمدينة والرمز الوطني المختصر أو رابط الخرائط بدون فوضى
- [ ] يملأ العنوان تلقائياً من short address / geocode عند توفر التكامل
- [ ] يُرسل payment link حقيقي
- [ ] يُعيد رابط الدفع عند طلبه ثانيةً
- [ ] يُتابع حالة طلب حقيقي

### الذكاء والقرار
- [ ] يكتشف نية شراء بـ accuracy >= 85% على رسائل عربية حقيقية
- [ ] يُطبّق PolicyGate دون أن يُخطئ في السيناريوهات المعروفة
- [ ] يُصعّد للإنسان عند الحاجة ولا يتجمّد في دوامة

### الرقابة والشفافية
- [ ] يُنتج BrainTurnTrace JSON لكل turn
- [ ] ConversationTrace مكتوب في DB لكل رسالة
- [ ] لا hallucination: كل منتج أو سعر مذكور موجود في الكتالوج الحقيقي

### الثبات
- [ ] 21+ اختبار يمر في CI
- [ ] الـ Legacy fallback يعمل عند فشل Brain
- [ ] لا exception غير معالجة تُسكت الرد

---

## 9. File Map — خريطة الملفات

```
backend/modules/ai/brain/
├── __init__.py                  ← exports عامة
├── types.py                     ← عقود البيانات بين الطبقات
├── protocols.py                 ← Protocol interfaces لكل طبقة
├── pipeline.py                  ← MerchantBrain + BrainTurnTrace + get_brain()
│
├── intent/
│   ├── rules.py                 ← 8 intents بـ regex عربي (0ms latency)
│   ├── slot_extractor.py        ← Haiku call للـ slots
│   └── classifier.py           ← hybrid: rules-first → LLM fallback
│
├── state/
│   ├── stages.py                ← 7 stage constants
│   └── store.py                 ← load/save عبر Customer → Conversation
│
├── facts/
│   └── commerce_facts.py        ← RichFactsLoader (Phase 2)
│
├── decision/
│   ├── actions.py               ← 10 ACTION_* constants
│   ├── engine.py                ← DefaultDecisionEngine (8+ قواعد)
│   └── policy.py                ← PassThrough + RealPolicyGate
│
├── execution/
│   ├── search.py                ← ProductSearchHandler → CatalogContextBuilder
│   ├── orders.py                ← DraftOrderHandler + TrackOrderHandler
│   └── executor.py              ← dispatcher
│
├── compose/
│   ├── templates.py             ← Arabic reply templates
│   └── responder.py             ← DefaultComposer + LLM fallback
│
└── memory/
    └── updater.py               ← ConversationTrace + ProductAffinity + Summary

config:
  backend/core/config.py         ← MERCHANT_BRAIN_ENABLED, MERCHANT_BRAIN_TENANT_IDS

entry point:
  backend/routers/whatsapp_webhook.py → _handle_merchant_message()
```

---

## 10. How to Use This Document

### كل يوم قبل البدء
1. افتح هذا الملف
2. اقرأ **Known Problems** — ما لم يُحلّ بعد
3. اقرأ **Next Priorities** — ما هو الأهم اليوم
4. ابدأ من أعلى القائمة

### كل يوم بعد الانتهاء
1. أضف entry جديدة في **Daily Progress Log** بتاريخ اليوم
2. حدّث **Current Status Table** إذا تغيّرت حالة أي طبقة
3. انقل الـ gaps التي حُلّت من **Known Problems**
4. حدّث **Current Phase** إذا انتهت مرحلة أو بدأت مرحلة جديدة

### قبل بناء feature جديدة
1. تأكد أنها مُدرجة في **Next Priorities**
2. تأكد أنها لا تتعارض مع **Known Problems** حالية
3. حدّث **Architecture Layers** إذا أضفت طبقة جديدة

---

*آخر تحديث: 2026-04-18 — اكتمال Phase 1 + Phase 2*
