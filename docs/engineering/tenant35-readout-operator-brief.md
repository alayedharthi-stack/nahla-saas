# تكليف وكيل التشغيل — ما بعد التشغيل الثالث: القراءات الضرورية فقط من لوحة Meta وWhatsApp Manager بعد تسجيل دخول المالك (قراءة فقط)

**الحالة (2026-10-02):** التشغيل الثالث (v3) **منجز ووصلت نتائجه نصًا**: `is_on_biz_app=true` و`platform_type=CLOUD_API` (coexistence مثبت)؛ المنتجات 185/188/190 = 20 متغيرًا تطابق سلة سعرًا وتوفرًا (8 متوفرة، 12 نافدة)؛ `GET /{waba}/product_catalogs` ما زال مرفوضًا برسالة نوع النشاط؛ `catalog_management` غائبة عن الرمز وسببها غير محسوم؛ جدول Meta الرسمي يفصل «لا تغيير» لكتالوج التطبيق عن «غير مدعوم» عبر Cloud API، والجملة المتداولة ليست في قسم Limitations نصًا. **لا تشغيل رابع للأداة.** **ق-4 منجز (2026-10-02):** تصدير الكتالوج `871742015873294` لا يحوي أي هوية من هويات المرشحين العشرين ولا أي معرّف من منتجات سلة العشرين الحالية؛ 27 عنصرًا بروابط متجر سلة تجريبي `dev-cgcaqkpx5wgewsyv` و7 عناصر `nahla_p_176`–`nahla_p_182` بروابط نهلة العامة. **المتبقي الآن: ق-5** (قراءة قاعدة الإنتاج، SELECT فقط، لوكيل Railway) أدناه. **قراءة فقط: لا تغيير إعداد أو صلاحية أو وضع تطبيق، لا طلب مراجعة، لا إنشاء أو ربط كتالوج، لا مسّ باتصال واتساب أو coexistence.** أعد النتائج **نصًا داخل الرسالة** إضافةً إلى الإرفاق.

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

> **منجز بقراءة المالك (2026-10-02):** الوصول إلى المحفظة `2138142656950660` نجح (غير موثقة تجاريًا)؛ الحساب ظاهر بوسم تطبيق واتساب للأعمال وحالة موافق عليها؛ صفحة الكتالوج تعرض «ربط كتالوج» و«اختيار كتالوج» دون معرّف ربط مثبت؛ كتالوج مملوك: `871742015873294` «كتالوج متجر فساتين - نحلة» (34 منتج أزياء). **البندان 1–2 أدناه للسجل. لا تنقر «ربط».** ق-4 **منجز**؛ المتبقي: **ق-5** أدناه.

1. **محفظة الأعمال المالكة للحساب ونوعها:** اسم المحفظة ومعرّفها كما تعرضهما الصفحة (هل هي `2138142656950660` أم محفظة أُنشئت تلقائيًا عند الربط من تطبيق WhatsApp Business؟)، وأي وصف لنوعها أو حالة توثيقها حرفيًا. *السبب:* Graph رفض `product_catalogs` برسالة «SMB business type»؛ هذه القراءة تسمّي النوع من الواجهة مباشرة.
2. **تبويب الكتالوج (Catalog / Commerce) لهذا الحساب:** ماذا يعرض بالضبط — خيار «Connect catalog» من Commerce Manager؟ كتالوج تطبيق WhatsApp Business فقط؟ رسالة بأن الميزة غير متاحة لهذا الحساب؟ انقل النص حرفيًا ولقطة شاشة. **اقرأ فقط؛ لا تنقر «ربط».** *السبب:* هذا هو الطريق الوحيد المتبقي لمعرفة هل ربط كتالوج API ممكن لهذا الحساب مع بقاء الرقم على التطبيق، بعد أن أغلق Graph القراءة.

### ق-2 (يحكم سبب غياب الصلاحية؛ ضروري لمسار Cloud API عامةً، ولـ Tenant 35 فقط إن كانت ق-1 «ربط متاح») — لوحة التطبيق (developers.facebook.com)

> **ق-2 لا يُطلب:** نتائج لوحة التطبيق مثبتة في ملف المالك (مستوى وصول `catalog_management` = **Standard**؛ طلب App Review مسودة غير مرسلة رقم `2077337856315947`؛ وبقية البنود في ملفه). **لا تُعاد قراءتها، ولا تُرسل المسودة ولا تُعدَّل.** البنود 3–5 أدناه للسجل فقط. **المتبقي فعلًا: ق-1 (محفظة Tenant 35 وتبويب الكتالوج) بعد تسجيل دخول المالك.**

