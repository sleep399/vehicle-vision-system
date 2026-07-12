"""
告警智能体 (Alert Agent) —— 具备自主感知、决策与告警发送能力的智能运维模块。

能力清单:
  - 感知: 车牌识别失败、手势置信度偏低、LLM API 超时/Token超额、未授权访问、
          服务健康异常、模型加载失败
  - 决策: 基于规则 + 历史上下文的告警级别自动判定（提示/警告/严重）
  - 推送: WebSocket 实时广播、SSE 事件流、Webhook(企业微信/钉钉)、Email(SMTP)
  - 记录: 告警事件持久化、告警日志、统计聚合
"""

import asyncio
import json
import os
import platform
import smtplib
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alerts import AlertEvent
from app.utils.logger import write_log, log_exception, write_alert_log, write_agent_log, get_logger, localize_utc as _localize_utc, alert_level_to_cn, channels_to_cn, level_to_cn
from app.utils.log_display import format_log_entry, format_record_entry, category_cn, record_type_cn, sanitize_log_message

agent_logger = get_logger("alert_agent")


# ──────────────────────────────────────────────
# 事件类型定义
# ──────────────────────────────────────────────
EVENT_TYPES = {
    "lpr_consecutive_failure": "车牌识别连续失败",
    "lpr_high_failure_rate": "车牌识别失败率过高",
    "gesture_low_confidence": "手势识别置信度持续偏低",
    "llm_api_timeout": "LLM API 调用超时",
    "llm_token_exhausted": "LLM Token 配额即将耗尽",
    "llm_token_exceeded": "LLM Token 配额已超额",
    "unauthorized_access": "未授权访问尝试",
    "service_unhealthy": "系统服务健康异常",
    "model_load_failure": "AI 模型加载失败",
    "database_connection_error": "数据库连接异常",
    "webhook_delivery_failure": "Webhook 推送失败",
    "email_delivery_failure": "邮件推送失败",
    "config_missing": "关键配置缺失",
    "test_event": "测试告警",
}

# 可选配置缺失：只记日志，不反复弹告警（webhook/邮件/LLM 未配属于正常演示状态）
OPTIONAL_CONFIG_KEYS = frozenset({
    "webhook_url", "webhook", "smtp", "smtp/email", "email",
    "llm_api_key", "llm",
})

DEFAULT_LEVELS = {
    "lpr_consecutive_failure": "critical",
    "lpr_high_failure_rate": "warning",
    "gesture_low_confidence": "warning",
    "llm_api_timeout": "critical",
    "llm_token_exhausted": "warning",
    "llm_token_exceeded": "critical",
    "unauthorized_access": "warning",
    "service_unhealthy": "critical",
    "model_load_failure": "critical",
    "database_connection_error": "critical",
    "webhook_delivery_failure": "warning",
    "email_delivery_failure": "warning",
    "config_missing": "warning",
    "test_event": "info",
}


