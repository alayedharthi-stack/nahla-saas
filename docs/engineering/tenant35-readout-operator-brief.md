# تكليف وكيل التشغيل — ما بعد التشغيل الثالث: القراءات الضرورية فقط من لوحة Meta وWhatsApp Manager بعد تسجيل دخول المالك (قراءة فقط)

**الحالة (2026-10-02):** التشغيل الثالث (v3) **منجز ووصلت نتائجه نصًا**: `is_on_biz_app=true` و`platform_type=CLOUD_API` (coexistence مثبت)؛ المنتجات 185/188/190 = 20 متغيرًا تطابق سلة سعرًا وتوفرًا (8 متوفرة، 12 نافدة)؛ `GET /{waba}/product_catalogs` ما زال مرفوضًا برسالة نوع النشاط؛ `catalog_management` غائبة عن الرمز وسببها غير محسوم؛ جدول Meta الرسمي يفصل «لا تغيير» لكتالوج التطبيق عن «غير مدعوم» عبر Cloud API، والجملة المتداولة ليست في قسم Limitations نصًا. **لا تشغيل رابع للأداة.** المتبقي قراءات لوحة محددة أدناه، كل منها بسببها، **معلّقة حتى يسجّل المالك الدخول**. **قراءة فقط: لا تغيير إعداد أو صلاحية أو وضع تطبيق، لا طلب مراجعة، لا إنشاء أو ربط كتالوج، لا مسّ باتصال واتساب أو coexistence.** أعد النتائج **نصًا داخل الرسالة** إضافةً إلى الإرفاق.

## الجزء أ — التشغيل الثالث (منجز؛ يُحتفظ به للمرجع والتحقق من البصمة)

> البصمة المثبَّتة أدناه هي للملف الذي **شُغِّل فعلًا** في التشغيل الثالث. النسخة اللاحقة من الملف في الفرع تختلف عنها في نصوص التوثيق المضمّنة فقط (تسجيل قراءة المشغّل لجدول Meta)، ولا تُشغَّل: لا تشغيل رابع.

### أ-1. ما تحتاجه

| البند | القيمة |
|---|---|
| مشروع Railway | `desirable-growth` (`f0090862-0a40-4293-bd5d-e94df58762b5`) |
| البيئة | `production` (`ede962ce-3042-4dae-94de-623837e83ed9`) |
| الخدمة | `nahla-saas` (`686b36c5-a926-4e58-912a-5e9d13fbc2e7`) |
| الصلاحية المطلوبة | `railway ssh` إلى هذه الخدمة فقط؛ لا تحتاج النشر أو تعديل المتغيرات ولا يجوز استخدامهما |
| **الملف المثبَّت** | `scripts/operators/catalog_trial_readout_standalone.py` من مستودع `alayedharthi-stack/nahla-saas`، فرع `claude/nahla-product-catalog-sync-7nhht0`، **عند الالتزام `457b4b33194fc6b80f723b0d13c9fadf0b269409` حصرًا** |
| **SHA-256 الكامل** | `1ffbeb2128b7846d2e533cf13c8f10c148078f78076cee4e53ee0ed33092031c` (الحجم 73071 بايت). إن اختلفت البصمة فلا تشغّل الملف وأبلغ |
| على جهازك | Python 3.9+ و Railway CLI مسجّل الدخول (`railway whoami`) |

> إصدارات سابقة من الملف (`18682b12…`، `c90f7d9e…`، `16ab1a5e…`، `362ec06d…`) انتهى دورها؛ لا تستخدمها.

### أ-2. ممنوعات صريحة

- لا `railway variables --set`، لا `railway up`/`redeploy`، لا `railway run` (يحمّل الأسرار إلى جهازك)، لا تعديل أي خدمة أو متغير.
- لا طباعة أو نقل أي قيمة من متغيرات البيئة (`env`, `printenv`, `echo $…`). لا اتصال مباشر بقاعدة البيانات.
- لا حذف ولا تعديل تحت `/app`. الملف يُكتب في `/tmp` فقط.
- لا تشغيل بمعرّف مستأجر غير 35. أي traceback يُنقل بعد حذف أي قيمة تشبه رمزًا أو رابط اتصال.

### أ-3. التنفيذ

```bash
railway whoami
railway status

# تحقق البصمة الكاملة قبل أي شيء
sha256sum catalog_trial_readout_standalone.py
# يجب أن تكون: 1ffbeb2128b7846d2e533cf13c8f10c148078f78076cee4e53ee0ed33092031c

# اطلب من الملف طباعة أمر railway ssh الذي يحمله ويشغّله (نفس خيارات v2)
python3 catalog_trial_readout_standalone.py --print-ssh-command \
  --tenant-id 35 --include-graph --include-salla \
  --candidate-ids 185,188,190 --expected-business-id 2138142656950660 > run_readout.sh
head -c 200 run_readout.sh   # يبدأ بـ: railway ssh --environment production --service nahla-saas -- bash -lc 'echo

bash run_readout.sh > tenant35_readout_v3.json 2> tenant35_readout_v3.stderr.txt
echo "exit=$?"
```

