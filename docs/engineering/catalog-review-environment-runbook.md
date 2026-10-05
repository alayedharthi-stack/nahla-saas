# بيئة مراجعة `catalog_management` المعزولة — دليل التشغيل (للمراجعة؛ لا شيء منه مُنفَّذ)

**الغرض:** تشغيل فرع الكتالوج (PR #1193) في بيئة معزولة تمامًا عن الإنتاج لإثبات رحلة
المراجعة (دخول → ربط → إعداد الكتالوج → منتج يدوي → تأكيد النشر → الحالة) على كتالوج اختبار
فارغ تحت محفظة مالك التطبيق `248365378024448`، كما خُطِّط في تقرير الكتالوج §7.10.

**الحزمة المقترحة (لم تُنشأ):** `nahla-catalog-review-api`، `nahla-catalog-review-web`،
`postgres-catalog-review` داخل مشروع Railway `desirable-growth` / بيئة `staging`.
العنوانان المقترحان `catalog-review.nahlah.ai` و`api.catalog-review.nahlah.ai` لم يُجهَّزا بعد.

> هذا الملف يصف **ما سيُعدّ وكيف يُتحقق منه**. إنشاء الموارد والنشر وتوليد الرموز وتعديل
> إعدادات Meta قرارات للمالك بموافقة مستقلة لكل بند؛ لا يُنفَّذ أي منها من الكود أو CI.

---

## 1. ما يضيفه فرع `claude/catalog-review-env-isolation` (كود فقط؛ لا موارد)

| البند | الملف | السلوك |
|---|---|---|
| حارس عزل الخادم | `backend/core/review_environment.py` | في وضع المراجعة يرفض الإقلاع/الهجرات/العمال ما لم تُثبت الهوية وربط القاعدة **وعلامة هوية القاعدة** وعنوان اللوحة. خامل خارج وضع المراجعة. لا يطبع DSN ولا أسرارًا |
| ربط الحارس | `scripts/preflight_check.py` (قبل uvicorn، لأي `ENVIRONMENT`؛ `NAHLA_SKIP_PREFLIGHT` **يُتجاهَل** في وضع المراجعة — `start.sh`)، `backend/main.py` (نقطة اختناق واحدة في رأس `on_startup` تقرأ علامة القاعدة وترفع استثناءً فيُنهي uvicorn الإقلاع قبل `create_all` وإصلاحات الأعمدة والمسوحات والعمال والطلبات؛ إضافةً إلى الخطوة 0 في سلسلة الهجرات وبوابة `_start`) | فشل الحارس ⇒ لا منفذ، لا `alembic`، لا `create_all`، لا عامل، لا طلب |
| سياسة عنوان API في اللوحة | `dashboard/src/lib/reviewEnvironmentPolicy.ts` (+ `reviewEnvironment.ts`) | في وضع المراجعة: غياب العنوان الصريح أو توجيهه إلى API الإنتاج أو `localhost` أو بغير https ⇒ **فشل البناء** (`vite.config.ts`) **وفشل التشغيل** (`auth.ts` يرمي؛ `main.tsx` يعرض شاشة فشل ثابتة بدل التطبيق؛ لا تجاوز من localStorage) |
| فحص CI للسياسة | `dashboard/scripts/check-review-env-api-base.mts` (ضمن `npm run check:platform-policy`) | جدول حالات + تحقق من الربط في البناء والتشغيل |
| روابط الدعوة | `backend/routers/admin.py` | `invite_url` من `DASHBOARD_URL` (الافتراضي الإنتاجي `https://app.nahlah.ai` بلا تغيير). روابط التحقق واستعادة كلمة المرور في `auth.py` كانت تستخدم `DASHBOARD_URL` أصلًا |
| مشغّل إعداد الاتصال | `scripts/operators/catalog_review_env_connection_setup.py` | محصور بوضع المراجعة؛ يتوقف إن لم يثبت العزل؛ الرمز من متغير بيئة أو stdin ويُخزَّن مشفَّرًا بمسار `wa_connection_secrets.store_access_token`؛ تجربة جافة افتراضيًا؛ **لا يُشغَّل الآن** |
| الإنجليزية في رحلة العرض | `authFlow` في `i18n/{types,en,ar}.ts`، `catalogRuntimeLabels.ts`، `apiErrorLabels.ts` | صفحات الدعوة/التحقق/تعيين واستعادة كلمة المرور عبر `useLanguage`؛ معوّقات المزامنة ومشكلات المعاينة والتأكيد تُحلّ من **رموزها** إلى تسميات ثابتة (ويبقى نص الخادم العربي عند رمز غير معروف)؛ أخطاء النقل في عميل API حسب اللغة المحفوظة. اللغة الافتراضية للإنتاج (`ar`) والوكيل بلا تغيير |

---

## 2. المتغيرات (أسماء فقط؛ القيم تُضبط في Railway على خدمات المراجعة وحدها)

**خدمة API (`nahla-catalog-review-api`):**

| المتغير | الغرض |
|---|---|
| `NAHLA_CATALOG_REVIEW_ENV` | `1` يفعّل الحارس |
| `DATABASE_URL` | قاعدة `postgres-catalog-review` فقط |
| `NAHLA_CATALOG_REVIEW_DB_HOST` | المضيف المتوقع (الافتراضي `postgres-catalog-review.railway.internal`) |
| `NAHLA_CATALOG_REVIEW_DB_NAME` | اختياري: اسم القاعدة المتوقع |
| `NAHLA_CATALOG_REVIEW_DB_MARKER` | اختياري: قيمة علامة الهوية (الافتراضي `catalog-review`) |
| `NAHLA_CATALOG_REVIEW_PROJECT` / `NAHLA_CATALOG_REVIEW_ENVIRONMENT` | اختياري: هوية Railway المتوقعة (الافتراضي `desirable-growth` / `staging`؛ `RAILWAY_PROJECT_NAME` و`RAILWAY_ENVIRONMENT_NAME` تضبطهما Railway) |
| `ENVIRONMENT` | `staging` (أي قيمة إنتاجية تُرفض) |
| `DASHBOARD_URL` | `https://catalog-review.nahlah.ai` (الافتراضي الإنتاجي يُرفض) |
| `META_APP_ID`, `META_APP_SECRET`, `META_EMBEDDED_SIGNUP_CONFIG_ID`, `META_REDIRECT_URI` | التطبيق نفسه؛ `META_EMBEDDED_SIGNUP_CONFIG_ID` يشير إلى **إعداد Embedded Signup الثاني** الخاص بالعرض (قرار ن-8) إن اعتُمد |
| `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `WHATSAPP_VERIFY_TOKEN`, `TOTP_ENC_KEY`, `WA_TOKEN_ENC_KEY` | أسرار الخدمة الخاصة بالبيئة (قيم جديدة؛ لا تُنسخ من الإنتاج) |
| `NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS` | معرّف مستأجر الاختبار في قاعدة المراجعة |
| `NAHLA_WHATSAPP_CATALOG_AUTO_SYNC` | `1` بعد نجاح التحقق الذاتي للرمز فقط |
| `NAHLA_AUTO_CATALOG_ONBOARDING` | غير مضبوط (أو `1` في العرض فقط إن اعتُمد عرض إنشاء الكتالوج من المنصة — ف-3أ) |
| `CORS_ORIGINS` | غير لازم: `CORS_ORIGIN_REGEX` الافتراضي يغطي `https://catalog-review.nahlah.ai` |

**خدمة الواجهة (`nahla-catalog-review-web`، متغيرات بناء):**

| المتغير | الغرض |
|---|---|
| `VITE_NAHLA_CATALOG_REVIEW_ENV` | `1` يفعّل السياسة (بناءً وتشغيلًا) |
| `VITE_API_BASE` | `https://api.catalog-review.nahlah.ai` (غيابه أو توجيهه إلى `api.nahlah.ai` يفشل البناء). مع nixpacks تُصدَّر متغيرات الخدمة إلى البناء تلقائيًا؛ مع `Dockerfile.dashboard` يلزم تمرير `--build-arg VITE_NAHLA_CATALOG_REVIEW_ENV=1 --build-arg VITE_API_BASE=…` |

**المشغّل (داخل حاوية API فقط):** `NAHLA_CATALOG_REVIEW_WA_TOKEN` (أو `--token-stdin`)،
`NAHLA_CATALOG_REVIEW_CONNECTION_WRITE_CONFIRM=RUN_CATALOG_REVIEW_CONNECTION_WRITE` مع `--write`؛ ويتوقف إن غاب `WA_TOKEN_ENC_KEY` (لا يُشفَّر الرمز الحقيقي بمفاتيح التطوير الاحتياطية). التجربة الجافة تعمل بلا رمز.

---

## 3. هوية قاعدة الاختبار — التحقق العملي قبل الهجرات والعمال

وجود `DATABASE_URL` ليس دليلًا. الحارس يثبت ثلاث طبقات، كلها بلا طباعة للقيم:

1. **هوية الخدمة:** `RAILWAY_PROJECT_NAME=desirable-growth` و`RAILWAY_ENVIRONMENT_NAME=staging` ولا علامة إنتاج في `ENVIRONMENT`.
2. **ربط القاعدة (ثابت من DSN):** مخطط PostgreSQL، المضيف يساوي `NAHLA_CATALOG_REVIEW_DB_HOST`، لا `postgres-staging` ولا علامة إنتاج في المضيف، واسم القاعدة إن ضُبط؛ **تُرفض معاملات الاستعلام في DSN عدا `sslmode`/`sslrootcert`/`sslcert`/`sslkey`/`connect_timeout`/`application_name`** (لأن `?host=`/`?hostaddr=`/`?options=`/`?service=` تعيد توجيه الاتصال أو تزوّر العلامة)، **وتُرفض متغيرات libpq** `PGHOST`/`PGHOSTADDR`/`PGPORT`/`PGSERVICE`/`PGSERVICEFILE`/`PGOPTIONS`/`PGPASSFILE`/`PGDATABASE` إن كانت مضبوطة.
3. **علامة هوية القاعدة (حي، قراءة واحدة):** `SELECT current_setting('nahla.environment', true), <قيد pg_db_role_setting لقاعدة current_database() مع setrole = 0>, current_database()` — يلزم **قيد محفوظ على مستوى القاعدة** في `pg_db_role_setting` (هو ما يكتبه `ALTER DATABASE … SET`) قيمته `catalog-review`، و**القيمة الفعّالة** مساوية له (تجاوز على مستوى الدور أو الجلسة يُرفض)، واسم القاعدة المتصلة يساوي اسمها في DSN (أو `NAHLA_CATALOG_REVIEW_DB_NAME`). قيمة جلسة وحدها (`SET`/`options=-c`/`PGOPTIONS`) لا تترك قيدًا وتُرفض. **لماذا لا `pg_settings`:** PostgreSQL يحفظ الإعداد المخصص غير المسجَّل كعنصر نائب ولا يُدرجه في `pg_settings` (ثُبت على 16.15 و18.6: صفر صفوف رغم تطبيق الإعداد).

   **قبل العبارة، تحقّق من الخادم المتصل:** قاعدة الإنتاج اسمها أيضًا `railway` ومستخدم Railway `postgres` مستخدم فائق، فالعبارة نفسها في جلسة psql خاطئة تعلّم الإنتاج. نفّذ أولًا في الجلسة نفسها `SELECT inet_server_addr(), inet_server_port(), current_database(), version();` وطابق المضيف مع خدمة `postgres-catalog-review` في Railway (لا مع `Postgres` أو `nahla-postgres-prod`)، ثم نفّذ العبارة، ثم شغّل `scripts/preflight_check.py` من خدمة المراجعة لتأكيد القبول. على PostgreSQL 15+ لا يستطيع إلا المستخدم الفائق ضبط هذا الإعداد على مستوى القاعدة (حتى مالك القاعدة يُرفض)، فالعلامة دليل على إجراء مشغّل مقصود، لا برهان تشفيري.

   ```sql
   ALTER DATABASE railway SET nahla.environment = 'catalog-review';
   ```

   أي قاعدة لم تُعلَّم (الإنتاج، `postgres-staging`، قاعدة محلية) تُعيد NULL — أو قيمة فارغة `''` إن سبق `ALTER SYSTEM` على هذا الاسم — وكلاهما سواء ⇒ رفض قبل `alembic`/`create_all`. لا جدول، لا هجرة، تبقى بعد الهجرات، وتسري على الاتصالات الجديدة.

---

## 4. أمر التحقق قبل النشر (داخل حاوية خدمة API للمراجعة؛ قراءة فقط)

```bash
python /app/scripts/preflight_check.py
```

المخرجات المتوقعة: أسطر `[preflight][review-env]` تذكر المضيف والعلامة والهوية **المتوقعة** ثم
`isolation verified: identity, database binding, persisted database marker (pg_db_role_setting), dashboard URL.` ورمز خروج `0`.
أي `[review-env][FAIL] <code>` يعني رفض الإقلاع؛ الرموز: `review_project_*`، `review_environment_*`،
`production_marker_detected`، `database_url_*`, `database_scheme_rejected`, `database_host_*`,
`database_name_mismatch`, `database_url_query_rejected`, `libpq_environment_override`, `database_marker_{unreadable,missing,mismatch}`, `database_marker_not_persisted`, `database_marker_effective_mismatch`, `database_identity_mismatch`, `dashboard_url_*`.

وللواجهة محليًا قبل البناء:

```bash
cd dashboard && npm run check:review-env-api-base
```

---

## 5. ترتيب الإعداد المقترح (كل بند بموافقة؛ لا شيء منه مُنفَّذ)

1. إنشاء `postgres-catalog-review` (جديد، فارغ) ثم تعليمه بعبارة `ALTER DATABASE` أعلاه.
   **قبل أول إقلاع للـAPI يجب تجهيز المخطط بخطوة مشغّل مستقلة بموافقة**، بالترتيب: (أ) `scripts/preflight_check.py` بمتغيرات خدمة المراجعة نفسها — يثبت أن DSN يشير إلى القاعدة المعلَّمة؛ لا خطوة مخطط قبله. (ب) من `database/` وبالمتغيرات نفسها: `python -m alembic upgrade 0111` ثم `python -m alembic upgrade 0113` — رأسا التطبيق في عقد الإقلاع (`APPLICATION_ALEMBIC_HEAD`، `NAVIGATION_ALEMBIC_HEAD`)، وهما ما يحمله الإنتاج اليوم؛ يُعاد التأكد منهما مقابل الإنتاج وقت التجهيز. الفروع الخاملة (`0092` التحقق الصيانيّ، `0114`، `0115`، ومراجعة الكتالوج) لا تُطبَّق إلا بقرار المالك. (ج) ثم الإقلاع؛ هدف الإقلاع العادي يبقى مثبتًا على `0093` ولا يتغير. **لماذا (مُثبت على PostgreSQL 16.15 و18.6 بقواعد مؤقتة):** على قاعدة فارغة يتسابق `create_all` الخلفي مع مراجعة 0001 فيفشل ترقية الإقلاع (`DuplicateTable` أو `UniqueViolation` بحسب السباق) ويبقى `alembic_version` غائبًا، وفي الإقلاع التالي يختم Step B على 0016 ثم يفشل الترقي (`DuplicateColumn`) على أعمدة أنشأها `create_all` — لم يبلغ الإقلاع وحده 0093 في أي تشغيل مرصود، وهو السلوك نفسه على `main`. وقاعدة رُقّيت إلى `0093` فقط تُقلع لكن ينقصها 23 عمودًا تعلنها النماذج (تضيفها مراجعات لاحقة إلى جداول قائمة، ولا يضيفها `create_all`)؛ و`0111` وحده ينقصه 7. بعد (أ)–(ج) يكتمل الإقلاع: Step C `upgrade 0093 OK rc=0` دون تغيير، `alembic_version` = `0111`,`0113`، ولا عمود نموذج ناقص (اختبار `test_review_provisioning_premigration_then_boot_serves_with_the_complete_schema`). مراجعة الكتالوج خطوة صريحة منفصلة بموافقة: `preflight_check.py` أولًا ثم `alembic upgrade` إلى مراجعة الكتالوج؛ تتبنّى الجدول الذي بناه `create_all` فقط إن طابق تعريفها، وإلا ترفض دون تغيير (اختبار `test_review_provisioning_catalog_schema_step_adopts_the_table_create_all_built`). اكتمال أعمدة النماذج ليس جاهزية المراجعة أو المزوّد من طرف إلى طرف: مسارات الكتالوج وMeta لا يغطيها هذا الاختبار. خطوة `alembic` لا يحرسها حارس الإقلاع، فـ`preflight_check.py` قبلها إلزامي.
2. إنشاء `nahla-catalog-review-api` من هذا الفرع بالمتغيرات في §2؛ النشر الأول يفشل بوضوح إن نقص أي شرط (هذا مقصود).
3. إنشاء `nahla-catalog-review-web` من هذا الفرع بمتغيرَي البناء؛ ربط النطاقين عند تجهيزهما.
4. تسجيل مستأجر اختبار من واجهة المراجعة؛ منح استحقاق `meta_catalog_sync` من مسار المشرف في بيئة المراجعة.
5. (بعد قرار ن-2/ن-8) تشغيل المشغّل بتجربة جافة ثم `--write` لإدراج اتصال الكتالوج بالرمز مشفَّرًا.
6. تنفيذ الرحلة كاملة وتوثيقها في تقرير الكتالوج §7.10 قبل أي حديث عن التصوير.

**ممنوع طوال ذلك:** أي مسّ بالإنتاج أو Tenant 1/33/35/67 أو الوكيل أو الرقم أو coexistence؛ لا تعديل لإعداد Meta الإنتاجي؛ لا كتابة حية خارج كتالوج الاختبار.
