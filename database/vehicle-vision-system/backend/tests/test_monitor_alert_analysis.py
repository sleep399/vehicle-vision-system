"""告警结构化分析单元测试 —— 对齐任务书四类信息互不重叠。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.utils.alert_analysis import (
    build_event_impact,
    build_severity_assessment,
    build_structured_alert,
    format_trigger_conditions,
    merge_llm_structured,
)
from app.utils.user_language import assistant_answer_for_user


class AlertAnalysisTest(unittest.TestCase):
    def test_structured_alert_has_required_spec_fields(self):
        structured = build_structured_alert(
            "lpr_consecutive_failure",
            "critical",
            {"count": 5, "module": "lpr"},
        )
        self.assertIn("event_type_cn", structured)
        self.assertIn("occurred_at", structured)
        self.assertIn("impact_scope", structured)
        self.assertIn("root_cause", structured)
        self.assertIn("suggestion", structured)
        self.assertIn("severity_assessment", structured)
        self.assertIn("车牌", structured["impact_scope"])

    def test_severity_assessment_differs_from_impact(self):
        ctx = {"count": 5, "rate": "45%"}
        sev = build_severity_assessment("lpr_high_failure_rate", "warning", ctx)
        impact = build_event_impact("lpr_high_failure_rate", "warning", ctx)
        self.assertIn("级别", sev["summary_text"])
        self.assertNotEqual(sev["summary_text"], impact)

    def test_format_trigger_conditions_human_readable(self):
        text = format_trigger_conditions(
            "email_delivery_failure",
            {"fails": 5, "window": "最近5次", "decided_level": "warning", "original_level": "warning"},
        )
        self.assertNotIn("decided_level", text)
        self.assertNotIn("fails", text)
        self.assertIn("邮件", text)
        self.assertIn("警告", text)

    def test_assistant_intents_produce_different_answers(self):
        ctx = {
            "event_type": "lpr_consecutive_failure",
            "title": "车牌识别连续失败",
            "level": "critical",
            "detail": {"count": 5, "structured": {}},
        }
        root = assistant_answer_for_user("根因是什么？", ctx, intent="root_cause")
        action = assistant_answer_for_user("怎么处理？", ctx, intent="action")
        severity = assistant_answer_for_user("要升级吗？", ctx, intent="severity")
        impact = assistant_answer_for_user("影响多大？", ctx, intent="impact")
        self.assertNotEqual(root, action)
        self.assertNotEqual(severity, impact)
        self.assertIn("级别", severity)

    def test_merge_llm_structured_keeps_impact_separate(self):
        structured = build_structured_alert(
            "gesture_low_confidence", "warning",
            {"confidence": 0.25, "module": "police"},
        )
        merged = merge_llm_structured(
            {
                "title": "手势不太准",
                "summary": "刚才交警手势识别连续几次结果不太确定。",
                "root_cause": "可能是手部没完全入镜。",
                "suggestion": "您可以先调整摄像头角度，再重试识别。",
                "impact_scope": "交警手势解读可能不可靠。",
                "occurred_at": "刚刚",
            },
            structured,
        )
        self.assertNotIn("您可以先", merged["root_cause"])
        self.assertEqual(merged["impact_scope"], "交警手势解读可能不可靠。")


if __name__ == "__main__":
    unittest.main()