**ما يحدث داخل الحاوية (v3):** يُفكّ الملف من base64 إلى `/tmp/catalog_trial_readout_standalone.py`، ثم من `/app` يُنفَّذ:

```text
python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --include-salla \
  --candidate-ids 185,188,190 --expected-business-id 2138142656950660 --candidates 3 --pretty
```

يستخدم `DATABASE_URL` ورموز واتساب ومعرّف/سر التطبيق (`META_APP_ID`/`META_APP_SECRET` لاستدعاء `GET /debug_token` فقط) ورمز سلة، كلها من بيئة الحاوية دون أن تخرج منها. القراءات كلها GET/SELECT:
- قاعدة البيانات: SELECT فقط (منتجات، متغيرات، عضويات، اتصال واتساب، استحقاق).
- Meta: `GET /{waba}/product_catalogs` (يُتوقع أن يفشل بكود 10 كما في v2؛ v3 تسجّل `error_class` ولا تستنتج «لا كتالوج»)، `GET /{waba}?fields=owner_business_info`، **`GET /{phone_number_id}?fields=is_on_biz_app,platform_type` (جديد: إثبات coexistence)**، `GET /me/permissions`، `GET /debug_token`، `GET /{catalog}?fields=…` و`GET /{catalog}/products` لكل كتالوج مرتبط **إن** نجحت قراءة الكتالوجات.
- سلة: `GET /products/{id}` و`GET /products/{id}/variants` للمنتجات الشاذة (186) **وللمرشحين الثلاثة 185/188/190 (جديد: `candidate_salla_crosscheck`)**.
- ملف الكود المنشور `/app/backend/routers/whatsapp_embedded.py` لاستخراج النطاق الصريح (قراءة فقط).

**بديل إن رفض `railway ssh` طول الأمر:** جلسة تفاعلية `railway ssh --environment production --service nahla-saas`، ثم لصق الملف إلى `/tmp/catalog_trial_readout_standalone.py` عبر heredoc، ثم `sha256sum` (يجب أن يطابق البصمة الكاملة أعلاه)، ثم `cd /app && python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --include-salla --candidate-ids 185,188,190 --expected-business-id 2138142656950660 --pretty`. **لا تُعدّل المخزون أو أي بيانات في سلة لتسهيل التجربة.**

### أ-4. التحقق قبل الإرسال

- JSON صالح يحوي `"tenant_id": 35`، `"read_only": true`، `"graph_reads_included": true`، `"secrets_included": false`.
- ابحث في الملف عن `EAA` و`postgres://` و`postgresql://` و`access_token` و`app_token` ⇒ يجب ألا يوجد أي منها. إن وُجد، لا ترسل وأبلغ.
- آخر سطر في stderr يبدأ بـ `trial-readout tenant=35`.

### أ-5. ما تعيده كاملًا

1. `tenant35_readout_v3.json` كاملًا. 2. `tenant35_readout_v3.stderr.txt` بعد التنقية. 3. ناتج `railway status`. 4. أول 200 حرف من الأمر المشغَّل ووقت التشغيل UTC. 5. تأكيد صريح بعدم تنفيذ أي ممنوع.

### أ-6. ما سيُقرأ من الناتج (للعلم)

`graph.coexistence` (`is_on_biz_app`, `platform_type`, `verdict`)؛ `graph.waba_catalogs` (`verdict`, `error_class.class`)؛ `graph.catalog_path_assessment` (`api_catalog_link_for_this_waba`, `catalog_exists_for_this_waba`, `would_catalog_management_alone_lift_the_block`, `needs_manual_reads`)؛ `graph.waba_owner_business.matches_expected`؛ `graph.token_catalog_management.raw` و`.interpretation` (احتمال الرفض يبقى `unknown` عند `absent`)؛ `trial_candidates.preferred_evaluation` و`scenario_coverage`؛ `candidate_payloads` (`local_payload_checks_passed` = فحص محلي فقط، `availability_counts`، `warnings` بأسبابها، `items`)؛ `candidate_salla_crosscheck` (لكل متغير: `verdict`, `salla_price`, `salla_available`, `salla_option_label`; `salla_variants_missing_locally`)؛ `salla_check.checked[].verdict` للمنتج 186؛ `graph.live_items` (`skipped=waba_catalog_link_unproven_graph_error` مع `conditional_actions` إن فشلت قراءة الكتالوجات)؛ `missing_requirements`.

## الجزء ب — القراءات الضرورية فقط، بعد تسجيل دخول المالك (قراءة فقط، بلا أي تعديل)

