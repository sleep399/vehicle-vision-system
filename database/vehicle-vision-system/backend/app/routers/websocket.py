from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt

from app.config import settings
from app.database import SessionLocal
from app.models.user import User
from app.routers.owner_gesture import (
    _apply_action_to_db,
    _get_or_create_state,
    _state_to_dict,
)
from app.services.alert_agent import alert_agent
from app.services.lpr_video_service import lpr_video_service
from app.services.network_stream_hub import (
    NetworkStreamError,
    network_stream_hub,
    normalize_stream_url,
)
from app.services.owner_gesture_service import owner_gesture_service
from app.services.police_gesture_service import police_gesture_service
from app.utils.logger import log_exception
from app.utils.recognition_monitor import (
    record_lpr_recognition,
    record_owner_recognition,
    record_police_recognition,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["WebSocket"])
_recognition_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="stream-recognition")
_owner_inference_lock = threading.Lock()
_stream_last_logged: dict[tuple[str, int | None, str], tuple[tuple, float]] = {}
_stream_last_error: dict[tuple[str, int | None, str], tuple[str, float]] = {}
_LOG_REFRESH_SECONDS = 5.0
_MAX_BACKGROUND_LOG_TASKS = 64
_background_log_tasks: set[asyncio.Task] = set()


def _normalize_source_id(value: object, fallback: str) -> str:
    source_id = str(value or "").strip()
    source_id = "".join(char for char in source_id if char.isprintable())
    if "://" in source_id:
        source_id = "source-" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
    return (source_id or fallback)[:128]


def _stream_state_signature(module: str, result: dict) -> tuple:
    if module == "lpr":
        plates = tuple(
            (p.get("plate_number"), p.get("plate_color"))
            for p in (result.get("plates") or [])[:3]
        )
        return (result.get("success"), result.get("plate_count"), plates)
    return (
        result.get("gesture"),
        round(float(result.get("confidence", 0) or 0), 1),
        result.get("action"),
        bool(result.get("needs_confirmation")),
    )


def _is_meaningful_stream_result(module: str, result: dict) -> bool:
    """Only stable, actionable detections need periodic refresh in the 30s window."""
    if module == "lpr":
        return bool(result.get("success") and int(result.get("plate_count", 0) or 0) > 0)
    gesture = result.get("gesture")
    confidence = float(result.get("confidence", 0.0) or 0.0)
    return bool(
        result.get("action")
        or (
            gesture
            and gesture != "no_gesture"
            and confidence >= settings.low_confidence_threshold
        )
    )


def _should_log_result(
    module: str,
    source_id: str,
    result: dict,
    user_id: int | None = None,
) -> bool:
    key = (module, user_id, source_id)
    signature = _stream_state_signature(module, result)
    now = time.monotonic()
    previous = _stream_last_logged.get(key)
    if previous and previous[0] == signature:
        if not _is_meaningful_stream_result(module, result):
            return False
        if now - previous[1] < _LOG_REFRESH_SECONDS:
            return False
    _stream_last_logged[key] = (signature, now)
    return True


def _should_log_error(
    module: str,
    source_id: str,
    message: str,
    user_id: int | None = None,
) -> bool:
    key = (module, user_id, source_id)
    now = time.monotonic()
    previous = _stream_last_error.get(key)
    if previous and previous[0] == message and now - previous[1] < _LOG_REFRESH_SECONDS:
        return False
    _stream_last_error[key] = (message, now)
    return True


def _forget_stream_log_state(
    module: str,
    source_id: str,
    user_id: int | None = None,
) -> None:
    _stream_last_logged.pop((module, user_id, source_id), None)
    _stream_last_error.pop((module, user_id, source_id), None)


