# تكليف وكيل التشغيل: قراءة Tenant 35 داخل حاوية Railway (قراءة فقط)

**الغرض:** تشغيل أداة قراءة مستقلة داخل حاوية خدمة `nahla-saas` في بيئة `production` على Railway، لإخراج تقرير JSON عن جاهزية Tenant 35 لتجربة كتالوج واتساب. الأداة لا تكتب في قاعدة البيانات، ولا ترسل إلى Meta إلا طلبات GET، ولا تطبع أي رمز أو سر. **لا دمج، لا نشر، لا تغيير متغيرات، لا هجرة، لا استبدال ملفات التطبيق.**

## 1. ما تحتاجه

| البند | القيمة |
|---|---|
| مشروع Railway | `desirable-growth` (`f0090862-0a40-4293-bd5d-e94df58762b5`) |
| البيئة | `production` (`ede962ce-3042-4dae-94de-623837e83ed9`) |
| الخدمة | `nahla-saas` (`686b36c5-a926-4e58-912a-5e9d13fbc2e7`) |
| الصلاحية المطلوبة | حساب Railway يستطيع `railway ssh` إلى هذه الخدمة (قراءة/تشغيل أوامر داخل الحاوية). **لا تحتاج** صلاحية تعديل المتغيرات أو النشر، ولا يجوز استخدامها إن وُجدت |
| الملف | `scripts/operators/catalog_trial_readout_standalone.py` من فرع `claude/nahla-product-catalog-sync-7nhht0` في مستودع `alayedharthi-stack/nahla-saas` من آخر التزام على الفرع (v2) |
| تحقق سلامة الملف | **v2:** `sha256sum` يبدأ بـ `c90f7d9ed52d484e`، الحجم 46735 بايت (الإصدار الأول `18682b12…` انتهى دوره) |
| على جهازك | Python 3.9+ و Railway CLI مسجّل الدخول (`railway whoami`) |

> إن لم يُتَح لك قراءة المستودع، اطلب من المالك إرسال الملف نفسه؛ لا تعدّل محتواه.

## 2. ممنوعات صريحة

- لا `railway variables --set`، لا `railway up`، لا `railway redeploy`، لا `railway run` (يُحمّل الأسرار إلى جهازك)، لا تعديل أي خدمة أو متغير.
- لا تطبع ولا تنقل أي قيمة من متغيرات البيئة (`env`, `printenv`, `echo $DATABASE_URL`, …). لا تفتح `psql` ولا تتصل بقاعدة البيانات مباشرة.
- لا تحذف ولا تعدّل ملفات تحت `/app`. الملف يُكتب في `/tmp` فقط.
- لا تشغّل الأداة بمعرّف مستأجر غير 35.
- إن ظهر أي خطأ، انقل الـtraceback بعد حذف أي قيمة تشبه رمزًا أو رابط اتصال.

## 3. خطوات التنفيذ (الطريقة المفضّلة: الملف يُنقل مشفَّرًا base64 داخل أمر واحد)

```bash
# 0) تأكيد الهوية والخدمة (قراءة فقط)
railway whoami
railway status

# 1) الملف محليًا (بعد الحصول عليه)، تحقق من سلامته
sha256sum catalog_trial_readout_standalone.py      # v2 يبدأ بـ c90f7d9ed52d484e

# 2) اطلب من الملف نفسه طباعة أمر railway ssh الذي يحمله ويشغّله من /app داخل الحاوية
python3 catalog_trial_readout_standalone.py --print-ssh-command \
  --tenant-id 35 --include-graph --include-salla \
  --candidate-ids 185,188,190 --expected-business-id 2138142656950660 > run_readout.sh
head -c 200 run_readout.sh      # يجب أن يبدأ بـ: railway ssh --environment production --service nahla-saas -- bash -lc 'echo

# 3) شغّل الأمر المطبوع واحفظ الناتج
bash run_readout.sh > tenant35_readout.json 2> tenant35_readout.stderr.txt
echo "exit=$?"
```

ما يحدث داخل الحاوية: `echo <base64> | base64 -d > /tmp/catalog_trial_readout_standalone.py && cd /app && python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --candidates 3 --pretty`. يستخدم `DATABASE_URL` ورموز واتساب من بيئة الحاوية نفسها دون أن تخرج منها.

