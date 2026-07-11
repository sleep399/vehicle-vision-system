import asyncio
import json
import smtplib
from collections import defaultdict, deque
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alerts import AlertEvent
from app.services.llm_service import llm_service
from app.utils.logger import write_log


class AlertAgent:
    """告警智能体：感知异常、决策级别、生成摘要、推送通知"""

    LEVELS = {"info": 1, "warning": 2, "critical": 3}

    def __init__(self):
        self._failure_counts: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._confidence_history: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._ws_clients: set = set()
        self._lock = asyncio.Lock()

    def register_ws(self, ws):
        self._ws_clients.add(ws)

    def unregister_ws(self, ws):
        self._ws_clients.discard(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    def record_lpr_result(self, success: bool):
        self._failure_counts["lpr"].append(0 if success else 1)

    def record_gesture_confidence(self, module: str, confidence: float):
        self._confidence_history[module].append(confidence)

    async def check_and_alert(self, db: Session, module: str) -> AlertEvent | None:
        event = None
        if module == "lpr":
            failures = list(self._failure_counts["lpr"])
            if len(failures) >= settings.alert_failure_threshold and sum(failures[-settings.alert_failure_threshold:]) == settings.alert_failure_threshold:
                event = await self._create_alert(db, "lpr_consecutive_failure", "critical", {"count": settings.alert_failure_threshold, "module": "lpr"})
        elif module in ("police", "owner"):
            confs = list(self._confidence_history[module])
            if len(confs) >= 5 and all(c < settings.low_confidence_threshold for c in confs[-5:]):
                event = await self._create_alert(db, "gesture_low_confidence", "warning", {"confidence": sum(confs[-5:]) / 5, "module": module})
        return event

    async def trigger_alert(
        self,
        db: Session,
        event_type: str,
        level: str = "warning",
        context: dict | None = None,
    ) -> AlertEvent:
        return await self._create_alert(db, event_type, level, context or {})

    async def _create_alert(
        self,
        db: Session,
        event_type: str,
        level: str,
        context: dict,
    ) -> AlertEvent:
        summary_data = await llm_service.generate_alert_summary(event_type, level, context)
        alert = AlertEvent(
            level=level,
            event_type=event_type,
            title=summary_data.get("title", event_type),
            summary=summary_data.get("summary", ""),
            detail_json=json.dumps(context, ensure_ascii=False),
            root_cause=summary_data.get("root_cause"),
            suggestion=summary_data.get("suggestion"),
            channels_sent="web",
            created_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)

        write_log(db, "alert", f"[{level}] {alert.title}", level=level.upper(), detail=context)

        payload = {
            "type": "alert",
            "id": alert.id,
            "level": level,
            "event_type": event_type,
            "title": alert.title,
            "summary": alert.summary,
            "root_cause": alert.root_cause,
            "suggestion": alert.suggestion,
            "created_at": alert.created_at.isoformat(),
        }
        await self.broadcast(payload)

        channels = ["web"]
        if await self._send_webhook(payload):
            channels.append("webhook")
        if await self._send_email(alert):
            channels.append("email")
        alert.channels_sent = ",".join(channels)
        db.commit()

        return alert

    async def _send_webhook(self, payload: dict) -> bool:
        if not settings.webhook_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(settings.webhook_url, json={
                    "msgtype": "text",
                    "text": {"content": f"[{payload['level'].upper()}] {payload['title']}\n{payload['summary']}\n建议: {payload.get('suggestion', '')}"},
                })
            return True
        except Exception:
            return False

    async def _send_email(self, alert: AlertEvent) -> bool:
        if not all([settings.smtp_host, settings.smtp_user, settings.alert_email_to]):
            return False
        try:
            msg = MIMEText(f"{alert.summary}\n\n根因: {alert.root_cause}\n建议: {alert.suggestion}")
            msg["Subject"] = f"[{alert.level.upper()}] {alert.title}"
            msg["From"] = settings.smtp_user
            msg["To"] = settings.alert_email_to
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
            return True
        except Exception:
            return False

    def get_stats(self, db: Session) -> dict[str, Any]:
        alerts = db.query(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(100).all()
        by_level = defaultdict(int)
        by_type = defaultdict(int)
        for a in alerts:
            by_level[a.level] += 1
            by_type[a.event_type] += 1
        return {
            "total": len(alerts),
            "by_level": dict(by_level),
            "by_type": dict(by_type),
            "recent": [
                {
                    "id": a.id,
                    "level": a.level,
                    "event_type": a.event_type,
                    "title": a.title,
                    "summary": a.summary,
                    "status": a.status,
                    "created_at": a.created_at.isoformat(),
                }
                for a in alerts[:20]
            ],
        }


alert_agent = AlertAgent()