كل بند أدناه له سؤال واحد يحسمه وسبب يجعله ضروريًا. **ابدأ بـ ق-1؛ إن كانت نتيجته «لا ربط متاح» فلا حاجة إلى ق-3، ويبقى ق-2 لمسار التجار على Cloud API فقط.**

### ق-1 (حاسم لـ Tenant 35) — WhatsApp Manager (business.facebook.com/wa/manage) → الحساب `1682673239554563`

> **منجز بقراءة المالك (2026-10-02):** الوصول إلى المحفظة `2138142656950660` نجح (غير موثقة تجاريًا)؛ الحساب ظاهر بوسم تطبيق واتساب للأعمال وحالة موافق عليها؛ صفحة الكتالوج تعرض «ربط كتالوج» و«اختيار كتالوج» دون معرّف ربط مثبت؛ كتالوج مملوك: `871742015873294` «كتالوج متجر فساتين - نحلة» (34 منتج أزياء). **البندان 1–2 أدناه للسجل. لا تنقر «ربط».** المتبقي: **ق-4** أدناه.

1. **محفظة الأعمال المالكة للحساب ونوعها:** اسم المحفظة ومعرّفها كما تعرضهما الصفحة (هل هي `2138142656950660` أم محفظة أُنشئت تلقائيًا عند الربط من تطبيق WhatsApp Business؟)، وأي وصف لنوعها أو حالة توثيقها حرفيًا. *السبب:* Graph رفض `product_catalogs` برسالة «SMB business type»؛ هذه القراءة تسمّي النوع من الواجهة مباشرة.
2. **تبويب الكتالوج (Catalog / Commerce) لهذا الحساب:** ماذا يعرض بالضبط — خيار «Connect catalog» من Commerce Manager؟ كتالوج تطبيق WhatsApp Business فقط؟ رسالة بأن الميزة غير متاحة لهذا الحساب؟ انقل النص حرفيًا ولقطة شاشة. **اقرأ فقط؛ لا تنقر «ربط».** *السبب:* هذا هو الطريق الوحيد المتبقي لمعرفة هل ربط كتالوج API ممكن لهذا الحساب مع بقاء الرقم على التطبيق، بعد أن أغلق Graph القراءة.

### ق-2 (يحكم سبب غياب الصلاحية؛ ضروري لمسار Cloud API عامةً، ولـ Tenant 35 فقط إن كانت ق-1 «ربط متاح») — لوحة التطبيق (developers.facebook.com)

> **ق-2 لا يُطلب:** نتائج لوحة التطبيق مثبتة في ملف المالك (مستوى وصول `catalog_management` = **Standard**؛ طلب App Review مسودة غير مرسلة رقم `2077337856315947`؛ وبقية البنود في ملفه). **لا تُعاد قراءتها، ولا تُرسل المسودة ولا تُعدَّل.** البنود 3–5 أدناه للسجل فقط. **المتبقي فعلًا: ق-1 (محفظة Tenant 35 وتبويب الكتالوج) بعد تسجيل دخول المالك.**

3. **إعداد Embedded Signup المرتبط بـ `config_id`:** WhatsApp → Embedded Signup → Configurations؛ طابق **آخر 4 أرقام** مع `graph.token_catalog_management.raw.embedded_signup_config.config_id_tail` في ناتج v3. انقل: اسم الإعداد، الصلاحيات/الأصول التي يطلبها (هل يتضمن إدارة الكتالوج/Commerce؟)، نوع التدفق، وهل هو إعداد coexistence. *السبب:* الحوار الذي رآه التاجر يُبنى من هذا الإعداد، ولا يُقرأ عبر Graph.
4. **مستوى وصول `catalog_management` وحالة App Review** (App Review → Permissions and Features)، ومعه `business_management` و`whatsapp_business_management` و`whatsapp_business_messaging`: Standard/Advanced/غير مضافة، وحالة أي طلب مراجعة بتاريخه. *السبب:* يحدد من يمكن منحه الصلاحية أصلًا.
5. **وضع التطبيق (Development/Live) وحالة توثيق المحفظة المالكة للتطبيق، ودور حساب Meta الذي ربط Tenant 35 في التطبيق.** *السبب:* يحدد هل Standard Access كافٍ لتجربة 35.

### ق-4 (الأقل واللازمة الآن) — Commerce Manager → الكتالوج `871742015873294` → Items → Export (قراءة فقط)

**الغرض: مطابقة فقط.** تصدير العناصر الـ34 لمقارنتها بالهويات العشرين للمنتجات 185/188/190 ومنع التكرار أو الخلط. **التصدير لا يثبت الملكية التشغيلية ولا مصدر النشر؛ وصيغة المعرّف أو نطاق الرابط لا تجيز تعديل أي عنصر.**

