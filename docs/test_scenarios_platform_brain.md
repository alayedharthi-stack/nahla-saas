# Nahla Platform Brain — 10 Test Scenarios
## Expected Paths, Decisions & Stage Transitions

---

## SCENARIO 1 — المسار المثالي: من التعرف إلى الاشتراك
**Persona**: تاجر مهتم، متجره على سلة، متجر متوسط الحجم

| # | الرسالة | IntentEngine | DecisionEngine | Stage | Claude؟ |
|---|---------|-------------|---------------|-------|---------|
| 1 | "هلا" | greeting | SHOW_WELCOME_MENU | discovery | ❌ |
| 2 | [يضغط "كيف تشتغل؟"] | button:menu_how | FILL_SLOT button | discovery | ❌ |
| 3 | [يضغط "سلة"] | button:store_salla | platform=سلة | qualification | ❌ |
| 4 | [يضغط "متوسط/كبير"] | button:store_big | size=large → recommendation | recommendation | ❌ |
| 5 | "أبي أشترك" | subscribe_now | SEND_CHECKOUT_LINK | checkout | ❌ |

**Expected stage transitions**: discovery → qualification → recommendation → checkout
**Claude يُستدعى**: 0 مرة
**Deduplication**: ask_platform و ask_store_size لا تُطرح ثانيةً

---

## SCENARIO 2 — التخطي المباشر للدفع
**Persona**: تاجر يعرف ما يريد، بدون أسئلة

| # | الرسالة | IntentEngine | DecisionEngine | Stage | Claude؟ |
|---|---------|-------------|---------------|-------|---------|
| 1 | "أرسل رابط الدفع" | request_payment_link | SEND_CHECKOUT_LINK | checkout | ❌ |

**Expected**: رابط التسجيل فوراً — بدون أسئلة — بدون Claude
**decision_reason**: `explicit_payment_link_request`

---

## SCENARIO 3 — سؤال الأسعار مع متابعة
**Persona**: تاجر مهتم بالأسعار

| # | الرسالة | IntentEngine | DecisionEngine | Stage | Claude؟ |
|---|---------|-------------|---------------|-------|---------|
| 1 | "وش الأسعار؟" | ask_price | SHOW_PLANS | discovery | ❌ |
| 2 | "وش الفرق بين الباقات؟" | ask_features | GENERATE_AI_REPLY | qualification | ✅ |
| 3 | "متجري صغير" | store_small | FILL_SLOT_SIZE | recommendation | ❌ |
| 4 | "ابي اجرب" | request_trial | SEND_TRIAL_LINK | checkout | ❌ |

**FactGuard**: Claude في رسالة 2 يُسمح له فقط باستخدام الأسعار الرسمية (899، 1499، 2499)
**Deduplication**: بعد ملء store_small، لا يُسأل عن الحجم مرة ثانية

---

## SCENARIO 4 — منع تكرار السؤال (Deduplication Test)
**Persona**: تاجر أجاب على المنصة لكن النظام يحاول يسأل مرة ثانية

| # | الرسالة | asked_keys | Deduplication | النتيجة |
|---|---------|-----------|--------------|---------|
| 1 | "سلة" | [] | - | platform=سلة، mark ask_platform |
| 2 | "كيف تشتغل نحلة؟" | [ask_platform] | ✅ ask_platform blocked | لا يُسأل عن المنصة ثانيةً |
| 3 | "وش المميزات؟" | [ask_platform] | ✅ ask_platform blocked | AI يشرح مميزة واحدة ويسأل عن الحجم |

**Expected**: النظام لا يسأل "متجرك على أي منصة؟" أبداً بعد الرسالة الأولى
**Context injection**: يُخبر Claude أن platform=سلة مع _asked_keys=[ask_platform]_

---

## SCENARIO 5 — Idempotency Test (رسائل مكررة من Meta)
**Persona**: webhook يرسل نفس الرسالة مرتين (retry)

| # | msg_id | الرسالة | processed_ids | النتيجة |
|---|--------|---------|--------------|---------|
| 1 | "wamid.abc123" | "أبي أشترك" | [] | معالجة كاملة |
| 2 | "wamid.abc123" | "أبي أشترك" | ["wamid.abc123"] | **SKIP** — Idempotency blocked |

**Expected log**: `detected_intent=DUPLICATE, decision=SKIP, decision_reason=idempotency_duplicate`
**لا يُرسل رابط مكرر** للمستخدم

---

## SCENARIO 6 — العودة بعد انقطاع (Session Persistence)
**Persona**: تاجر تحدث يوم أمس وعاد اليوم