async def _log_stream_result(
    module: str,
    source_id: str,
    result: dict,
    user_id: int | None = None,
    *,
    _skip_dedupe: bool = False,
) -> None:
    """Send every distinct/refresh result through the shared monitor pipeline."""
    if not _skip_dedupe and not _should_log_result(module, source_id, result, user_id):
        return
    db = SessionLocal()
    try:
        common_extra = {"stream": True}
        if module == "lpr":
            await record_lpr_recognition(
                db,
                success=bool(result.get("success") and result.get("plate_count", 0) > 0),
                source="WebSocket流",
                plate_count=result.get("plate_count", 0),
                plates=result.get("plates", []),
                model_available=result.get("model_available"),
                source_id=source_id,
                user_id=user_id,
                extra=common_extra,
            )
        elif module == "owner":
            await record_owner_recognition(
                db,
                source="WebSocket流",
                gesture_cn=result.get("gesture_cn"),
                confidence=result.get("confidence", 0),
                gesture=result.get("gesture"),
                action=result.get("action"),
                needs_confirmation=bool(result.get("needs_confirmation")),
                confirm_prompt=result.get("confirm_prompt"),
                vehicle_state=result.get("vehicle_state"),
                source_id=source_id,
                user_id=user_id,
                extra=common_extra,
            )
        else:
            await record_police_recognition(
                db,
                source="WebSocket流",
                gesture_cn=result.get("gesture_cn"),
                confidence=result.get("confidence", 0),
                gesture=result.get("gesture"),
                source_id=source_id,
                user_id=user_id,
                extra=common_extra,
            )
    except Exception as exc:
        log_exception(
            db,
            module,
            "[WebSocket流] 日志写入失败",
            exc,
            user_id=user_id,
        )
    finally:
        db.close()


def _schedule_stream_result_log(
    module: str,
    source_id: str,
    result: dict,
    user_id: int | None = None,
) -> None:
    """Keep DB/alert/LLM work off the frame-response hot path."""
    if len(_background_log_tasks) >= _MAX_BACKGROUND_LOG_TASKS:
        logger.warning("实时识别日志队列已满，丢弃一条重复状态: %s/%s", module, source_id)
        return
    if not _should_log_result(module, source_id, result, user_id):
        return
    task = asyncio.create_task(
        _log_stream_result(
            module,
            source_id,
            result,
            user_id,
            _skip_dedupe=True,
        )
    )
    _background_log_tasks.add(task)
    task.add_done_callback(_background_log_tasks.discard)


