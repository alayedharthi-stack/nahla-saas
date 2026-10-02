# تكليف وكيل التشغيل — التشغيل الثاني (v2): قراءة Tenant 35 داخل حاوية Railway + قراءة لوحة Meta (قراءة فقط)

**الغرض:** استكمال ما لم يحسمه التشغيل الأول (20 منتجًا/130 متغيرًا، الاستحقاق متاح، `catalog_enabled=false`، `meta_catalog_id=null`، `catalog_management=missing`، الربط `unknown/missing_catalog_id`، المنتج 186 متناقض المخزون). الأداة v2 تقرأ كتالوجات الـ WABA مباشرة وتقارن مالكها بالمحفظة `2138142656950660`، وتفصل حقائق الصلاحية الخام عن تفسيرها، وتقيّم المنتجات 185/188/190 وتطبع هويات متغيراتها وحمولات نشرها، وتعيد قراءة المنتج 186 من سلة. **كل شيء قراءة فقط: لا كتابة في قاعدة البيانات، لا POST إلى Meta أو سلة، لا أسرار في الناتج. لا دمج، لا نشر، لا تغيير متغيرات أو إعدادات، لا هجرة، لا استبدال ملفات التطبيق.**

## الجزء أ — تشغيل الأداة v2 عبر `railway ssh`

### أ-1. ما تحتاجه

| البند | القيمة |
|---|---|
| مشروع Railway | `desirable-growth` (`f0090862-0a40-4293-bd5d-e94df58762b5`) |
| البيئة | `production` (`ede962ce-3042-4dae-94de-623837e83ed9`) |
| الخدمة | `nahla-saas` (`686b36c5-a926-4e58-912a-5e9d13fbc2e7`) |
| الصلاحية المطلوبة | `railway ssh` إلى هذه الخدمة فقط؛ لا تحتاج النشر أو تعديل المتغيرات ولا يجوز استخدامهما |
| **الملف المثبَّت** | `scripts/operators/catalog_trial_readout_standalone.py` من مستودع `alayedharthi-stack/nahla-saas`، فرع `claude/nahla-product-catalog-sync-7nhht0`، **عند الالتزام `6f269ec3f219eec3e3a97db319f21a58e0373d31` حصرًا** |
| **SHA-256 الكامل** | `16ab1a5e68e11a8425f6c4611de3f005787c50c38c3b3b1de349cd8d746edc2f` (الحجم 54050 بايت). إن اختلفت البصمة فلا تشغّل الملف وأبلغ |
| على جهازك | Python 3.9+ و Railway CLI مسجّل الدخول (`railway whoami`) |

> إصدارات سابقة من الملف (`18682b12…`، `c90f7d9e…`) انتهى دورها؛ لا تستخدمها.

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
# يجب أن تكون: 16ab1a5e68e11a8425f6c4611de3f005787c50c38c3b3b1de349cd8d746edc2f

# اطلب من الملف طباعة أمر railway ssh الذي يحمله ويشغّله بخيارات v2
python3 catalog_trial_readout_standalone.py --print-ssh-command \
  --tenant-id 35 --include-graph --include-salla \
  --candidate-ids 185,188,190 --expected-business-id 2138142656950660 > run_readout.sh
head -c 200 run_readout.sh   # يبدأ بـ: railway ssh --environment production --service nahla-saas -- bash -lc 'echo

bash run_readout.sh > tenant35_readout_v2.json 2> tenant35_readout_v2.stderr.txt
echo "exit=$?"
```

**ما يحدث داخل الحاوية (v2):** يُفكّ الملف من base64 إلى `/tmp/catalog_trial_readout_standalone.py`، ثم من `/app` يُنفَّذ:

```text
python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --include-salla \
  --candidate-ids 185,188,190 --expected-business-id 2138142656950660 --candidates 3 --pretty
```

يستخدم `DATABASE_URL` ورموز واتساب ومعرّف/سر التطبيق (`META_APP_ID`/`META_APP_SECRET` لاستدعاء `GET /debug_token` فقط) ورمز سلة، كلها من بيئة الحاوية دون أن تخرج منها. القراءات: قاعدة البيانات (SELECT فقط)؛ Meta: `GET /{waba}/product_catalogs`، `GET /{waba}?fields=owner_business_info`، `GET /me/permissions`، `GET /debug_token`، `GET /{catalog}?fields=…` لكل كتالوج مرتبط، `GET /{catalog}/products` لفحص وجود هويات المرشحين؛ سلة: `GET /products/{id}` و`GET /products/{id}/variants` للمنتجات الشاذة فقط (المنتج 186)؛ وقراءة ملف الكود المنشور `/app/backend/routers/whatsapp_embedded.py` لاستخراج النطاق الصريح (قراءة فقط).

**بديل إن رفض `railway ssh` طول الأمر:** جلسة تفاعلية `railway ssh --environment production --service nahla-saas`، ثم لصق الملف إلى `/tmp/catalog_trial_readout_standalone.py` عبر heredoc، ثم `sha256sum` (يجب أن يطابق البصمة الكاملة أعلاه)، ثم `cd /app && python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --include-salla --candidate-ids 185,188,190 --expected-business-id 2138142656950660 --pretty`.

### أ-4. التحقق قبل الإرسال

- JSON صالح يحوي `"tenant_id": 35`، `"read_only": true`، `"graph_reads_included": true`، `"secrets_included": false`.
- ابحث في الملف عن `EAA` و`postgres://` و`postgresql://` و`access_token` و`app_token` ⇒ يجب ألا يوجد أي منها. إن وُجد، لا ترسل وأبلغ.
- آخر سطر في stderr يبدأ بـ `trial-readout tenant=35`.

