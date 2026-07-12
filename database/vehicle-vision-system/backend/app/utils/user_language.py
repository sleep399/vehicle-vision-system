"""将系统/运维术语转换为用户能听懂的大白话，并提供可执行的处理步骤。"""

from typing import Any

EVENT_USER_NAMES: dict[str, str] = {
    "lpr_consecutive_failure": "车牌识别一直失败",
    "lpr_high_failure_rate": "车牌识别成功率偏低",
    "gesture_low_confidence": "手势识别不太准",
    "llm_api_timeout": "智能分析响应较慢",
    "llm_token_exhausted": "智能分析额度快用完了",
    "llm_token_exceeded": "智能分析额度已用完",
    "unauthorized_access": "有人试图未授权登录",
    "service_unhealthy": "系统某项服务不太正常",
    "model_load_failure": "识别模型加载失败",
    "database_connection_error": "数据库连接异常",
    "webhook_delivery_failure": "群消息推送失败",
    "email_delivery_failure": "邮件通知发送失败",
    "config_missing": "系统配置不完整",
    "test_event": "测试提醒",
    "unknown": "系统异常",
}

LEVEL_USER_NAMES: dict[str, str] = {
    "info": "提示",
    "warning": "需要注意",
    "critical": "比较紧急",
}

# 每种异常 → 根因 / 处理步骤 / 影响（给用户看的）
ACTION_PLANS: dict[str, dict[str, Any]] = {
    "test_event": {
        "root_cause": "这是我帮您发的一条测试消息，用来确认「发现问题→提醒您」这条链路是正常的，不是车辆或识别出了真实故障。",
        "actions": [
            "不用担心，这只是一次功能测试，不影响您正常使用。",
            "如果想走完流程，请打开左侧「告警中心」，找到这条提醒后点「已处理」。",
            "以后出现真实问题时，我会用同样方式提醒您，并告诉您具体怎么处理。",
        ],
        "impact": "没有实际影响，只是演示告警功能是否正常工作。",
    },
    "lpr_consecutive_failure": {
        "root_cause": "连续多次上传的图片都没能识别出车牌，常见原因是画面太暗、太模糊，或摄像头被遮挡。",
        "actions": [
            "换一张光线充足、车牌清晰可见的道路照片再试。",
            "检查摄像头镜头是否干净、有没有被物体挡住。",
            "如果多次失败，到「车牌识别」页重新上传，或到「告警中心」查看失败记录。",
        ],
        "impact": "暂时无法获取周围车辆的车牌信息，辅助驾驶相关功能可能不准确。",
    },
    "lpr_high_failure_rate": {
        "root_cause": "最近一段时间里，车牌识别成功的比例偏低，可能是环境光线或摄像头状态不稳定。",
        "actions": [
            "尽量在白天或光线好的环境下使用识别功能。",
            "避免强烈逆光、雨雪雾等恶劣天气下依赖识别结果。",
            "持续偏低时，建议检查摄像头安装角度和清洁度。",
        ],
        "impact": "车牌识别准确率下降，部分结果可能不可信。",
    },
    "gesture_low_confidence": {
        "root_cause": "手势在画面里不够完整，或背景干扰、光线不好，导致系统不太确定识别结果。",
        "actions": [
            "把手或身体完整伸进摄像头画面，避免遮挡。",
            "在光线均匀的环境下再试一次。",
            "如果仍不准，到对应手势识别页面重新拍摄。",
        ],
        "impact": "手势控制或交警手势解读可能不可靠，建议修复前不要依赖结果做关键操作。",
    },
    "unauthorized_access": {
        "root_cause": "有人用无效或已过期的账号信息尝试进入系统，也可能是您登录过期后仍在操作。",
        "actions": [
            "请先退出后重新登录。",
            "如果不是您本人操作，建议尽快修改密码。",
            "到「告警中心」查看访问记录，必要时联系管理员。",
        ],
        "impact": "可能存在账号安全风险，需尽快确认是否为本人操作。",
    },
    "service_unhealthy": {
        "root_cause": "系统某项后台服务运行异常，可能是数据库或识别模块出了问题。",
        "actions": [
            "尝试刷新页面或重启系统后再试。",
            "到「告警中心」查看具体告警说明。",
            "若持续异常，联系管理员排查服务器状态。",
        ],
        "impact": "部分功能可能变慢或暂时不可用。",
    },
    "llm_token_exceeded": {
        "root_cause": "智能分析服务的调用额度已经用完，暂时无法生成更详细的解读。",
        "actions": [
            "告警中心的基础提醒仍然有效，可以先按文字说明处理。",
            "联系管理员充值或更换 API 密钥后，智能分析会自动恢复。",
        ],
        "impact": "暂时无法生成 AI 智能摘要，基础监控和模板告警不受影响。",
    },
    "model_load_failure": {
        "root_cause": "识别用的 AI 模型文件缺失或损坏，相关识别功能无法启动。",
        "actions": [
            "确认 models 目录下模型文件是否完整。",
            "检查磁盘空间是否充足。",
            "重启服务后再次尝试识别。",
        ],
        "impact": "对应识别功能（车牌/手势）暂时无法使用。",
    },
    "database_connection_error": {
        "root_cause": "系统无法连接数据库，历史记录和告警可能无法保存。",
        "actions": [
            "确认数据库服务是否已启动。",
            "检查 .env 中的 DATABASE_URL 配置是否正确。",
            "重启数据库和应用后再试。",
        ],
        "impact": "数据读写可能失败，识别结果可能无法保存。",
    },
    "webhook_delivery_failure": {
        "root_cause": "往企业微信或钉钉群推送消息时失败了，多半是推送地址填错、机器人被停用，或者当时网络不太通。",
        "actions": [
            "别担心，网页上的告警中心照样能看，功能没丢。",
            "请管理员到系统配置里检查一下群消息推送地址是否正确。",
        ],
        "impact": "群里可能收不到推送，但网页告警不受影响。",
    },
    "email_delivery_failure": {
        "root_cause": "邮件通知发不出去，通常是邮箱服务器或授权码没配对。",
        "actions": [
            "网页告警中心仍然可以查看所有提醒。",
            "请管理员检查邮件服务器和邮箱授权码是否配置正确。",
        ],
        "impact": "邮箱可能收不到通知，网页告警不受影响。",
    },
    "config_missing": {
        "root_cause": "系统发现有项配置还没填完整，部分通知或智能分析功能可能暂时用不了。",
        "actions": [
            "打开系统配置页面，把缺少的那一项补全（详情里会提示缺什么）。",
            "如果不确定怎么填，联系管理员协助配置即可。",
        ],
        "impact": "像智能摘要、邮件或群推送这类增强功能可能暂时受限，基础监控不受影响。",
    },
    "llm_token_exhausted": {
        "root_cause": "智能分析服务的调用额度快用完了，继续大量使用可能失败。",
        "actions": [
            "非紧急告警可先查看模板说明，不必每次都依赖 AI 解读。",
            "联系管理员检查 API 账户余额或提高配额。",
        ],
        "impact": "智能摘要可能不稳定，基础告警功能正常。",
    },
    "llm_api_timeout": {
        "root_cause": "我在帮您做智能分析时，后台服务响应超时，可能是网络波动或服务繁忙。",
        "actions": [
            "稍等 1 分钟后再问我一次。",
            "您也可以直接看告警中心里的文字说明，不依赖智能分析。",
        ],
        "impact": "暂时无法生成更详细的智能解读，但基础告警信息仍然有效。",
    },
}