3. **إعداد Embedded Signup المرتبط بـ `config_id`:** WhatsApp → Embedded Signup → Configurations؛ طابق **آخر 4 أرقام** مع `graph.token_catalog_management.raw.embedded_signup_config.config_id_tail` في ناتج v3. انقل: اسم الإعداد، الصلاحيات/الأصول التي يطلبها (هل يتضمن إدارة الكتالوج/Commerce؟)، نوع التدفق، وهل هو إعداد coexistence. *السبب:* الحوار الذي رآه التاجر يُبنى من هذا الإعداد، ولا يُقرأ عبر Graph.
4. **مستوى وصول `catalog_management` وحالة App Review** (App Review → Permissions and Features)، ومعه `business_management` و`whatsapp_business_management` و`whatsapp_business_messaging`: Standard/Advanced/غير مضافة، وحالة أي طلب مراجعة بتاريخه. *السبب:* يحدد من يمكن منحه الصلاحية أصلًا.
5. **وضع التطبيق (Development/Live) وحالة توثيق المحفظة المالكة للتطبيق، ودور حساب Meta الذي ربط Tenant 35 في التطبيق.** *السبب:* يحدد هل Standard Access كافٍ لتجربة 35.

### ق-4 (**منجز** 2026-10-02 — للسجل) — Commerce Manager → الكتالوج `871742015873294` → Items → Export (قراءة فقط)

> **النتيجة (مقارنة المالك بقراءة Tenant 35 الثالثة):** صفر تطابق مع هويات المرشحين العشرين؛ صفر تطابق مع معرّفات منتجات سلة العشرين الحالية؛ 27 عنصرًا بروابط متجر سلة تجريبي `dev-cgcaqkpx5wgewsyv`؛ 7 عناصر `nahla_p_176`–`nahla_p_182` بروابط نهلة العامة. **لا يُصنَّف المرشحون `create` بعد، ولا يُعتمد الكتالوج للتجربة**: ظهور `nahla_p_*` مع مصدر «Nahlah.Ai»/«API» **مؤشر** يحتاج ربطًا بقاعدة البيانات (ق-5)، وليس وحده إثباتًا للتاجر ولا لدليل النشر المقبول. التصدير نفسه (Content IDs، الروابط، ومعرّفات عناصر Meta الـ34) هو **مدخل ق-5** بملفاته الثلاثة.

**الغرض: مطابقة فقط.** تصدير العناصر الـ34 لمقارنتها بالهويات العشرين للمنتجات 185/188/190 ومنع التكرار أو الخلط. **التصدير لا يثبت الملكية التشغيلية ولا مصدر النشر؛ وصيغة المعرّف أو نطاق الرابط لا تجيز تعديل أي عنصر.**

