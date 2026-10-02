# تكليف وكيل التشغيل — التشغيل الثالث (v3): قراءة Tenant 35 داخل حاوية Railway + قراءة لوحة Meta/WhatsApp Manager بعد تسجيل دخول المالك (قراءة فقط)

**الغرض:** التشغيل الثاني (v2) أعاد: `GET /{waba}/product_catalogs` → HTTP 400 كود 10 «This operation can not be performed on SMB business type» (الربط **غير مثبت**، لا «لا كتالوج»)، المنتج 186 متناقض في سلة نفسها (الأب 1، خمسة متغيرات × 0)، المنتجات 185/188/190 = 20 متغيرًا (8 متوفرة، 12 نافدة) اجتازت **الفحص المحلي** فقط، ولوحة Meta لم تُقرأ (طلبت تسجيل الدخول). الأداة v3 تصحح تفسير فشل القراءة، وتقرأ coexistence من الرقم، وتسمّي قيد نوع المحفظة منفصلًا عن الصلاحية، وتقارن متغيرات المرشحين الثلاثة بسلة (سعر/توفر/تسمية خيار)، وتطبع تحذيرات الحمولات بأسبابها. **كل شيء قراءة فقط: لا كتابة في قاعدة البيانات، لا POST إلى Meta أو سلة، لا أسرار في الناتج. لا دمج، لا نشر، لا تغيير متغيرات أو إعدادات أو صلاحيات، لا هجرة، لا استبدال ملفات التطبيق، لا تغيير لوضع coexistence.**

> **تسليم الملفات:** ملفات التشغيل الثاني لم تصل إلى بيئة التحليل. أعد هذه المرة الملفات **نصًا داخل الرسالة** (JSON كاملًا) إضافةً إلى الإرفاق.

## الجزء أ — تشغيل الأداة v3 عبر `railway ssh`

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

## الجزء ب — طلب منفصل: قراءة لوحة تطبيق Meta وWhatsApp Manager **بعد تسجيل دخول المالك** (قراءة فقط، بلا أي تعديل)

في التشغيل الثاني طلبت الصفحة تسجيل الدخول ولم يُقرأ أي بند؛ **كل البنود أدناه ما زالت «غير معروفة»**. المالك يسجّل الدخول بحسابه ثم ينفّذ المشغّل القراءة أمامه أو ينفّذها المالك بنفسه. لا يمكن قراءة هذه البنود عبر Graph برمز تاجر، وهي حاكمة في تفسير غياب `catalog_management` **وفي حسم ما إذا كان WABA `1682673239554563` يقبل كتالوج API أصلًا** (خطأ «SMB business type» يثبت رفض العملية الحالية فقط، لا السبب النهائي ولا كل المسارات). المطلوب قراءة ونقل فقط؛ **لا تغيّر أي إعداد أو صلاحية أو وضع تطبيق، ولا تقدّم طلب مراجعة، ولا تربط أو تنشئ كتالوجًا، ولا تلمس اتصال واتساب أو وضع coexistence.**

### ب-1. لوحة التطبيق (developers.facebook.com → التطبيق)