**الخطوات (بلا أي تعديل):**
1. افتح Commerce Manager → الكتالوج `871742015873294` («كتالوج متجر فساتين - نحلة») → Items.
2. صدّر العناصر كلها (Export → CSV). إن لم يتوفر التصدير، انقل قائمة العناصر بالأعمدة أدناه.
3. تأكد أن الملف يحوي لكل عنصر: **`id`/*Content ID*** (هو **`retailer_id`** الذي يضعه التاجر أو المنصة — **ليس** معرّف عنصر Meta), `title`, `description`, `price`, `availability`, `link`, `image_link`, `item_group_id`, وأي عمود للظهور (`visibility`/`status`) إن وُجد. **إن ظهر عمود لمعرّف عنصر Meta الرقمي** (مثل `fb_product_id` / *Product ID* / `product_id` من فيسبوك — رقم طويل مستقل عن Content ID) فانقله **في عمود منفصل باسمه كما ورد**، ولا تخلطه بـContent ID؛ نحتاج الاثنين للمطابقة مع `meta_item_id` في عضوياتنا. انقل الأعمدة كما هي دون إعادة تسمية.
4. من صفحة الكتالوج (معلومات/إعدادات) انقل: عدد العناصر، تاريخ آخر تحديث، مصدر البيانات (feed/تحديث يدوي/API — كما تعرضه الواجهة)، وهل الكتالوج مربوط بأي حساب واتساب أو أصل آخر (قراءة فقط).
5. **لا تعدّل عنصرًا، لا تحذف، لا تربط الكتالوج، لا تنقر «ربط كتالوج»، لا تنشئ feed.**

**ما تعيده:** CSV كاملًا **نصًا داخل الرسالة** إضافةً إلى الإرفاق، ومعلومات البند 4 نصًا، وتأكيدًا بعدم تنفيذ أي ممنوع.

**ما سيُستخرج منه (للعلم):** لكل هوية من العشرين (`617350990-*`, `792574531-*`, `59407425-*`): غائبة → `create`؛ موجودة → **مطابقة تحتاج تحققًا** (لا تعديل حتى ق-5)؛ منتج Tenant 35 موجود بهوية مختلفة → `duplicate_risk`. **لا يُعلن `noop` من التصدير وحده**: يلزم إثبات مصدر النشر (ق-5) ثم مقارنة كل الحقول المزامَنة؛ ما لم يُقرأ (مثل الظهور) يبقى غير محسوم.

### ق-5 (تُطلب فقط إن أظهر ق-4 أي تطابق أو أي هوية بصيغة نهلة) — قراءة قاعدة الإنتاج (SELECT واحدة عبر `railway ssh`)

هذا هو **إثبات مصدر النشر** الوحيد المتاح: صفوف `meta_catalog_memberships` حيث `catalog_id='871742015873294'` (المستأجر، `retailer_id`, `meta_item_id`, `provenance`)، ومعها `products.meta_item_id` غير الفارغة بالمستأجر **كمؤشر فقط** (الختم يكتبه الاستيراد والتبنّي أيضًا فلا يثبت النشر). عضوية بمصدر **`salla_variant_push`** ومعرّف عنصر Meta مطابق لمعرّف العنصر في التصدير = نشر مثبت لذلك المستأجر؛ عضوية `meta_graph_reconcile` أو `literal_retailer_bind` (لا كاتب لها) أو غياب العضوية = غير مثبت. الاستعلام الدقيق يُعطى عند الحاجة؛ قراءة فقط؛ **لا SSH قبل نتيجة ق-4.**

### ق-3 (منجز جزئيًا) — Business Manager → المحفظة `2138142656950660` → Data sources → Catalogs

6. ~~هل يوجد كتالوج مملوك للمحفظة؟~~ **منجز:** `871742015873294`، 34 عنصرًا. **المتبقي من ق-3:** هل هو مربوط بأي حساب واتساب كما تعرضه صفحة الكتالوج في Business Manager (قراءة فقط)؟

### ملاحق (غير حاسمة)

7. **النقل الحرفي لجدول المقارنة** في صفحة «Onboard WhatsApp Business app users» بأعمدته وصفوفه (قُرئ على الصفحة وأُكِّد؛ يُرفق النص الحرفي للسجل).
8. فقرة دليل «Sell products and services» عن شروط ربط الكتالوج بالـ WABA والصلاحيات (غير حاسمة لـ ق-1؛ للسجل).

**الإخراج:** نص داخل الرسالة أولًا (ثم لقطات شاشة مرفقة) مع إخفاء أي أسرار (App Secret، رموز). **ما لا يُطلب:** تشغيل رابع للأداة، إعادة `GET /{waba}/product_catalogs`، أي قراءة سلة إضافية، أي تعديل.
