from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alerts import AlertEvent
from app.models.logs import SystemLog
from app.schemas import AlertResponse, LogResponse
from app.services.alert_agent import alert_agent
from app.services.llm_service import llm_service
import json

router = APIRouter(prefix="/api/monitor", tags=["监控与告警"])


class AssistantQuery(BaseModel):
    question: str
    event_type: str | None = None
    path: str | None = None
    ip: str | None = None


@router.get("/logs", response_model=list[LogResponse], summary="系统日志查询")
def get_logs(
    category: str | None = None,
    level: str | None = None,
    user_id: int | None = None,
    start: datetime | None = Query(None, description="开始时间，ISO 格式"),
    end: datetime | None = Query(None, description="结束时间，ISO 格式"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(SystemLog).order_by(SystemLog.created_at.desc())
    if category:
        q = q.filter(SystemLog.category == category)
    if level:
        q = q.filter(SystemLog.level == level)
    if user_id is not None:
        q = q.filter(SystemLog.user_id == user_id)
    if start:
        q = q.filter(SystemLog.created_at >= start)
    if end:
        q = q.filter(SystemLog.created_at <= end)
    rows = q.offset(skip).limit(limit).all()
    out = []
    for r in rows:
        detail = None
        if r.detail_json:
            try:
                detail = json.loads(r.detail_json)
            except Exception:
                detail = r.detail_json
        out.append({
            "id": r.id,
            "category": r.category,
            "level": r.level,
            "message": r.message,
            "detail_json": detail,
            "user_id": r.user_id,
            "created_at": r.created_at,
        })
    return out


@router.get("/alerts", response_model=list[AlertResponse], summary="告警历史")
def get_alerts(
    level: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(AlertEvent).order_by(AlertEvent.created_at.desc())
    if level:
        q = q.filter(AlertEvent.level == level)
    return q.offset(skip).limit(limit).all()


@router.get("/alerts/stats", summary="告警统计仪表盘")
def alert_stats(db: Session = Depends(get_db)):
    return alert_agent.get_stats(db)


@router.post("/alerts/{alert_id}/resolve", summary="标记告警已处理")
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.query(AlertEvent).get(alert_id)
    if not alert:
        return {"message": "告警不存在"}
    alert.status = "resolved"
    alert.resolved_at = datetime.utcnow()
    db.commit()
    return {"message": "已处理", "id": alert_id}


@router.post("/alerts/test", summary="触发测试告警")
async def test_alert(db: Session = Depends(get_db)):
    alert = await alert_agent.trigger_alert(db, "test_event", "info", {"source": "manual_test"})
    return {"id": alert.id, "title": alert.title, "summary": alert.summary}


@router.post("/assistant", summary="告警助手问答")
async def assistant_chat(payload: AssistantQuery):
    context = {
        "event_type": payload.event_type or "未知异常",
        "path": payload.path or "未知路径",
        "ip": payload.ip or "未知 IP",
    }
    answer = await llm_service.ask_assistant(payload.question, context)
    return {"answer": answer, "context": context}