1. **إعداد Embedded Signup المرتبط بـ `config_id`:** WhatsApp → Embedded Signup → Configurations. معرّف الإعداد المستخدم هو قيمة المتغير `META_EMBEDDED_SIGNUP_CONFIG_ID` (أو الاسم القديم `META_WA_CONFIG_ID`) في خدمة `nahla-saas`؛ يكفي مطابقة **آخر 4 أرقام** مع `graph.token_catalog_management.raw.embedded_signup_config.config_id_tail` في ناتج الأداة، دون نقل المعرّف كاملًا إن لم يلزم. انقل: اسم الإعداد، الصلاحيات/الأصول التي يطلبها (هل يتضمن إدارة الكتالوج/Commerce؟)، ونوع التدفق (WhatsApp فقط أم WhatsApp + Commerce)، وهل هو إعداد coexistence (يسمح بالرقم الموجود على تطبيق WhatsApp Business).
2. **مستوى وصول الصلاحيات:** App Review → Permissions and Features → لكل من `catalog_management` و`business_management` و`whatsapp_business_management` و`whatsapp_business_messaging`: الحالة (Standard Access / Advanced Access / غير مضافة)، وحالة طلب المراجعة إن وُجد (Pending / Approved / Rejected مع التاريخ).
3. **حالة التطبيق:** App Mode (Development / Live)، وحالة Business Verification للمحفظة المالكة للتطبيق.
4. **دور مشغّل Tenant 35 في التطبيق:** هل حساب Meta الذي ربط واتساب لـ Tenant 35 له دور في التطبيق (Admin / Developer / Tester) أم لا دور. يحدد هذا ما إذا كان Standard Access كافيًا لتجربة 35.

### ب-2. WhatsApp Manager وBusiness Manager (business.facebook.com)

5. **محفظة الـ WABA `1682673239554563`:** في WhatsApp Manager → Account tools / Overview: اسم ومعرّف محفظة الأعمال المالكة للحساب، وهل هي المحفظة `2138142656950660` أم محفظة أُنشئت تلقائيًا عند الربط من تطبيق WhatsApp Business (تظهر عادةً باسم المتجر دون توثيق). انقل ما تعرضه الصفحة حرفيًا عن «نوع» المحفظة أو حالتها.
6. **تبويب الكتالوج للـ WABA:** في WhatsApp Manager → Catalog (أو Commerce): هل يعرض خيار «Connect catalog» من Commerce Manager لهذا الحساب، أم يعرض كتالوج تطبيق WhatsApp Business، أم رسالة بأن الكتالوج غير متاح؟ **اقرأ فقط ولا تنقر «ربط».**
7. **المحفظة `2138142656950660`:** Business settings → Data sources → Catalogs: هل يوجد كتالوج مملوك لها؟ اسمه ومعرّفه وعدد عناصره، وهل هو مربوط بحساب واتساب (أي حساب)؟ (قراءة فقط.)
8. **كتالوج التطبيق:** إن كان رقم Tenant 35 على تطبيق WhatsApp Business: هل يوجد كتالوج داخل التطبيق؟ عدد عناصره فقط (قراءة من هاتف التاجر بإذنه، دون تعديل).

### ب-3. نص الوثائق الرسمية (للتحقق من مقتطفات البحث — نقل حرفي، لا تلخيص)

9. افتح صفحة Meta الرسمية «Onboard WhatsApp Business app users» (Embedded Signup → coexistence). انقل **جدول مقارنة الميزات حرفيًا بأعمدته** (المتوقع حسب مقتطف البحث: *Feature* / *Changes to the WhatsApp Business app feature after onboarding to Cloud API* / *WhatsApp Business app feature supported on Cloud API?* — انقل الأعمدة كما هي حتى لو اختلفت) **وكل صفوفه**، وخاصةً صف *Business tools (catalog, orders, status)* بقيمتَي عموديه. ثم انقل **قسم Limitations** حرفيًا إن وُجد، وبيّن **هل** الجملة «…business tools such as the catalog are not supported once a number is running Coexistence» موجودة فيه نصًا أم لا (لا تُنسب إليه قبل التحقق).
10. افتح دليل «Sell products and services» (Cloud API) وانقل الفقرة التي تحدد شروط ربط الكتالوج بالـ WABA والصلاحيات المطلوبة، وأي ذكر لنوع محفظة الأعمال أو لـcoexistence.

**الإخراج:** نص أو لقطات شاشة مع إخفاء أي أسرار (App Secret، رموز). يُرفق مع ناتج الجزء أ، **ويُنقل نصًا داخل الرسالة أيضًا** (JSON الجزء أ كاملًا، ونص الجزء ب) لتجاوز مشكلة وصول الملفات.