async def cancel_stream_background_tasks() -> None:
    tasks = list(_background_log_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background_log_tasks.clear()


async def _log_stream_error(
    module: str,
    source_id: str,
    message: str,
    user_id: int | None = None,
) -> None:
    if not _should_log_error(module, source_id, message, user_id):
        return
    db = SessionLocal()
    try:
        extra = {"stream": True}
        if module == "lpr":
            await record_lpr_recognition(
                db, success=False, source="WebSocket流", error=message,
                source_id=source_id, user_id=user_id, extra=extra,
            )
        elif module == "owner":
            await record_owner_recognition(
                db, source="WebSocket流", error=message,
                source_id=source_id, user_id=user_id, extra=extra,
            )
        else:
            await record_police_recognition(
                db, source="WebSocket流", error=message,
                source_id=source_id, user_id=user_id, extra=extra,
            )
    except Exception as exc:
        log_exception(
            db,
            module,
            "[WebSocket流] 错误日志写入失败",
            exc,
            user_id=user_id,
        )
    finally:
        db.close()


def _resolve_websocket_user_id(websocket: WebSocket) -> tuple[bool, int | None]:
    token = websocket.query_params.get("token")
    if not token:
        return True, None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        username = payload.get("sub")
    except JWTError:
        return False, None
    if not username:
        return False, None
    db = SessionLocal()
    try:
        try:
            user = db.query(User).filter(User.username == username).first()
            if not user or not user.is_active:
                return False, None
            return True, user.id
        except Exception as exc:
            logger.warning("WebSocket 用户解析失败，拒绝令牌流: %s", exc)
            return False, None
    finally:
        db.close()


def _owner_recognize_sync(
    frame: np.ndarray,
    vehicle_state: dict,
    user_id: int | None,
) -> dict:
    # OwnerGestureService keeps debounce/confirmation state; serialize its
    # realtime calls while still keeping expensive inference off the event loop.
    with _owner_inference_lock:
        return owner_gesture_service.recognize_frame(
            frame,
            vehicle_state=vehicle_state,
            respect_standby=True,
            realtime_mode=True,
            context_id=user_id,
        )


async def _recognize_owner_frame(
    loop: asyncio.AbstractEventLoop,
    frame: np.ndarray,
    user_id: int | None,
) -> dict:
    db = SessionLocal()
    try:
        vehicle_state = _state_to_dict(_get_or_create_state(db, user_id))
    finally:
        db.close()

    result = await loop.run_in_executor(
        _recognition_executor,
        _owner_recognize_sync,
        frame,
        vehicle_state,
        user_id,
    )
    result = dict(result)

    db = SessionLocal()
    try:
        if result.get("action"):
            result["vehicle_state"] = _apply_action_to_db(
                db,
                user_id,
                result["action"],
                source="实时网络手势",
            )
        elif not result.get("vehicle_state"):
            result["vehicle_state"] = _state_to_dict(_get_or_create_state(db, user_id))
    finally:
        db.close()
    return result


async def _recognize_stream_frame(
    module: str,
    frame: np.ndarray,
    frame_index: int,
    sequence_state: dict | None,
    user_id: int | None,
) -> dict:
    loop = asyncio.get_running_loop()
    if module == "lpr":
        return await loop.run_in_executor(
            _recognition_executor,
            lpr_video_service.recognize_frame,
            frame,
            frame_index,
        )
    if module == "police":
        return await loop.run_in_executor(
            _recognition_executor,
            police_gesture_service.recognize_frame_continuous,
            frame,
            sequence_state,
        )
    return await _recognize_owner_frame(loop, frame, user_id)


def _update_police_confirmation(
    result: dict,
    confirmed_gesture: str | None,
    confirmed_count: int,
    threshold: int = 3,
    min_confidence: float = 0.4,
) -> tuple[dict, str | None, int]:
    gesture = result.get("gesture")
    confidence = float(result.get("confidence", 0.0) or 0.0)
    if gesture == confirmed_gesture and confidence >= min_confidence:
        confirmed_count += 1
    else:
        confirmed_gesture = gesture
        confirmed_count = 1 if confidence >= min_confidence else 0
    payload = dict(result)
    payload["confirmed"] = confidence >= min_confidence and confirmed_count >= threshold
    payload["confirmed_count"] = confirmed_count
    return payload, confirmed_gesture, confirmed_count


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    await websocket.accept()
    token_valid, user_id = _resolve_websocket_user_id(websocket)
    if not token_valid:
        await websocket.send_json({"type": "error", "message": "令牌无效或已过期"})
        await websocket.close(code=1008)
        return

    alert_agent.register_ws(websocket, user_id=user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        alert_agent.unregister_ws(websocket)


@router.websocket("/ws/stream/{module}")
async def ws_stream(websocket: WebSocket, module: str):
    """Recognize JPEG frames uploaded by a browser: lpr | police | owner."""
    await websocket.accept()
    if module not in {"lpr", "police", "owner"}:
        await websocket.send_json({"type": "error", "message": "无效模块"})
        await websocket.close()
        return

    token_valid, user_id = _resolve_websocket_user_id(websocket)
    if not token_valid:
        await websocket.send_json({"type": "error", "message": "令牌无效或已过期"})
        await websocket.close(code=1008)
        return

    owner_session_id: str | None = None
    if module == "owner":
        owner_session_id = f"owner-browser-{uuid.uuid4().hex}"
        if not owner_gesture_service.acquire_realtime_session(
            owner_session_id,
            context_id=user_id,
        ):
            await websocket.send_json(
                {"type": "error", "message": "车主实时识别已在其他通道运行"}
            )
            await websocket.close(code=1008)
            return

    frame_index = 0
    source_ids: set[str] = set()
    bound_source_id: str | None = None
    sequence_state = None
    if module == "police":
        try:
            sequence_state = police_gesture_service.create_sequence_state()
        except Exception as exc:
            logger.warning("初始化交警连续识别状态失败，将使用无状态模式: %s", exc)
    police_confirmed_gesture = None
    police_confirmed_count = 0

    try:
        while True:
            try:
                msg = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid json"})
                continue

            msg_type = msg.get("type")
            source_id = _normalize_source_id(msg.get("source_id"), "browser")
            if msg_type == "frame":
                if bound_source_id is None:
                    bound_source_id = source_id
                    source_ids.add(source_id)
                elif source_id != bound_source_id:
                    await websocket.send_json(
                        {"type": "error", "message": "source_id cannot change within one connection"}
                    )
                    continue
                source_id = bound_source_id
                try:
                    encoded_frame = msg["data"]
                    if not isinstance(encoded_frame, str) or len(encoded_frame) > 8 * 1024 * 1024:
                        raise ValueError("视频帧过大")
                    img_bytes = base64.b64decode(encoded_frame, validate=True)
                    if len(img_bytes) > 6 * 1024 * 1024:
                        raise ValueError("视频帧过大")
                    frame = _decode_jpeg_frame(img_bytes)
                except (KeyError, ValueError, binascii.Error) as exc:
                    logger.warning("视频帧解析失败: %s", exc)
                    await _log_stream_error(module, source_id, "视频帧解析失败", user_id)
                    await websocket.send_json(
                        {"type": "error", "message": "视频帧解析失败", "source_id": source_id}
                    )
                    continue

                try:
                    result = await _recognize_stream_frame(
                        module, frame, frame_index, sequence_state, user_id,
                    )
                    if module == "police":
                        result, police_confirmed_gesture, police_confirmed_count = (
                            _update_police_confirmation(
                                result,
                                police_confirmed_gesture,
                                police_confirmed_count,
                            )
                        )
                except Exception as exc:
                    logger.exception("browser stream recognition failed: %s", exc)
                    await _log_stream_error(module, source_id, str(exc), user_id)
                    await websocket.send_json(
                        {"type": "frame_error", "message": str(exc), "source_id": source_id}
                    )
                    continue

                frame_index += 1
                await websocket.send_json(
                    {
                        "type": "result",
                        "module": module,
                        "source_id": source_id,
                        "time_sec": msg.get("time_sec"),
                        "data": result,
                    }
                )
                _schedule_stream_result_log(module, source_id, result, user_id)
            elif msg_type == "end":
                source_id = bound_source_id or source_id
                await websocket.send_json(
                    {
                        "type": "done",
                        "module": module,
                        "source_id": source_id,
                        "frames": frame_index,
                    }
                )
                break
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "source_id": source_id})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        for source_id in source_ids:
            _forget_stream_log_state(module, source_id, user_id)
        if owner_session_id is not None:
            owner_gesture_service.release_realtime_session(owner_session_id)