def event_type_to_user(event_type: str | None) -> str:
    if not event_type or event_type in ("unknown", "未知异常"):
        return "系统运行异常"
    return EVENT_USER_NAMES.get(event_type, event_type.replace("_", " "))


def level_to_user(level: str | None) -> str:
    return LEVEL_USER_NAMES.get(level or "info", "提示")


def _get_plan(event_type: str | None) -> dict[str, Any]:
    key = event_type or "unknown"
    if key in ACTION_PLANS:
        return ACTION_PLANS[key]
    return {
        "root_cause": "系统检测到了一项异常，具体原因需要结合当时的操作情况判断。",
        "actions": [
            "打开左侧「告警中心」，查看这条提醒的详细说明。",
            "回忆出现问题前您在做什么（识别车牌、手势还是登录），便于定位原因。",
            "处理完成后，在告警中心点「已处理」标记完成。",
        ],
        "impact": "相关功能可能暂时受影响，建议尽快查看并处理。",
    }


def _is_useless_suggestion(text: str) -> bool:
    """过滤会循环引用、没有信息量的建议。"""
    if not text or len(text.strip()) < 4:
        return True
    bad_phrases = (
        "点击智能体询问",
        "应该怎么办",
        "告警中心查看详情，或点击",
        "待进一步分析",
        "请查看系统日志",
        "未知路径",
        "/api/",
    )
    return any(p in text for p in bad_phrases)


def _format_steps(steps: list[str]) -> str:
    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))