| # | الرسالة | State من DB | النتيجة |
|---|---------|------------|---------|
| يوم أمس | "متجري على سلة" | - | platform=سلة saved |
| اليوم | "كم أسعاركم؟" | platform=سلة, stage=qualification | SHOW_PLANS — يعرف منصته |
| اليوم | "أبي أجرب" | purchase_score=1 | SEND_TRIAL_LINK |

**Expected**: ConversationState محفوظة عبر الجلسات — لا تبدأ من صفر

---

## SCENARIO 7 — تواصل مع المؤسس
**Persona**: تاجر يريد التحدث مع شخص حقيقي

| # | الرسالة | IntentEngine | DecisionEngine | Claude؟ |
|---|---------|-------------|---------------|---------|
| 1 | "أبي أتكلم مع المؤسس" | contact_founder | SEND_FOUNDER_LINK | ❌ |

**Expected**: رابط wa.me/966555906901 فوراً
**FactGuard**: الرقم من STATIC_FACTS.founder_wa — Claude لا يختار رقماً

---

## SCENARIO 8 — FactGuard Test (منع الهلوسة)
**Persona**: المستخدم يسأل سؤالاً مفتوحاً

| # | الرسالة | IntentEngine | Action | FactGuard |
|---|---------|-------------|--------|-----------|
| 1 | "كم مدة التجربة المجانية؟" | general | GENERATE_AI_REPLY | ✅ 14 يوم فقط |
| 2 | "كم سعر الباقة الذهبية؟" | ask_price | SHOW_PLANS | ❌ لا توجد "باقة ذهبية" |

**Expected**: Claude يقول "التجربة 14 يوم" وليس "30 يوم" أو "شهر"
**FactGuard verify_reply**: يفحص أن الأرقام في الرد من STATIC_FACTS فقط

---

## SCENARIO 9 — اللهجات العربية المختلفة
**Persona**: تاجر يكتب بأساليب مختلفة

| الرسالة | Intent المتوقع |
|---------|--------------|
| "ابي اشترك" | subscribe_now ✅ |
| "أبغى أجرب" | request_trial ✅ |
| "وش الباقات" | ask_price ✅ |
| "كيف تشتغل المنصة" | ask_how_it_works ✅ |
| "اشرح لي وش نحلة" | ask_how_it_works ✅ |
| "متجري على سلة" | platform_salla ✅ |
| "my store is on salla" | platform_salla ✅ |
| "small store" | store_small ✅ |
| "متجر ناشئ" | store_small ✅ |
| "send payment link" | request_payment_link ✅ |

---

## SCENARIO 10 — Edge Cases
**حالات الحافة التي يجب أن يتعامل معها النظام**

| الحالة | الرسالة | Expected |
|--------|---------|---------|
| رسالة فارغة | "" | تجاهل — لا رد |
| رسالة جداً طويلة (+500 حرف) | lorem... | general → GENERATE_AI_REPLY |
| Stage عالق في checkout | "أبي أشترك" مرة ثانية | SEND_CHECKOUT_LINK مرة ثانية (لا مشكلة) |
| سؤال دعم | "في مشكلة مع الدفع" | ESCALATE_SUPPORT → support@nahlah.ai |
| طلب منصة غير مدعومة | "متجري على Shopify" | store_other → نص توضيحي |
| تجربة منتهية والتاجر يرجع | الرسالة الأولى | يحمّل state القديم بدون أن يبدأ من صفر |
| رسالة صورة أو صوت | [image/audio] | تجاهل — msg_type != text |
| رسالتان بسرعة | "هلا" + "أسعار" | idempotency يمنع التكرار، ينتج رسالتين |

---

## ملخص القرارات

| Action | Claude؟ | متى؟ |
|--------|---------|------|
| SHOW_WELCOME_MENU | ❌ | greeting |
| SEND_CHECKOUT_LINK | ❌ | subscribe/payment intent أو stage=checkout |
| SEND_TRIAL_LINK | ❌ | trial intent |
| SHOW_PLANS | ❌ | ask_price |
| FILL_SLOT_PLATFORM | ❌ | platform_salla/zid |
| FILL_SLOT_SIZE | ❌ | store_small/large |
| SEND_FOUNDER_LINK | ❌ | contact_founder |
| ESCALATE_SUPPORT | ❌ | request_support |
| **GENERATE_AI_REPLY** | **✅** | **general / ask_features / ask_how_it_works** |

**Claude يُستدعى في < 20% من الرسائل في المسار المثالي.**
