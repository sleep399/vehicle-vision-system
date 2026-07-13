"""监控与告警路由 —— 日志查询、告警管理、SSE推送、告警回放"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, SessionLocal
from app.models.alerts import AlertEvent
from app.models.logs import SystemLog
from app.schemas import AlertResponse, LogResponse
from app.services.alert_agent import alert_agent, EVENT_TYPES, DEFAULT_LEVELS
from app.services.llm_service import llm_service
from app.services.scenario_fusion_service import scenario_fusion_service
from app.services.log_stream import register as register_log_sse, unregister as unregister_log_sse, client_count as log_stream_client_count
from app.utils.auth import get_current_user
from app.utils.logger import write_log, get_logger, localize_utc, level_to_cn, level_filter_variants
from app.utils.log_display import format_log_entry, category_cn, sanitize_log_message
from app.utils.user_language import (
    briefing_for_user,
    alert_for_user,
    event_type_to_user,
    detect_assistant_intent,
    needs_alert_context,
    build_which_alert_prompt,
)

router = APIRouter(prefix="/api/monitor", tags=["监控与告警"])

monitor_logger = get_logger("monitor")


class HistoryMessage(BaseModel):
    role: str
    content: str


class AssistantQuery(BaseModel):
    question: str
    event_type: str | None = None
    path: str | None = None
    ip: str | None = None
    alert_id: int | None = None
    intent: str | None = None
    history: list[HistoryMessage] | None = None


class MarkResolvedRequest(BaseModel):
    resolution_note: str | None = None


def _get_monitor_user(
    request: Request,
):
    """Resolve bearer auth for REST and the query token used by SSE clients."""
    authorization = request.headers.get("authorization", "")
    bearer_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    token = bearer_token or request.query_params.get("token")
    db = SessionLocal()
    try:
        return get_current_user(token=token, db=db)
    finally:
        db.close()


def _scope_user_id(user) -> int | None:
    return getattr(user, "id", None)


def _to_utc_naive(dt: datetime | None) -> datetime | None:
    """将查询时间统一为 UTC naive，与数据库存储的 utcnow() 对齐。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ════════════════════════════════════════════
# 日志查询 API
# ════════════════════════════════════════════