def _format_steps_conversational(steps: list[str]) -> str:
    """助手/摘要场景：用口语串联步骤，避免僵硬的「处理方法：1.2.3.」"""
    if not steps:
        return ""
    if len(steps) == 1:
        return steps[0]
    cleaned = [humanize_tech_terms(s) for s in steps]
    if len(cleaned) == 2:
        return f"您可以先{cleaned[0]}，然后{cleaned[1]}。"
    body = "，".join(cleaned[:-1])
    return f"建议您这样处理：先{body}，最后{cleaned[-1]}。"


TECH_TERM_REPLACEMENTS: dict[str, str] = {
    "webhook_url": "群消息推送地址",
    "WEBHOOK_URL": "群消息推送地址",
    "webhook": "群消息推送",
    "Webhook": "群消息推送",
    "SMTP_HOST": "邮件服务器地址",
    "SMTP_USER": "发件邮箱账号",
    "SMTP_PASSWORD": "邮箱授权码",
    "DATABASE_URL": "数据库连接配置",
    "LLM_API_KEY": "智能分析服务密钥",
    "alert_token_limit": "智能分析额度上限",
    ".env": "系统配置文件",
    "models 目录": "模型文件目录",
    "models/": "模型文件目录/",
}


def humanize_tech_terms(text: str) -> str:
    """把运维术语替换成用户能听懂的说法。"""
    if not text:
        return text
    result = text
    for term, friendly in TECH_TERM_REPLACEMENTS.items():
        result = result.replace(term, friendly)
    return result


def detect_assistant_intent(question: str) -> str:
    """识别用户提问意图，用于生成差异化回答。"""
    q = (question or "").strip()
    if any(k in q for k in ("根因", "原因", "为什么", "怎么回事", "咋回事", "什么情况")):
        return "root_cause"
    if any(k in q for k in ("处理", "建议", "怎么办", "如何", "该怎么做", "怎么解决", "步骤", "修复")):
        return "action"
    if any(k in q for k in ("严重", "升级", "要紧", "紧急", "要不要紧")):
        return "severity"
    if any(k in q for k in ("影响", "危害", "后果", "有多大")):
        return "impact"
    if any(k in q for k in ("状态", "正常吗", "有没有问题", "巡检", "现在怎样")):
        return "status"
    return "general"


def needs_alert_context(question: str, intent: str | None = None) -> bool:
    """问题是否必须先绑定到某一条具体告警（否则应反问用户）。"""
    q = (question or "").strip()
    q_intent = intent or detect_assistant_intent(question)

    if any(k in q for k in ("深度根因分析", "对这个告警", "该告警的", "这条告警的")):
        return True
    if any(k in q for k in (
        "这个异常", "这条异常", "当前告警", "该告警", "这个提醒", "这条提醒",
        "刚才那条", "刚才的告警", "刚才的提醒",
    )):
        return True
    if q_intent in ("root_cause", "action", "severity", "impact"):
        return True
    return False


def build_which_alert_prompt(open_alerts: list[dict[str, Any]]) -> str:
    """无明确告警上下文时，列出可选告警并引导用户选定。"""
    if not open_alerts:
        return (
            "您指的是哪条告警？我这边没有看到未处理的告警。\n\n"
            "您可以点「立即巡检」了解系统整体状态；"
            "或在告警中心打开某条告警的「回放」后再问我根因、影响或处理建议。"
        )

    level_names = {"info": "提示", "warning": "需注意", "critical": "较紧急"}
    lines = [
        "您指的是哪条告警？我需要您先选定一条，才能准确回答根因、影响或处理建议。",
        "",
        "当前未处理的告警：",
    ]
    for i, alert in enumerate(open_alerts[:5], 1):
        lv = level_names.get(alert.get("level"), alert.get("level", ""))
        title = alert.get("title") or alert.get("event_type_cn") or "系统提醒"
        lines.append(f"{i}. [{lv}] {title}")

    if len(open_alerts) > 5:
        lines.append(f"… 还有 {len(open_alerts) - 5} 条")

    lines.extend([
        "",
        "您可以：",
        "• 在告警中心点「回放」或「根因」选定一条",
        "• 点击智能体面板上方最新告警卡片",
        "• 收到新告警推送后，直接点「根因 / 建议 / 影响」快捷按钮",
    ])
    return "\n".join(lines)


