import asyncio
import base64
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.lpr_service import lpr_service
from app.services.police_gesture_service import police_gesture_service
from app.services.owner_gesture_service import owner_gesture_service
from app.services.alert_agent import alert_agent

router = APIRouter(tags=["WebSocket"])


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
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "frame":
                img_bytes = base64.b64decode(msg["data"])
                if module == "lpr":
                    result = service.recognize(img_bytes)
                else:
                    result = service.recognize(img_bytes)
                await websocket.send_json({"type": "result", "module": module, "data": result})
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