**بديل إن رفض `railway ssh` طول الأمر:** افتح جلسة تفاعلية `railway ssh --environment production --service nahla-saas`، ثم:
```bash
cat > /tmp/catalog_trial_readout_standalone.py <<'PYEOF'
# (الصق محتوى الملف كاملًا هنا)
PYEOF
sha256sum /tmp/catalog_trial_readout_standalone.py   # v2 يبدأ بـ c90f7d9ed52d484e
cd /app && python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph --include-salla --candidate-ids 185,188,190 --expected-business-id 2138142656950660 --pretty
```
وانسخ ناتج JSON من الطرفية.

## 4. التحقق من الناتج قبل الإرسال

- `tenant35_readout.json` هو JSON صالح، يحوي `"tenant_id": 35`، `"read_only": true`، `"graph_reads_included": true`، `"secrets_included": false`.
- ابحث في الملف عن `EAA` و`postgres://` و`postgresql://` و`access_token` ⇒ يجب ألا يوجد أي منها (الأداة تحذفها؛ هذا تحقق مزدوج). إن وُجد شيء، لا ترسله وأبلغ.
- السطر الأخير في `stderr` يبدأ بـ `trial-readout tenant=35 products=… eligible=… missing=…`.

## 5. ما تعيده إلى المالك/الوكيل المنفّذ (كاملًا، بلا اختصار)

1. محتوى `tenant35_readout.json` كاملًا.
2. محتوى `tenant35_readout.stderr.txt` (بعد حذف أي قيمة سرية إن ظهرت في traceback).
3. ناتج `railway status` (اسم المشروع/البيئة/الخدمة ورقم النشر الحالي).
4. الأمر الذي شُغِّل بالضبط (دون الـbase64 نفسه؛ يكفي الـ200 حرفًا الأولى) ووقت التشغيل UTC.
5. تأكيد صريح: لم يُنفَّذ أي أمر من ممنوعات القسم 2.

## 6. ما الجديد في التشغيل الثاني (v2) ولماذا

التشغيل الأول أثبت: 20 منتجًا/130 متغيرًا، الاستحقاق متاح، `catalog_enabled=false`، `meta_catalog_id=null`، `catalog_management=missing`، لكنه لم يحسم ربط الكتالوج (أعاد `missing_catalog_id` لغياب معرّف محلي) واختار منتجًا متناقض المخزون (186). v2 يقرأ كتالوجات الـ WABA مباشرة ويقارن مالكها بالمحفظة `2138142656950660`، يطبع الحالة الخام للصلاحية، يقيّم المنتجات 185/188/190 ويطبع هويات متغيراتها وحمولات نشرها، يفحص وجودها في أي كتالوج مرتبط، ويعيد قراءة المنتج 186 من سلة (GET فقط) لتحديد موضع التناقض. **لا يزال قراءة فقط.**

## 7. ما سيُقرأ من الناتج (للعلم، لا يلزم تحليله)

- `products[]`: المعرّفات المحلية `product_id` والخارجية `external_id`، المتغيرات وهوياتها `retailer_id`، `anomalies` (يُفحص فيها المنتج المحلي 183).
- `connection`: `catalog_enabled`، `meta_catalog_id`، مصدر الرمز بلا قيمته.
- `graph.waba_catalogs` (الكتالوجات المرتبطة فعلًا بالـ WABA `1682673239554563` بلا اشتراط معرّف محلي)؛ `graph.waba_owner_business.matches_expected`؛ `graph.catalogs` و`graph.catalog_business_matches_waba_owner`؛ `graph.token_catalog_management` (`catalog_management_status`: `absent`/`declined`/`granted` و`listed_permissions`)؛ `graph.live_items.against_linked_catalogs` (أي هوية مرشحة موجودة/غائبة في كل كتالوج مرتبط)؛ `candidate_payloads.items`؛ `salla_check.checked[].verdict`.
- `entitlement.meta_catalog_sync`، `readiness.blocker_code`، `trial_candidates.selected` و`proposed_env`، و`missing_requirements`.

**الخطأ المتوقع إن كان الكتالوج غير مفعّل:** `readiness.blocker_code = "catalog_disabled"` مع بقاء قراءات Graph تعمل؛ هذا ليس فشلًا في الأداة.
