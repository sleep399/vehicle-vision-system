import base64
import json
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.records import OwnerGestureRecord, VehicleState
from app.schemas import GestureResponse, VehicleStateResponse
from app.services.owner_gesture_service import OWNER_GESTURES, owner_gesture_service
from app.utils.auth import get_current_user
from app.utils.recognition_monitor import (
    record_owner_confirm,
    record_owner_recognition,
    record_owner_vehicle_state,
)

router = APIRouter(prefix="/api/owner-gesture", tags=["车主手势控车"])

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
_owner_stream_last_signature: tuple | None = None


def _state_to_dict(state: VehicleState) -> dict:
    return {
        "volume": state.volume,
        "temperature": state.temperature,
        "phone_status": state.phone_status,
        "current_page": state.current_page,
        "is_awake": state.is_awake,
    }


def _apply_state_dict_to_model(state: VehicleState, data: dict) -> None:
    state.volume = int(data.get("volume", state.volume))
    state.temperature = int(data.get("temperature", state.temperature))
    state.phone_status = data.get("phone_status", state.phone_status)
    state.current_page = data.get("current_page", state.current_page)
    state.is_awake = int(data.get("is_awake", state.is_awake))
    state.updated_at = datetime.utcnow()


def _get_or_create_state(db: Session, user_id: int | None) -> VehicleState:
    state = db.query(VehicleState).filter(VehicleState.user_id == user_id).first()
    if not state:
        state = VehicleState(user_id=user_id, current_page="standby", is_awake=0, phone_status="idle")
        db.add(state)
        db.commit()
        db.refresh(state)
    elif state.current_page == "home":
        state.current_page = "standby"
        state.is_awake = 0
        state.phone_status = "idle"
        state.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(state)
    elif not state.is_awake and state.current_page != "standby":
        state.current_page = "standby"
        state.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(state)
    return state


def _apply_action_to_db(
    db: Session,
    user_id: int | None,
    action: str,
    source: str = "手势控车",
) -> dict:
    if not action:
        state = _get_or_create_state(db, user_id)
        return _state_to_dict(state)
    state = _get_or_create_state(db, user_id)
    updated = owner_gesture_service.apply_action_to_state(action, _state_to_dict(state))
    _apply_state_dict_to_model(state, updated)
    db.commit()
    db.refresh(state)
    result = _state_to_dict(state)
    record_owner_vehicle_state(
        db, source=source, vehicle_state=result, action=action, user_id=user_id,
    )
    return result


def _persist_final_state(db: Session, user_id: int | None, final_state: dict | None) -> dict | None:
    if not final_state:
        return None
    state = _get_or_create_state(db, user_id)
    _apply_state_dict_to_model(state, final_state)
    db.commit()
    db.refresh(state)
    return _state_to_dict(state)


def _is_video_upload(file: UploadFile) -> bool:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    return content_type.startswith("video/") or any(filename.endswith(ext) for ext in VIDEO_EXTENSIONS)


def _upload_suffix(file: UploadFile, is_video: bool) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix:
        return suffix
    return ".mp4" if is_video else ".jpg"


def _save_upload(content: bytes, category: str, suffix: str) -> Path:
    save_path = settings.upload_dir / category / f"{uuid.uuid4().hex}{suffix}"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)
    return save_path