class AlertAgent:
    """告警智能体：感知异常 → 自主决策级别 → 生成LLM摘要 → 多渠道推送通知"""

    LEVELS = {"info": 1, "warning": 2, "critical": 3}

    def __init__(self):
        # ── 感知状态 ──
        self._failure_counts: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._confidence_history: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._failure_timestamps: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._token_usage: dict[str, int] = {"used": 0, "limit": settings.alert_token_limit}

        # ── 推送通道 ──
        self._ws_clients: set = set()
        self._sse_queues: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

        # ── 告警冷却（去重） ──
        self._last_alert_time: dict[str, datetime] = {}
        self._patrol_task: asyncio.Task | None = None

    # ════════════════════════════════════════════
    # 连接管理
    # ════════════════════════════════════════════

    def register_ws(self, ws):
        self._ws_clients.add(ws)

    def unregister_ws(self, ws):
        self._ws_clients.discard(ws)

    def register_sse(self, queue: asyncio.Queue):
        self._sse_queues.append(queue)

    def unregister_sse(self, queue: asyncio.Queue):
        try:
            self._sse_queues.remove(queue)
        except ValueError:
            pass

    async def broadcast(self, data: dict):
        """WebSocket 广播"""
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    async def broadcast_sse(self, data: dict):
        """SSE 广播"""
        for q in self._sse_queues[:]:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    # ════════════════════════════════════════════
    # 感知模块 —— 记录各模块运行状态
    # ════════════════════════════════════════════

    def record_lpr_result(self, success: bool):
        """记录车牌识别结果（成功/失败）"""
        self._failure_counts["lpr"].append(0 if success else 1)
        self._failure_timestamps["lpr"].append((datetime.utcnow(), success))

    def record_gesture_confidence(self, module: str, confidence: float):
        """记录手势识别置信度"""
        self._confidence_history[module].append(confidence)

    def record_gesture_failure(self, module: str):
        """记录手势识别失败（等效于置信度 0）"""
        self._confidence_history[module].append(0.0)

    def record_llm_call(self, success: bool, tokens_used: int = 0):
        """记录 LLM API 调用情况与 Token 用量"""
        self._token_usage["used"] += tokens_used
        if not success:
            self._failure_counts["llm"].append(1)
        else:
            self._failure_counts["llm"].append(0)

    def record_db_connection(self, success: bool):
        """记录数据库连接状态"""
        self._failure_counts["db"].append(0 if success else 1)

    def record_webhook_result(self, success: bool):
        """记录 Webhook 推送结果"""
        if not success:
            self._failure_counts["webhook"].append(1)
        else:
            self._failure_counts["webhook"].append(0)

    def record_email_result(self, success: bool):
        """记录邮件推送结果"""
        if not success:
            self._failure_counts["email"].append(1)
        else:
            self._failure_counts["email"].append(0)

    @staticmethod
    def _count_trailing_ones(items: list[int | float]) -> int:
        """统计 deque 末尾连续为 1（失败）的次数。"""
        count = 0
        for val in reversed(items):
            if val == 1 or val == 1.0:
                count += 1
            else:
                break
        return count

    def get_perception_snapshot(self) -> dict[str, Any]:
        """返回各模块实时感知状态，供智能助手问答使用。"""
        snapshot: dict[str, Any] = {}
        now = datetime.utcnow()
        window = timedelta(seconds=settings.alert_window_seconds)

        lpr_fails = list(self._failure_counts.get("lpr", []))
        if lpr_fails:
            recent = lpr_fails[-10:]
            window_records = [
                (ts, ok) for ts, ok in self._failure_timestamps.get("lpr", [])
                if ts >= now - window
            ]
            window_total = len(window_records)
            window_fail_count = sum(1 for _, ok in window_records if not ok)
            snapshot["lpr"] = {
                "recent_attempts": len(recent),
                "recent_failures": int(sum(recent)),
                "consecutive_failures": self._count_trailing_ones(lpr_fails),
                "failure_threshold": settings.alert_failure_threshold,
                "window_seconds": settings.alert_window_seconds,
                "window_failure_rate": (
                    round(window_fail_count / window_total, 2) if window_total else 0
                ),
            }

        gesture_modules: dict[str, Any] = {}
        for module in ("police", "owner"):
            confs = list(self._confidence_history.get(module, []))
            if not confs:
                continue
            recent_5 = confs[-5:]
            avg_conf = sum(recent_5) / len(recent_5)
            gesture_modules[module] = {
                "avg_confidence_last_5": round(avg_conf, 3),
                "threshold": settings.low_confidence_threshold,
                "all_below_threshold": (
                    len(recent_5) >= 5
                    and all(c < settings.low_confidence_threshold for c in recent_5)
                ),
                "latest_confidence": round(confs[-1], 3),
            }
        if gesture_modules:
            snapshot["gesture"] = gesture_modules

        llm_fails = list(self._failure_counts.get("llm", []))
        used = self._token_usage["used"]
        limit = self._token_usage["limit"]
        snapshot["llm"] = {
            "token_used": used,
            "token_limit": limit,
            "token_ratio_pct": round(used / max(limit, 1) * 100, 1),
            "recent_api_failures": int(sum(llm_fails[-3:])) if llm_fails else 0,
        }

        db_fails = list(self._failure_counts.get("db", []))
        if db_fails:
            snapshot["database"] = {
                "consecutive_failures": self._count_trailing_ones(db_fails),
            }

        unauth = list(self._failure_timestamps.get("unauthorized", []))
        if unauth:
            snapshot["unauthorized_access"] = {
                "attempts_in_window": sum(1 for ts, _ in unauth if ts >= now - window),
                "window_seconds": settings.alert_window_seconds,
            }

        webhook_fails = list(self._failure_counts.get("webhook", []))
        if webhook_fails:
            snapshot["webhook"] = {"recent_failures": int(sum(webhook_fails[-5:]))}

        email_fails = list(self._failure_counts.get("email", []))
        if email_fails:
            snapshot["email"] = {"recent_failures": int(sum(email_fails[-5:]))}

        return snapshot

    # ════════════════════════════════════════════
    # 感知与检测 —— 周期检查异常事件
    # ════════════════════════════════════════════

    async def track_llm_success(self, db: Session, tokens_used: int = 0) -> None:
        """记录 LLM 成功调用并检查 Token/失败率异常"""
        self.record_llm_call(success=True, tokens_used=tokens_used)
        await self.check_and_alert(db, "llm")

    async def check_and_alert(self, db: Session, module: str) -> AlertEvent | None:
        """感知系统异常并触发告警"""
        event = None

        if module == "lpr":
            event = await self._check_lpr_anomalies(db)
        elif module in ("police", "owner"):
            event = await self._check_gesture_anomalies(db, module)
        elif module == "llm":
            event = await self._check_llm_anomalies(db)
        elif module == "db":
            event = await self._check_db_anomalies(db)
        elif module == "webhook":
            event = await self._check_webhook_anomalies(db)
        elif module == "email":
            event = await self._check_email_anomalies(db)

        return event

    async def _check_lpr_anomalies(self, db: Session) -> AlertEvent | None:
        """检测车牌识别异常"""
        failures = list(self._failure_counts["lpr"])
        threshold = settings.alert_failure_threshold

        # 1) 连续失败检测
        if len(failures) >= threshold:
            recent = failures[-threshold:]
            if sum(recent) == threshold:
                return await self.monitor(
                    db, "lpr_consecutive_failure", "critical",
                    {"count": threshold, "module": "lpr", "window": f"最近{threshold}次"}
                )

        # 2) 滑动窗口失败率检测
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=settings.alert_window_seconds)
        window_fails = [
            ts for ts, ok in self._failure_timestamps["lpr"]
            if ts >= cutoff and not ok
        ]
        window_total = len([ts for ts, _ in self._failure_timestamps["lpr"] if ts >= cutoff])
        if window_total >= 10:
            rate = len(window_fails) / window_total
            if rate > settings.alert_anomaly_rate_threshold:
                return await self.monitor(
                    db, "lpr_high_failure_rate", "warning",
                    {"rate": f"{rate:.0%}", "fails": len(window_fails), "total": window_total,
                     "window_seconds": settings.alert_window_seconds}
                )

        return None

    async def _check_gesture_anomalies(self, db: Session, module: str) -> AlertEvent | None:
        """检测手势识别置信度异常"""
        confs = list(self._confidence_history[module])
        if len(confs) >= 5:
            recent_5 = confs[-5:]
            if all(c < settings.low_confidence_threshold for c in recent_5):
                avg_conf = sum(recent_5) / 5
                return await self.monitor(
                    db, "gesture_low_confidence", "warning",
                    {"confidence": avg_conf, "module": module, "threshold": settings.low_confidence_threshold}
                )
        return None

    async def _check_llm_anomalies(self, db: Session) -> AlertEvent | None:
        """检测 LLM 异常（Token 用量 + 调用失败）"""
        # Token 用量检测
        used = self._token_usage["used"]
        limit = self._token_usage["limit"]
        ratio = used / limit if limit > 0 else 0

        if ratio >= 1.0:
            return await self.monitor(
                db, "llm_token_exceeded", "critical",
                {"used": used, "limit": limit, "ratio": f"{ratio:.1%}"}
            )
        if ratio >= (settings.alert_token_critical_threshold / max(settings.alert_token_limit, 1)):
            return await self.monitor(
                db, "llm_token_exhausted", "critical",
                {"used": used, "limit": limit, "ratio": f"{ratio:.1%}",
                 "remaining": limit - used}
            )
        if ratio >= (settings.alert_token_warning_threshold / max(settings.alert_token_limit, 1)):
            return await self.monitor(
                db, "llm_token_exhausted", "warning",
                {"used": used, "limit": limit, "ratio": f"{ratio:.1%}",
                 "remaining": limit - used}
            )

        # LLM 调用失败检测
        llm_fails = list(self._failure_counts["llm"])
        if len(llm_fails) >= 3 and sum(llm_fails[-3:]) >= 2:
            return await self.monitor(
                db, "llm_api_timeout", "critical",
                {"fails": sum(llm_fails[-3:]), "window": "最近3次"}
            )

        return None

    async def _check_db_anomalies(self, db: Session) -> AlertEvent | None:
        """检测数据库异常"""
        fails = list(self._failure_counts["db"])
        if len(fails) >= 3 and all(f == 1 for f in fails[-3:]):
            return await self.monitor(
                db, "database_connection_error", "critical",
                {"consecutive_fails": 3}
            )
        return None

    async def _check_webhook_anomalies(self, db: Session) -> AlertEvent | None:
        """检测 Webhook 推送异常"""
        fails = list(self._failure_counts["webhook"])
        if len(fails) >= 5 and sum(fails[-5:]) >= 3:
            return await self.monitor(
                db, "webhook_delivery_failure", "warning",
                {"fails": sum(fails[-5:]), "window": "最近5次"}
            )
        return None

    async def _check_email_anomalies(self, db: Session) -> AlertEvent | None:
        """检测邮件推送异常"""
        fails = list(self._failure_counts["email"])
        if len(fails) >= 5 and sum(fails[-5:]) >= 3:
            return await self.monitor(
                db, "email_delivery_failure", "warning",
                {"fails": sum(fails[-5:]), "window": "最近5次"}
            )
        return None

    @staticmethod
    def _log_categories_for_event(event_type: str) -> list[str]:
        """告警回放时关联的日志类别"""
        if event_type.startswith("lpr"):
            return ["lpr", "agent", "alert"]
        if "gesture" in event_type:
            return ["police_gesture", "owner_gesture", "agent", "alert"]
        if event_type.startswith("llm"):
            return ["agent", "alert", "system"]
        if event_type == "unauthorized_access":
            return ["user", "agent", "alert"]
        return ["agent", "alert", "system"]

    async def run_patrol(self, db: Session) -> None:
        """后台巡检：重检各模块感知状态并写智能体日志"""
        write_agent_log(
            db,
            "智能体后台巡检：重检车牌/手势/LLM/数据库感知状态",
            level="INFO",
            detail={"modules": ["lpr", "police", "owner", "llm", "db"]},
        )
        for module in ("lpr", "police", "owner", "llm", "db"):
            await self.check_and_alert(db, module)

    async def start_patrol_loop(self, db_factory):
        """启动后台巡检循环（默认 60 秒）"""
        async def _loop():
            while True:
                try:
                    db = db_factory()
                    try:
                        await self.run_patrol(db)
                    finally:
                        db.close()
                except Exception as e:
                    agent_logger.error(f"Patrol failed: {e}")
                await asyncio.sleep(60)

        self._patrol_task = asyncio.create_task(_loop())

    def get_recent_agent_logs(self, db: Session, limit: int = 10) -> list[dict]:
        """获取最近智能体决策/推送日志"""
        from app.models.logs import SystemLog

        rows = (
            db.query(SystemLog)
            .filter(SystemLog.category == "agent")
            .order_by(SystemLog.created_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for row in rows:
            detail = None
            if row.detail_json:
                try:
                    detail = json.loads(row.detail_json)
                except Exception:
                    detail = row.detail_json
            result.append({
                "id": row.id,
                "level": level_to_cn(row.level),
                "message": row.message,
                "detail": detail,
                "created_at": _localize_utc(row.created_at),
            })
        return result

    # ════════════════════════════════════════════
    # 决策模块 —— 自主判定告警级别
    # ════════════════════════════════════════════

    def _decide_level(self, event_type: str, level: str, context: dict) -> str:
        """自主决策告警级别（prompt / warning / critical）

        决策依据:
          1. 事件类型的固有严重程度
          2. 上下文中的量化指标（失败次数、使用率等）
          3. 历史告警频率
        """
        # 根据上下文动态升级
        if event_type == "unauthorized_access":
            count = context.get("count", 1)
            if count >= 10:
                return "critical"  # 频繁未授权访问 → 升级为严重
            return "warning"

        if event_type == "lpr_high_failure_rate":
            rate_str = context.get("rate", "0%").replace("%", "")
            try:
                rate = float(rate_str) / 100
            except (ValueError, TypeError):
                rate = 0
            if rate > 0.6:
                return "critical"
            return "warning"

        if event_type == "llm_token_exhausted":
            ratio_str = context.get("ratio", "0%").replace("%", "")
            try:
                ratio = float(ratio_str) / 100
            except (ValueError, TypeError):
                ratio = 0
            if ratio > 0.95:
                return "critical"
            return "warning"

        if event_type == "gesture_low_confidence":
            conf = context.get("confidence", 0)
            if conf < 0.2:
                return "critical"
            return "warning"

        # 使用默认映射
        return DEFAULT_LEVELS.get(event_type, level)

    # ════════════════════════════════════════════
    # 核心告警流程
    # ════════════════════════════════════════════

    async def monitor(
        self,
        db: Session,
        event_type: str,
        level: str = "warning",
        context: dict | None = None,
        *,
        force_template: bool = False,
    ) -> AlertEvent | None:
        """Agent 核心工作流：感知异常 → 决策级别 → 生成摘要 → 推送通知"""
        observed = context or {}

        # 1) 自主决策告警级别
        decision_level = self._decide_level(event_type, level, observed)
        observed["decided_level"] = decision_level
        observed["original_level"] = level

        # 2) 冷却检查（同类型告警间隔）
        if not self._should_alert(event_type):
            agent_logger.info(
                f"Alert suppressed by cooldown: {event_type} "
                f"(last={self._last_alert_time.get(event_type)})"
            )
            write_agent_log(
                db,
                f"告警冷却抑制: {EVENT_TYPES.get(event_type, event_type)}",
                level="INFO",
                detail={
                    "event_type": event_type,
                    "decided_level": decision_level,
                    "last_alert_at": _localize_utc(self._last_alert_time.get(event_type)),
                },
            )
            return None

        write_agent_log(
            db,
            f"告警级别决策: {EVENT_TYPES.get(event_type, event_type)} → {alert_level_to_cn(decision_level)}",
            level="INFO" if decision_level == "info" else ("WARN" if decision_level == "warning" else "CRITICAL"),
            detail={
                "event_type": event_type,
                "original_level": level,
                "decided_level": decision_level,
                "context": observed,
            },
        )

        # 3) 生成告警
        return await self.trigger_alert(db, event_type, decision_level, observed, force_template=force_template)

    def _should_alert(self, event_type: str) -> bool:
        """检查是否应该发送告警（冷却机制）"""
        last = self._last_alert_time.get(event_type)
        if last is None:
            return True
        cooldown_sec = (
            settings.alert_config_cooldown_seconds
            if event_type == "config_missing"
            else settings.alert_cooldown_seconds
        )
        cooldown = timedelta(seconds=cooldown_sec)
        if datetime.utcnow() - last >= cooldown:
            return True
        return False

    async def trigger_alert(
        self,
        db: Session,
        event_type: str,
        level: str = "warning",
        context: dict | None = None,
        *,
        force_template: bool = False,
    ) -> AlertEvent:
        """手动触发告警（绕过冷却）"""
        return await self._create_alert(db, event_type, level, context or {}, force_template=force_template)

    async def handle_llm_failure(
        self,
        db: Session,
        exc: Exception,
        context: dict | None = None,
    ) -> AlertEvent | None:
        """处理 LLM 调用失败（强制模板，避免再次调用 LLM 造成递归）"""
        self.record_llm_call(success=False)
        return await self.monitor(
            db, "llm_api_timeout", "critical",
            {**(context or {}), "error": str(exc), "error_type": type(exc).__name__},
            force_template=True,
        )

    async def handle_unauthorized_access(
        self,
        db: Session,
        path: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AlertEvent:
        """处理未授权访问"""
        # 统计近期未授权访问次数
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=settings.alert_window_seconds)
        recent_count = sum(
            1 for ts, _ in self._failure_timestamps.get("unauthorized", deque())
            if ts >= cutoff
        ) + 1
        self._failure_timestamps["unauthorized"].append((now, False))

        return await self.monitor(
            db, "unauthorized_access", "warning",
            {
                "path": path,
                "ip": ip or "unknown",
                "user_agent": user_agent or "unknown",
                "count": recent_count,
                "window_seconds": settings.alert_window_seconds,
            }
        )

    async def handle_model_load_failure(
        self,
        db: Session,
        model_name: str,
        exc: Exception,
    ) -> AlertEvent:
        """处理 AI 模型加载失败"""
        return await self.monitor(
            db, "model_load_failure", "critical",
            {"model_name": model_name, "error": str(exc), "error_type": type(exc).__name__}
        )

    async def handle_config_missing(
        self,
        db: Session,
        config_key: str,
        severity: str = "warning",
    ) -> AlertEvent | None:
        """处理关键配置缺失；可选配置（webhook/邮件/LLM）仅记日志，不弹告警。"""
        key = (config_key or "").lower()
        if any(opt in key for opt in OPTIONAL_CONFIG_KEYS):
            write_agent_log(
                db,
                f"可选配置未填写: {config_key}（不影响核心功能，已跳过告警）",
                level="INFO",
                detail={"config_key": config_key},
            )
            return None
        return await self.monitor(
            db, "config_missing", severity,
            {"config_key": config_key}
        )

    async def handle_service_unhealthy(
        self,
        db: Session,
        service_name: str,
        detail: str = "",
    ) -> AlertEvent:
        """处理服务健康异常"""
        return await self.monitor(
            db, "service_unhealthy", "critical",
            {"service": service_name, "detail": detail}
        )

    # ════════════════════════════════════════════
    # 告警创建 —— LLM 摘要生成 + 多渠道推送
    # ════════════════════════════════════════════

    async def _create_alert(
        self,
        db: Session,
        event_type: str,
        level: str,
        context: dict,
        *,
        force_template: bool = False,
    ) -> AlertEvent:
        """创建告警事件（生成摘要、持久化、多渠道推送）"""
        from app.services.llm_service import llm_service
        from app.utils.alert_analysis import build_structured_alert, merge_llm_structured

        # 1) 规则结构化分析 + LLM 自然语言润色（字段互不重叠）
        summary_data = await llm_service.generate_alert_summary(
            event_type, level, context, force_template=force_template,
        )
        summary_data.pop("_llm_failed", None)

        now = datetime.utcnow()
        structured = build_structured_alert(
            event_type, level, context, created_at=now,
            root_cause=summary_data.get("root_cause"),
            suggestion=summary_data.get("suggestion"),
            summary=summary_data.get("summary"),
        )
        merged = merge_llm_structured(summary_data, structured)

        detail_payload = {
            **context,
            "structured": {
                "event_type_cn": merged["event_type_cn"],
                "occurred_at": merged["occurred_at"],
                "impact_scope": merged["impact_scope"],
                "severity_assessment": merged["severity_assessment"],
            },
        }

        # 2) 持久化
        alert = AlertEvent(
            level=level,
            event_type=event_type,
            title=summary_data.get("title", EVENT_TYPES.get(event_type, event_type)),
            summary=merged.get("summary", ""),
            detail_json=json.dumps(detail_payload, ensure_ascii=False),
            root_cause=merged.get("root_cause"),
            suggestion=merged.get("suggestion"),
            channels_sent="web",
            system_health_json=json.dumps({
                "perception": self.get_perception_snapshot(),
                "decision_level": level,
                "event_type": event_type,
                "structured": detail_payload.get("structured"),
            }, ensure_ascii=False),
            created_at=now,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)

        # 3) 记录告警冷却时间
        self._last_alert_time[event_type] = now

        # 4) 写入告警日志
        write_alert_log(db, alert.id, level, alert.title, event_type, alert.summary, "web")

        # 5) 构建推送 payload
        payload = {
            "type": "alert",
            "id": alert.id,
            "level": level,
            "level_cn": alert_level_to_cn(level),
            "event_type": event_type,
            "event_type_cn": EVENT_TYPES.get(event_type, event_type),
            "title": alert.title,
            "summary": alert.summary,
            "root_cause": alert.root_cause,
            "suggestion": alert.suggestion,
            "impact_scope": merged.get("impact_scope"),
            "occurred_at": merged.get("occurred_at"),
            "severity_assessment": merged.get("severity_assessment"),
            "detail": detail_payload,
            "created_at": _localize_utc(alert.created_at),
        }

        # 6) 多渠道推送
        channels = ["web"]

        # WebSocket 推送
        await self.broadcast(payload)

        # SSE 推送
        if settings.alert_sse_enabled:
            await self.broadcast_sse(payload)
            channels.append("sse")

        # Webhook 推送（企业微信/钉钉/飞书）
        if settings.alert_webhook_enabled:
            if await self._send_webhook(payload):
                channels.append("webhook")
            else:
                self.record_webhook_result(False)
                agent_logger.warning("Webhook delivery failed")
                await self.check_and_alert(db, "webhook")

        # 邮件推送
        if settings.alert_email_enabled:
            if await self._send_email(alert):
                channels.append("email")
            else:
                self.record_email_result(False)
                agent_logger.warning("Email delivery failed")
                await self.check_and_alert(db, "email")

        # 7) 更新推送渠道
        alert.channels_sent = ",".join(channels)
        db.commit()

        write_agent_log(
            db,
            f"告警 #{alert.id} 已推送 · {EVENT_TYPES.get(event_type, event_type)} · {alert_level_to_cn(level)} · {channels_to_cn(channels)}",
            level="INFO" if level == "info" else ("WARN" if level == "warning" else "CRITICAL"),
            detail={
                "alert_id": alert.id,
                "event_type": event_type,
                "event_type_cn": EVENT_TYPES.get(event_type, event_type),
                "level": level,
                "level_cn": alert_level_to_cn(level),
                "channels": channels,
                "channels_cn": channels_to_cn(channels),
            },
        )

        agent_logger.info(
            f"Alert #{alert.id} dispatched: [{level}] {event_type} "
            f"→ channels={channels}, title={alert.title}"
        )

        return alert

    # ════════════════════════════════════════════
    # 推送渠道实现
    # ════════════════════════════════════════════

    def _detect_webhook_platform(self, url: str) -> str:
        """根据 URL 识别 Webhook 平台类型"""
        url_lower = url.lower()
        if "oapi.dingtalk.com" in url_lower:
            return "dingtalk"
        if "open.feishu.cn" in url_lower or "open.larksuite.com" in url_lower:
            return "feishu"
        if "qyapi.weixin.qq.com" in url_lower:
            return "wechat"
        return "generic"

    def _build_webhook_payload(self, platform: str, payload: dict) -> dict:
        """构建各平台 Webhook 消息体"""
        level_emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(payload["level"], "📢")
        text_content = (
            f"{level_emoji} [{payload['level'].upper()}] {payload['title']}\n\n"
            f"📋 摘要：{payload['summary']}\n"
            f"🔍 根因：{payload.get('root_cause', '待分析')}\n"
            f"💡 建议：{payload.get('suggestion', '请查看系统日志')}\n"
            f"⏰ 时间：{payload['created_at']}"
        )
        if platform == "dingtalk":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": payload["title"],
                    "text": text_content.replace("\n", "\n\n"),
                },
            }
        if platform == "feishu":
            return {
                "msg_type": "text",
                "content": {"text": text_content},
            }
        # 企业微信 / 通用
        return {
            "msgtype": "text",
            "text": {"content": text_content},
        }

    async def _send_webhook(self, payload: dict) -> bool:
        """发送 Webhook（支持企业微信/钉钉/飞书群机器人）"""
        if not settings.webhook_url:
            return False

        try:
            platform = self._detect_webhook_platform(settings.webhook_url)
            body = self._build_webhook_payload(platform, payload)

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(settings.webhook_url, json=body)
                resp.raise_for_status()
            self.record_webhook_result(True)
            return True
        except Exception as e:
            agent_logger.warning(f"Webhook send failed: {e}")
            self.record_webhook_result(False)
            return False

    async def _send_email(self, alert: AlertEvent) -> bool:
        """发送邮件通知"""
        if not all([settings.smtp_host, settings.smtp_user, settings.alert_email_to]):
            return False

        try:
            level_label = {"info": "提示", "warning": "警告", "critical": "严重"}.get(alert.level, "通知")
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif;">
                <h2 style="color: {'#f87171' if alert.level == 'critical' else '#f59e0b' if alert.level == 'warning' else '#60a5fa'};">
                    [{level_label}] {alert.title}
                </h2>
                <table style="border-collapse: collapse; width: 100%;">
                    <tr><td style="padding: 8px; border: 1px solid #ddd; background: #f8fafc;"><b>异常类型</b></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{EVENT_TYPES.get(alert.event_type, alert.event_type)}</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd; background: #f8fafc;"><b>摘要</b></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{alert.summary}</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd; background: #f8fafc;"><b>根因分析</b></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{alert.root_cause or '待分析'}</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd; background: #f8fafc;"><b>建议措施</b></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{alert.suggestion or '请查看系统日志'}</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd; background: #f8fafc;"><b>发生时间</b></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{(_localize_utc(alert.created_at) or alert.created_at.strftime('%Y-%m-%d %H:%M:%S')).replace('T', ' ')[:19]}</td></tr>
                </table>
                <p style="color: #94a3b8; font-size: 12px;">此邮件由车载视觉感知系统告警智能体自动发送</p>
            </body>
            </html>
            """

            msg = MIMEText(html_body, "html", "utf-8")
            msg["Subject"] = f"[{level_label}] [{alert.event_type}] {alert.title}"
            msg["From"] = settings.smtp_user
            msg["To"] = settings.alert_email_to

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)

            self.record_email_result(True)
            return True
        except Exception as e:
            agent_logger.warning(f"Email send failed: {e}")
            self.record_email_result(False)
            return False

    async def send_test_notification(self, channel: str = "all") -> dict[str, Any]:
        """向指定渠道发送测试通知，不写入告警库。"""
        from types import SimpleNamespace

        now = datetime.utcnow()
        created_str = _localize_utc(now) or now.isoformat()
        payload = {
            "type": "test",
            "id": 0,
            "level": "info",
            "event_type": "test_event",
            "title": "【测试】车载视觉系统通知渠道连通性检查",
            "summary": "这是一条测试消息，用于验证 Webhook / 邮件等外部通知渠道配置是否正确。",
            "root_cause": "用户手动触发通知测试",
            "suggestion": "若收到本消息，说明对应渠道配置正常",
            "detail": {"source": "notification_test"},
            "created_at": created_str,
        }
        results: dict[str, Any] = {"channel": channel, "channels": {}}

        if channel in ("web", "all"):
            await self.broadcast(payload)
            results["channels"]["web"] = {"ok": True}
            if settings.alert_sse_enabled:
                await self.broadcast_sse(payload)
                results["channels"]["sse"] = {"ok": True}
            else:
                results["channels"]["sse"] = {"ok": False, "reason": "未启用"}

        if channel in ("webhook", "all"):
            if not settings.alert_webhook_enabled:
                results["channels"]["webhook"] = {"ok": False, "reason": "未启用"}
            elif not settings.webhook_url:
                results["channels"]["webhook"] = {"ok": False, "reason": "未配置 URL"}
            else:
                ok = await self._send_webhook(payload)
                results["channels"]["webhook"] = {"ok": ok}

        if channel in ("email", "all"):
            if not settings.alert_email_enabled:
                results["channels"]["email"] = {"ok": False, "reason": "未启用"}
            elif not all([settings.smtp_host, settings.smtp_user, settings.alert_email_to]):
                results["channels"]["email"] = {"ok": False, "reason": "SMTP 配置不完整"}
            else:
                fake_alert = SimpleNamespace(
                    level="info",
                    title=payload["title"],
                    event_type="test_event",
                    summary=payload["summary"],
                    root_cause=payload["root_cause"],
                    suggestion=payload["suggestion"],
                    created_at=now,
                )
                ok = await self._send_email(fake_alert)
                results["channels"]["email"] = {"ok": ok}

        return results

    # ════════════════════════════════════════════
    # 统计与可视化数据
    # ════════════════════════════════════════════

    def _alert_to_dict(self, a: AlertEvent) -> dict[str, Any]:
        """将告警 ORM 对象转为 API 字典"""
        detail = {}
        structured = {}
        if a.detail_json:
            try:
                detail = json.loads(a.detail_json)
                structured = detail.get("structured") or {}
            except Exception:
                detail = {"raw": a.detail_json}
        return {
            "id": a.id,
            "level": a.level,
            "level_cn": alert_level_to_cn(a.level),
            "event_type": a.event_type,
            "event_type_cn": EVENT_TYPES.get(a.event_type, a.event_type),
            "title": a.title,
            "summary": a.summary,
            "root_cause": a.root_cause,
            "suggestion": a.suggestion,
            "impact_scope": structured.get("impact_scope"),
            "occurred_at": structured.get("occurred_at"),
            "severity_assessment": structured.get("severity_assessment"),
            "channels": a.channels_sent,
            "status": a.status,
            "status_cn": "已处理" if a.status == "resolved" else "未处理",
            "resolution_note": a.resolution_note,
            "detail": detail,
            "system_health": json.loads(a.system_health_json) if a.system_health_json else {},
            "created_at": _localize_utc(a.created_at),
            "resolved_at": _localize_utc(a.resolved_at),
        }

    def _compute_mttr_minutes(self, db: Session) -> float | None:
        """计算平均处理时长（分钟）"""
        resolved = (
            db.query(AlertEvent)
            .filter(AlertEvent.status == "resolved", AlertEvent.resolved_at.isnot(None))
            .all()
        )
        if not resolved:
            return None
        total_minutes = 0.0
        count = 0
        for a in resolved:
            if a.created_at and a.resolved_at:
                delta = (a.resolved_at - a.created_at).total_seconds() / 60
                if delta >= 0:
                    total_minutes += delta
                    count += 1
        return round(total_minutes / count, 1) if count else None

    def get_stats(self, db: Session) -> dict[str, Any]:
        """获取告警统计仪表盘数据"""
        from sqlalchemy import func

        total = db.query(func.count(AlertEvent.id)).scalar() or 0
        open_count = db.query(func.count(AlertEvent.id)).filter(AlertEvent.status == "open").scalar() or 0
        resolved_count = db.query(func.count(AlertEvent.id)).filter(AlertEvent.status == "resolved").scalar() or 0
        open_critical = (
            db.query(func.count(AlertEvent.id))
            .filter(AlertEvent.status == "open", AlertEvent.level == "critical")
            .scalar() or 0
        )

        by_level_rows = db.query(AlertEvent.level, func.count(AlertEvent.id)).group_by(AlertEvent.level).all()
        by_type_rows = db.query(AlertEvent.event_type, func.count(AlertEvent.id)).group_by(AlertEvent.event_type).all()
        by_status_rows = db.query(AlertEvent.status, func.count(AlertEvent.id)).group_by(AlertEvent.status).all()

        by_level = {r[0]: r[1] for r in by_level_rows}
        by_type = {r[0]: r[1] for r in by_type_rows}
        by_status = {r[0]: r[1] for r in by_status_rows}

        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        today_count = db.query(func.count(AlertEvent.id)).filter(AlertEvent.created_at >= today_start).scalar() or 0
        week_count = db.query(func.count(AlertEvent.id)).filter(AlertEvent.created_at >= week_start).scalar() or 0

        recent_alerts = db.query(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(100).all()
        by_hour: dict[int, int] = defaultdict(int)
        by_date: dict[str, int] = defaultdict(int)
        timeline_data: list[dict] = []

        for a in recent_alerts:
            by_hour[a.created_at.hour] += 1
            by_date[a.created_at.strftime("%Y-%m-%d")] += 1
            if len(timeline_data) < 100:
                timeline_data.append(self._alert_to_dict(a))

        date_trend = [{"date": k, "count": v} for k, v in sorted(by_date.items())][-30:]

        by_type_ranked = sorted(
            [
                {
                    "event_type": k,
                    "name": EVENT_TYPES.get(k, k),
                    "count": v,
                    "pct": round(v / total * 100, 1) if total else 0,
                }
                for k, v in by_type.items()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )[:10]

        hourly_distribution = [
            {"hour": h, "label": f"{h:02d}:00", "count": by_hour.get(h, 0)}
            for h in range(24)
        ]

        return {
            "total": total,
            "open": open_count,
            "resolved": resolved_count,
            "open_critical": open_critical,
            "today_count": today_count,
            "week_count": week_count,
            "mttr_minutes": self._compute_mttr_minutes(db),
            "resolution_rate": round(resolved_count / total * 100, 1) if total > 0 else 0,
            "by_level": dict(by_level),
            "by_type": dict(by_type),
            "by_type_ranked": by_type_ranked,
            "by_hour": {str(k): v for k, v in sorted(by_hour.items())},
            "hourly_distribution": hourly_distribution,
            "by_status": dict(by_status),
            "date_trend": date_trend,
            "recent": timeline_data[:20],
            "token_usage": {
                "used": self._token_usage["used"],
                "limit": self._token_usage["limit"],
                "ratio": round(self._token_usage["used"] / max(self._token_usage["limit"], 1) * 100, 1),
            },
        }

    def get_analytics(self, db: Session, days: int = 7) -> dict[str, Any]:
        """获取告警分析仪表盘数据（指定天数范围）"""
        from sqlalchemy import func

        days = max(1, min(days, 90))
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = db.query(AlertEvent).filter(AlertEvent.created_at >= cutoff).all()

        by_level: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        by_date: dict[str, int] = defaultdict(int)
        by_hour: dict[int, int] = defaultdict(int)
        resolved_in_range = 0
        mttr_samples: list[float] = []

        for a in rows:
            by_level[a.level] += 1
            by_type[a.event_type] += 1
            by_date[a.created_at.strftime("%Y-%m-%d")] += 1
            by_hour[a.created_at.hour] += 1
            if a.status == "resolved":
                resolved_in_range += 1
                if a.resolved_at and a.created_at:
                    mttr_samples.append((a.resolved_at - a.created_at).total_seconds() / 60)

        total = len(rows)
        date_trend = [{"date": k, "count": v} for k, v in sorted(by_date.items())]
        type_ranked = sorted(
            [
                {
                    "event_type": k,
                    "name": EVENT_TYPES.get(k, k),
                    "count": v,
                    "pct": round(v / total * 100, 1) if total else 0,
                }
                for k, v in by_type.items()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )

        return {
            "days": days,
            "total": total,
            "resolved": resolved_in_range,
            "open": total - resolved_in_range,
            "resolution_rate": round(resolved_in_range / total * 100, 1) if total else 0,
            "mttr_minutes": round(sum(mttr_samples) / len(mttr_samples), 1) if mttr_samples else None,
            "by_level": dict(by_level),
            "by_type_ranked": type_ranked,
            "date_trend": date_trend,
            "hourly_distribution": [
                {"hour": h, "label": f"{h:02d}:00", "count": by_hour.get(h, 0)}
                for h in range(24)
            ],
        }

    def get_timeline(
        self,
        db: Session,
        level: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        skip: int = 0,
        limit: int = 30,
    ) -> dict[str, Any]:
        """获取按日期分组的告警历史时间线"""
        q = db.query(AlertEvent).order_by(AlertEvent.created_at.desc())
        if level:
            q = q.filter(AlertEvent.level == level)
        if event_type:
            q = q.filter(AlertEvent.event_type == event_type)
        if status:
            q = q.filter(AlertEvent.status == status)
        if start:
            q = q.filter(AlertEvent.created_at >= start)
        if end:
            q = q.filter(AlertEvent.created_at <= end)

        total = q.count()
        rows = q.offset(skip).limit(limit).all()

        groups: dict[str, list[dict]] = {}
        group_order: list[str] = []
        for a in rows:
            date_key = a.created_at.strftime("%Y-%m-%d") if a.created_at else "未知日期"
            if date_key not in groups:
                groups[date_key] = []
                group_order.append(date_key)
            groups[date_key].append(self._alert_to_dict(a))

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "has_more": skip + limit < total,
            "groups": [{"date": d, "items": groups[d]} for d in group_order],
        }

    def build_cause_analysis(
        self,
        alert: AlertEvent,
        detail: dict,
        related_logs: list[dict],
    ) -> dict[str, Any]:
        """构建结构化根因分析数据"""
        event_type = alert.event_type
        cause_chain: list[dict] = []

        if detail:
            from app.utils.alert_analysis import (
                format_trigger_conditions,
                format_log_chain_title,
                humanize_replay_log_message,
            )

            trigger_desc = format_trigger_conditions(event_type, detail)
            cause_chain.append({
                "step": 1,
                "type": "trigger",
                "title": "触发条件",
                "description": trigger_desc,
                "timestamp": _localize_utc(alert.created_at),
            })

        for i, log in enumerate(related_logs[:5]):
            cause_chain.append({
                "step": len(cause_chain) + 1,
                "type": "log",
                "title": format_log_chain_title(log.get("category"), log.get("level")),
                "category": log.get("category"),
                "level": log.get("level"),
                "description": log.get("display_message") or humanize_replay_log_message(
                    log.get("message", ""), log.get("category"),
                ),
                "timestamp": log.get("created_at"),
            })

        cause_chain.append({
            "step": len(cause_chain) + 1,
            "type": "alert",
            "title": "告警生成",
            "description": alert.title or EVENT_TYPES.get(event_type, event_type),
            "timestamp": _localize_utc(alert.created_at),
        })

        contributing: list[str] = []
        if event_type.startswith("lpr"):
            contributing.append("车牌识别模块连续失败或失败率过高")
        elif "gesture" in event_type:
            contributing.append("手势识别置信度低于阈值")
        elif event_type.startswith("llm"):
            contributing.append("大语言模型 API 或 Token 配额异常")
        if related_logs:
            contributing.append(f"关联 {len(related_logs)} 条警告/错误日志")

        impact_map = {
            "critical": "可能影响核心识别功能或系统稳定性，需立即处理",
            "warning": "可能影响部分功能体验，建议尽快排查",
            "info": "提示性信息，建议关注趋势变化",
        }
        structured = {}
        if alert.detail_json:
            try:
                structured = json.loads(alert.detail_json).get("structured") or {}
            except Exception:
                pass
        from app.utils.alert_analysis import build_event_impact, build_severity_assessment

        impact_text = structured.get("impact_scope") or build_event_impact(
            event_type, alert.level, detail,
        )
        severity = structured.get("severity_assessment") or build_severity_assessment(
            event_type, alert.level, detail,
        )

        return {
            "primary_cause": alert.root_cause or "暂无根因分析，可点击「深度分析」获取 AI 解读",
            "suggestion": alert.suggestion or "",
            "contributing_factors": contributing,
            "cause_chain": cause_chain,
            "impact": impact_text,
            "severity_assessment": severity,
            "event_type_cn": EVENT_TYPES.get(event_type, event_type),
            "occurred_at": structured.get("occurred_at"),
            "impact_scope": impact_text,
        }

    def _build_replay_timeline(
        self,
        alert: AlertEvent,
        related_logs: list[dict],
        related_records: list[dict],
    ) -> list[dict]:
        """合并日志、识别记录与告警，生成按时间排序的回放事件流"""
        events: list[dict] = []

        for log in related_logs:
            cat = log.get("category_cn") or category_cn(log.get("category"))
            lvl = log.get("level_cn") or level_to_cn(log.get("level"))
            msg = log.get("display_message") or sanitize_log_message(log.get("message"), log.get("detail_json") or log.get("detail"))
            events.append({
                "time": log.get("created_at"),
                "type": "log",
                "level": lvl,
                "title": f"{cat} · {msg[:80]}{'…' if len(msg) > 80 else ''}",
                "detail": None,
            })

        for rec in related_records:
            type_cn = rec.get("type_cn") or record_type_cn(rec.get("type"))
            if rec.get("gesture_cn"):
                title = f"{rec['gesture_cn']}（置信度 {round((rec.get('confidence') or 0) * 100)}%）"
            else:
                title = f"{type_cn}记录 #{rec.get('id', '')}"
            events.append({
                "time": rec.get("created_at"),
                "type": "record",
                "level": "信息",
                "title": f"识别记录 · {title}",
                "detail": None,
                "image": rec.get("annotated_image"),
            })

        events.append({
            "time": _localize_utc(alert.created_at),
            "type": "alert",
            "level": alert_level_to_cn(alert.level),
            "title": f"告警触发 · {alert.title}",
            "detail": None,
        })

        def _sort_key(e: dict) -> str:
            t = e.get("time") or ""
            return t if isinstance(t, str) else ""

        events.sort(key=_sort_key)
        return events

    def _get_related_records(self, db: Session, alert: AlertEvent) -> list[dict]:
        """获取告警时间窗口内的识别记录（含标注图）"""
        if not alert.created_at:
            return []
        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            from app.models.records import LicensePlateRecord, PoliceGestureRecord, OwnerGestureRecord
        except ImportError:
            return []

        start = alert.created_at - timedelta(minutes=5)
        end = alert.created_at + timedelta(minutes=5)
        records: list[dict] = []

        try:
            event_type = alert.event_type
            if event_type.startswith("lpr"):
                rows = (
                    db.query(LicensePlateRecord)
                    .filter(LicensePlateRecord.created_at >= start, LicensePlateRecord.created_at <= end)
                    .order_by(LicensePlateRecord.created_at.desc())
                    .limit(5)
                    .all()
                )
                for r in rows:
                    records.append(format_record_entry({
                        "type": "lpr",
                        "id": r.id,
                        "source_type": r.source_type,
                        "annotated_image": r.annotated_image,
                        "created_at": _localize_utc(r.created_at),
                    }))
            elif event_type == "gesture_low_confidence":
                module = None
                if alert.detail_json:
                    try:
                        module = json.loads(alert.detail_json).get("module")
                    except (json.JSONDecodeError, TypeError):
                        pass
                if module == "owner":
                    rows = (
                        db.query(OwnerGestureRecord)
                        .filter(OwnerGestureRecord.created_at >= start, OwnerGestureRecord.created_at <= end)
                        .order_by(OwnerGestureRecord.created_at.desc())
                        .limit(5)
                        .all()
                    )
                    for r in rows:
                        records.append(format_record_entry({
                            "type": "owner_gesture",
                            "id": r.id,
                            "gesture_cn": r.gesture_cn,
                            "confidence": r.confidence,
                            "annotated_image": r.annotated_image,
                            "created_at": _localize_utc(r.created_at),
                        }))
                else:
                    rows = (
                        db.query(PoliceGestureRecord)
                        .filter(PoliceGestureRecord.created_at >= start, PoliceGestureRecord.created_at <= end)
                        .order_by(PoliceGestureRecord.created_at.desc())
                        .limit(5)
                        .all()
                    )
                    for r in rows:
                        records.append(format_record_entry({
                            "type": "police_gesture",
                            "id": r.id,
                            "gesture_cn": r.gesture_cn,
                            "confidence": r.confidence,
                            "annotated_image": r.annotated_image,
                            "created_at": _localize_utc(r.created_at),
                        }))
        except (OperationalError, ProgrammingError):
            return []

        return records

    def get_event_replay(self, db: Session, alert_id: int) -> dict | None:
        """获取告警事件回放数据"""
        alert = db.query(AlertEvent).filter(AlertEvent.id == alert_id).first()
        if not alert:
            return None

        # 解析详情 JSON
        detail = {}
        if alert.detail_json:
            try:
                detail = json.loads(alert.detail_json)
            except (json.JSONDecodeError, TypeError):
                detail = {"raw": alert.detail_json}

        # 查找相关日志
        relevant_logs = []
        if alert.created_at:
            from app.models.logs import SystemLog
            from sqlalchemy import or_

            time_window_start = alert.created_at - timedelta(minutes=5)
            time_window_end = alert.created_at + timedelta(minutes=5)
            categories = self._log_categories_for_event(alert.event_type)
            logs = (
                db.query(SystemLog)
                .filter(
                    SystemLog.created_at >= time_window_start,
                    SystemLog.created_at <= time_window_end,
                    or_(
                        SystemLog.level.in_(["WARN", "ERROR", "CRITICAL", "警告", "错误", "严重"]),
                        SystemLog.category.in_(categories),
                    ),
                )
                .order_by(SystemLog.created_at.asc())
                .limit(30)
                .all()
            )
            for log in logs:
                log_detail = None
                if log.detail_json:
                    try:
                        log_detail = json.loads(log.detail_json)
                    except Exception:
                        log_detail = log.detail_json
                relevant_logs.append(format_log_entry(
                    category=log.category,
                    level=log.level,
                    message=log.message,
                    detail=log_detail,
                    id=log.id,
                    created_at=_localize_utc(log.created_at),
                ))

        related_records = self._get_related_records(db, alert)
        cause_analysis = self.build_cause_analysis(alert, detail, relevant_logs)
        timeline_events = self._build_replay_timeline(alert, relevant_logs, related_records)

        health = {}
        if alert.system_health_json:
            try:
                health = json.loads(alert.system_health_json)
            except Exception:
                health = {"raw": alert.system_health_json}
        return {
            "alert": self._alert_to_dict(alert),
            "related_logs": relevant_logs,
            "related_records": related_records,
            "cause_analysis": cause_analysis,
            "timeline_events": timeline_events,
        }

    def get_event_types(self) -> list[dict]:
        """获取支持的事件类型列表"""
        return [
            {
                "key": key,
                "name": name,
                "default_level": DEFAULT_LEVELS.get(key, "warning"),
                "default_level_cn": alert_level_to_cn(DEFAULT_LEVELS.get(key, "warning")),
            }
            for key, name in EVENT_TYPES.items()
        ]


# ── 全局单例 ──
alert_agent = AlertAgent()
