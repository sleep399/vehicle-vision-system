import asyncio
import base64
import binascii
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import SessionLocal
from app.services.lpr_service import lpr_service
from app.services.lpr_video_service import lpr_video_service
from app.services.police_gesture_service import police_gesture_service
from app.services.owner_gesture_service import owner_gesture_service
from app.services.alert_agent import alert_agent
from app.utils.logger import log_exception
from app.utils.recognition_monitor import (
    record_lpr_recognition,
    record_owner_recognition,
    record_police_recognition,
)
from app.utils.video import validate_stream_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["WebSocket"])
_lpr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lpr-video")
_stream_last_logged: dict[str, tuple] = {}


def _stream_state_signature(module: str, result: dict) -> tuple:
    if module == "lpr":
        plates = tuple(
            (p.get("plate_number"), p.get("plate_color"))
            for p in (result.get("plates") or [])[:3]
        )
        return (result.get("success"), result.get("plate_count"), plates)
    return (
        result.get("gesture"), round(float(result.get("confidence", 0)), 1),
        result.get("action"), bool(result.get("needs_confirmation")),
    )


async def _log_stream_result(module: str, result: dict) -> None:
    signature = _stream_state_signature(module, result)
    if _stream_last_logged.get(module) == signature:
        return
    _stream_last_logged[module] = signature
    db = SessionLocal()
    try:
        if module == "lpr":
            await record_lpr_recognition(
                db, success=bool(result.get("success") and result.get("plate_count", 0) > 0),
                source="WebSocket流", plate_count=result.get("plate_count", 0),
                plates=result.get("plates", []), model_available=result.get("model_available"),
            )
        elif module == "owner":
            await record_owner_recognition(
                db, source="WebSocket流", gesture_cn=result.get("gesture_cn"),
                confidence=result.get("confidence", 0), gesture=result.get("gesture"),
                action=result.get("action"), needs_confirmation=bool(result.get("needs_confirmation")),
                confirm_prompt=result.get("confirm_prompt"), vehicle_state=result.get("vehicle_state"),
                extra={"stream": True},
            )
        else:
            await record_police_recognition(
                db, source="WebSocket流", gesture_cn=result.get("gesture_cn"),
                confidence=result.get("confidence", 0), gesture=result.get("gesture"),
                extra={"stream": True},
            )
    except Exception as exc:
        log_exception(db, module, "[WebSocket流] 日志写入失败", exc)
    finally:
        db.close()


