"""告警智能体单元测试 —— 覆盖感知、决策、告警、统计、回放等核心功能"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.database import Base, SessionLocal, engine
from app.config import settings
from app.models.alerts import AlertEvent
from app.models.logs import SystemLog
from app.services.alert_agent import alert_agent, EVENT_TYPES, DEFAULT_LEVELS
from app.services.llm_service import llm_service
from app.utils.logger import alert_level_to_cn


class AlertAgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        # 重置告警冷却状态，避免跨测试干扰
        alert_agent._last_alert_time.clear()
        alert_agent._failure_counts.clear()
        alert_agent._confidence_history.clear()
        alert_agent._failure_timestamps.clear()
        alert_agent._token_usage = {"used": 0, "limit": 100000}
        # 单元测试必须可离线、可重复，不能消耗开发者真实 LLM 配额。
        self._llm_api_key = settings.llm_api_key
        settings.llm_api_key = ""

    async def asyncTearDown(self):
        settings.llm_api_key = self._llm_api_key
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    # ── 基础告警能力 ──
    async def test_handle_llm_failure_creates_alert_and_log(self):
        alert = await alert_agent.handle_llm_failure(
            self.db, RuntimeError("timeout"), {"request_id": "demo-001"},
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.event_type, "llm_api_timeout")
        self.assertEqual(alert.level, "critical")
        self.assertIn("timeout", alert.detail_json)
        self.assertEqual(self.db.query(AlertEvent).count(), 1)
        self.assertGreaterEqual(self.db.query(SystemLog).count(), 1)

    async def test_monitor_pipeline_uses_agent_workflow(self):
        alert = await alert_agent.monitor(
            self.db, event_type="unauthorized_access", level="warning",
            context={"path": "/api/monitor", "ip": "127.0.0.1"},
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.event_type, "unauthorized_access")
        self.assertEqual(alert.level, "warning")

    async def test_assistant_can_answer_alert_questions(self):
        answer = await llm_service.ask_assistant(
            "这个异常的根因是什么？",
            {"event_type": "unauthorized_access", "path": "/api/monitor", "ip": "127.0.0.1"},
        )
        self.assertIsInstance(answer, str)
        self.assertGreater(len(answer), 0)

    def test_strip_markdown_removes_bold_markers(self):
        """助手回答应去除 Markdown 加粗符号"""
        raw = "现在最应该处理的告警是：**系统配置不完整**。**发生了什么**：配置缺失。"
        cleaned = llm_service._strip_markdown(raw)
        self.assertNotIn("**", cleaned)
        self.assertIn("系统配置不完整", cleaned)
        self.assertIn("发生了什么", cleaned)

    # ── 增强：事件类型完整性 ──
    def test_all_event_types_defined(self):
        """验证所有事件类型都已定义且有中文名称"""
        required_types = [
            "lpr_consecutive_failure", "lpr_high_failure_rate",
            "gesture_low_confidence", "llm_api_timeout",
            "llm_token_exhausted", "llm_token_exceeded",
            "unauthorized_access", "service_unhealthy",
            "model_load_failure", "database_connection_error",
            "webhook_delivery_failure", "email_delivery_failure",
            "config_missing", "test_event",
        ]
        for t in required_types:
            self.assertIn(t, EVENT_TYPES, f"缺少事件类型: {t}")
            self.assertIsInstance(EVENT_TYPES[t], str)

    def test_all_event_types_have_default_levels(self):
        """验证所有事件类型都有默认告警级别"""
        for event_type in EVENT_TYPES:
            self.assertIn(event_type, DEFAULT_LEVELS, f"缺少默认级别: {event_type}")
            self.assertIn(DEFAULT_LEVELS[event_type], ["info", "warning", "critical"])

    # ── 增强：告警冷却机制 ──
    async def test_alert_cooldown_respected(self):
        """同类型告警在冷却期内不应重复触发"""
        alert1 = await alert_agent.monitor(
            self.db, "unauthorized_access", "warning",
            {"path": "/api/test", "ip": "127.0.0.1"},
        )
        self.assertIsNotNone(alert1)

        # 同一个 monitor 循环内，冷却应该阻止第二次告警
        alert2 = await alert_agent.monitor(
            self.db, "unauthorized_access", "warning",
            {"path": "/api/test", "ip": "127.0.0.1"},
        )
        self.assertIsNone(alert2, "冷却期内不应重复告警")

    async def test_trigger_alert_bypasses_cooldown(self):
        """手动触发告警应绕过冷却机制"""
        await alert_agent.trigger_alert(self.db, "config_missing", "warning", {"config_key": "TEST"})
        alert = await alert_agent.trigger_alert(self.db, "config_missing", "warning", {"config_key": "TEST"})
        self.assertIsNotNone(alert)
        self.assertEqual(self.db.query(AlertEvent).count(), 2)

    # ── 增强：告警统计 ──
    async def test_get_stats_returns_structured_data(self):
        """验证统计数据包含所有必需字段"""
        await alert_agent.trigger_alert(self.db, "lpr_consecutive_failure", "critical", {"count": 5})
        await alert_agent.trigger_alert(self.db, "gesture_low_confidence", "warning", {"confidence": 0.3})
        await alert_agent.trigger_alert(self.db, "llm_token_exhausted", "warning", {"used": 80000})

        stats = alert_agent.get_stats(self.db)
        self.assertGreaterEqual(stats["total"], 3)
        self.assertIn("by_level", stats)
        self.assertIn("by_type", stats)
        self.assertIn("by_type_ranked", stats)
        self.assertIn("hourly_distribution", stats)
        self.assertIn("date_trend", stats)
        self.assertIn("recent", stats)
        self.assertIn("token_usage", stats)
        self.assertIn("resolution_rate", stats)
        self.assertIn("today_count", stats)
        self.assertIn("week_count", stats)

    # ── 增强：告警回放 ──
    async def test_event_replay_returns_full_context(self):
        """验证告警回放返回完整的上下文信息"""
        alert = await alert_agent.trigger_alert(
            self.db, "lpr_consecutive_failure", "critical",
            {"count": 5, "module": "lpr"},
        )
        replay = alert_agent.get_event_replay(self.db, alert.id)
        self.assertIsNotNone(replay)
        trigger = replay["cause_analysis"]["cause_chain"][0]
        self.assertEqual(trigger["title"], "触发条件")
        self.assertNotIn("decided_level", trigger["description"])
        self.assertIn("车牌", trigger["description"])
        self.assertIn("alert", replay)
        self.assertIn("related_logs", replay)
        self.assertIn("cause_analysis", replay)
        self.assertIn("timeline_events", replay)
        self.assertEqual(replay["alert"]["id"], alert.id)
        self.assertEqual(replay["alert"]["level"], "critical")
        self.assertEqual(replay["alert"]["level_cn"], "严重")
        self.assertEqual(replay["alert"]["status_cn"], "未处理")
        self.assertIn("primary_cause", replay["cause_analysis"])

    def test_event_replay_returns_none_for_invalid_id(self):
        """无效告警 ID 应返回 None"""
        replay = alert_agent.get_event_replay(self.db, 99999)
        self.assertIsNone(replay)

    async def test_get_timeline_groups_by_date(self):
        """时间线应按日期分组并支持分页"""
        await alert_agent.trigger_alert(self.db, "test_event", "info", {"source": "t1"})
        await alert_agent.trigger_alert(self.db, "test_event", "warning", {"source": "t2"})
        timeline = alert_agent.get_timeline(self.db, limit=10)
        self.assertIn("groups", timeline)
        self.assertIn("total", timeline)
        self.assertIn("has_more", timeline)
        self.assertGreaterEqual(timeline["total"], 2)

    async def test_get_analytics_returns_range_stats(self):
        """分析接口应返回区间统计"""
        await alert_agent.trigger_alert(self.db, "lpr_consecutive_failure", "critical", {})
        analytics = alert_agent.get_analytics(self.db, days=7)
        self.assertEqual(analytics["days"], 7)
        self.assertIn("by_type_ranked", analytics)
        self.assertIn("hourly_distribution", analytics)
        self.assertGreaterEqual(analytics["total"], 1)

    # ── 增强：事件类型列表 ──
    def test_get_event_types_returns_all_types(self):
        """验证事件类型列表完整性"""
        types = alert_agent.get_event_types()
        self.assertIsInstance(types, list)
        self.assertEqual(len(types), len(EVENT_TYPES))
        for item in types:
            self.assertIn("key", item)
            self.assertIn("name", item)
            self.assertIn("default_level", item)
            self.assertIn("default_level_cn", item)
            self.assertEqual(item["default_level_cn"], alert_level_to_cn(item["default_level"]))

    # ── 增强：自动感知走 monitor 管线（含冷却） ──
    async def test_check_and_alert_respects_cooldown(self):
        """连续失败触发告警后，冷却期内不应重复弹窗"""
        from app.config import settings
        threshold = settings.alert_failure_threshold
        for _ in range(threshold):
            alert_agent.record_lpr_result(False)
        alert1 = await alert_agent.check_and_alert(self.db, "lpr")
        self.assertIsNotNone(alert1)
        self.assertEqual(alert1.event_type, "lpr_consecutive_failure")
        alert2 = await alert_agent.check_and_alert(self.db, "lpr")
        self.assertIsNone(alert2)

    # ── 增强：自主决策 ──
    def test_decide_level_upgrades_on_high_failure_rate(self):
        """高失败率应升级为 critical"""
        level = alert_agent._decide_level("lpr_high_failure_rate", "warning", {"rate": "70%"})
        self.assertEqual(level, "critical")

    def test_decide_level_keeps_warning_on_moderate_rate(self):
        """中等失败率保持 warning"""
        level = alert_agent._decide_level("lpr_high_failure_rate", "warning", {"rate": "40%"})
        self.assertEqual(level, "warning")

    def test_decide_level_upgrades_unauthorized_on_high_count(self):
        """大量未授权访问应升级为 critical"""
        level = alert_agent._decide_level("unauthorized_access", "warning", {"count": 15})
        self.assertEqual(level, "critical")

    # ── 增强：感知模块 ──
    def test_record_lpr_result_tracks_failures(self):
        """车牌识别结果记录应正确追踪"""
        for _ in range(8):
            alert_agent.record_lpr_result(False)
        failures = list(alert_agent._failure_counts["lpr"])
        self.assertEqual(len(failures), 8)
        self.assertEqual(sum(failures), 8)

    def test_record_gesture_confidence_tracks_history(self):
        """手势置信度记录应正确追踪"""
        for i in range(10):
            alert_agent.record_gesture_confidence("police", 0.3)
        confs = list(alert_agent._confidence_history["police"])
        self.assertEqual(len(confs), 10)
        self.assertTrue(all(c == 0.3 for c in confs))

    def test_record_llm_call_tracks_token_usage(self):
        """LLM 调用应正确追踪 Token 用量"""
        initial = alert_agent._token_usage["used"]
        alert_agent.record_llm_call(success=True, tokens_used=1500)
        self.assertEqual(alert_agent._token_usage["used"], initial + 1500)

    def test_perception_snapshot_includes_lpr_and_gesture(self):
        """感知快照应包含车牌与手势模块状态"""
        alert_agent.record_lpr_result(False)
        alert_agent.record_lpr_result(False)
        alert_agent.record_gesture_confidence("police", 0.25)
        alert_agent.record_gesture_confidence("police", 0.22)

        snap = alert_agent.get_perception_snapshot()
        self.assertIn("lpr", snap)
        self.assertGreaterEqual(snap["lpr"]["consecutive_failures"], 2)
        self.assertIn("gesture", snap)
        self.assertIn("police", snap["gesture"])

    def test_assistant_template_differs_by_event_type(self):
        """模板回答应随异常类型变化"""
        from app.utils.user_language import assistant_answer_for_user

        lpr = assistant_answer_for_user(
            "应该怎么处理？",
            {"event_type": "lpr_consecutive_failure", "title": "车牌识别连续失败",
             "detail": {"count": 5}, "level": "critical"},
        )
        gesture = assistant_answer_for_user(
            "应该怎么处理？",
            {"event_type": "gesture_low_confidence", "title": "手势识别不太准",
             "detail": {"confidence": 0.28, "module": "police"}, "level": "warning"},
        )
        self.assertIn("车牌", lpr)
        self.assertIn("5", lpr)
        self.assertIn("手势", gesture)
        self.assertNotEqual(lpr, gesture)

    def test_assistant_intent_detection(self):
        """问题意图识别应区分根因/处理/影响"""
        from app.utils.user_language import detect_assistant_intent

        self.assertEqual(detect_assistant_intent("这个异常的根因是什么？"), "root_cause")
        self.assertEqual(detect_assistant_intent("应该怎么处理？"), "action")
        self.assertEqual(detect_assistant_intent("当前告警影响有多大？"), "impact")
        self.assertEqual(detect_assistant_intent("是否需要升级为严重告警？"), "severity")

    def test_needs_alert_context(self):
        """需绑定具体告警的问题与系统级问题应区分"""
        from app.utils.user_language import needs_alert_context

        self.assertTrue(needs_alert_context("这个异常的根因是什么？"))
        self.assertTrue(needs_alert_context("应该怎么处理？"))
        self.assertTrue(needs_alert_context("当前告警影响有多大？"))
        self.assertFalse(needs_alert_context("系统正常吗？"))
        self.assertFalse(needs_alert_context("现在整体状态怎么样？"))

    def test_build_which_alert_prompt_lists_open_alerts(self):
        from app.utils.user_language import build_which_alert_prompt

        msg = build_which_alert_prompt([
            {"level": "warning", "title": "系统配置不完整", "event_type_cn": "关键配置缺失"},
        ])
        self.assertIn("您指的是哪条告警", msg)
        self.assertIn("系统配置不完整", msg)

    async def test_assistant_asks_clarification_without_alert_context(self):
        """未指定告警时，根因类问题应反问而非直接作答"""
        from app.routers.monitor import assistant_chat, AssistantQuery

        await alert_agent.trigger_alert(
            self.db, "config_missing", "warning", {"config_key": "webhook_url"},
        )
        result = await assistant_chat(
            AssistantQuery(question="这个异常的根因是什么？"),
            db=self.db,
        )
        self.assertTrue(result.get("needs_clarification"))
        self.assertIn("您指的是哪条告警", result["answer"])
        self.assertNotIn("群消息推送", result["answer"])

    async def test_assistant_answers_when_alert_id_provided(self):
        from app.routers.monitor import assistant_chat, AssistantQuery

        alert = await alert_agent.trigger_alert(
            self.db, "config_missing", "warning", {"config_key": "webhook_url"},
        )
        result = await assistant_chat(
            AssistantQuery(question="这个异常的根因是什么？", alert_id=alert.id),
            db=self.db,
        )
        self.assertFalse(result.get("needs_clarification"))
        self.assertGreater(len(result["answer"]), 0)

    # ── 增强：多渠道告警创建 ──
    async def test_create_alert_includes_structured_detail(self):
        """告警创建应包含任务书要求的结构化字段（类型/时间/影响/根因/建议/级别决策）"""
        alert = await alert_agent.trigger_alert(
            self.db, "lpr_consecutive_failure", "critical", {"count": 5, "module": "lpr"},
        )
        detail = __import__("json").loads(alert.detail_json)
        structured = detail.get("structured") or {}
        self.assertIn("occurred_at", structured)
        self.assertIn("impact_scope", structured)
        self.assertIn("severity_assessment", structured)
        self.assertTrue(alert.root_cause)
        self.assertTrue(alert.suggestion)

    async def test_create_alert_includes_channels(self):
        """告警创建应包含推送渠道信息"""
        alert = await alert_agent.trigger_alert(
            self.db, "test_event", "info", {"source": "unit_test"},
        )
        self.assertIn("web", alert.channels_sent)
        self.assertEqual(alert.status, "open")


if __name__ == "__main__":
    unittest.main()
