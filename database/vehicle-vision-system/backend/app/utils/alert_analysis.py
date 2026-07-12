"""告警结构化分析 —— 为智能体生成互不重叠的四类信息（根因/建议/级别/影响）。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.config import settings
from app.utils.user_language import (
    EVENT_USER_NAMES,
    _format_detail_hint,
    _get_plan,
    _personalize_actions,
    event_type_to_user,
    level_to_user,
)

LOG_CATEGORY_CN: dict[str, str] = {
    "lpr": "车牌识别",
    "police_gesture": "交警手势",
    "owner_gesture": "车主手势",
    "alert": "告警",
    "user": "用户操作",
    "system": "系统运行",
    "agent": "智能体决策",
}

LEVEL_DISPLAY_CN: dict[str, str] = {
    "info": "提示",
    "warning": "警告",
    "critical": "严重",
    "INFO": "信息",
    "WARN": "警告",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
    "信息": "信息",
    "警告": "警告",
    "错误": "错误",
    "严重": "严重",
    "提示": "提示",
}

ALERT_STATUS_CN: dict[str, str] = {
    "open": "未处理",
    "resolved": "已处理",
}


def level_display(level: str | None) -> str:
    if not level:
        return "提示"
    text = str(level).strip()
    if text in LEVEL_DISPLAY_CN:
        return LEVEL_DISPLAY_CN[text]
    return LEVEL_DISPLAY_CN.get(text.lower(), text)


def log_category_display(category: str | None) -> str:
    if not category:
        return "系统"
    return LOG_CATEGORY_CN.get(category, category)


def alert_status_display(status: str | None) -> str:
    if not status:
        return "未处理"
    return ALERT_STATUS_CN.get(status, status)


def format_trigger_conditions(event_type: str, detail: dict | None) -> str:
    """将告警 detail 转为普通人能看懂的触发条件说明。"""
    if not detail:
        return "系统检测到异常指标达到告警阈值。"

    ctx = {k: v for k, v in detail.items() if k != "structured"}
    if ctx.get("message"):
        return str(ctx["message"])
    if ctx.get("error"):
        return f"发生错误：{ctx['error']}"
    if ctx.get("source"):
        return f"来源：{ctx['source']}"

    hint = _format_detail_hint(event_type, ctx)
    parts: list[str] = []

    if event_type == "email_delivery_failure":
        window = ctx.get("window", "最近几次")
        fails = ctx.get("fails", "?")
        parts.append(f"邮件通知在{window}推送中有 {fails} 次失败")
    elif event_type == "webhook_delivery_failure":
        window = ctx.get("window", "最近几次")
        fails = ctx.get("fails", "?")
        parts.append(f"群消息推送在{window}中有 {fails} 次失败")
    elif event_type == "lpr_consecutive_failure":
        parts.append(f"车牌识别连续失败 {ctx.get('count', '?')} 次")
    elif event_type == "lpr_high_failure_rate":
        parts.append(
            f"近 {ctx.get('window_seconds', 300)} 秒内失败率约 {ctx.get('rate', '?')} "
            f"（{ctx.get('fails', '?')}/{ctx.get('total', '?')} 次）"
        )
    elif event_type == "gesture_low_confidence":
        module = "交警手势" if ctx.get("module") == "police" else (
            "车主手势" if ctx.get("module") == "owner" else "手势识别"
        )
        conf = ctx.get("confidence")
        conf_txt = f"{conf:.0%}" if isinstance(conf, (int, float)) else "偏低"
        parts.append(f"{module} 置信度持续偏低（约 {conf_txt}）")
    elif event_type in ("llm_token_exhausted", "llm_token_exceeded"):
        parts.append(
            f"智能分析额度已用 {ctx.get('used', '?')}/{ctx.get('limit', '?')}（{ctx.get('ratio', '?')}）"
        )
    elif event_type == "llm_api_timeout":
        parts.append(f"智能分析服务近 {ctx.get('window', '几次')} 调用失败 {ctx.get('fails', '?')} 次")
    elif event_type == "unauthorized_access":
        parts.append(
            f"来自 {ctx.get('ip', '未知地址')} 的未授权访问，"
            f"近 {ctx.get('window_seconds', 300)} 秒内累计 {ctx.get('count', 1)} 次"
        )
    elif event_type == "database_connection_error":
        parts.append(f"数据库连续 {ctx.get('consecutive_fails', 3)} 次连接失败")
    elif event_type == "model_load_failure":
        parts.append(f"模型「{ctx.get('model_name', '未知')}」加载失败")
    elif event_type == "config_missing":
        parts.append(f"缺少配置项：{ctx.get('config_key', '未知')}")
    elif event_type == "test_event":
        parts.append("手动触发测试告警")
    elif hint:
        parts.append(hint.rstrip("。"))
    else:
        parts.append(f"检测到{event_type_to_user(event_type)}相关异常")

    decided = ctx.get("decided_level") or ctx.get("level")
    original = ctx.get("original_level")
    if decided:
        line = f"智能体判定级别：{level_display(decided)}"
        if original and str(original).lower() != str(decided).lower():
            line += f"（初始：{level_display(original)}）"
        parts.append(line)

    return "；".join(parts) + "。"


def humanize_replay_log_message(message: str, category: str | None = None) -> str:
    """回放因果链中的日志消息中文化。"""
    from app.utils.log_display import humanize_error_text

    text = humanize_error_text(message or "")
    for key, label in EVENT_USER_NAMES.items():
        text = text.replace(key, label)
    text = re.sub(
        r"\[(critical|warning|info)\]",
        lambda m: f"[{level_display(m.group(1))}]",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("→ critical", f"→ {level_display('critical')}")
    text = text.replace("→ warning", f"→ {level_display('warning')}")
    text = text.replace("→ info", f"→ {level_display('info')}")
    if category == "agent" and "告警级别决策" in text:
        text = text.replace("critical", level_display("critical"))
        text = text.replace("warning", level_display("warning"))
        text = text.replace("info", level_display("info"))
    return text


def format_log_chain_title(category: str | None, level: str | None) -> str:
    return f"{log_category_display(category)} · {level_display(level)}"


def format_occurred_at(created_at: datetime | None = None, context: dict | None = None) -> str:
    """发生时间描述（任务书要求：发生时间）。"""
    ctx = context or {}
    if ctx.get("occurred_at"):
        return str(ctx["occurred_at"])
    if ctx.get("window"):
        return f"最近{ctx['window']}内"
    if ctx.get("window_seconds"):
        return f"最近 {ctx['window_seconds']} 秒内"
    if created_at:
        return created_at.strftime("%Y-%m-%d %H:%M")
    return "刚刚"


def build_event_impact(event_type: str, level: str, context: dict | None = None) -> str:
    """影响范围（任务书要求：影响范围）—— 按异常类型映射到具体模块。"""
    ctx = context or {}
    plan = _get_plan(event_type)
    base = plan.get("impact", "相关功能可能暂时受影响。")

    if event_type.startswith("lpr"):
        return (
            f"车牌识别模块：{base} "
            f"历史记录与实时识别结果可能不完整。"
        )
    if event_type == "gesture_low_confidence":
        module = ctx.get("module", "手势")
        mod_cn = "交警手势识别" if module == "police" else (
            "车主手势控车" if module == "owner" else "手势识别"
        )
        return f"{mod_cn}：{base}"
    if event_type.startswith("llm"):
        return f"智能分析服务：{base} 基础监控与模板告警仍可用。"
    if event_type == "unauthorized_access":
        return "账号安全与系统访问控制：可能存在非本人操作或凭证过期。"
    if event_type == "database_connection_error":
        return "数据持久化：识别记录、告警与日志可能无法保存。"
    if event_type == "model_load_failure":
        name = ctx.get("model_name", "识别模型")
        return f"AI 识别能力：模型「{name}」不可用，对应识别页功能中断。"
    if event_type in ("webhook_delivery_failure", "email_delivery_failure"):
        return f"外部通知渠道：{base}"
    if event_type == "config_missing":
        return f"系统配置：{base}"
    if event_type == "service_unhealthy":
        svc = ctx.get("service", "后台服务")
        return f"系统服务「{svc}」：{base}"
    if event_type == "test_event":
        return "无实际业务影响，仅用于验证告警链路。"
    return base


def build_severity_assessment(
    event_type: str,
    level: str,
    context: dict | None = None,
) -> dict[str, Any]:
    """级别决策说明（任务书：自主决策告警级别 —— 提示/警告/严重）。"""
    ctx = context or {}
    original = ctx.get("original_level", level)
    decided = ctx.get("decided_level", level)
    level_cn = level_to_user(decided)
    original_cn = level_to_user(original)

    should_escalate = False
    escalation_reason = ""
    hold_reason = ""

    if event_type == "lpr_consecutive_failure":
        count = ctx.get("count", settings.alert_failure_threshold)
        threshold = settings.alert_failure_threshold
        hold_reason = f"已连续失败 {count} 次，达到严重告警阈值（≥{threshold}）。"
    elif event_type == "lpr_high_failure_rate":
        rate_str = str(ctx.get("rate", "0")).replace("%", "")
        try:
            rate = float(rate_str) / 100
        except (ValueError, TypeError):
            rate = 0
        if rate > 0.6:
            hold_reason = f"失败率 {ctx.get('rate')} 超过 60%，已升为严重。"
        else:
            hold_reason = f"失败率 {ctx.get('rate')} 处于警告区间（40%–60% 为警告，>60% 升为严重）。"
            if rate > 0.5:
                should_escalate = True
                escalation_reason = "若失败率继续上升超过 60%，系统将自动升为严重。"
    elif event_type == "gesture_low_confidence":
        conf = ctx.get("confidence", 0)
        if isinstance(conf, (int, float)) and conf < 0.2:
            hold_reason = f"置信度约 {conf:.0%} 极低，已按严重级别处理。"
        else:
            hold_reason = f"置信度持续低于 {ctx.get('threshold', settings.low_confidence_threshold):.0%}，当前为警告级。"
            if isinstance(conf, (int, float)) and conf < 0.25:
                should_escalate = True
                escalation_reason = "若置信度跌破 20%，建议视为严重并暂停依赖识别结果。"
    elif event_type == "unauthorized_access":
        count = ctx.get("count", 1)
        if count >= 10:
            hold_reason = f"近 {ctx.get('window_seconds', 300)} 秒内未授权访问 {count} 次，已升为严重。"
        else:
            hold_reason = f"检测到 {count} 次未授权访问，当前为警告级。"
            if count >= 5:
                should_escalate = True
                escalation_reason = "若 5 分钟内累计达 10 次，系统将自动升为严重。"
    elif event_type in ("llm_token_exceeded", "llm_api_timeout", "database_connection_error", "model_load_failure"):
        hold_reason = "属于核心依赖异常，智能体已判定为严重级别。"
    elif event_type == "llm_token_exhausted":
        ratio_str = str(ctx.get("ratio", "0")).replace("%", "")
        try:
            ratio = float(ratio_str) / 100
        except (ValueError, TypeError):
            ratio = 0
        hold_reason = f"额度使用率 {ctx.get('ratio', '?')}，当前为{level_cn}。"
        if ratio >= 0.95:
            should_escalate = True
            escalation_reason = "额度即将耗尽，建议尽快充值以免智能摘要中断。"
    elif event_type == "test_event":
        hold_reason = "测试告警固定为提示级，无需升级。"
    else:
        hold_reason = f"智能体根据事件类型「{event_type_to_user(event_type)}」判定为{level_cn}。"

    if original != decided:
        hold_reason = f"初始级别为{original_cn}，依据规则调整为{level_cn}。{hold_reason}"

    recommendation = "建议优先处理并标记已处理。" if decided == "critical" else (
        "建议尽快排查；若指标继续恶化，系统可能自动升级。" if should_escalate
        else "当前级别合理，按建议处置即可，暂不必手动升级。"
    )

    return {
        "current_level": decided,
        "current_level_cn": level_cn,
        "should_escalate": should_escalate,
        "decision_reason": hold_reason,
        "escalation_hint": escalation_reason,
        "recommendation": recommendation,
        "summary_text": "\n".join(
            x for x in [
                f"当前告警级别：{level_cn}。",
                hold_reason,
                escalation_reason,
                recommendation,
            ] if x
        ),
    }


def build_structured_alert(
    event_type: str,
    level: str,
    context: dict | None = None,
    *,
    created_at: datetime | None = None,
    root_cause: str | None = None,
    suggestion: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """组装任务书要求的结构化告警信息。"""
    ctx = dict(context or {})
    plan = _get_plan(event_type)
    detail_hint = _format_detail_hint(event_type, ctx)
    actions = _personalize_actions(event_type, plan["actions"], ctx)

    structured_root = root_cause or plan["root_cause"]
    if detail_hint and detail_hint not in structured_root:
        structured_root = f"{detail_hint} {structured_root}"

    structured_suggestion = suggestion or "\n".join(
        f"{i + 1}. {a}" for i, a in enumerate(actions)
    )

    severity = build_severity_assessment(event_type, level, ctx)
    impact = build_event_impact(event_type, level, ctx)

    return {
        "event_type": event_type,
        "event_type_cn": event_type_to_user(event_type),
        "occurred_at": format_occurred_at(created_at, ctx),
        "impact_scope": impact,
        "root_cause": structured_root,
        "suggestion": structured_suggestion,
        "severity_assessment": severity,
        "summary": summary or "",
        "level": level,
        "level_cn": level_to_user(level),
    }


def merge_llm_structured(
    llm_data: dict[str, str],
    structured: dict[str, Any],
) -> dict[str, Any]:
    """LLM 输出与规则结构化字段合并：LLM 润色优先，空字段用规则兜底。"""
    merged = dict(structured)
    for key in ("title", "summary", "root_cause", "suggestion"):
        if llm_data.get(key):
            merged[key] = llm_data[key]
    if llm_data.get("impact_scope"):
        merged["impact_scope"] = llm_data["impact_scope"]
    if llm_data.get("occurred_at"):
        merged["occurred_at"] = llm_data["occurred_at"]
    return merged