@router.get("/logs", response_model=list[LogResponse], summary="系统日志查询")
def get_logs(
    category: str | None = None,
    level: str | None = None,
    search: str | None = Query(None, description="关键词搜索（消息内容）"),
    start: datetime | None = Query(None, description="开始时间，ISO 格式"),
    end: datetime | None = Query(None, description="结束时间，ISO 格式"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """查询系统日志，支持多维度过滤。

    支持按类别（lpr/police_gesture/owner_gesture/alert/user/system/agent）、
    级别（信息/警告/错误/严重）、关键词和时间范围进行过滤。
    登录账号仅能查询自己的日志，未登录游客共享游客日志。
    """
    user_id = _scope_user_id(user)
    q = (
        db.query(SystemLog)
        .filter(SystemLog.user_id == user_id)
        .order_by(SystemLog.created_at.desc())
    )
    if category:
        q = q.filter(SystemLog.category == category)
    if level:
        variants = level_filter_variants(level)
        if variants:
            q = q.filter(SystemLog.level.in_(variants))
    if search:
        q = q.filter(SystemLog.message.contains(search))
    start_utc = _to_utc_naive(start)
    end_utc = _to_utc_naive(end)
    if start_utc:
        q = q.filter(SystemLog.created_at >= start_utc)
    if end_utc:
        q = q.filter(SystemLog.created_at <= end_utc)
    rows = q.offset(skip).limit(limit).all()

    out = []
    for r in rows:
        detail = None
        if r.detail_json:
            try:
                detail = json.loads(r.detail_json)
            except Exception:
                detail = r.detail_json
        out.append(format_log_entry(
            category=r.category,
            level=r.level,
            message=r.message,
            detail=detail,
            id=r.id,
            user_id=r.user_id,
            created_at=localize_utc(r.created_at),
        ))
    return out


@router.get("/logs/categories", summary="日志类别列表")
def log_categories():
    """返回所有支持的日志类别"""
    return {
        "categories": [
            {"key": "lpr", "name": "车牌识别日志"},
            {"key": "police_gesture", "name": "交警手势识别日志"},
            {"key": "owner_gesture", "name": "车主手势识别日志"},
            {"key": "alert", "name": "告警日志"},
            {"key": "user", "name": "用户操作日志"},
            {"key": "system", "name": "系统运行日志"},
            {"key": "agent", "name": "智能体决策日志"},
        ],
        "levels": [
            {"key": "信息", "name": "信息"},
            {"key": "警告", "name": "警告"},
            {"key": "错误", "name": "错误"},
            {"key": "严重", "name": "严重"},
        ],
    }


@router.get("/logs/stats", summary="日志统计概览")
def log_stats(
    hours: int = Query(24, description="统计最近N小时"),
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """获取日志统计概览：各类别/各级别数量汇总"""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    user_id = _scope_user_id(user)
    rows = (
        db.query(SystemLog)
        .filter(SystemLog.created_at >= cutoff, SystemLog.user_id == user_id)
        .all()
    )

    by_category: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for r in rows:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        level_key = level_to_cn(r.level)
        by_level[level_key] = by_level.get(level_key, 0) + 1

    # 按小时统计趋势
    by_hour: dict[str, int] = {}
    for r in rows:
        hour_key = r.created_at.strftime("%Y-%m-%d %H:00")
        by_hour[hour_key] = by_hour.get(hour_key, 0) + 1

    hour_trend = [{"hour": k, "count": v} for k, v in sorted(by_hour.items())]

    category_labels = {
        "lpr": "车牌识别",
        "police_gesture": "交警手势",
        "owner_gesture": "车主手势",
        "alert": "告警",
        "user": "用户操作",
        "system": "系统运行",
        "agent": "智能体决策",
    }
    category_ranked = [
        {"key": k, "name": category_labels.get(k, k), "count": v}
        for k, v in sorted(by_category.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "total": len(rows),
        "hours": hours,
        "by_category": by_category,
        "by_level": by_level,
        "hour_trend": hour_trend,
        "category_ranked": category_ranked,
    }


@router.get("/logs/stream", summary="SSE 实时日志推送")
async def logs_stream(request: Request, user=Depends(_get_monitor_user)):
    """订阅系统日志写入事件，供监控日志页实时展示新日志。"""
    user_id = _scope_user_id(user)
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    register_log_sse(queue, user_id=user_id)

    async def event_generator():
        try:
            yield f"event: connected\ndata: {json.dumps({'status': 'connected', 'timestamp': localize_utc(datetime.utcnow())}, ensure_ascii=False)}\n\n"
            yield ": ping\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unregister_log_sse(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ════════════════════════════════════════════
# 告警管理 API
# ════════════════════════════════════════════

@router.get("/alerts", response_model=list[AlertResponse], summary="告警历史查询")
def get_alerts(
    level: str | None = None,
    event_type: str | None = None,
    status: str | None = Query(None, description="open/resolved"),
    start: datetime | None = None,
    end: datetime | None = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """查询告警历史记录，支持多维度过滤"""
    user_id = _scope_user_id(user)
    q = (
        db.query(AlertEvent)
        .filter(AlertEvent.user_id == user_id)
        .order_by(AlertEvent.created_at.desc())
    )
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
    rows = q.offset(skip).limit(limit).all()
    out = []
    for a in rows:
        detail = None
        if a.detail_json:
            try:
                detail = json.loads(a.detail_json)
            except Exception:
                detail = a.detail_json
        out.append({
            "id": a.id,
            "level": a.level,
            "event_type": a.event_type,
            "event_type_cn": EVENT_TYPES.get(a.event_type, a.event_type),
            "title": a.title,
            "summary": a.summary,
            "detail": detail,
            "root_cause": a.root_cause,
            "suggestion": a.suggestion,
            "channels_sent": a.channels_sent,
            "status": a.status,
            "resolution_note": a.resolution_note,
            "created_at": localize_utc(a.created_at),
            "resolved_at": localize_utc(a.resolved_at),
        })
    return out


@router.get("/alerts/stats", summary="告警统计仪表盘")
def alert_stats(db: Session = Depends(get_db), user=Depends(_get_monitor_user)):
    """获取完整的告警统计仪表盘数据：
    - 各级别/各类型分布
    - 时间趋势
    - 处理率
    - Token 用量
    """
    return alert_agent.get_stats(db, user_id=_scope_user_id(user))


@router.get("/alerts/analytics", summary="告警分析仪表盘")
def alert_analytics(
    days: int = Query(7, ge=1, le=90, description="统计最近N天"),
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """获取指定天数范围内的告警分析数据：趋势、类型排名、小时分布、MTTR"""
    return alert_agent.get_analytics(db, days=days, user_id=_scope_user_id(user))


@router.get("/alerts/timeline", summary="告警历史时间线")
def alert_timeline(
    level: str | None = None,
    event_type: str | None = None,
    status: str | None = Query(None, description="open/resolved"),
    start: datetime | None = None,
    end: datetime | None = None,
    skip: int = 0,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """按日期分组的告警历史时间线，支持分页加载"""
    return alert_agent.get_timeline(
        db, level=level, event_type=event_type, status=status,
        start=start, end=end, skip=skip, limit=limit,
        user_id=_scope_user_id(user),
    )


@router.get("/alerts/event-types", summary="支持的事件类型")
def event_types():
    """获取智能体支持检测的所有异常事件类型"""
    return alert_agent.get_event_types()


@router.get("/alerts/{alert_id}", summary="告警详情")
def get_alert_detail(
    alert_id: int,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """获取指定告警的详细信息"""
    replay = alert_agent.get_event_replay(db, alert_id, user_id=_scope_user_id(user))
    if not replay:
        raise HTTPException(404, "告警不存在")
    return replay


@router.get("/alerts/{alert_id}/replay", summary="告警事件回放")
def get_alert_replay(
    alert_id: int,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """获取告警事件的回放数据：
    - 告警详情（含结构化上下文）
    - 时间窗口内的关联日志
    """
    replay = alert_agent.get_event_replay(db, alert_id, user_id=_scope_user_id(user))
    if not replay:
        raise HTTPException(404, "告警不存在")
    return replay


@router.post("/alerts/{alert_id}/resolve", summary="标记告警已处理")
def resolve_alert(
    alert_id: int,
    body: MarkResolvedRequest | None = None,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """将告警标记为已处理，可附带处理说明"""
    user_id = _scope_user_id(user)
    alert = (
        db.query(AlertEvent)
        .filter(AlertEvent.id == alert_id, AlertEvent.user_id == user_id)
        .first()
    )
    if not alert:
        raise HTTPException(404, "告警不存在")
    if alert.status == "resolved":
        return {"message": "告警已是已处理状态", "id": alert_id}

    alert.status = "resolved"
    alert.resolved_at = datetime.utcnow()
    note = body.resolution_note if body and body.resolution_note else ""
    if note:
        alert.resolution_note = note
    db.commit()

    write_log(
        db, "alert",
        f"告警 #{alert_id} 已处理: {alert.title}" + (f" ({note})" if note else ""),
        level="INFO",
        detail={"alert_id": alert_id, "note": note},
        user_id=user_id,
    )
    return {
        "message": "已处理",
        "id": alert_id,
        "resolved_at": localize_utc(alert.resolved_at),
        "resolution_note": alert.resolution_note,
    }


@router.post("/alerts/cleanup-noise", summary="清理历史噪声告警")
def cleanup_noise_alerts(
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """将测试告警、可选配置缺失等历史噪声标记为已处理，避免仪表盘一直显示未处理。"""
    noise_types = ("config_missing", "test_event")
    rows = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.status == "open",
            AlertEvent.event_type.in_(noise_types),
            AlertEvent.user_id == _scope_user_id(user),
        )
        .all()
    )
    now = datetime.utcnow()
    for alert in rows:
        alert.status = "resolved"
        alert.resolved_at = now
        if not alert.resolution_note:
            alert.resolution_note = "系统自动清理：测试/可选配置类历史告警，非真实故障"
    db.commit()
    return {"resolved": len(rows), "types": list(noise_types)}


@router.post("/alerts/test", summary="触发测试告警")
async def test_alert(
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """手动触发一条测试告警，验证告警链路是否正常"""
    alert = await alert_agent.trigger_alert(
        db, "test_event", "info",
        {"source": "manual_test", "timestamp": localize_utc(datetime.utcnow())},
        user_id=_scope_user_id(user),
    )
    return {
        "id": alert.id,
        "title": alert.title,
        "summary": alert.summary,
        "root_cause": alert.root_cause,
        "suggestion": alert.suggestion,
        "event_type": alert.event_type,
        "level": alert.level,
        "channels": alert.channels_sent,
    }


@router.post("/alerts/test/{event_type}", summary="触发指定类型的测试告警")
async def test_alert_type(
    event_type: str,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """触发指定类型的测试告警"""
    if event_type not in EVENT_TYPES:
        raise HTTPException(400, f"不支持的告警类型: {event_type}，支持的类型: {list(EVENT_TYPES.keys())}")
    level = DEFAULT_LEVELS.get(event_type, "warning")
    alert = await alert_agent.trigger_alert(
        db, event_type, level,
        {"source": "manual_test_" + event_type, "timestamp": localize_utc(datetime.utcnow())},
        user_id=_scope_user_id(user),
    )
    return {
        "id": alert.id,
        "title": alert.title,
        "summary": alert.summary,
        "root_cause": alert.root_cause,
        "suggestion": alert.suggestion,
        "event_type": alert.event_type,
        "level": alert.level,
        "channels": alert.channels_sent,
    }


# ════════════════════════════════════════════
# SSE 实时告警推送
# ════════════════════════════════════════════

@router.get("/stream", summary="SSE 实时告警推送", include_in_schema=True)
async def sse_stream(request: Request, user=Depends(_get_monitor_user)):
    """Server-Sent Events 实时告警推送端点。

    客户端通过 EventSource 连接此端点即可接收实时告警事件。
    与 WebSocket 相比，SSE 更轻量、穿透防火墙更好、自动重连。

    用法:
        const es = new EventSource('/api/monitor/stream');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    user_id = _scope_user_id(user)
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    alert_agent.register_sse(queue, user_id=user_id)

    async def event_generator():
        try:
            # 连接成功消息
            yield f"event: connected\ndata: {json.dumps({'status': 'connected', 'timestamp': localize_utc(datetime.utcnow())})}\n\n"

            # 心跳 ping
            yield f": ping\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳
                    yield f": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            alert_agent.unregister_sse(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ════════════════════════════════════════════
# 监控指标
# ════════════════════════════════════════════

@router.get("/token-usage", summary="LLM Token 用量")
def token_usage(user=Depends(_get_monitor_user)):
    """获取 LLM Token 用量统计"""
    usage = alert_agent.get_token_usage(user_id=_scope_user_id(user))
    used = usage["used"]
    limit = usage["limit"]
    return {
        "used": used,
        "limit": limit,
        "remaining": limit - used,
        "ratio_pct": round(used / max(limit, 1) * 100, 1),
        "status": "critical" if used >= limit else ("warning" if used >= limit * 0.8 else "healthy"),
    }


@router.get("/connections", summary="推送连接状态")
def connection_status(user=Depends(_get_monitor_user)):
    """获取当前 WebSocket/SSE 连接数"""
    user_id = _scope_user_id(user)
    counts = alert_agent.connection_counts(user_id=user_id)
    return {
        **counts,
        "log_sse_clients": log_stream_client_count(user_id=user_id),
    }


# ════════════════════════════════════════════
# 智能体状态简报
# ════════════════════════════════════════════

@router.get("/agent/briefing", summary="智能体巡检简报")
def agent_briefing(
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """返回告警智能体当前感知到的系统全貌，供前端主动播报与巡检展示。"""
    from datetime import timedelta

    user_id = _scope_user_id(user)
    stats = alert_agent.get_stats(db, user_id=user_id)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    log_rows = (
        db.query(SystemLog)
        .filter(SystemLog.created_at >= cutoff, SystemLog.user_id == user_id)
        .all()
    )

    by_category: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for r in log_rows:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_level[r.level] = by_level.get(r.level, 0) + 1

    recent_alerts = (
        db.query(AlertEvent)
        .filter(AlertEvent.user_id == user_id)
        .order_by(AlertEvent.created_at.desc())
        .limit(5)
        .all()
    )

    open_count = stats.get("open", 0)

    issues: list[str] = []
    if open_count > 0:
        issues.append(f"有 {open_count} 条未处理告警")

    warn_logs = (
        by_level.get("警告", 0)
        + by_level.get("错误", 0)
        + by_level.get("严重", 0)
        + by_level.get("WARN", 0)
        + by_level.get("ERROR", 0)
        + by_level.get("CRITICAL", 0)
    )

    if not issues and warn_logs == 0:
        summary = (
            f"系统运行正常。过去24小时共记录 {len(log_rows)} 条日志，"
            f"暂无未处理告警。我正在持续监听车牌识别、手势识别与用户操作。"
        )
    else:
        summary = (
            f"巡检发现需关注项：{'；'.join(issues) if issues else '无未处理告警'}。"
            f"过去24小时日志 {len(log_rows)} 条（含 {warn_logs} 条警告/错误）。"
            f"建议查看告警中心或让我分析具体异常。"
        )

    summary_user = briefing_for_user(
        open_count=open_count,
        log_total=len(log_rows),
        warn_logs=warn_logs,
        issues=issues,
    )

    return {
        "summary": summary,
        "summary_user": summary_user,
        "open_alerts": open_count,
        "total_alerts": stats.get("total", 0),
        "resolution_rate": stats.get("resolution_rate", 0),
        "logs_24h": {
            "total": len(log_rows),
            "by_category": by_category,
            "by_level": by_level,
            "warn_or_above": warn_logs,
        },
        "recent_alerts": [
            {
                "id": a.id,
                "level": a.level,
                "event_type": a.event_type,
                "event_type_cn": EVENT_TYPES.get(a.event_type, a.event_type),
                "event_type_user": event_type_to_user(a.event_type),
                "title": a.title,
                "summary": a.summary,
                "summary_user": alert_for_user({
                    "level": a.level,
                    "event_type": a.event_type,
                    "title": a.title,
                    "summary": a.summary,
                    "root_cause": a.root_cause,
                    "suggestion": a.suggestion,
                }),
                "status": a.status,
                "created_at": localize_utc(a.created_at),
            }
            for a in recent_alerts
        ],
        "token_usage": stats.get("token_usage", {}),
        "recent_agent_logs": alert_agent.get_recent_agent_logs(db, limit=8, user_id=user_id),
        "monitoring": True,
        "timestamp": localize_utc(datetime.utcnow()),
    }


# ════════════════════════════════════════════
# 告警助手 AI
# ════════════════════════════════════════════

@router.post("/assistant", summary="告警智能助手问答")
async def assistant_chat(
    payload: AssistantQuery,
    db: Session = Depends(get_db),
    user=Depends(_get_monitor_user),
):
    """告警智能助手：基于异常上下文与实时感知状态回答自然语言问题。"""
    user_id = _scope_user_id(user)
    context: dict[str, Any] = {
        "event_type": payload.event_type or "unknown",
        "path": payload.path or "",
        "ip": payload.ip or "",
    }

    if payload.alert_id:
        alert = (
            db.query(AlertEvent)
            .filter(AlertEvent.id == payload.alert_id, AlertEvent.user_id == user_id)
            .first()
        )
        if not alert:
            raise HTTPException(404, "告警不存在")
        context.update({
            "event_type": alert.event_type,
            "event_type_user": event_type_to_user(alert.event_type),
            "title": alert.title,
            "summary": alert.summary,
            "root_cause": alert.root_cause,
            "suggestion": alert.suggestion,
            "level": alert.level,
            "alert_id": alert.id,
            "status": alert.status,
        })
        if alert.detail_json:
            try:
                context["detail"] = json.loads(alert.detail_json)
            except Exception:
                context["detail"] = {"raw": alert.detail_json}
        replay = alert_agent.get_event_replay(db, alert.id, user_id=user_id)
        if replay:
            context["cause_analysis"] = replay.get("cause_analysis")
            context["related_logs_count"] = len(replay.get("related_logs") or [])
    elif payload.event_type and payload.event_type not in ("unknown", "未知异常"):
        context["event_type_user"] = event_type_to_user(payload.event_type)

    # 注入实时感知快照与系统概况
    context["perception"] = alert_agent.get_perception_snapshot(user_id=user_id)
    stats = alert_agent.get_stats(db, user_id=user_id)
    context["system_status"] = {
        "open_alerts": stats.get("open", 0),
        "open_critical": stats.get("open_critical", 0),
        "token_usage": stats.get("token_usage", {}),
        "today_alerts": stats.get("today_count", 0),
    }

    intent = payload.intent or detect_assistant_intent(payload.question)
    if intent == "driving" or any(
        k in (payload.question or "")
        for k in ("综合驾驶", "融合建议", "三路感知", "怎么开", "前方交警")
    ):
        context["driving_advice"] = await scenario_fusion_service.get_driving_advice(user_id=user_id)
    has_alert_context = bool(
        payload.alert_id
        and context.get("title")
        and context.get("event_type") not in (None, "unknown", "")
    )

    if payload.alert_id and context.get("detail"):
        structured = (context.get("detail") or {}).get("structured") or {}
        context["severity_assessment"] = structured.get("severity_assessment")
        context["impact_scope"] = structured.get("impact_scope")
        context["occurred_at"] = structured.get("occurred_at")

    if needs_alert_context(payload.question, intent) and not has_alert_context:
        open_rows = (
            db.query(AlertEvent)
            .filter(AlertEvent.status == "open", AlertEvent.user_id == user_id)
            .order_by(AlertEvent.created_at.desc())
            .limit(10)
            .all()
        )
        open_alerts = [alert_agent._alert_to_dict(a) for a in open_rows]
        return {
            "answer": build_which_alert_prompt(open_alerts),
            "context": context,
            "needs_clarification": True,
            "intent": intent,
        }

    answer = await llm_service.ask_assistant(
        payload.question,
        context,
        intent=intent,
        history=[{"role": h.role, "content": h.content} for h in (payload.history or [])],
        user_id=user_id,
    )
    ai_mode = getattr(llm_service, "last_assistant_mode", "template")
    ai_reason = getattr(llm_service, "last_assistant_reason", "")
    ai_hint = ""
    if ai_mode == "template":
        if ai_reason == "not_configured":
            ai_hint = "未配置 LLM_API_KEY，当前为本地模板回答。"
        elif ai_reason == "api_error":
            ai_hint = "大模型调用失败，已降级为本地模板；请检查 API Key 与网络。"
    return {
        "answer": answer,
        "context": context,
        "needs_clarification": False,
        "intent": intent,
        "ai": {
            "mode": ai_mode,
            "reason": ai_reason,
            "hint": ai_hint,
            "configured": settings.llm_configured,
            "provider": settings.llm_provider_label,
            "model": settings.effective_llm_model,
        },
    }


# ════════════════════════════════════════════
# 配置查询
# ════════════════════════════════════════════

@router.get("/config", summary="当前告警配置")
def get_alert_config():
    """获取当前告警智能体的配置参数"""
    return {
        "failure_threshold": settings.alert_failure_threshold,
        "window_seconds": settings.alert_window_seconds,
        "cooldown_seconds": settings.alert_cooldown_seconds,
        "low_confidence_threshold": settings.low_confidence_threshold,
        "anomaly_rate_threshold": settings.alert_anomaly_rate_threshold,
        "token_limit": settings.alert_token_limit,
        "token_warning_threshold": settings.alert_token_warning_threshold,
        "token_critical_threshold": settings.alert_token_critical_threshold,
        "webhook_enabled": settings.alert_webhook_enabled,
        "email_enabled": settings.alert_email_enabled,
        "sse_enabled": settings.alert_sse_enabled,
        "webhook_url_configured": bool(settings.webhook_url),
        "email_configured": bool(settings.smtp_host and settings.smtp_user and settings.alert_email_to),
        "llm_configured": settings.llm_configured,
        "llm_provider": settings.llm_provider,
        "llm_provider_label": settings.llm_provider_label,
        "llm_model": settings.effective_llm_model,
        "llm_api_base": settings.effective_llm_base,
        "llm_timeout": settings.llm_timeout,
        "llm_providers": llm_service.get_provider_options(),
    }


@router.post("/llm/test", summary="测试 LLM API 连接")
async def test_llm_connection(user=Depends(_get_monitor_user)):
    """测试大语言模型 API 是否可用。配置 LLM_API_KEY 后调用此接口验证。"""
    return await llm_service.test_connection(user_id=_scope_user_id(user))


@router.post("/notifications/test", summary="测试通知渠道")
async def test_notifications(
    channel: str = Query("all", description="web / webhook / email / all"),
    user=Depends(_get_monitor_user),
):
    """向 Web / SSE / Webhook / 邮件发送测试消息，验证通知渠道配置。"""
    allowed = {"web", "webhook", "email", "all"}
    if channel not in allowed:
        raise HTTPException(400, f"不支持的渠道: {channel}，可选: {', '.join(sorted(allowed))}")
    return await alert_agent.send_test_notification(channel, user_id=_scope_user_id(user))