def _decode_jpeg_frame(img_bytes: bytes) -> np.ndarray:
    nparr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Unable to decode video frame")
    return frame


@router.websocket("/ws/stream-url/{module}")
async def ws_stream_url(websocket: WebSocket, module: str):
    """Subscribe any recognition module to a shared RTSP/HTTP(S)/RTMP stream."""
    await websocket.accept()
    if module not in {"lpr", "police", "owner"}:
        await websocket.send_json({"type": "error", "message": "无效模块"})
        await websocket.close()
        return

    token_valid, user_id = _resolve_websocket_user_id(websocket)
    if not token_valid:
        await websocket.send_json({"type": "error", "message": "令牌无效或已过期"})
        await websocket.close(code=1008)
        return

    owner_session_id: str | None = None
    if module == "owner":
        owner_session_id = f"owner-network-{uuid.uuid4().hex}"
        if not owner_gesture_service.acquire_realtime_session(
            owner_session_id,
            context_id=user_id,
        ):
            await websocket.send_json(
                {"type": "error", "message": "车主实时识别已在其他通道运行"}
            )
            await websocket.close(code=1008)
            return

    subscription = None
    disconnect_task: asyncio.Task | None = None
    source_id = "network"
    loop = asyncio.get_running_loop()
    try:
        try:
            start_msg = json.loads(await websocket.receive_text())
        except json.JSONDecodeError as exc:
            raise ValueError("invalid start message") from exc

        normalized_url = normalize_stream_url(start_msg.get("url", ""))
        default_source_id = "network-" + hashlib.sha256(
            normalized_url.encode("utf-8")
        ).hexdigest()[:12]
        source_id = _normalize_source_id(start_msg.get("source_id"), default_source_id)
        interval = max(1, min(int(start_msg.get("interval", 1)), 120))
        target_fps = max(1.0, min(float(start_msg.get("target_fps", 15)), 15.0))
        min_frame_gap = 1.0 / target_fps

        subscription = network_stream_hub.subscribe(normalized_url)
        sequence_state = (
            police_gesture_service.create_sequence_state() if module == "police" else None
        )

        frame_index = 0
        last_processed_at = 0.0
        police_confirmed_gesture = None
        police_confirmed_count = 0
        stream_ready = False
        disconnect_task = asyncio.create_task(websocket.receive_text())
        while True:
            frame_future = loop.run_in_executor(
                None,
                partial(subscription.next_frame, timeout=1.0),
            )
            done, _pending = await asyncio.wait(
                {frame_future, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                if not frame_future.done():
                    frame_future.cancel()
                try:
                    control = json.loads(disconnect_task.result())
                except WebSocketDisconnect:
                    break
                except (json.JSONDecodeError, TypeError):
                    control = {}
                if control.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "source_id": source_id})
                elif control.get("type") in {"stop", "end"}:
                    break
                disconnect_task = asyncio.create_task(websocket.receive_text())
                continue

            item = frame_future.result()
            if item is None:
                continue

            if not stream_ready:
                await websocket.send_json(
                    {
                        "type": "status",
                        "message": "stream ready",
                        "module": module,
                        "source_id": source_id,
                        "target_fps": target_fps,
                    }
                )
                stream_ready = True

            now = time.monotonic()
            should_process = frame_index % interval == 0 and now - last_processed_at >= min_frame_gap
            if should_process:
                last_processed_at = now
                try:
                    result = await _recognize_stream_frame(
                        module,
                        item.frame,
                        frame_index,
                        sequence_state,
                        user_id,
                    )
                    if module == "police":
                        result, police_confirmed_gesture, police_confirmed_count = (
                            _update_police_confirmation(
                                result,
                                police_confirmed_gesture,
                                police_confirmed_count,
                            )
                        )
                    await websocket.send_json(
                        {
                            "type": "result",
                            "module": module,
                            "source_id": source_id,
                            "frame": frame_index,
                            "capture_sequence": item.sequence,
                            "captured_at": item.captured_at,
                            "target_fps": target_fps,
                            "data": result,
                        }
                    )
                    _schedule_stream_result_log(module, source_id, result, user_id)
                except Exception as exc:
                    logger.exception("URL stream recognition failed: %s", exc)
                    await _log_stream_error(module, source_id, str(exc), user_id)
                    await websocket.send_json(
                        {"type": "frame_error", "message": str(exc), "source_id": source_id}
                    )
            frame_index += 1
    except WebSocketDisconnect:
        pass
    except (NetworkStreamError, ValueError) as exc:
        await _log_stream_error(module, source_id, str(exc), user_id)
        try:
            await websocket.send_json(
                {"type": "error", "message": str(exc), "source_id": source_id}
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("network stream websocket failed: %s", exc)
        await _log_stream_error(module, source_id, str(exc), user_id)
        try:
            await websocket.send_json(
                {"type": "error", "message": str(exc), "source_id": source_id}
            )
        except Exception:
            pass
    finally:
        if disconnect_task is not None:
            disconnect_task.cancel()
        if subscription is not None:
            await loop.run_in_executor(None, subscription.close)
        _forget_stream_log_state(module, source_id, user_id)
        if owner_session_id is not None:
            owner_gesture_service.release_realtime_session(owner_session_id)