**الخطوات (بلا أي تعديل):**
1. افتح Commerce Manager → الكتالوج `871742015873294` («كتالوج متجر فساتين - نحلة») → Items.
2. صدّر العناصر كلها (Export → CSV). إن لم يتوفر التصدير، انقل قائمة العناصر بالأعمدة أدناه.
3. تأكد أن الملف يحوي لكل عنصر: **`id`/*Content ID*** (هو **`retailer_id`** الذي يضعه التاجر أو المنصة — **ليس** معرّف عنصر Meta), `title`, `description`, `price`, `availability`, `link`, `image_link`, `item_group_id`, وأي عمود للظهور (`visibility`/`status`) إن وُجد. **إن ظهر عمود لمعرّف عنصر Meta الرقمي** (مثل `fb_product_id` / *Product ID* / `product_id` من فيسبوك — رقم طويل مستقل عن Content ID) فانقله **في عمود منفصل باسمه كما ورد**، ولا تخلطه بـContent ID؛ نحتاج الاثنين للمطابقة مع `meta_item_id` في عضوياتنا. انقل الأعمدة كما هي دون إعادة تسمية.
4. من صفحة الكتالوج (معلومات/إعدادات) انقل: عدد العناصر، تاريخ آخر تحديث، مصدر البيانات (feed/تحديث يدوي/API — كما تعرضه الواجهة)، وهل الكتالوج مربوط بأي حساب واتساب أو أصل آخر (قراءة فقط).
5. **لا تعدّل عنصرًا، لا تحذف، لا تربط الكتالوج، لا تنقر «ربط كتالوج»، لا تنشئ feed.**

**ما تعيده:** CSV كاملًا **نصًا داخل الرسالة** إضافةً إلى الإرفاق، ومعلومات البند 4 نصًا، وتأكيدًا بعدم تنفيذ أي ممنوع.

**ما سيُستخرج منه (للعلم):** لكل هوية من العشرين (`617350990-*`, `792574531-*`, `59407425-*`): غائبة → `create`؛ موجودة → **مطابقة تحتاج تحققًا** (لا تعديل حتى ق-5)؛ منتج Tenant 35 موجود بهوية مختلفة → `duplicate_risk`. **لا يُعلن `noop` من التصدير وحده**: يلزم إثبات مصدر النشر (ق-5) ثم مقارنة كل الحقول المزامَنة؛ ما لم يُقرأ (مثل الظهور) يبقى غير محسوم.

### ق-5 (**التالي — لوكيل Railway**) — قراءة قاعدة الإنتاج، SELECT فقط، تشغيل واحد

**السؤال الذي يحسمه:** لمن عناصر الكتالوج `871742015873294` الـ34 (التاجر)، وما **مصدر نشرها** المقبول، وأي متجر تعود إليه روابط `dev-cgcaqkpx5wgewsyv` مقارنةً بمتجر Tenant 35 الحالي. **ما لا يحسمه:** دعم API مع coexistence، الربط الفعلي، سبب رفض Graph، قبول Meta.

**السكربت المثبَّت:** `scripts/operators/catalog_q5_membership_readout.py` من الفرع `claude/nahla-product-catalog-sync-7nhht0` (PR #1193) **عند الالتزام الكامل** `c61ac8c5ef2340dff2fee69ef4613df402a2ed94`؛ SHA-256 للملف:

```text
1e4233db5b4516143541ffc2555a197e184a6078ab99af9fd484cbc9c4342545
```

```bash
# جلب الملف المثبَّت بعينه (قراءة فقط من المستودع)
git fetch origin c61ac8c5ef2340dff2fee69ef4613df402a2ed94
git show c61ac8c5ef2340dff2fee69ef4613df402a2ed94:scripts/operators/catalog_q5_membership_readout.py > catalog_q5_membership_readout.py
```

- **قراءة فقط:** جلسة `readonly` + `SET default_transaction_read_only = on` + مهلة 30 ثانية؛ كل عبارة تُفحص أنها تبدأ بـ`SELECT` قبل تنفيذها؛ المعاملة تُرجَع (`rollback`) في النهاية. لا يختار أي عمود رمز/سر ولا أي عمود JSON كاملًا (مفاتيح محددة فقط)، ويرفض الطباعة إن ظهر في الناتج شكل رمز أو DSN. لا Graph ولا سلة ولا شبكة سوى قاعدة البيانات. `DATABASE_URL` من بيئة الحاوية ولا يُطبع.
- **فحص مخطط مسبق (قراءة فقط):** يقرأ `information_schema.columns` و`alembic_version` أولًا. جدول غائب — **`catalog_channel_retirements` قد لا يوجد على الإنتاج قبل 0116** — أو عمود مطلوب غائب ⇒ يُتخطّى ذلك الاستعلام ويُسجَّل تحت `skipped` باسمه، وتستمر بقية القراءة؛ الأعمدة الاختيارية الغائبة تُسجَّل تحت `schema_preflight.columns_missing`. **لا يُنشئ جدولًا ولا يطبّق هجرة.**
- **التوافق:** Python 3.11 وpsycopg2 (كلاهما في صورة الإنتاج؛ لا تثبيت حزم)؛ SQL لـPostgreSQL ≥ 9.6؛ جُرّب على PostgreSQL 16 بوجود جدول السحب وبغيابه (اختبارات `backend/tests/test_catalog_q5_membership_readout.py`؛ حالتا PostgreSQL جُرّبتا محليًا على PostgreSQL 16 وتعملان حيث يتوفر `WA_CATALOG_SYNC_PG_TEST_DATABASE_URL`؛ إدراجهما في مهمة CI تعديل حوكمي على `ci.yml` يُقدَّم منفصلًا).
- **الروابط:** معرّفات سلة تُستخرج من الصيغتين `/p1207801870` و`/p/1207801870` (ورقم خالص في آخر المسار)؛ روابط نهلة العامة `…/public/catalog/items/nahla_p_<id>` **لا تُعدّ معرّفات سلة** وتُسجَّل هوياتها على حدة (`nahla_public_ids_from_links`).

#### ما تحتاجه
- `railway` مسجّل الدخول على مشروع الإنتاج (كما في الجزء أ)؛ **لا** `DATABASE_URL` ولا أي رمز في المحادثة.
- من تصدير ق-4 (نصًا) **ثلاثة ملفات لازمة كلها**: الـ34 **Content ID** (= `retailer_id`)، الروابط الـ34، و**معرّفات عناصر Meta الـ34** (العمود الرقمي المستقل عن Content ID). `--require-inputs` يرفض التشغيل بغياب أحدها.

#### ممنوعات صريحة
لا `UPDATE/INSERT/DELETE/CREATE/ALTER`، لا هجرة، لا `psql` تفاعلي حر، لا تشغيل ثانٍ للسكربت إلا إن فشل الأول قبل أي ناتج، لا تعديل للسكربت، لا Graph ولا سلة، لا ربط ولا إعدادات ولا صلاحيات، لا حذف أو نقل أو تعديل لأي عنصر أو منتج أو عضوية مهما كانت النتيجة.

#### التنفيذ

```bash
# 1) الملف من الالتزام c61ac8c5ef23 (أعلاه)، وتحقق البصمة قبل أي شيء
sha256sum catalog_q5_membership_readout.py
# يجب أن تكون: 1e4233db5b4516143541ffc2555a197e184a6078ab99af9fd484cbc9c4342545

# 2) ملفات المدخلات الثلاثة من تصدير ق-4 (نص فقط؛ سطر لكل قيمة؛ بلا رموز)
#    q4_content_ids.txt   ← عمود id / Content ID كما ورد (34 سطرًا)
#    q4_links.txt         ← عمود link كما ورد (34 سطرًا؛ روابط سلة وروابط نهلة معًا)
#    q4_meta_item_ids.txt ← عمود معرّف عنصر Meta الرقمي كما ورد (34 سطرًا)
wc -l q4_content_ids.txt q4_links.txt q4_meta_item_ids.txt     # 34 34 34

# 3) تحقق محلي بلا قاعدة: يطبع المدخلات المستخرجة (27 معرّف سلة، 7 هويات نهلة، معرّف المتجر) والعبارات
python3 catalog_q5_membership_readout.py --print-sql --require-inputs \
  --content-ids-file q4_content_ids.txt --links-file q4_links.txt \
  --meta-item-ids-file q4_meta_item_ids.txt --store-marker dev-cgcaqkpx5wgewsyv | head -40
# تحقق: "salla_link_count": 27 و"nahla_public_link_count": 7 و"link_external_ids" فيها 27 رقمًا و"store_marker": "dev-cgcaqkpx5wgewsyv"

# 4) انقل الملفات الأربعة إلى الحاوية ثم شغّل مرة واحدة
railway ssh --environment production --service nahla-saas -- bash -lc 'cat > /tmp/catalog_q5_membership_readout.py' < catalog_q5_membership_readout.py
railway ssh --environment production --service nahla-saas -- bash -lc 'cat > /tmp/q4_content_ids.txt' < q4_content_ids.txt
railway ssh --environment production --service nahla-saas -- bash -lc 'cat > /tmp/q4_links.txt' < q4_links.txt
railway ssh --environment production --service nahla-saas -- bash -lc 'cat > /tmp/q4_meta_item_ids.txt' < q4_meta_item_ids.txt

railway ssh --environment production --service nahla-saas -- bash -lc \
  'sha256sum /tmp/catalog_q5_membership_readout.py && cd /app && python /tmp/catalog_q5_membership_readout.py \
   --catalog-id 871742015873294 --tenant-id 35 --product-ids 176-182 \
   --content-ids-file /tmp/q4_content_ids.txt --links-file /tmp/q4_links.txt \
   --meta-item-ids-file /tmp/q4_meta_item_ids.txt \
   --store-marker dev-cgcaqkpx5wgewsyv --require-inputs --pretty' > q5_readout.json 2> q5_readout.stderr.txt
echo "exit=$?"
```

إن رفض `railway ssh` إعادة التوجيه من stdin: جلسة تفاعلية، ثم لصق الملفات الأربعة عبر heredoc إلى `/tmp/`، ثم `sha256sum` (يجب أن يطابق)، ثم أمر التشغيل نفسه.

#### التحقق قبل الإرسال
- أول سطر في stdout قبل JSON هو بصمة الملف داخل الحاوية وتطابق البصمة أعلاه.
- JSON صالح يحوي `"read_only": true` و`"secrets_included": false` و`"nothing_created_or_migrated": true`؛ تحت `results` حتى 17 مفتاحًا، وما تخطّاه الفحص المسبق مذكور بالاسم تحت `skipped` (يُتوقع `retirements_for_catalog: table_missing:catalog_channel_retirements` إن لم تُطبَّق 0116 بعد — **هذا ليس خطأً ولا يُعالج**).
- آخر سطر في stderr يبدأ بـ `q5-readout catalog=871742015873294 tenant=35` ويذكر `executed=` و`skipped=`.
- ابحث في الناتج عن `EAA` و`postgres://` و`postgresql://` و`access_token` ⇒ لا شيء منها (السكربت يرفض الطباعة أصلًا إن وُجدت).

#### ما تعيده كاملًا (نصًا داخل الرسالة)
1. `q5_readout.json` كاملًا. 2. `q5_readout.stderr.txt`. 3. ناتج `sha256sum` المحلي وداخل الحاوية. 4. ناتج الخطوة 3 (`--print-sql`) حتى سطر `statements`. 5. وقت التشغيل UTC. 6. تأكيد صريح بعدم تنفيذ أي ممنوع. **لا تفسير ولا حكم من جهتك؛** الجدول الذي يربط كل عنصر بدليله يُبنى من الناتج في التقرير (§7.0-هـ).

#### ما سيُقرأ من الناتج (للعلم)
- `schema_preflight` / `skipped`: ما وُجد وما غاب من جداول وأعمدة، ونسخة alembic؛ يُسجَّل في التقرير كما هو.
- `memberships_for_catalog` / `memberships_matching_q4_content_ids` / `memberships_matching_q4_meta_item_ids`: لكل عنصر — المستأجر، `provenance` (`salla_variant_push` = نشر مثبت لذلك المستأجر؛ `meta_graph_reconcile` = وجود فقط؛ غياب = غير مثبت)، ومطابقة `meta_item_id` بمعرّفات Meta من التصدير **منفصلةً** عن مطابقة Content ID.
- `products_by_id` / `variants_of_products` / `claims_on_nahla_identities`: مستأجر المنتجات 176–182 ومصدرها (`source`, `ownership_mode`, `external_id`, `imported_at`, `archived_at`) وأختامها، وهل تدّعي هوياتها جهة ثانية — **هذا هو الربط الذي يحوّل مؤشر `nahla_p_*` إلى حكم أو يبقيه غير محسوم.**
- `store_identity_*` / `store_marker_hits` / `products_matching_link_external_ids` / `tenants_involved`: أي مستأجر يحمل `dev-cgcaqkpx5wgewsyv` أو معرّفات سلة الـ27 المضمّنة في الروابط (بما فيها المنتجات المؤرشفة)، مقابل `store_id`/`store_url` الحاليين لـTenant 35.
- `whatsapp_connection_catalog_stamps` / `retirements_for_catalog` / `product_stamp_indicator_by_tenant`: مؤشرات فقط، ليست دليل نشر.

**إن أظهر الناتج عناصر لمستأجر آخر:** يتوقف اعتماد الكتالوج `871742015873294` للتجربة؛ **لا حذف ولا نقل ولا تعديل** لأي عنصر، ويُعرض الأمر على المالك.

### ق-3 (منجز جزئيًا) — Business Manager → المحفظة `2138142656950660` → Data sources → Catalogs

6. ~~هل يوجد كتالوج مملوك للمحفظة؟~~ **منجز:** `871742015873294`، 34 عنصرًا. **المتبقي من ق-3:** هل هو مربوط بأي حساب واتساب كما تعرضه صفحة الكتالوج في Business Manager (قراءة فقط)؟

### ملاحق (غير حاسمة)

7. **النقل الحرفي لجدول المقارنة** في صفحة «Onboard WhatsApp Business app users» بأعمدته وصفوفه (قُرئ على الصفحة وأُكِّد؛ يُرفق النص الحرفي للسجل).
8. فقرة دليل «Sell products and services» عن شروط ربط الكتالوج بالـ WABA والصلاحيات (غير حاسمة لـ ق-1؛ للسجل).

**الإخراج:** نص داخل الرسالة أولًا (ثم لقطات شاشة مرفقة) مع إخفاء أي أسرار (App Secret، رموز). **ما لا يُطلب:** تشغيل رابع للأداة، إعادة `GET /{waba}/product_catalogs`، أي قراءة سلة إضافية، أي تعديل.
