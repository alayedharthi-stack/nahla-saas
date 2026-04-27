"""
scripts/test_email_templates.py
────────────────────────────────
اختبار القوالب الثلاثة:  welcome_email | first_whatsapp_message | trial_expiring

يفحص لكل قالب:
  ✓ الـ sender صحيح (من SENDER_MAP + TEMPLATE_SENDER)
  ✓ القالب يُرسم بدون أخطاء Jinja
  ✓ الشعار (logo) موجود في الـ HTML
  ✓ زر CTA موجود
  ✓ الخلفية بيضاء (#ffffff)
  ✓ العنوان (title) صحيح
  ✓ الإرسال الفعلي عبر Resend (إذا EMAIL_ENABLED=True)

تشغيل:
    python backend/scripts/test_email_templates.py [--to you@example.com]
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import argparse
import pathlib
from typing import Any, Dict

# Force UTF-8 on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── path setup ────────────────────────────────────────────────────────────────
_SCRIPT_DIR = pathlib.Path(__file__).parent
_BACKEND    = _SCRIPT_DIR.parent
_REPO_ROOT  = _BACKEND.parent
_DB_DIR     = _REPO_ROOT / "database"
for _p in (str(_BACKEND), str(_REPO_ROOT), str(_DB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── import email service internals ────────────────────────────────────────────
from services.email_service import (  # noqa: E402
    SENDER_MAP, TEMPLATE_SENDER, _resolve_sender, _get_jinja_env, send_email
)

# ── sample variables per template ─────────────────────────────────────────────
DASHBOARD_URL = "https://app.nahlah.ai"

TEMPLATE_CASES: list[dict] = [
    {
        "template":    "welcome_email",
        "subject":     "🎉 مرحباً بك في نحلة — اختبار",
        "expected_from_key": "welcome",
        "variables": {
            "merchant_name": "أحمد الاختبار",
            "store_name":    "متجر الاختبار",
            "dashboard_url": DASHBOARD_URL,
            "trial_days":    14,
        },
        "checks": {
            "title":  "مرحباً بك في نحلة",
            "logo":   "logo-nahla.png",
            "cta":    "افتح لوحة التحكم",
            "bg":     "#ffffff",
        },
    },
    {
        "template":    "first_whatsapp_message",
        "subject":     "💬 أول رسالة واتساب — اختبار",
        "expected_from_key": "growth",
        "variables": {
            "merchant_name":    "فاطمة الاختبار",
            "customer_name":    "عبدالله عميل",
            "customer_phone":   "966501234567",
            "message_preview":  "السلام عليكم، أريد الاستفسار عن المنتج",
            "conversation_url": f"{DASHBOARD_URL}/conversations",
            "dashboard_url":    DASHBOARD_URL,
        },
        "checks": {
            "title":  "أول رسالة واتساب",
            "logo":   "logo-nahla.png",
            "cta":    "عرض المحادثة",
            "bg":     "#ffffff",
        },
    },
    {
        "template":    "trial_expiring",
        "subject":     "⏳ تجربتك تنتهي قريباً — اختبار",
        "expected_from_key": "billing",
        "variables": {
            "merchant_name":   "خالد الاختبار",
            "days_remaining":  3,
            "dashboard_url":   DASHBOARD_URL,
        },
        "checks": {
            "title":  "تجربتك المجانية تنتهي",
            "logo":   "logo-nahla.png",
            "cta":    "اشترك الآن",
            "bg":     "#ffffff",
        },
    },
]

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

ok  = lambda msg: f"{GREEN}✓{RESET} {msg}"
err = lambda msg: f"{RED}✗{RESET} {msg}"
inf = lambda msg: f"{CYAN}→{RESET} {msg}"


def check_html(html: str, checks: Dict[str, str]) -> list[tuple[str, bool, str]]:
    """Run simple string-presence checks on rendered HTML."""
    results = []
    for key, needle in checks.items():
        found = needle in html
        results.append((key, found, needle))
    return results


async def run_tests(to_address: str | None) -> None:
    env = _get_jinja_env()

    print(f"\n{BOLD}{'═' * 62}{RESET}")
    print(f"{BOLD}  نحلة — اختبار قوالب الإيميل{RESET}")
    print(f"{BOLD}{'═' * 62}{RESET}\n")

    all_passed = True

    for case in TEMPLATE_CASES:
        tmpl      = case["template"]
        subject   = case["subject"]
        exp_key   = case["expected_from_key"]
        variables = case["variables"]
        checks    = case["checks"]

        print(f"{BOLD}{'─' * 62}{RESET}")
        print(f"{BOLD}  قالب: {tmpl}{RESET}")
        print(f"{'─' * 62}")

        # ── 1. Sender resolution ──────────────────────────────────────────────
        resolved_key = TEMPLATE_SENDER.get(tmpl, None)
        from_address = _resolve_sender(None, tmpl)
        exp_address  = SENDER_MAP.get(exp_key, "")
        sender_ok    = from_address == exp_address

        print(f"  {ok('sender') if sender_ok else err('sender')}")
        print(f"     from      : {CYAN}{from_address}{RESET}")
        print(f"     expected  : {exp_address}")
        if not sender_ok:
            all_passed = False

        # ── 2. Template render ───────────────────────────────────────────────
        try:
            tmpl_obj = env.get_template(f"{tmpl}.html")
            # inject dashboard_url fallback for base template
            merged_vars = {"dashboard_url": DASHBOARD_URL, **variables}
            html = tmpl_obj.render(**merged_vars)
            render_ok = True
        except Exception as exc:
            render_ok = False
            html = ""
            print(f"  {err(f'render failed: {exc}')}")
            all_passed = False

        if render_ok:
            print(f"  {ok('render')} ({len(html):,} chars)")

        # ── 3. HTML structure checks ─────────────────────────────────────────
        if html:
            for check_key, found, needle in check_html(html, checks):
                label = f"{check_key:8s} '{needle}'"
                print(f"  {ok(label) if found else err(label)}")
                if not found:
                    all_passed = False

        # ── 4. Subject + summary line ─────────────────────────────────────────
        print(f"  {inf('subject')}   : {subject}")

        # ── 5. Optional real send ────────────────────────────────────────────
        if to_address and html:
            print(f"  {inf('sending')}  : → {to_address}")
            try:
                success = await send_email(
                    to=to_address,
                    subject=subject,
                    template=tmpl,
                    variables=variables,
                )
                status = ok("sent via Resend") if success else err("send failed (check logs)")
                print(f"  {status}")
                if not success:
                    all_passed = False
            except Exception as exc:
                print(f"  {err(f'exception during send: {exc}')}")
                all_passed = False
        elif not to_address:
            print(f"  {YELLOW}⊘  dry-run (pass --to EMAIL to send for real){RESET}")

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'═' * 62}")
    if all_passed:
        print(f"{GREEN}{BOLD}  ✓ جميع الفحوصات نجحت{RESET}")
    else:
        print(f"{RED}{BOLD}  ✗ بعض الفحوصات فشلت — راجع التفاصيل أعلاه{RESET}")
    print(f"{'═' * 62}\n")

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Test Nahla email templates")
    parser.add_argument("--to", default=None, help="Send a real email to this address")
    args = parser.parse_args()

    passed = asyncio.run(run_tests(args.to))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
