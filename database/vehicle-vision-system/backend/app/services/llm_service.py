"""LLM 服务 —— 调用预训练大模型 API 生成告警摘要与智能助手回答（OpenAI 兼容接口）。"""

import json
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import settings, LLM_PROVIDER_PRESETS
from app.database import SessionLocal
from app.utils.logger import get_logger
from app.utils.user_language import (
    event_type_to_user,
    level_to_user,
    assistant_answer_for_user,
    build_assistant_knowledge,
    detect_assistant_intent,
    humanize_tech_terms,
    _get_plan,
    _format_steps,
    _format_steps_conversational,
    _is_useless_suggestion,
    is_chitchat,
)

llm_logger = get_logger("llm_service")

ALERT_SUMMARY_SYSTEM = """你是车载视觉感知系统的告警助手「小智」，正在帮车主/管理员解读系统异常。

说话风格：像一位靠谱同事在口头汇报——自然、有温度、好懂，不要公文腔或机器人腔。
禁止：「经检测」「请注意」「处理方法：」「影响范围：」等模板化标题；禁止 API 路径、Token、Webhook 等英文术语。

输出要求：
1. 必须返回合法 JSON，不要 Markdown 代码块
2. 各字段用完整、流畅的中文句子，像人在说话
3. 摘要里自然涵盖：异常类型、发生时间、影响范围、建议怎么处置
4. 处置建议要具体可执行，用「您可以先…再…」这类口语，不要写「请查看日志」"""

ALERT_SUMMARY_USER_TEMPLATE = """刚检测到一项系统异常，请用 JSON 生成结构化告警摘要（各字段职责严格分离，禁止重复）：

{{
  "title": "一句话标题，口语化",
  "summary": "1-2 句：仅描述「发生了什么 + 何时」，不要写原因和处理步骤",
  "root_cause": "仅解释「为什么发生」，用可能是/多半因为，禁止写处置步骤",
  "suggestion": "仅写用户可执行的处置步骤（1-3条），禁止重复 root_cause",
  "impact_scope": "仅写影响哪些功能模块、对用户有什么后果，一句话",
  "occurred_at": "发生时间描述（如：刚刚 / 过去5分钟内连续出现）"
}}

异常类型: {event_type}（含义：{event_type_cn}）
告警级别: {level}（info=提示, warning=警告, critical=严重）
当前时间: {now}
上下文数据: {context}
"""

INTENT_PROMPTS: dict[str, str] = {
    "root_cause": (
        "用户只想知道「为什么发生」。"
        "只回答原因分析，引用上下文中的具体数字和关联日志条数。"
        "禁止写处理步骤、禁止写影响范围、禁止重复摘要全文。2-3 句即可。"
    ),
    "action": (
        "用户只想知道「现在怎么做」。"
        "只给出 1-3 条可执行步骤，用「您可以先…再…」口语。"
        "禁止解释原因、禁止评估级别、禁止重复已有建议原文。"
    ),
    "severity": (
        "用户问「是否需要升级为严重告警」。"
        "必须包含：①当前级别 ②智能体判定依据（引用 detail 中的 count/rate/confidence 等）"
        "③是否建议手动升级 ④若继续恶化会怎样。"
        "禁止列出操作步骤清单、禁止重复影响范围全文。"
    ),
    "impact": (
        "用户只想知道「影响有多大」。"
        "只说明哪些功能模块受影响、用户能感知到什么、是否影响核心功能。"
        "禁止写原因、禁止写处理建议、禁止评估是否升级。"
    ),
    "status": (
        "用户想了解当前系统/该告警的实时状态。"
        "结合实时感知数据中的数字回答，说明各模块是否正常。"
    ),
    "driving": (
        "用户想要基于三路感知（车牌+交警手势+车主控车）的综合驾驶建议。"
        "优先引用 driving_advice 字段；结合具体车牌、手势、动作给出 1-2 句可执行驾驶指引。"
        "道路安全优先，像导航播报，禁止重复告警运维处置步骤。"
    ),
    "general": (
        "用户可能在闲聊、泛泛提问，或想了解系统能力。"
        "优先直接回应用户原话；若与车载感知/告警无关，可简短友好回复后再说明你能帮什么。"
        "若上下文里有告警或感知数据，可自然引用，但不强制按根因/建议/影响四项结构全写。"
        "可结合对话历史延续话题，保持口语自然。"
    ),
}