def _format_detail_hint(event_type: str | None, detail: dict[str, Any]) -> str:
    """把告警 detail 里的数字翻译成用户能懂的一句话。"""
    if not detail:
        return ""
    et = event_type or ""
    if et == "lpr_consecutive_failure":
        return f"系统已连续 {detail.get('count', '?')} 次未能识别车牌。"
    if et == "lpr_high_failure_rate":
        return (
            f"近 {detail.get('window_seconds', 300)} 秒内失败率约 {detail.get('rate', '?')} "
            f"（{detail.get('fails', '?')}/{detail.get('total', '?')} 次）。"
        )
    if et == "gesture_low_confidence":
        conf = detail.get("confidence")
        conf_txt = f"{conf:.0%}" if isinstance(conf, (int, float)) else "偏低"
        module = "交警手势" if detail.get("module") == "police" else (
            "车主手势" if detail.get("module") == "owner" else "手势识别"
        )
        return f"{module} 最近平均置信度约 {conf_txt}，低于正常水平。"
    if et == "llm_api_timeout":
        return f"智能分析服务近 {detail.get('window', '几次')} 调用失败 {detail.get('fails', '?')} 次。"
    if et in ("llm_token_exhausted", "llm_token_exceeded"):
        return (
            f"智能分析额度已用 {detail.get('used', '?')}/{detail.get('limit', '?')} "
            f"（{detail.get('ratio', '?')}）。"
        )
    if et == "unauthorized_access":
        return (
            f"来自 {detail.get('ip', '未知地址')} 的未授权访问，"
            f"近 {detail.get('window_seconds', 300)} 秒内累计 {detail.get('count', 1)} 次。"
        )
    if et == "database_connection_error":
        return f"数据库已连续 {detail.get('consecutive_fails', 3)} 次连接失败。"
    if et == "model_load_failure":
        return f"模型「{detail.get('model_name', '未知')}」加载失败。"
    if et == "service_unhealthy":
        return f"服务「{detail.get('service', '未知')}」状态异常：{detail.get('detail', '')}"
    if et == "config_missing":
        return f"缺少配置项：{detail.get('config_key', '未知')}。"
    if et == "test_event":
        return "这是一条测试提醒，不代表真实故障。"
    return ""


def _personalize_actions(
    event_type: str | None,
    actions: list[str],
    detail: dict[str, Any],
) -> list[str]:
    """结合 detail 数据个性化处理步骤。"""
    if not detail:
        return actions
    et = event_type or ""
    personalized = list(actions)
    if et == "lpr_consecutive_failure" and detail.get("count"):
        personalized[0] = (
            f"已连续失败 {detail['count']} 次，请换一张光线充足、车牌清晰的照片再试。"
        )
    elif et == "gesture_low_confidence" and detail.get("confidence") is not None:
        conf = detail["confidence"]
        module = detail.get("module", "手势")
        personalized[0] = (
            f"「{module}」置信度仅约 {conf:.0%}，请把手完整伸入画面、避免逆光后重试。"
        )
    elif et == "unauthorized_access" and detail.get("ip"):
        personalized[1] = (
            f"若 IP {detail['ip']} 不是您本人，请尽快修改密码并联系管理员。"
        )
    elif et == "llm_token_exceeded" and detail.get("ratio"):
        personalized[0] = (
            f"额度已用 {detail.get('ratio', '100%')}，请联系管理员充值或更换 API 密钥。"
        )
    return personalized


def build_assistant_knowledge(context: dict[str, Any]) -> dict[str, Any]:
    """为智能助手组装事件知识包。"""
    event_type = context.get("event_type")
    detail = context.get("detail") or {}
    plan = _get_plan(event_type)
    return {
        "event_type": event_type,
        "event_name": event_type_to_user(event_type),
        "level": level_to_user(context.get("level")),
        "detail_hint": _format_detail_hint(event_type, detail),
        "plan": plan,
        "personalized_actions": _personalize_actions(event_type, plan["actions"], detail),
    }


def alert_for_user(alert: dict[str, Any]) -> str:
    event_type = alert.get("event_type")
    title = alert.get("title") or event_type_to_user(event_type)
    summary = alert.get("summary") or ""
    level = level_to_user(alert.get("level"))
    plan = _get_plan(event_type)

    parts = [f"【{level}】{title}"]
    if summary and summary not in title and "test_event" not in str(event_type):
        parts.append(summary)
    elif event_type == "test_event":
        parts.append(plan["root_cause"])
    parts.append("您可以：" + " ".join(plan["actions"][:2]))
    return "\n".join(parts)


