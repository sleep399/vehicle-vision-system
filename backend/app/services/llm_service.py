import json
from typing import Any
import httpx
from app.config import settings


class LLMService:
    async def generate_alert_summary(
        self,
        event_type: str,
        level: str,
        context: dict[str, Any],
    ) -> dict[str, str]:
        if not settings.llm_api_key:
            return self._template_summary(event_type, level, context)

        prompt = f"""你是车载视觉感知系统的运维告警智能体。请根据以下异常事件生成结构化告警摘要，用中文回复 JSON 格式：
{{
  "title": "简短标题",
  "summary": "自然语言摘要，包含异常类型、发生时间、影响范围",
  "root_cause": "可能的根因分析",
  "suggestion": "建议处置措施"
}}

异常类型: {event_type}
告警级别: {level}
上下文: {json.dumps(context, ensure_ascii=False)}
"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{settings.llm_api_base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={
                        "model": settings.llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
        except Exception as e:
            result = self._template_summary(event_type, level, context)
            result["root_cause"] = f"LLM 调用失败: {e}"
            return result
        return self._template_summary(event_type, level, context)

    def _template_summary(self, event_type: str, level: str, context: dict) -> dict[str, str]:
        templates = {
            "lpr_consecutive_failure": {
                "title": "车牌识别连续失败",
                "summary": f"系统检测到连续 {context.get('count', 5)} 次车牌识别失败，可能影响道路感知功能。",
                "root_cause": "可能原因：摄像头遮挡、光照不足、模型加载异常或输入图像质量过低。",
                "suggestion": "检查摄像头状态，确认模型服务正常，尝试更换输入源或调整曝光参数。",
            },
            "gesture_low_confidence": {
                "title": "手势识别置信度持续偏低",
                "summary": f"手势识别模块置信度低于阈值 ({context.get('confidence', 0.3):.0%})，识别结果可能不可靠。",
                "root_cause": "可能原因：手部/人体未完整入镜、背景干扰、光照变化或遮挡。",
                "suggestion": "调整摄像头角度，改善光照条件，确保目标完整可见。",
            },
            "llm_api_timeout": {
                "title": "LLM API 调用超时",
                "summary": "告警智能体调用大语言模型 API 超时，自动降级为模板告警。",
                "root_cause": "网络延迟、API 服务不可用或 Token 配额不足。",
                "suggestion": "检查 API 密钥与网络连接，确认配额余额，必要时切换备用模型。",
            },
            "unauthorized_access": {
                "title": "未授权访问尝试",
                "summary": f"检测到来自 {context.get('ip', '未知')} 的未授权 API 访问。",
                "root_cause": "无效或过期的访问令牌，或恶意扫描行为。",
                "suggestion": "审查访问日志，更新密钥策略，必要时封禁 IP。",
            },
        }
        base = templates.get(event_type, {
            "title": f"系统异常: {event_type}",
            "summary": f"检测到 {event_type} 事件，级别: {level}",
            "root_cause": "待进一步分析",
            "suggestion": "请查看系统日志获取详细信息",
        })
        return base


llm_service = LLMService()