ASSISTANT_SYSTEM = """你是车载视觉感知系统的告警助手「小智」，像一位熟悉系统的同事，帮用户理解异常并给出建议。

你能解读：车牌识别失败、手势识别不准、智能分析超时或额度不足、未授权访问、数据库异常、模型加载失败等，也能综合三路感知给出驾驶建议。

回答风格：
- 先直接回应用户问题，再补充必要细节；语气亲切、自然，像微信里跟同事解释
- 结合上下文里的具体数字（失败次数、置信度、IP、额度等）个性化说明，不要套话
- 问原因就解释原因，问怎么办就给可执行建议，问影响就说对用户实际有什么影响
- 用中文大白话，2-4 段即可，不要写「处理方法：」「影响范围：」这类小标题
- 禁止 API 路径、Token、Webhook、SSE、unknown 等技术词；配置项用中文描述（如「群消息推送地址」）
- 纯文本输出，不要用 Markdown（禁止 **、#、* 等符号）"""

DRIVING_ADVICE_SYSTEM = """你是车载多路感知融合驾驶助手「小智」，综合车牌识别、交警手势、车主手势控车三路信号，为驾驶员生成简洁、可执行的综合驾驶建议。

原则：
1. 道路安全优先：交警指挥 > 车辆行驶 > 车内控车
2. 结合具体信号写建议，例如「前方交警停止手势 + 检测到前车车牌 → 建议减速避让」
3. 1-2 句口语化中文，像导航播报，不要公文腔
4. 禁止 API、技术术语；无信号时如实说明保持正常行驶

必须返回合法 JSON：
{
  "advice": "综合驾驶建议正文",
  "signals_summary": "用 + 连接的信号摘要",
  "priority": "high/medium/normal"
}"""


