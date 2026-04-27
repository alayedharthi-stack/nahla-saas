/* ============================================================
   AYED HONEY  —  Lead Capture Popup  v5 (Banner-as-Full-Design)
   ------------------------------------------------------------
   - مستقل تمامًا. لا يلمس أي كود من Nahla.
   - Namespace: ayed-lead-*
   - بدون <script> / Backend / API / مكتبات خارجية.
   - الصق هذا الكود مباشرة داخل محرر JavaScript في سلة.

   البانر يحتوي على التصميم الكامل (شعار، عنوان، عرض، حقل الإدخال،
   زر، شارات ثقة...). الكود يضع طبقات تفاعلية شفافة فوق
   مناطق الحقل والزر والإغلاق في البانر.
   ============================================================ */
(function () {
  'use strict';

  if (window.__ayedLeadInitialized) return;
  window.__ayedLeadInitialized = true;

  /* ====== رابط البانر المرفوع على سلة ====== */
  const BANNER = 'https://cdn.salla.sa/XVEDq/73f19a24-3891-4883-be58-68b4afca0384-231.29067245119x500-1zLxkQCfBsx5wDEBpGfQMRmp3X2EeJgFiNmVdmE4.png';
  /* ====== أبعاد البانر الأصلية (للحفاظ على نسبة الأبعاد) ====== */
  const BANNER_W = 231.29;
  const BANNER_H = 500;
  /* ============================================== */

  const NS = 'ayed-lead';

  const STORE = {
    closedAt: 'ayed_lead_closed_at',
    coupon:   'ayed_lead_coupon',
    phone:    'ayed_lead_phone',
    used:     'ayed_lead_used_coupons',
    issuedAt: 'ayed_lead_issued_at'
  };

  const COOLDOWN_CLOSED = 7  * 24 * 60 * 60 * 1000;
  const COOLDOWN_ISSUED = 60 * 24 * 60 * 60 * 1000;
  const SHOW_DELAY      = 5000;
  const WHATSAPP_NUM    = '966555906901';

  /* ---------- Coupon Pool حقيقي من سلة ---------- */
  const AYED_COUPONS = [
    "AY101A","AY102B","AY103C","AY104D","AY105E","AY106F","AY107G","AY108H","AY109J","AY110K",
    "AY111L","AY112M","AY113N","AY114P","AY115Q","AY116R","AY117S","AY118T","AY119U","AY120V",
    "AY121W","AY122X","AY123Y","AY124Z","AY125A","AY126B","AY127C","AY128D","AY129E","AY130F",
    "AY131G","AY132H","AY133J","AY134K","AY135L","AY136M","AY137N","AY138P","AY139Q","AY140R",
    "AY141S","AY142T","AY143U","AY144V","AY145W","AY146X","AY147Y","AY148Z","AY149A","AY150B",
    "AY151C","AY152D","AY153E","AY154F","AY155G","AY156H","AY157J","AY158K","AY159L","AY160M",
    "AY161N","AY162P","AY163Q","AY164R","AY165S","AY166T","AY167U","AY168V","AY169W","AY170X",
    "AY171Y","AY172Z","AY173A","AY174B","AY175C","AY176D","AY177E","AY178F","AY179G","AY180H",
    "AY181J","AY182K","AY183L","AY184M","AY185N","AY186P","AY187Q","AY188R","AY189S","AY190T",
    "AY191U","AY192V","AY193W","AY194X","AY195Y","AY196Z","AY197A","AY198B","AY199C","AY200D",
    "AY201E","AY202F","AY203G","AY204H","AY205J","AY206K","AY207L","AY208M","AY209N","AY210P",
    "AY211Q","AY212R","AY213S","AY214T","AY215U","AY216V","AY217W","AY218X","AY219Y","AY220Z",
    "AY221A","AY222B","AY223C","AY224D","AY225E","AY226F","AY227G","AY228H","AY229J","AY230K",
    "AY231L","AY232M","AY233N","AY234P","AY235Q","AY236R","AY237S","AY238T","AY239U","AY240V",
    "AY241W","AY242X","AY243Y","AY244Z","AY245A","AY246B","AY247C","AY248D","AY249E","AY250F",
    "AY251G","AY252H","AY253J","AY254K","AY255L","AY256M","AY257N","AY258P","AY259Q","AY260R",
    "AY261S","AY262T","AY263U","AY264V","AY265W","AY266X","AY267Y","AY268Z","AY269A","AY270B",
    "AY271C","AY272D","AY273E","AY274F","AY275G","AY276H","AY277J","AY278K","AY279L","AY280M",
    "AY281N","AY282P","AY283Q","AY284R","AY285S","AY286T","AY287U","AY288V","AY289W","AY290X",
    "AY291Y","AY292Z","AY293A","AY294B","AY295C","AY296D","AY297E","AY298F","AY299G","AY300H"
  ];

  /* ---------- helpers ---------- */
  function lsGet(k, fb) { try { return localStorage.getItem(k); } catch (e) { return fb; } }
  function lsSet(k, v)  { try { localStorage.setItem(k, v); } catch (e) {} }

  function getCoupon() {
    try {
      const used = JSON.parse(lsGet(STORE.used) || "[]");
      const available = AYED_COUPONS.filter(function (c) { return used.indexOf(c) === -1; });
      let coupon;
      if (available.length > 0) {
        coupon = available[Math.floor(Math.random() * available.length)];
        used.push(coupon);
        lsSet(STORE.used, JSON.stringify(used));
      } else {
        coupon = AYED_COUPONS[Math.floor(Math.random() * AYED_COUPONS.length)];
      }
      return coupon;
    } catch (e) {
      return AYED_COUPONS[Math.floor(Math.random() * AYED_COUPONS.length)];
    }
  }

  function normalizePhone(raw) {
    if (!raw) return null;
    let p = String(raw).replace(/[\s\-()._]/g, '');
    if (p.indexOf('+') === 0) p = p.slice(1);
    if (p.indexOf('00') === 0) p = p.slice(2);
    if (/^05\d{8}$/.test(p))   return '966' + p.slice(1);
    if (/^5\d{8}$/.test(p))    return '966' + p;
    if (/^9665\d{8}$/.test(p)) return p;
    return null;
  }

  function shouldAutoShow() {
    if (lsGet(STORE.coupon)) return false;
    const closed = parseInt(lsGet(STORE.closedAt) || '0', 10);
    if (closed && (Date.now() - closed) < COOLDOWN_CLOSED) return false;
    return true;
  }

  /* ============================================================
     مواقع المناطق التفاعلية فوق البانر (نسب من ارتفاع وعرض البانر)
     ============================================================
     هذه القيم محسوبة بناءً على البانر الحالي. إذا غيّرت البانر،
     عدّل هذه القيم لتطابق المواقع البصرية الجديدة.
     ============================================================ */
  const ZONES = {
    /* زر إغلاق X في أعلى يسار البانر */
    close:  { top: '2.4%',  left: '5%',   width: '8%',  height: '4.2%' },
    /* حقل الإدخال - منطقة الـ "05xxxxxxxx" بين الأيقونة و+966 */
    input:  { top: '66.6%', left: '24%',  right: '23%', height: '6.6%' },
    /* الزر الذهبي "احصل على الكود" */
    submit: { top: '74.8%', left: '6.5%', right: '6.5%', height: '7.2%' }
  };

  /* ---------- CSS ---------- */
  const CSS = `
  .${NS}-root, .${NS}-root *,
  .${NS}-fab, .${NS}-fab *,
  .${NS}-toast { box-sizing: border-box; }

  .${NS}-root, .${NS}-fab, .${NS}-toast {
    font-family: "Tajawal","Cairo","Segoe UI",system-ui,-apple-system,sans-serif;
    -webkit-font-smoothing: antialiased;
  }

  .${NS}-root {
    position: fixed; inset: 0; z-index: 2147483600;
    direction: rtl; color: #2A1B0E;
    pointer-events: none;
  }
  .${NS}-root.${NS}-open { pointer-events: auto; }

  .${NS}-backdrop {
    position: absolute; inset: 0;
    background: radial-gradient(circle at 50% 25%, rgba(40,25,12,.65) 0%, rgba(10,6,3,.88) 75%);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    opacity: 0; transition: opacity .35s ease;
  }
  .${NS}-root.${NS}-open .${NS}-backdrop { opacity: 1; }

  /* البطاقة بنفس نسبة أبعاد البانر */
  .${NS}-card {
    position: absolute;
    left: 50%; top: 50%;
    transform: translate(-50%, -50%) scale(.94);
    width: min(420px, 92vw, calc(92vh * ${BANNER_W} / ${BANNER_H}));
    aspect-ratio: ${BANNER_W} / ${BANNER_H};
    max-height: 92vh;
    background: #FFFDF6;
    border-radius: 22px;
    overflow: hidden;
    box-shadow:
      0 24px 60px rgba(20,12,5,.6),
      0 0 0 1px rgba(212,160,23,.25);
    opacity: 0;
    transition: opacity .35s ease, transform .5s cubic-bezier(.2,.9,.25,1);
    isolation: isolate;
  }
  .${NS}-root.${NS}-open .${NS}-card {
    opacity: 1; transform: translate(-50%, -50%) scale(1);
  }

  /* البانر */
  .${NS}-banner {
    position: absolute; inset: 0;
    width: 100%; height: 100%;
  }
  .${NS}-banner img {
    width: 100%; height: 100%;
    object-fit: cover;
    display: block;
  }

  /* ===== المناطق التفاعلية فوق البانر ===== */

  /* زر الإغلاق الشفاف */
  .${NS}-close {
    position: absolute;
    top: ${ZONES.close.top};
    left: ${ZONES.close.left};
    width: ${ZONES.close.width};
    height: ${ZONES.close.height};
    min-width: 32px; min-height: 32px;
    background: transparent;
    border: none;
    cursor: pointer;
    z-index: 10;
    border-radius: 50%;
    transition: background .2s ease;
  }
  .${NS}-close:hover { background: rgba(255,255,255,.15); }
  .${NS}-close:focus-visible {
    outline: 2px solid rgba(244,196,48,.8);
    outline-offset: 2px;
  }

  /* حقل الإدخال - يغطي منطقة "05xxxxxxxx" فقط بخلفية بيضاء */
  .${NS}-input-wrap {
    position: absolute;
    top: ${ZONES.input.top};
    left: ${ZONES.input.left};
    right: ${ZONES.input.right};
    height: ${ZONES.input.height};
    background: #FFFFFF;
    border-radius: 6px;
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: box-shadow .2s ease;
  }
  .${NS}-input-wrap.${NS}-focused {
    box-shadow: 0 0 0 2px rgba(212,160,23,.55), 0 4px 10px rgba(212,160,23,.25);
  }
  .${NS}-input {
    width: 100%; height: 100%;
    background: transparent;
    border: none;
    text-align: center;
    font-size: clamp(13px, 3.2vw, 16px);
    font-weight: 700;
    color: #2A1B0E;
    letter-spacing: 1.5px;
    direction: ltr;
    font-family: inherit;
    padding: 0 8px;
  }
  .${NS}-input:focus { outline: none; }
  .${NS}-input::placeholder {
    color: #B5A88A;
    font-weight: 500;
    letter-spacing: 2px;
  }

  /* زر الإرسال الشفاف */
  .${NS}-submit {
    position: absolute;
    top: ${ZONES.submit.top};
    left: ${ZONES.submit.left};
    right: ${ZONES.submit.right};
    height: ${ZONES.submit.height};
    background: transparent;
    border: none;
    cursor: pointer;
    z-index: 10;
    border-radius: 999px;
    transition: filter .2s ease, transform .15s ease;
  }
  .${NS}-submit:hover { filter: brightness(1.05); }
  .${NS}-submit:active { transform: scale(.985); }
  .${NS}-submit:focus-visible {
    outline: 2px solid rgba(31,58,27,.7);
    outline-offset: 3px;
  }

  /* رسالة الخطأ */
  .${NS}-error {
    position: absolute;
    top: 60%;
    left: 6%; right: 6%;
    background: rgba(181,52,26,.95);
    color: #fff;
    border-radius: 8px;
    font-size: 11.5px;
    text-align: center;
    padding: 6px 10px;
    font-weight: 700;
    z-index: 12;
    opacity: 0;
    transform: translateY(-6px);
    pointer-events: none;
    transition: opacity .25s ease, transform .25s ease;
    box-shadow: 0 4px 12px rgba(181,52,26,.4);
  }
  .${NS}-error.${NS}-show {
    opacity: 1; transform: translateY(0);
    animation: ${NS}-shake .35s ease;
  }
  @keyframes ${NS}-shake {
    0%,100% { transform: translateY(0) translateX(0); }
    25% { transform: translateY(0) translateX(-4px); }
    75% { transform: translateY(0) translateX(4px); }
  }

  /* ===== Drawer النجاح ===== */
  .${NS}-drawer {
    position: absolute;
    left: 0; right: 0; bottom: 0;
    top: 55%;
    background: linear-gradient(180deg,#FFFDF6 0%,#FFF8E7 100%);
    border-radius: 22px 22px 0 0;
    box-shadow: 0 -16px 40px rgba(20,12,5,.45);
    padding: 16px 18px 18px;
    transform: translateY(105%);
    transition: transform .55s cubic-bezier(.2,.9,.25,1);
    z-index: 20;
    overflow-y: auto;
    direction: rtl;
  }
  .${NS}-drawer.${NS}-show { transform: translateY(0); }
  .${NS}-drawer::before {
    content:""; position: absolute; top: 7px; left: 50%;
    transform: translateX(-50%);
    width: 38px; height: 4px; border-radius: 4px;
    background: rgba(61,40,23,.18);
  }

  .${NS}-success-title {
    text-align: center;
    font-size: 16px; font-weight: 900;
    color: #2A1B0E; margin: 8px 0 4px;
  }
  .${NS}-success-sub {
    text-align: center;
    font-size: 11.5px; color: #8a6f4d;
    margin: 0 0 12px;
  }

  .${NS}-ticket {
    position: relative;
    padding: 12px 18px;
    margin-bottom: 12px;
    background:
      repeating-linear-gradient(45deg, rgba(212,160,23,.07) 0 8px, transparent 8px 16px),
      linear-gradient(180deg,#FFFDF6 0%,#FFF3D3 100%);
    border: 2px dashed #D4A017;
    border-radius: 14px;
    text-align: center;
  }
  .${NS}-ticket::before, .${NS}-ticket::after {
    content:""; position: absolute; top: 50%;
    width: 16px; height: 16px; border-radius: 50%;
    background: #FFF8E7;
    border: 2px dashed #D4A017;
    transform: translateY(-50%);
  }
  .${NS}-ticket::before { left: -9px; clip-path: inset(0 0 0 50%); }
  .${NS}-ticket::after  { right: -9px; clip-path: inset(0 50% 0 0); }
  .${NS}-ticket-label {
    font-size: 9.5px; font-weight: 800;
    color: #8a6f4d; letter-spacing: 3px;
    margin-bottom: 4px;
  }
  .${NS}-ticket-code {
    font-size: 24px; font-weight: 900;
    color: #2A1B0E;
    letter-spacing: 4px;
    font-family: "Courier New", "Tajawal", monospace;
    direction: ltr;
  }
  .${NS}-ticket-note {
    margin-top: 4px;
    font-size: 10.5px; color: #8a6f4d; font-weight: 600;
  }
  .${NS}-actions { display: flex; flex-direction: column; gap: 8px; }
  .${NS}-btn {
    width: 100%; height: 46px;
    border: none; cursor: pointer;
    border-radius: 12px;
    font-size: 14px; font-weight: 800;
    font-family: inherit;
    transition: transform .15s ease, box-shadow .25s ease, filter .2s ease;
    display: flex; align-items: center; justify-content: center; gap: 7px;
  }
  .${NS}-btn:active { transform: translateY(1px) scale(.99); }
  .${NS}-btn-wa {
    background: linear-gradient(135deg,#25D366 0%,#128C7E 100%);
    color: #fff;
    box-shadow: 0 8px 18px rgba(37,211,102,.32);
  }
  .${NS}-btn-copy {
    background: #FFFDF6;
    color: #3D2817;
    border: 1.5px solid rgba(212,160,23,.45);
  }
  .${NS}-btn-copy:hover { background: #FFF3D3; }

  /* Confetti */
  .${NS}-confetti {
    position: absolute; top: 0; left: 0; right: 0; height: 0;
    pointer-events: none; overflow: visible;
    z-index: 21;
  }
  .${NS}-confetti span {
    position: absolute; top: 0; width: 7px; height: 11px;
    border-radius: 2px;
    animation: ${NS}-confetti 2.4s cubic-bezier(.2,.7,.4,1) forwards;
    opacity: 0;
  }
  @keyframes ${NS}-confetti {
    0%   { transform: translateY(-10px) rotate(0); opacity: 0; }
    20%  { opacity: 1; }
    100% { transform: translateY(380px) rotate(720deg); opacity: 0; }
  }

  /* الزر العائم لاستعادة الكود */
  .${NS}-fab {
    position: fixed;
    bottom: 16px; left: 16px;
    z-index: 2147483500;
    height: 42px;
    padding: 0 14px 0 10px;
    display: inline-flex; align-items: center; gap: 7px;
    background: linear-gradient(135deg,#F4C430,#D4A017);
    color: #2A1B0E;
    border: none; border-radius: 999px; cursor: pointer;
    font-size: 12.5px; font-weight: 800;
    box-shadow: 0 10px 22px rgba(212,160,23,.45), inset 0 1px 0 rgba(255,255,255,.4);
    transition: transform .2s ease;
    direction: rtl;
  }
  .${NS}-fab:hover { transform: translateY(-2px); }
  .${NS}-fab svg { width: 16px; height: 16px; }
  @media (max-width: 540px) {
    .${NS}-fab { bottom: 12px; left: 12px; height: 38px; font-size: 12px; }
  }

  /* Toast */
  .${NS}-toast {
    position: fixed; bottom: 22px; left: 50%;
    transform: translateX(-50%) translateY(20px);
    background: #2A1B0E; color: #FFD978;
    padding: 10px 18px; border-radius: 999px;
    font-size: 12.5px; font-weight: 700;
    opacity: 0; pointer-events: none;
    transition: opacity .25s ease, transform .25s ease;
    z-index: 2147483647;
    box-shadow: 0 10px 30px rgba(20,12,5,.45);
    direction: rtl;
  }
  .${NS}-toast.${NS}-show {
    opacity: 1; transform: translateX(-50%) translateY(0);
  }
  `;

  /* ---------- HTML ---------- */
  function buildHTML() {
    return `
      <div class="${NS}-backdrop" data-${NS}-close="1"></div>
      <div class="${NS}-card" role="dialog" aria-modal="true" aria-label="هدية ترحيبية من مناحل آل عايد">
        <div class="${NS}-banner">
          <img src="${BANNER}" alt="مناحل آل عايد - هدية ترحيبية" loading="eager">
        </div>

        <button class="${NS}-close" type="button" aria-label="إغلاق" data-${NS}-close="1"></button>

        <div class="${NS}-input-wrap">
          <input
            type="tel"
            class="${NS}-input"
            placeholder="05xxxxxxxx"
            inputmode="numeric"
            maxlength="15"
            autocomplete="tel"
            aria-label="رقم الجوال">
        </div>

        <button class="${NS}-submit" type="button" aria-label="احصل على الكود"></button>

        <div class="${NS}-error" role="alert">فضلاً أدخل رقم جوال سعودي صحيح.</div>

        <div class="${NS}-drawer" aria-hidden="true">
          <div class="${NS}-confetti" aria-hidden="true"></div>
          <h3 class="${NS}-success-title">🎉 كودك الخاص جاهز</h3>
          <p class="${NS}-success-sub">استخدمه عند إتمام طلبك للحصول على خصم 10%</p>
          <div class="${NS}-ticket">
            <div class="${NS}-ticket-label">كود الخصم</div>
            <div class="${NS}-ticket-code"></div>
            <div class="${NS}-ticket-note">صالح لمدة محدودة • يُستخدم مرة واحدة</div>
          </div>
          <div class="${NS}-actions">
            <button type="button" class="${NS}-btn ${NS}-btn-wa ${NS}-wa">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                <path d="M.057 24l1.687-6.163a11.867 11.867 0 0 1-1.587-5.946C.16 5.335 5.495 0 12.05 0a11.817 11.817 0 0 1 8.413 3.488 11.824 11.824 0 0 1 3.48 8.414c-.003 6.557-5.338 11.892-11.893 11.892a11.9 11.9 0 0 1-5.688-1.448L.057 24zm6.597-3.807c1.676.995 3.276 1.591 5.392 1.592 5.448 0 9.886-4.434 9.889-9.885.002-5.462-4.415-9.89-9.881-9.892-5.452 0-9.887 4.434-9.889 9.884a9.86 9.86 0 0 0 1.692 5.59l-.999 3.648 3.796-.937zm11.387-5.464c-.074-.124-.272-.198-.57-.347-.297-.149-1.758-.868-2.031-.967-.272-.099-.47-.149-.669.149-.198.297-.768.967-.941 1.165-.173.198-.347.223-.644.074-.297-.149-1.255-.462-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.297-.347.446-.521.151-.172.2-.296.3-.495.099-.198.05-.372-.025-.521-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.501-.669-.51l-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.876 1.213 3.074.149.198 2.095 3.2 5.076 4.487.71.306 1.263.489 1.694.626.712.226 1.36.194 1.872.118.571-.085 1.758-.719 2.006-1.413.247-.694.247-1.289.173-1.413z"/>
              </svg>
              استلامه على واتساب
            </button>
            <button type="button" class="${NS}-btn ${NS}-btn-copy ${NS}-copy">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
              </svg>
              نسخ الكود
            </button>
          </div>
        </div>
      </div>
    `;
  }

  function fireConfetti(container) {
    const colors = ['#F4C430','#D4A017','#FFD978','#A8C09A','#1F3A1B','#3D2817'];
    container.innerHTML = '';
    for (let i = 0; i < 24; i++) {
      const s = document.createElement('span');
      s.style.left = (Math.random() * 100) + '%';
      s.style.background = colors[Math.floor(Math.random() * colors.length)];
      s.style.animationDelay = (Math.random() * 0.4) + 's';
      s.style.transform = 'rotate(' + (Math.random() * 360) + 'deg)';
      container.appendChild(s);
    }
    setTimeout(function () { container.innerHTML = ''; }, 2800);
  }

  /* ---------- Floating button ---------- */
  let fabEl = null;
  function mountFab() {
    if (fabEl || document.querySelector('.' + NS + '-fab')) return;
    fabEl = document.createElement('button');
    fabEl.type = 'button';
    fabEl.className = NS + '-fab';
    fabEl.setAttribute('aria-label', 'عرض كود الخصم');
    fabEl.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 12V8a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v4a2 2 0 0 1 0 4v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4a2 2 0 0 1 0-4z"/>
        <path d="M9 12h6"/>
      </svg>
      <span>كود الخصم</span>
    `;
    fabEl.addEventListener('click', function () {
      mount({ reopen: true });
      try { fabEl.remove(); fabEl = null; } catch (e) {}
    });
    document.body.appendChild(fabEl);
  }

  /* ---------- mount ---------- */
  function mount(opts) {
    opts = opts || {};
    if (document.querySelector('.' + NS + '-root')) return;

    if (!document.getElementById(NS + '-style')) {
      const style = document.createElement('style');
      style.id = NS + '-style';
      style.textContent = CSS;
      document.head.appendChild(style);
    }

    const root = document.createElement('div');
    root.className = NS + '-root';
    root.setAttribute('aria-hidden', 'true');
    root.innerHTML = buildHTML();
    document.body.appendChild(root);

    const toast = document.createElement('div');
    toast.className = NS + '-toast';
    toast.textContent = 'تم نسخ الكود';
    document.body.appendChild(toast);

    const $ = function (sel) { return root.querySelector(sel); };
    const inputWrap = $('.' + NS + '-input-wrap');
    const input    = $('.' + NS + '-input');
    const errorEl  = $('.' + NS + '-error');
    const submit   = $('.' + NS + '-submit');
    const drawer   = $('.' + NS + '-drawer');
    const codeEl   = $('.' + NS + '-ticket-code');
    const copyBtn  = $('.' + NS + '-copy');
    const waBtn    = $('.' + NS + '-wa');
    const confetti = $('.' + NS + '-confetti');

    function open() {
      requestAnimationFrame(function () {
        root.classList.add(NS + '-open');
        root.setAttribute('aria-hidden', 'false');
        try { document.body.style.overflow = 'hidden'; } catch (e) {}
      });
    }
    function close() {
      root.classList.remove(NS + '-open');
      root.setAttribute('aria-hidden', 'true');
      try { document.body.style.overflow = ''; } catch (e) {}
      setTimeout(function () {
        try { root.remove(); toast.remove(); } catch (e) {}
        if (lsGet(STORE.coupon)) mountFab();
      }, 400);
    }

    function showToast(msg) {
      toast.textContent = msg || 'تم';
      toast.classList.add(NS + '-show');
      clearTimeout(showToast._t);
      showToast._t = setTimeout(function () { toast.classList.remove(NS + '-show'); }, 1900);
    }

    function showError() {
      errorEl.classList.remove(NS + '-show');
      void errorEl.offsetWidth;
      errorEl.classList.add(NS + '-show');
      clearTimeout(showError._t);
      showError._t = setTimeout(function () {
        errorEl.classList.remove(NS + '-show');
      }, 3500);
    }

    function buildWaUrl(code) {
      const text = 'مرحبًا مناحل آل عايد 🍯\nكود الخصم الخاص بي: ' + code;
      return 'https://wa.me/' + WHATSAPP_NUM + '?text=' + encodeURIComponent(text);
    }

    function showSuccess(code, withConfetti) {
      codeEl.textContent = code;
      drawer.classList.add(NS + '-show');
      drawer.setAttribute('aria-hidden', 'false');
      if (withConfetti) {
        try { fireConfetti(confetti); } catch (e) {}
      }
    }

    /* listeners */
    root.addEventListener('click', function (e) {
      const t = e.target.closest('[data-' + NS + '-close]');
      if (t) {
        lsSet(STORE.closedAt, String(Date.now()));
        close();
      }
    });

    document.addEventListener('keydown', function onEsc(e) {
      if (e.key === 'Escape' && root.isConnected && root.classList.contains(NS + '-open')) {
        lsSet(STORE.closedAt, String(Date.now()));
        close();
        document.removeEventListener('keydown', onEsc);
      }
    });

    input.addEventListener('focus', function () {
      inputWrap.classList.add(NS + '-focused');
    });
    input.addEventListener('blur', function () {
      inputWrap.classList.remove(NS + '-focused');
    });
    input.addEventListener('input', function () {
      errorEl.classList.remove(NS + '-show');
      this.value = this.value.replace(/[^\d+]/g, '');
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); submit.click(); }
    });

    submit.addEventListener('click', function () {
      const phone = normalizePhone(input.value);
      if (!phone) {
        showError();
        input.focus();
        return;
      }
      lsSet(STORE.phone, phone);
      let code = lsGet(STORE.coupon);
      if (!code) {
        code = getCoupon();
        lsSet(STORE.coupon, code);
        lsSet(STORE.issuedAt, String(Date.now()));
      }
      lsSet(STORE.closedAt, String(Date.now()));
      showSuccess(code, true);
    });

    copyBtn.addEventListener('click', function () {
      const code = codeEl.textContent.trim();
      const done = function () { showToast('تم نسخ الكود ✔'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(done).catch(function () {
          fallbackCopy(code); done();
        });
      } else {
        fallbackCopy(code); done();
      }
    });

    function fallbackCopy(text) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
      } catch (e) {}
    }

    waBtn.addEventListener('click', function () {
      const code = codeEl.textContent.trim();
      window.open(buildWaUrl(code), '_blank', 'noopener');
    });

    open();

    /* إذا فتحه عبر الزر العائم وعنده كوبون من قبل، اعرض الكوبون مباشرة */
    const existing = lsGet(STORE.coupon);
    if (existing && opts.reopen) {
      setTimeout(function () { showSuccess(existing, false); }, 350);
    }
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else { fn(); }
  }

  ready(function () {
    if (shouldAutoShow()) {
      setTimeout(mount, SHOW_DELAY);
    } else if (lsGet(STORE.coupon)) {
      setTimeout(mountFab, 1500);
    }
  });

})();
