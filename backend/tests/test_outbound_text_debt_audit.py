"""Tests for outbound text debt audit classification."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.outbound_text_allowlist import LEGACY_DETECTION_MARKERS
from core.outbound_text_audit_classification import (
    BUCKET_DETERMINISTIC_TEMPLATE,
    BUCKET_INTERNAL_TOOLING,
    BUCKET_LEGACY_DETECTION,
    BUCKET_OUTBOUND_CUSTOMER_FACING_RISK,
    BUCKET_PROMPT_ONLY,
    BUCKET_REGEX_INTENT,
    BUCKET_TECHNICAL_ALLOWLIST,
    build_summary,
    classify_audit_finding,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
AUDIT_SCRIPT = BACKEND / "scripts" / "audit_outbound_text_debt.py"


class TestAuditClassification:
    def test_intent_regex_classified_not_customer_debt(self):
        result = classify_audit_finding(
            filepath="modules/ai/brain/decision/intent/rules.py",
            line_content='PATTERNS = re.compile(r"(?:مرحب|هلا|السلام)")',
            literal="مرحب",
        )
        assert result["bucket"] == BUCKET_REGEX_INTENT

    def test_outbound_template_classified_as_risk(self):
        result = classify_audit_finding(
            filepath="modules/ai/brain/compose/templates.py",
            line_content='    return "ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً."',
            literal="ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً.",
            current_function="faq_store_info",
        )
        assert result["bucket"] == BUCKET_DETERMINISTIC_TEMPLATE
        assert result.get("kb_risk_path") == "templates.faq_store_info"
        assert result.get("kb_delivery_mode") == "merchant_structured_field_in_reply"

    def test_cta_label_classified_technical_allowlist(self):
        result = classify_audit_finding(
            filepath="modules/ai/brain/compose/responder.py",
            line_content='cta_title = "فتح الرابط"',
            literal="فتح الرابط",
        )
        assert result["bucket"] == BUCKET_TECHNICAL_ALLOWLIST

    def test_legacy_catalog_phrase_detection_only(self):
        phrase = next(iter(LEGACY_DETECTION_MARKERS))
        result = classify_audit_finding(
            filepath="modules/ai/postprocess/safety_nets.py",
            line_content=f'FORBIDDEN_CATALOG = frozenset({{"{phrase}"}})',
            literal=phrase,
        )
        assert result["bucket"] == BUCKET_LEGACY_DETECTION

    def test_prompt_hint_classified_prompt_only(self):
        result = classify_audit_finding(
            filepath="modules/ai/brain/prompt/prompt_builder.py",
            line_content='hint = "اذكر سياسة الشحن بوضوح للعميل"',
            literal="اذكر سياسة الشحن بوضوح للعميل",
        )
        assert result["bucket"] == BUCKET_PROMPT_ONLY

    def test_internal_advisor_classified_internal_tooling(self):
        result = classify_audit_finding(
            filepath="modules/ai/improvement_advisor.py",
            line_content='note = "تحليل داخلي: راجع قالب FAQ"',
            literal="تحليل داخلي: راجع قالب FAQ",
        )
        assert result["bucket"] == BUCKET_INTERNAL_TOOLING

    def test_whatsapp_webhook_outbound_risk(self):
        result = classify_audit_finding(
            filepath="routers/whatsapp_webhook.py",
            line_content='reply = "تم استلام طلبك وسنتواصل معك قريباً"',
            literal="تم استلام طلبك وسنتواصل معك قريباً",
        )
        assert result["bucket"] == BUCKET_OUTBOUND_CUSTOMER_FACING_RISK


class TestAuditSummary:
    def test_duplicate_strings_do_not_inflate_unique_counts(self):
        dup = "نفس النص العربي للاختبار"
        findings = [
            {
                "file": "modules/ai/brain/compose/templates.py",
                "line": 1,
                "bucket": BUCKET_DETERMINISTIC_TEMPLATE,
                "preview": dup,
            },
            {
                "file": "modules/ai/postprocess/safety_nets.py",
                "line": 2,
                "bucket": BUCKET_OUTBOUND_CUSTOMER_FACING_RISK,
                "preview": dup,
            },
            {
                "file": "modules/ai/postprocess/safety_nets.py",
                "line": 3,
                "bucket": BUCKET_OUTBOUND_CUSTOMER_FACING_RISK,
                "preview": "نص مختلف",
            },
        ]
        summary = build_summary(findings, raw_arabic_string_count=3, scanned_paths=["modules/ai"])
        assert summary["actual_customer_facing_risk_count"] == 3
        assert summary["unique_customer_facing_risk_count"] == 2
        assert summary["duplicates_count"] == 1

    def test_json_output_contains_summary_fields(self):
        proc = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        required = {
            "total_findings",
            "raw_arabic_string_count",
            "production_code_count",
            "tests_count",
            "actual_customer_facing_risk_count",
            "unique_customer_facing_risk_count",
            "regex_or_intent_count",
            "prompt_only_count",
            "internal_only_count",
            "technical_allowlist_count",
            "meta_template_count",
            "duplicates_count",
            "scanned_paths",
            "excluded_paths",
            "kb_disclaimer",
            "by_bucket",
        }
        missing = required - set(report.keys())
        assert not missing, f"missing summary keys: {missing}"
        assert isinstance(report["scanned_paths"], list)
        assert isinstance(report["excluded_paths"], list)
        assert report["raw_arabic_string_count"] >= report["total_findings"]