def _create_record(
    db: Session,
    user_id: int | None,
    source_type: str,
    save_path: Path,
    result: dict,
) -> OwnerGestureRecord:
    record = OwnerGestureRecord(
        user_id=user_id,
        source_type=source_type,
        image_path=str(save_path),
        gesture=result["gesture"],
        gesture_cn=result["gesture_cn"],
        confidence=result["confidence"],
        action=result.get("action"),
        keypoints_json=json.dumps(result.get("keypoints", []), ensure_ascii=False),
        annotated_image=result.get("annotated_image"),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _empty_video_result() -> dict:
    return {
        "gesture": "no_gesture",
        "gesture_cn": "无手势",
        "confidence": 0.0,
        "action": None,
        "needs_confirmation": False,
        "confirmation_resolved": False,
        "confirmation_accepted": False,
        "confirm_prompt": None,
        "debug_info": {"stage": "video", "message": "未识别到有效手势"},
        "keypoints": [],
        "annotated_image": "",
        "success": False,
        "vehicle_state": None,
    }


@router.post("/recognize", response_model=GestureResponse, summary="识别车主手势并触发控车")
async def recognize(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    content = await file.read()
    user_id = user.id if user else None
    state = _get_or_create_state(db, user_id)
    state_context = _state_to_dict(state)
    is_video = _is_video_upload(file)
    save_path = _save_upload(content, "owner", _upload_suffix(file, is_video))

    try:
        if is_video:
            video_payload = owner_gesture_service.process_video(
                save_path,
                sample_interval=1,
                vehicle_state=state_context,
                respect_standby=True,
            )
            result = video_payload.get("best_result") or video_payload.get("preview_result") or _empty_video_result()
            final_state = _persist_final_state(db, user_id, video_payload.get("final_vehicle_state"))
            if final_state:
                result["vehicle_state"] = final_state
        else:
            result = owner_gesture_service.recognize(
                content,
                apply_debounce=False,
                vehicle_state=state_context,
                respect_standby=True,
            )
            if result.get("action"):
                result["vehicle_state"] = _apply_action_to_db(db, user_id, result["action"])
            elif not result.get("vehicle_state"):
                latest_state = _get_or_create_state(db, user_id)
                result["vehicle_state"] = _state_to_dict(latest_state)
    except Exception as exc:
        await record_owner_recognition(
            db, source="视频上传" if is_video else "图片上传",
            error=str(exc), user_id=user_id,
        )
        raise HTTPException(500, str(exc))

    await record_owner_recognition(
        db, source="视频上传" if is_video else "图片上传",
        gesture_cn=result.get("gesture_cn"), confidence=result.get("confidence", 0),
        gesture=result.get("gesture"), action=result.get("action"),
        needs_confirmation=bool(result.get("needs_confirmation")),
        confirm_prompt=result.get("confirm_prompt"), vehicle_state=result.get("vehicle_state"),
        user_id=user_id,
    )

    record = _create_record(db, user_id, "video" if is_video else "image", save_path, result)
    return GestureResponse(**result, record_id=record.id)


@router.post("/recognize-video", summary="上传视频识别车主手势")
async def recognize_video(
    file: UploadFile = File(...),
    interval: int = Query(1, ge=1, le=30),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    content = await file.read()
    user_id = user.id if user else None
    state = _get_or_create_state(db, user_id)
    save_path = _save_upload(content, "owner", _upload_suffix(file, True))

    try:
        payload = owner_gesture_service.process_video(
            save_path,
            sample_interval=interval,
            vehicle_state=_state_to_dict(state),
            respect_standby=True,
        )
    except Exception as exc:
        await record_owner_recognition(db, source="视频上传", error=str(exc), user_id=user_id)
        raise HTTPException(500, str(exc))

    best_result = payload.get("best_result") or payload.get("preview_result") or _empty_video_result()
    final_state = _persist_final_state(db, user_id, payload.get("final_vehicle_state"))
    if final_state:
        best_result["vehicle_state"] = final_state
    elif not best_result.get("vehicle_state"):
        latest_state = _get_or_create_state(db, user_id)
        best_result["vehicle_state"] = _state_to_dict(latest_state)

    await record_owner_recognition(
        db, source="视频上传",
        gesture_cn=best_result.get("gesture_cn"), confidence=best_result.get("confidence", 0),
        gesture=best_result.get("gesture"), action=best_result.get("action"),
        needs_confirmation=bool(best_result.get("needs_confirmation")),
        confirm_prompt=best_result.get("confirm_prompt"), vehicle_state=best_result.get("vehicle_state"),
        user_id=user_id,
        extra={"sampled_frames": payload["sampled_frames"], "recognized_frames": payload["recognized_frames"]},
    )

    record = _create_record(db, user_id, "video", save_path, best_result)
    return {
        **payload,
        "best_result": best_result,
        "record_id": record.id,
        "vehicle_state": final_state,
    }


@router.post("/confirm", summary="确认/取消低置信度手势动作")
def confirm_gesture(
    accept: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    pending = owner_gesture_service.confirm_pending(accept)
    if not pending:
        record_owner_confirm(
            db, source="二次确认", accepted=accept, pending=None,
            user_id=user.id if user else None,
        )
        return {"confirmed": False, "action": None, "message": "没有待确认的动作或已取消"}

    user_id = user.id if user else None
    action = pending["action"]
    vehicle_state = _apply_action_to_db(db, user_id, action, source="二次确认")
    record_owner_confirm(
        db, source="二次确认", accepted=True, pending=pending,
        vehicle_state=vehicle_state, user_id=user_id,
    )
    return {
        "confirmed": True,
        "action": action,
        "gesture": pending["gesture"],
        "gesture_cn": pending["gesture_cn"],
        "vehicle_state": vehicle_state,
        "message": f"已确认执行 {pending['gesture_cn']}",
    }


@router.get("/vehicle-state", response_model=VehicleStateResponse, summary="获取模拟车辆状态")
def get_vehicle_state(db: Session = Depends(get_db), user=Depends(get_current_user)):
    state = _get_or_create_state(db, user.id if user else None)
    return VehicleStateResponse(**_state_to_dict(state))


@router.put("/vehicle-state", response_model=VehicleStateResponse, summary="手动更新车辆状态")
def update_vehicle_state(
    data: VehicleStateResponse,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    state = _get_or_create_state(db, user.id if user else None)
    _apply_state_dict_to_model(state, data.model_dump())
    db.commit()
    db.refresh(state)
    result = _state_to_dict(state)
    record_owner_vehicle_state(
        db, source="手动更新", vehicle_state=result,
        user_id=user.id if user else None,
    )
    return VehicleStateResponse(**result)


@router.get("/gestures", summary="支持的手势列表")
def gesture_list():
    seen = set()
    items = []
    for key, (en, cn, action) in OWNER_GESTURES.items():
        if key == "no_gesture" or key in seen:
            continue
        seen.add(key)
        items.append({"key": key, "en": en, "cn": cn, "action": action})
    return items


@router.get("/history", summary="历史记录")
def history(skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    records = (
        db.query(OwnerGestureRecord)
        .order_by(OwnerGestureRecord.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "source_type": r.source_type,
            "gesture": r.gesture,
            "gesture_cn": r.gesture_cn,
            "confidence": r.confidence,
            "action": r.action,
            "annotated_image": r.annotated_image,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]


@router.websocket("/ws-stream")
async def gesture_websocket(websocket: WebSocket, db: Session = Depends(get_db)):
    global _owner_stream_last_signature
    from app.config import settings as _settings
    from app.models.user import User
    from jose import JWTError, jwt

    token = websocket.query_params.get("token")
    user_id: int | None = None
    if token:
        try:
            payload = jwt.decode(token, _settings.secret_key, algorithms=["HS256"])
            username = payload.get("sub")
            if username:
                user_obj = db.query(User).filter(User.username == username).first()
                if user_obj:
                    user_id = user_obj.id
        except JWTError:
            user_id = None

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid json"})
                continue

            msg_type = msg.get("type")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if msg_type not in ("frame", "confirm"):
                continue

            if msg_type == "confirm":
                accept = bool(msg.get("accept", True))
                pending = owner_gesture_service.confirm_pending(accept)
                if pending:
                    vehicle_state = _apply_action_to_db(db, user_id, pending["action"], source="实时二次确认")
                    pending["vehicle_state"] = vehicle_state
                    record_owner_confirm(
                        db, source="WebSocket流", accepted=True, pending=pending,
                        vehicle_state=vehicle_state, user_id=user_id,
                    )
                    await websocket.send_json({"type": "confirmed", "data": pending})
                else:
                    record_owner_confirm(
                        db, source="WebSocket流", accepted=accept, pending=None, user_id=user_id,
                    )
                    await websocket.send_json({"type": "confirmed", "data": None})
                continue

            data_b64 = msg.get("data", "")
            try:
                img_bytes = base64.b64decode(data_b64)
            except Exception:
                await record_owner_recognition(db, source="WebSocket流", error="invalid base64", user_id=user_id)
                await websocket.send_json({"type": "error", "message": "invalid base64"})
                continue

            arr = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                await record_owner_recognition(db, source="WebSocket流", error="decode failed", user_id=user_id)
                await websocket.send_json({"type": "error", "message": "decode failed"})
                continue

            state = _get_or_create_state(db, user_id)
            result = owner_gesture_service.recognize_frame(
                frame,
                vehicle_state=_state_to_dict(state),
                respect_standby=True,
                realtime_mode=True,
            )

            if result.get("action"):
                result["vehicle_state"] = _apply_action_to_db(
                    db, user_id, result["action"], source="实时手势",
                )

            if not result.get("vehicle_state"):
                latest_state = _get_or_create_state(db, user_id)
                result["vehicle_state"] = _state_to_dict(latest_state)

            signature = (
                result.get("gesture"), round(float(result.get("confidence", 0)), 1),
                result.get("action"), bool(result.get("needs_confirmation")),
            )
            if signature != _owner_stream_last_signature:
                _owner_stream_last_signature = signature
                await record_owner_recognition(
                    db, source="WebSocket流", gesture_cn=result.get("gesture_cn"),
                    confidence=result.get("confidence", 0), gesture=result.get("gesture"),
                    action=result.get("action"), needs_confirmation=bool(result.get("needs_confirmation")),
                    confirm_prompt=result.get("confirm_prompt"), vehicle_state=result.get("vehicle_state"),
                    user_id=user_id, extra={"stream": True},
                )

            await websocket.send_json({"type": "result", "data": result})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await record_owner_recognition(db, source="WebSocket流", error=str(exc), user_id=user_id)
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