class LLMService:
    def _extract_tokens(self, resp_data: dict) -> int:
        usage = resp_data.get("usage") or {}
        return (
            usage.get("total_tokens")
            or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            or 0
        )

    async def _track_llm_response(self, resp_data: dict) -> None:
        tokens = self._extract_tokens(resp_data)
        from app.services.alert_agent import alert_agent

        db = SessionLocal()
        try:
            await alert_agent.track_llm_success(db, tokens_used=tokens)
        finally:
            db.close()

    async def _record_llm_failure(self, exc: Exception | None = None) -> None:
        from app.services.alert_agent import alert_agent

        db = SessionLocal()
        try:
            # LLM 自身失败所产生的告警必须强制使用本地模板。若这里再次请求
            # LLM 生成摘要，会形成“失败 -> 告警摘要 -> 再失败”的递归风暴，
            # 最终耗尽数据库连接池。
            await alert_agent.handle_llm_failure(
                db,
                exc or RuntimeError("LLM request failed"),
                {"source": "llm_service"},
            )
        finally:
            db.close()

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """统一 OpenAI 兼容 Chat Completions 调用。"""
        if not settings.llm_configured:
            raise RuntimeError("LLM API Key 未配置")

        payload: dict[str, Any] = {
            "model": settings.effective_llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            resp = await client.post(
                f"{settings.effective_llm_base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        await self._track_llm_response(data)
        return data

    async def test_connection(self) -> dict[str, Any]:
        """测试 LLM API 连通性（启动时或手动调用）。"""
        if not settings.llm_configured:
            return {
                "ok": False,
                "mode": "template",
                "message": "未配置 LLM_API_KEY，告警摘要将使用内置模板降级",
                "provider": settings.llm_provider,
            }

        try:
            data = await self.chat_completion(
                [
                    {"role": "system", "content": "你是系统助手，请用 JSON 回复。"},
                    {"role": "user", "content": '回复 JSON: {"status":"ok","message":"连接成功"}'},
                ],
                temperature=0,
                json_mode=True,
                max_tokens=64,
            )
            content = data["choices"][0]["message"]["content"]
            return {
                "ok": True,
                "mode": "llm",
                "message": "LLM API 连接正常",
                "provider": settings.llm_provider,
                "provider_label": settings.llm_provider_label,
                "model": settings.effective_llm_model,
                "base_url": settings.effective_llm_base,
                "sample_response": content[:200],
                "tokens_used": self._extract_tokens(data),
            }
        except Exception as e:
            llm_logger.warning("LLM 智能助手调用失败，降级本地模板: %s", e)
            await self._record_llm_failure(e)
            llm_logger.warning("LLM 连接测试失败: %s", e)
            return {
                "ok": False,
                "mode": "template",
                "message": f"LLM API 连接失败，将降级为模板告警: {e}",
                "provider": settings.llm_provider,
                "provider_label": settings.llm_provider_label,
                "model": settings.effective_llm_model,
                "base_url": settings.effective_llm_base,
                "error": str(e),
            }

    async def ask_assistant(
        self,
        question: str,
        context: dict[str, Any] | None = None,
        *,
        intent: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        context = context or {}
        q_intent = intent or detect_assistant_intent(question)
        if is_chitchat(question):
            q_intent = "general"
        knowledge = build_assistant_knowledge(context)

        if not settings.llm_configured:
            self.last_assistant_mode = "template"
            self.last_assistant_reason = "not_configured"
            return self._template_assistant_answer(question, context, intent=q_intent)

        plan = knowledge["plan"]
        structured = (context.get("detail") or {}).get("structured") or {}
        severity_block = structured.get("severity_assessment") or context.get("severity_assessment") or {}
        intent_instruction = INTENT_PROMPTS.get(q_intent, INTENT_PROMPTS["general"])

        user_prompt = f"""用户问: {question}
（回答意图: {q_intent}）

【本次回答约束】
{intent_instruction}

当前这条告警：
- 类型: {knowledge['event_name']}
- 级别: {knowledge.get('level', '提示')}
- 标题: {context.get('title', '')}
- 摘要: {context.get('summary', '')}
- 发生时间: {structured.get('occurred_at', '')}
- 影响范围: {structured.get('impact_scope', '')}
- 已有根因: {context.get('root_cause', '')}
- 已有建议: {context.get('suggestion', '')}
- 级别决策: {severity_block.get('summary_text') or severity_block.get('decision_reason', '')}
- 详情: {json.dumps(context.get('detail') or {}, ensure_ascii=False)}
- 补充: {knowledge.get('detail_hint', '')}
- 关联日志: {context.get('related_logs_count', 0)} 条

背景参考（按需选用，禁止四项全抄）：
- 常见原因: {plan['root_cause']}
- 可参考做法: {_format_steps_conversational(knowledge['personalized_actions'])}
- 可能影响: {plan['impact']}

实时感知: {json.dumps(context.get('perception') or {}, ensure_ascii=False)}
综合驾驶建议: {json.dumps(context.get('driving_advice') or {}, ensure_ascii=False)}
系统概况: {json.dumps(context.get('system_status') or {}, ensure_ascii=False)}

请结合对话历史（若有）自然延续话题，并严格按「本次回答约束」回答。"""

        messages: list[dict[str, str]] = [{"role": "system", "content": ASSISTANT_SYSTEM}]
        for item in (history or [])[-10:]:
            role = (item.get("role") or "").strip()
            content = (item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:2000]})
        messages.append({"role": "user", "content": user_prompt})

        try:
            data = await self.chat_completion(
                messages,
                temperature=0.6,
            )
            answer = data["choices"][0]["message"]["content"]
            answer = humanize_tech_terms(self._strip_markdown(answer))
            if not answer or len(answer.strip()) < 8:
                self.last_assistant_mode = "template"
                self.last_assistant_reason = "empty_response"
                return self._template_assistant_answer(question, context, intent=q_intent)
            if _is_useless_suggestion(answer):
                template = self._template_assistant_answer(question, context, intent=q_intent)
                if template and len(template) > len(answer):
                    self.last_assistant_mode = "template"
                    self.last_assistant_reason = "low_quality_response"
                    return template
            self.last_assistant_mode = "llm"
            self.last_assistant_reason = ""
            return answer
        except Exception as e:
            await self._record_llm_failure(e)
            self.last_assistant_mode = "template"
            self.last_assistant_reason = "api_error"
            return self._template_assistant_answer(question, context, intent=q_intent)

    def _template_assistant_answer(
        self,
        question: str,
        context: dict[str, Any],
        *,
        intent: str | None = None,
    ) -> str:
        return assistant_answer_for_user(question, context, intent=intent)

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """移除 LLM 常输出的 Markdown 标记，避免前端显示原始 ** 符号。"""
        if not text:
            return text
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        return text.strip()

    def _parse_json_response(self, content: str) -> dict[str, str] | None:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(content[start:end])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return None

    async def generate_alert_summary(
        self,
        event_type: str,
        level: str,
        context: dict[str, Any],
        *,
        force_template: bool = False,
    ) -> dict[str, str]:
        """通过 LLM API 生成结构化告警摘要；失败时降级为模板（不递归触发告警）。"""
        if force_template or not settings.llm_configured:
            return self._template_summary(event_type, level, context)

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        user_prompt = ALERT_SUMMARY_USER_TEMPLATE.format(
            event_type=event_type,
            event_type_cn=event_type_to_user(event_type),
            level=level,
            now=now,
            context=json.dumps(context, ensure_ascii=False),
        )

        try:
            data = await self.chat_completion(
                [
                    {"role": "system", "content": ALERT_SUMMARY_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.55,
                json_mode=True,
            )
            content = data["choices"][0]["message"]["content"]
            parsed = self._parse_json_response(content)
            if parsed and parsed.get("title") and parsed.get("summary"):
                return self._normalize_summary(parsed, event_type, level, context)
        except Exception as e:
            llm_logger.warning("LLM 告警摘要生成失败，降级模板: %s", e)
            await self._record_llm_failure(e)

        result = self._template_summary(event_type, level, context)
        result["_llm_failed"] = True
        return result

    def _normalize_summary(
        self,
        parsed: dict[str, Any],
        event_type: str,
        level: str,
        context: dict[str, Any],
    ) -> dict[str, str]:
        """合并 LLM 输出与模板兜底，确保字段完整。"""
        fallback = self._template_summary(event_type, level, context)
        summary = humanize_tech_terms(str(parsed.get("summary") or fallback["summary"]))

        return {
            "title": humanize_tech_terms(str(parsed.get("title") or fallback["title"])),
            "summary": summary,
            "root_cause": humanize_tech_terms(str(parsed.get("root_cause") or fallback["root_cause"])),
            "suggestion": humanize_tech_terms(str(parsed.get("suggestion") or fallback["suggestion"])),
            "impact_scope": humanize_tech_terms(
                str(parsed.get("impact_scope") or fallback.get("impact_scope", ""))
            ),
            "occurred_at": humanize_tech_terms(
                str(parsed.get("occurred_at") or fallback.get("occurred_at", ""))
            ),
        }

    def _template_summary(self, event_type: str, level: str, context: dict) -> dict[str, str]:
        plan = _get_plan(event_type)
        now_hint = context.get("timestamp") or "刚刚"
        templates = {
            "lpr_consecutive_failure": {
                "title": "车牌识别连续失败",
                "summary": (
                    f"系统检测到连续 {context.get('count', 5)} 次车牌识别失败（{now_hint}），"
                    f"道路感知中的车牌识别功能可能暂时不可用。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps(plan["actions"]),
            },
            "lpr_high_failure_rate": {
                "title": "车牌识别失败率过高",
                "summary": (
                    f"最近 {context.get('window_seconds', 300)} 秒内车牌识别失败率约 "
                    f"{context.get('rate', '30%')}（{context.get('fails', '?')}/{context.get('total', '?')} 次），"
                    f"识别准确率明显下降。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps(plan["actions"]),
            },
            "gesture_low_confidence": {
                "title": "手势识别置信度持续偏低",
                "summary": (
                    f"「{context.get('module', '手势')}」模块连续多次置信度低于 "
                    f"{context.get('threshold', 0.4):.0%}（当前约 {context.get('confidence', 0.3):.0%}），"
                    f"识别结果可能不可靠。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps(plan["actions"]),
            },
            "llm_api_timeout": {
                "title": "智能分析响应较慢",
                "summary": "告警智能体调用大语言模型 API 超时或失败，已自动降级为模板告警。",
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps(plan["actions"]),
            },
            "llm_token_exhausted": {
                "title": "智能分析额度即将耗尽",
                "summary": (
                    f"LLM Token 已使用 {context.get('used', '?')}/{context.get('limit', '?')} "
                    f"（{context.get('ratio', '80%')}），剩余 {context.get('remaining', '?')}。"
                ),
                "root_cause": "大语言模型 API 调用配额接近上限，继续调用可能失败。",
                "suggestion": "1. 检查 API 账户余额或配额\n2. 适当提高 alert_token_limit 配置\n3. 非紧急告警可暂时依赖模板模式",
            },
            "llm_token_exceeded": {
                "title": "智能分析额度已用完",
                "summary": f"LLM Token 配额已超额（{context.get('ratio', '100%')}），智能摘要功能已暂停。",
                "root_cause": "API 调用次数或 Token 用量达到账户上限。",
                "suggestion": "1. 充值或升级 API 套餐\n2. 系统将继续使用模板告警，不影响基础监控",
            },
            "unauthorized_access": {
                "title": "未授权访问尝试",
                "summary": (
                    f"检测到来自 {context.get('ip', '未知')} 的未授权 API 访问"
                    f"（路径: {context.get('path', '未知')}），"
                    f"近 {context.get('window_seconds', 300)} 秒内累计 {context.get('count', 1)} 次。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps(plan["actions"]),
            },
            "service_unhealthy": {
                "title": "系统服务健康异常",
                "summary": f"服务「{context.get('service', '未知')}」状态异常：{context.get('detail', '需检查')}。",
                "root_cause": "系统组件运行异常，可能影响相关功能。",
                "suggestion": "1. 查看告警中心了解详情\n2. 重启相关服务\n3. 检查数据库连接",
            },
            "model_load_failure": {
                "title": "AI 模型加载失败",
                "summary": f"模型「{context.get('model_name', '未知')}」加载失败，相关识别功能不可用。",
                "root_cause": f"错误类型: {context.get('error_type', '未知')}，详情: {context.get('error', '未知')}",
                "suggestion": "1. 确认模型文件已下载到 models 目录\n2. 检查磁盘空间与文件权限\n3. 重启服务后重试",
            },
            "database_connection_error": {
                "title": "数据库连接异常",
                "summary": f"连续 {context.get('consecutive_fails', 3)} 次数据库连接失败，数据读写可能中断。",
                "root_cause": "数据库服务未启动、连接字符串错误或网络异常。",
                "suggestion": "1. 检查 SQL Server / SQLite 文件是否存在\n2. 验证 DATABASE_URL 配置\n3. 重启数据库服务",
            },
            "webhook_delivery_failure": {
                "title": "群消息推送失败",
                "summary": (
                    f"刚才往群里发告警消息时失败了（近 {context.get('window', '几次')} "
                    f"约 {context.get('fails', '?')} 次没发出去），群里可能暂时收不到推送。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps_conversational(plan["actions"]),
            },
            "email_delivery_failure": {
                "title": "邮件通知发送失败",
                "summary": (
                    f"邮件通知最近发送不太顺利（近 {context.get('window', '几次')} "
                    f"约 {context.get('fails', '?')} 次失败），邮箱可能收不到提醒。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps_conversational(plan["actions"]),
            },
            "config_missing": {
                "title": "系统配置不完整",
                "summary": (
                    f"系统发现「{humanize_tech_terms(str(context.get('config_key', '某项配置')))}」还没设置好，"
                    f"部分功能可能会受影响。"
                ),
                "root_cause": plan["root_cause"],
                "suggestion": _format_steps_conversational(plan["actions"]),
            },
            "test_event": {
                "title": "这是一条测试提醒",
                "summary": plan["root_cause"],
                "root_cause": "这是功能演示，不代表真实故障。",
                "suggestion": _format_steps(plan["actions"]),
            },
        }
        base = templates.get(event_type, {
            "title": event_type_to_user(event_type),
            "summary": f"刚才检测到{event_type_to_user(event_type)}（{level_to_user(level)}级别），建议留意一下。",
            "root_cause": plan["root_cause"],
            "suggestion": _format_steps_conversational(plan["actions"]),
        })
        from app.utils.alert_analysis import build_event_impact, format_occurred_at

        result = {k: humanize_tech_terms(v) if isinstance(v, str) else v for k, v in base.items()}
        result["impact_scope"] = humanize_tech_terms(
            build_event_impact(event_type, level, context)
        )
        result["occurred_at"] = format_occurred_at(None, context)
        return result

    async def generate_driving_advice(
        self,
        correlated: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        *,
        force_template: bool = False,
    ) -> dict[str, Any]:
        """综合三路感知，由 LLM 生成驾驶建议；失败时降级模板。"""
        from app.utils.driving_advice import build_template_driving_advice

        fallback = build_template_driving_advice(correlated, snapshot)
        if force_template or not settings.llm_configured:
            return fallback

        user_prompt = f"""请根据以下多路感知数据生成综合驾驶建议 JSON：

实时快照: {json.dumps(snapshot or {}, ensure_ascii=False)}
关联信号（{snapshot.get('window_seconds', 30) if snapshot else 30}秒窗口）: {json.dumps(correlated, ensure_ascii=False)}

模板参考（可优化表述，勿偏离事实）: {fallback.get('advice', '')}"""

        try:
            data = await self.chat_completion(
                [
                    {"role": "system", "content": DRIVING_ADVICE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
                json_mode=True,
                max_tokens=400,
            )
            content = data["choices"][0]["message"]["content"]
            parsed = self._parse_json_response(content)
            if parsed and parsed.get("advice"):
                return {
                    "advice": humanize_tech_terms(str(parsed["advice"]).strip()),
                    "signals_summary": humanize_tech_terms(
                        str(parsed.get("signals_summary") or fallback.get("signals_summary", ""))
                    ),
                    "priority": str(parsed.get("priority") or fallback.get("priority", "normal")),
                    "mode": "llm",
                    "sources": fallback.get("sources", {}),
                }
        except Exception as e:
            llm_logger.warning("LLM 驾驶建议生成失败，降级模板: %s", e)
            await self._record_llm_failure(e)

        result = dict(fallback)
        result["mode"] = "template"
        result["_llm_failed"] = True
        return result

    def get_provider_options(self) -> list[dict[str, str]]:
        """返回支持的 LLM 厂商列表（供前端/文档展示）。"""
        return [
            {"key": key, "label": val["label"], "base": val["base"], "model": val["model"]}
            for key, val in LLM_PROVIDER_PRESETS.items()
        ]


llm_service = LLMService()
