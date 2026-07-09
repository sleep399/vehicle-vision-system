from __future__ import annotations
import asyncio
import base64
import json
import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.alert_agent import alert_agent
from app.services.police_gesture_service import police_gesture_service
from app.utils.video import validate_stream_url

router = APIRouter(tags=["websocket"])


async def _run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def _decode_jpeg_frame(data: str):
    img_bytes = base64.b64decode(data)
    nparr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("unable to decode frame")
    return frame


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
    await websocket.accept()
    if module != "police":
        await websocket.send_json({"type": "error", "message": "only police module is enabled"})
        await websocket.close()
        return
    sequence_state = police_gesture_service.create_sequence_state()
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            if msg.get("type") == "frame":
                frame = _decode_jpeg_frame(msg["data"])
                result = await _run_blocking(police_gesture_service.recognize_frame_continuous, frame, sequence_state)
                await websocket.send_json({
                    "type": "result",
                    "module": module,
                    "time_sec": msg.get("time_sec"),
                    "data": result,
                })
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


@router.websocket("/ws/stream-url/{module}")
async def ws_stream_url(websocket: WebSocket, module: str):
    await websocket.accept()
    if module != "police":
        await websocket.send_json({"type": "error", "message": "only police module is enabled"})
        await websocket.close()
        return
    cap = None
    try:
        start_msg = json.loads(await websocket.receive_text())
        url = validate_stream_url(start_msg.get("url", ""))
        interval = max(1, min(int(start_msg.get("interval", 1)), 120))
        sequence_state = police_gesture_service.create_sequence_state()
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            await websocket.send_json({"type": "error", "message": f"unable to open stream: {url}"})
            return
        await websocket.send_json({"type": "status", "message": "stream opened", "url": url})
        frame_index = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                await websocket.send_json({"type": "error", "message": "stream read failed or ended"})
                break
            if frame_index % interval == 0:
                result = await _run_blocking(police_gesture_service.recognize_frame_continuous, frame, sequence_state)
                await websocket.send_json({"type": "result", "module": module, "frame": frame_index, "data": result})
            frame_index += 1
            await asyncio.sleep(0.01)
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
