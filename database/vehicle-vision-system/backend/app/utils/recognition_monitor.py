"""将车牌/交警/车主手势识别与控车状态统一写入系统日志并触发告警智能体感知。

与前端展示的状态对齐：
  - 识别成功 / 未识别 / 识别失败 / 待确认 / 二次确认 / 车辆状态变更
  - 模型未加载
  - 视频/RTSP/WebSocket 流识别结果
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.alert_agent import alert_agent
from app.utils.logger import write_log
from app.utils.log_display import humanize_error_text

async def record_lpr_recognition(
    db: Session,
    *,
    success: bool,
    source: str,
    plate_count: int = 0,
    plates: list | None = None,
    model_available: bool | None = None,
    error: str | None = None,
    user_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """记录车牌识别结果到日志与告警感知。"""
    detail: dict[str, Any] = {
        "source": source,
        "plate_count": plate_count,
        "success": success,
    }
    if plates is not None:
        detail["plates"] = plates
    if model_available is not None:
        detail["model_available"] = model_available
    if extra:
        detail.update(extra)

    if error:
        level = "ERROR"
        message = f"[{source}] 识别失败: {humanize_error_text(error)}"
        success = False
    elif model_available is False:
        level = "WARN"
        message = f"[{source}] 模型未加载，无法识别"
        success = False
    elif success and plate_count > 0:
        level = "INFO"
        message = f"[{source}] 识别成功，检测到 {plate_count} 个车牌"
    else:
        level = "WARN"
        message = f"[{source}] 未识别到有效车牌"

    write_log(db, "lpr", message, level=level, detail=detail, user_id=user_id)
    alert_agent.record_lpr_result(success)
    await alert_agent.check_and_alert(db, "lpr")


async def record_police_recognition(
    db: Session,
    *,
    source: str,
    gesture_cn: str | None = None,
    confidence: float = 0.0,
    gesture: str | None = None,
    error: str | None = None,
    user_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """记录交警手势识别结果到日志与告警感知。"""
    detail: dict[str, Any] = {
        "source": source,
        "confidence": confidence,
    }
    if gesture:
        detail["gesture"] = gesture
    if gesture_cn:
        detail["gesture_cn"] = gesture_cn
    if extra:
        detail.update(extra)

    if error:
        level = "ERROR"
        message = f"[{source}] 识别失败: {humanize_error_text(error)}"
        alert_agent.record_gesture_failure("police")
    else:
        label = gesture_cn or "无手势"
        level = "WARN" if confidence < 0.4 else "INFO"
        message = f"[{source}] 识别手势: {label} ({confidence:.0%})"
        alert_agent.record_gesture_confidence("police", confidence)

    write_log(db, "police_gesture", message, level=level, detail=detail, user_id=user_id)
    await alert_agent.check_and_alert(db, "police")


async def record_owner_recognition(
    db: Session,
    *,
    source: str,
    gesture_cn: str | None = None,
    confidence: float = 0.0,
    gesture: str | None = None,
    action: str | None = None,
    error: str | None = None,
    needs_confirmation: bool = False,
    confirm_prompt: str | None = None,
    vehicle_state: dict | None = None,
    user_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """记录车主手势识别/控车结果到日志与告警感知。"""
    detail: dict[str, Any] = {
        "source": source,
        "confidence": confidence,
    }
    if gesture:
        detail["gesture"] = gesture
    if gesture_cn:
        detail["gesture_cn"] = gesture_cn
    if action:
        detail["action"] = action
    if vehicle_state:
        detail["vehicle_state"] = vehicle_state
    if confirm_prompt:
        detail["confirm_prompt"] = confirm_prompt
    if extra:
        detail.update(extra)

    if error:
        level = "ERROR"
        message = f"[{source}] 识别失败: {humanize_error_text(error)}"
        alert_agent.record_gesture_failure("owner")
    elif needs_confirmation:
        level = "WARN"
        message = f"[{source}] 待确认低置信度手势: {gesture_cn or '未知'} ({confidence:.0%})"
        alert_agent.record_gesture_confidence("owner", confidence)
    elif action:
        level = "INFO"
        message = f"[{source}] 手势触发: {gesture_cn or gesture or '未知'} -> {action}"
        alert_agent.record_gesture_confidence("owner", confidence)
    elif gesture in (None, "no_gesture") or confidence < 0.35:
        level = "WARN"
        message = f"[{source}] 未识别到有效手势"
        alert_agent.record_gesture_confidence("owner", confidence)
    else:
        level = "INFO"
        message = f"[{source}] 识别手势: {gesture_cn or '未知'} ({confidence:.0%})"
        alert_agent.record_gesture_confidence("owner", confidence)

    write_log(db, "owner_gesture", message, level=level, detail=detail, user_id=user_id)
    await alert_agent.check_and_alert(db, "owner")


def record_owner_vehicle_state(
    db: Session,
    *,
    source: str,
    vehicle_state: dict,
    action: str | None = None,
    user_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """记录车辆模拟状态变更（唤醒/休眠、音量、温度、电话等）。"""
    awake = "已唤醒" if vehicle_state.get("is_awake") else "休眠"
    page = vehicle_state.get("current_page", "")
    phone = "通话中" if vehicle_state.get("phone_status") == "in_call" else "空闲"
    detail: dict[str, Any] = {"source": source, "vehicle_state": vehicle_state}
    if action:
        detail["action"] = action
    if extra:
        detail.update(extra)

    state_summary = (
        f"{awake} · 页面={page} · 音量={vehicle_state.get('volume')} · "
        f"温度={vehicle_state.get('temperature')}°C · 电话={phone}"
    )
    if action:
        message = f"[{source}] 控车动作 {action} -> {state_summary}"
    else:
        message = f"[{source}] 车辆状态更新 -> {state_summary}"

    write_log(db, "owner_gesture", message, level="INFO", detail=detail, user_id=user_id)


def record_owner_confirm(
    db: Session,
    *,
    source: str,
    accepted: bool,
    pending: dict | None = None,
    vehicle_state: dict | None = None,
    user_id: int | None = None,
) -> None:
    """记录低置信度手势二次确认（确认/取消/无待确认）。"""
    if not pending:
        write_log(
            db, "owner_gesture",
            f"[{source}] 二次确认: 无待确认动作",
            level="INFO",
            user_id=user_id,
        )
        return

    if accepted:
        write_log(
            db, "owner_gesture",
            f"[{source}] 二次确认执行: {pending.get('gesture_cn')} -> {pending.get('action')}",
            level="INFO",
            detail={
                "gesture": pending.get("gesture"),
                "gesture_cn": pending.get("gesture_cn"),
                "action": pending.get("action"),
                "vehicle_state": vehicle_state,
            },
            user_id=user_id,
        )
        if vehicle_state:
            record_owner_vehicle_state(
                db,
                source=f"{source}/确认后",
                vehicle_state=vehicle_state,
                action=pending.get("action"),
                user_id=user_id,
            )
    else:
        write_log(
            db, "owner_gesture",
            f"[{source}] 二次确认已取消: {pending.get('gesture_cn')}",
            level="WARN",
            detail={
                "gesture": pending.get("gesture"),
                "gesture_cn": pending.get("gesture_cn"),
                "action": pending.get("action"),
            },
            user_id=user_id,
        )