async def _log_stream_error(module: str, message: str) -> None:
    db = SessionLocal()
    try:
        if module == "lpr":
            await record_lpr_recognition(db, success=False, source="WebSocket流", error=message)
        elif module == "owner":
            await record_owner_recognition(db, source="WebSocket流", error=message)
        else:
            await record_police_recognition(db, source="WebSocket流", error=message)
    except Exception as exc:
        log_exception(db, module, "[WebSocket流] 错误日志写入失败", exc)
    finally:
        db.close()


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    await websocket.accept()
    alert_agent.register_ws(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        alert_agent.unregister_ws(websocket)


@router.websocket("/ws/stream/{module}")
async def ws_stream(websocket: WebSocket, module: str):
    """实时视频流识别: module = lpr | police | owner"""
    await websocket.accept()
    services = {"lpr": lpr_service, "police": police_gesture_service, "owner": owner_gesture_service}
    if module not in services:
        await websocket.send_json({"error": "无效模块"})
        await websocket.close()
        return

    service = services[module]
    frame_index = 0
    loop = asyncio.get_running_loop()
    sequence_state = service.create_sequence_state() if hasattr(service, "create_sequence_state") else None
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "frame":
                try:
                    img_bytes = base64.b64decode(msg["data"])
                except (KeyError, ValueError, binascii.Error) as exc:
                    logger.warning("视频帧 base64 解析失败: %s", exc)
                    await _log_stream_error(module, "视频帧解析失败")
                    await websocket.send_json({"type": "error", "message": "视频帧解析失败"})
                    continue

                if module == "lpr":
                    try:
                        result = await loop.run_in_executor(
                            _lpr_executor,
                            lpr_video_service.recognize_bytes,
                            img_bytes,
                            frame_index,
                        )
                    except Exception as exc:
                        logger.exception("LPR 视频帧处理失败: %s", exc)
                        await _log_stream_error(module, str(exc))
                        await websocket.send_json({"type": "error", "message": str(exc)})
                        continue
                elif hasattr(service, "recognize_frame_continuous"):
                    try:
                        frame_array = _decode_jpeg_frame(img_bytes)
                        result = await loop.run_in_executor(
                            None,
                            service.recognize_frame_continuous,
                            frame_array,
                            sequence_state,
                        )
                    except Exception as exc:
                        logger.exception("gesture video frame failed: %s", exc)
                        await _log_stream_error(module, str(exc))
                        await websocket.send_json({"type": "frame_error", "message": str(exc)})
                        continue
                else:
                    result = service.recognize(img_bytes)

                logger.info(
                    "ws frame module=%s frame=%s plates=%s success=%s model=%s",
                    module,
                    frame_index,
                    result.get("plate_count"),
                    result.get("success"),
                    result.get("model_available"),
                )
                await _log_stream_result(module, result)
                frame_index += 1
                await websocket.send_json(
                    {
                        "type": "result",
                        "module": module,
                        "time_sec": msg.get("time_sec"),
                        "data": result,
                    }
                )
            elif msg.get("type") == "end":
                await websocket.send_json({"type": "done", "module": module, "frames": frame_index})
                break
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


def _decode_jpeg_frame(img_bytes: bytes) -> np.ndarray:
    nparr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Unable to decode video frame")
    return frame


@router.websocket("/ws/stream-url/{module}")
async def ws_stream_url(websocket: WebSocket, module: str):
    await websocket.accept()
    if module != "police":
        await websocket.send_json({"type": "error", "message": "only police module is enabled for URL streams"})
        await websocket.close()
        return

    cap = None
    loop = asyncio.get_running_loop()
    try:
        start_msg = json.loads(await websocket.receive_text())
        url = validate_stream_url(start_msg.get("url", ""))
        interval = max(1, min(int(start_msg.get("interval", 1)), 120))
        target_fps = max(1.0, min(float(start_msg.get("target_fps", 15)), 15.0))
        min_frame_gap = 1.0 / target_fps
        sequence_state = police_gesture_service.create_sequence_state()

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            await _log_stream_error(module, f"unable to open stream: {url}")
            await websocket.send_json({"type": "error", "message": f"unable to open stream: {url}"})
            return

        await websocket.send_json({"type": "status", "message": "stream opened", "url": url, "target_fps": target_fps})
        frame_index = 0
        last_processed_at = 0.0
        while True:
            ret, frame = cap.read()
            if not ret:
                await _log_stream_error(module, "stream read failed or ended")
                await websocket.send_json({"type": "error", "message": "stream read failed or ended"})
                break

            now = time.monotonic()
            if frame_index % interval == 0 and now - last_processed_at >= min_frame_gap:
                last_processed_at = now
                try:
                    result = await loop.run_in_executor(
                        None,
                        police_gesture_service.recognize_frame_continuous,
                        frame,
                        sequence_state,
                    )
                    await _log_stream_result(module, result)
                    await websocket.send_json(
                        {
                            "type": "result",
                            "module": module,
                            "frame": frame_index,
                            "target_fps": target_fps,
                            "data": result,
                        }
                    )
                except Exception as exc:
                    logger.exception("URL stream recognition failed: %s", exc)
                    await _log_stream_error(module, str(exc))
                    await websocket.send_json({"type": "error", "message": str(exc)})
            frame_index += 1
            await asyncio.sleep(0)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        if cap is not None:
            cap.release()