def briefing_for_user(
    *,
    open_count: int,
    log_total: int,
    warn_logs: int,
    issues: list[str],
) -> str:
    if open_count == 0 and warn_logs == 0:
        return (
            f"您好，我刚帮您看了一圈，一切正常。"
            f"车牌识别、手势识别都在正常工作（近24小时 {log_total} 次记录）。"
            f"您放心使用，有问题我会主动告诉您。"
        )

    lines = ["您好，巡检发现以下情况："]
    if open_count > 0:
        lines.append(f"• 还有 {open_count} 条提醒没处理，建议打开「告警中心」逐条查看。")
    if warn_logs > 0:
        lines.append(f"• 近24小时有 {warn_logs} 次操作出现了警告，值得留意。")
    if len(lines) == 1:
        lines.append("• 整体正常，我会继续帮您盯着。")
    lines.append("不确定怎么处理的话，直接问我「怎么办」就行。")
    return "\n".join(lines)


def assistant_answer_for_user(
    question: str,
    context: dict[str, Any],
    *,
    intent: str | None = None,
) -> str:
    """无 LLM 或 LLM 失败时的模板回答 —— 按事件类型与问题意图差异化输出。"""
    event_type = context.get("event_type")
    event = event_type_to_user(event_type)
    title = context.get("title") or event
    summary = context.get("summary") or ""
    root_cause = context.get("root_cause") or ""
    suggestion = context.get("suggestion") or ""
    detail = context.get("detail") or {}
    knowledge = build_assistant_knowledge(context)
    plan = knowledge["plan"]
    detail_hint = knowledge["detail_hint"]
    steps = knowledge["personalized_actions"]
    q_intent = intent or detect_assistant_intent(question)
    perception = context.get("perception") or {}

    def _with_hint(body: str) -> str:
        if detail_hint and detail_hint not in body:
            return f"{detail_hint}\n{body}"
        return body

    if q_intent == "status":
        lines = [f"当前关注：「{title}」"]
        if detail_hint:
            lines.append(detail_hint)
        if perception.get("lpr"):
            lpr = perception["lpr"]
            lines.append(
                f"车牌识别：近 {lpr.get('recent_attempts', 0)} 次中失败 {lpr.get('recent_failures', 0)} 次，"
                f"连续失败 {lpr.get('consecutive_failures', 0)} 次。"
            )
        if perception.get("gesture"):
            for mod, info in perception["gesture"].items():
                mod_cn = "交警手势" if mod == "police" else "车主手势"
                lines.append(
                    f"{mod_cn}：最近置信度约 {info.get('avg_confidence_last_5', 0):.0%}。"
                )
        if perception.get("llm"):
            llm = perception["llm"]
            lines.append(f"智能分析额度已用 {llm.get('token_ratio_pct', 0)}%。")
        open_count = (context.get("system_status") or {}).get("open_alerts")
        if open_count is not None:
            lines.append(f"当前未处理告警 {open_count} 条。")
        if len(lines) == 1:
            lines.append(plan["root_cause"])
        return "\n".join(lines)

    if q_intent == "root_cause":
        if root_cause and not _is_useless_suggestion(root_cause):
            body = f"这条「{title}」的情况，{root_cause}"
            return _with_hint(humanize_tech_terms(body))
        body = f"我帮您看了下，「{title}」{plan['root_cause']}"
        if detail_hint:
            body = f"{detail_hint}\n{body}"
        return humanize_tech_terms(body)

    if q_intent == "action":
        step_text = _format_steps_conversational(steps)
        if suggestion and not _is_useless_suggestion(suggestion):
            return _with_hint(
                humanize_tech_terms(
                    f"针对「{title}」，{step_text}\n另外，{suggestion}"
                )
            )
        return _with_hint(humanize_tech_terms(f"针对「{title}」，{step_text}"))

    if q_intent == "severity":
        from app.utils.alert_analysis import build_severity_assessment

        structured = (context.get("detail") or {}).get("structured") or {}
        sev = structured.get("severity_assessment") or build_severity_assessment(
            event_type or "", context.get("level") or "warning", detail,
        )
        return _with_hint(humanize_tech_terms(sev.get("summary_text", sev.get("decision_reason", ""))))

    if q_intent == "impact":
        from app.utils.alert_analysis import build_event_impact

        structured = (context.get("detail") or {}).get("structured") or {}
        impact = structured.get("impact_scope") or build_event_impact(
            event_type or "", context.get("level") or "warning", detail,
        )
        if detail_hint:
            return humanize_tech_terms(f"对您来说，{impact}\n具体情况：{detail_hint}")
        return humanize_tech_terms(f"对您来说，{impact}")

    # general：综合回答，避免千篇一律
    parts = [f"关于「{title}」"]
    if detail_hint:
        parts.append(detail_hint)
    elif summary and not _is_useless_suggestion(summary):
        parts.append(summary)
    else:
        parts.append(plan["root_cause"])
    parts.append(_format_steps_conversational(steps[:2]))
    return humanize_tech_terms("\n".join(parts))