### أ-5. ما تعيده كاملًا

1. `tenant35_readout_v2.json` كاملًا. 2. `tenant35_readout_v2.stderr.txt` بعد التنقية. 3. ناتج `railway status`. 4. أول 200 حرف من الأمر المشغَّل ووقت التشغيل UTC. 5. تأكيد صريح بعدم تنفيذ أي ممنوع.

### أ-6. ما سيُقرأ من الناتج (للعلم)

`graph.waba_catalogs` (الكتالوجات المرتبطة فعلًا بالـ WABA `1682673239554563` بلا اشتراط معرّف محلي) و`graph.waba_owner_business.matches_expected` و`graph.catalog_business_matches_waba_owner`؛ `graph.token_catalog_management.raw` (`me_permissions.catalog_management_status`، `debug_token.scopes`، `explicit_oauth_scope_in_deployed_code`، `embedded_signup_config.config_id_present`) و`.interpretation` (`catalog_management_on_token`، `possible_causes`، `needs_manual_reads`)؛ `trial_candidates.preferred_evaluation` و`scenario_coverage`؛ `candidate_payloads.items` (هويات المتغيرات وحمولاتها)؛ `graph.live_items.against_linked_catalogs`؛ `salla_check.checked[].verdict` للمنتج 186؛ `missing_requirements`.

## الجزء ب — طلب منفصل: قراءة لوحة تطبيق Meta (قراءة فقط، بلا أي تعديل)

لا يمكن قراءة هذه البنود عبر Graph برمز تاجر، وهي حاكمة في تفسير غياب `catalog_management`. المطلوب قراءة ونقل فقط؛ **لا تغيّر أي إعداد أو صلاحية أو وضع تطبيق، ولا تقدّم طلب مراجعة.**

1. **إعداد Embedded Signup المرتبط بـ `config_id`:** في لوحة التطبيق → WhatsApp → Embedded Signup → Configurations. معرّف الإعداد المستخدم هو قيمة المتغير `META_EMBEDDED_SIGNUP_CONFIG_ID` (أو الاسم القديم `META_WA_CONFIG_ID`) في خدمة `nahla-saas`؛ يكفي مطابقة **آخر 4 أرقام** مع `graph.token_catalog_management.raw.embedded_signup_config.config_id_tail` في ناتج الأداة، دون نقل المعرّف كاملًا إن لم يلزم. انقل: اسم الإعداد، الصلاحيات/الأصول التي يطلبها (هل يتضمن إدارة الكتالوج/Commerce؟)، ونوع التدفق (WhatsApp فقط أم WhatsApp + Commerce).
2. **مستوى وصول الصلاحيات:** App Review → Permissions and Features → لكل من `catalog_management` و`business_management` و`whatsapp_business_management` و`whatsapp_business_messaging`: الحالة (Standard Access / Advanced Access / غير مضافة)، وحالة طلب المراجعة إن وُجد (Pending / Approved / Rejected مع التاريخ).
3. **حالة التطبيق:** App Mode (Development / Live)، وحالة Business Verification للمحفظة المالكة للتطبيق.
4. **دور مشغّل Tenant 35 في التطبيق:** هل حساب Meta الذي ربط واتساب لـ Tenant 35 له دور في التطبيق (Admin / Developer / Tester) أم لا دور. يحدد هذا ما إذا كان Standard Access كافيًا لتجربة 35.
5. **المحفظة `2138142656950660`:** في Business Manager → الإعدادات → الحسابات → الكتالوجات: هل يوجد كتالوج مملوك لها؟ اسمه ومعرّفه، وهل هو مربوط بحساب واتساب `1682673239554563`؟ (قراءة فقط؛ لا إنشاء ولا ربط.)

**الإخراج:** نص أو لقطات شاشة مع إخفاء أي أسرار (App Secret، رموز). يُرفق مع ناتج الجزء أ.
