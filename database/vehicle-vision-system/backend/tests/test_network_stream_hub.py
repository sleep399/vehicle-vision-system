from __future__ import annotations

import asyncio
import base64
import threading
import time

import numpy as np
import pytest
from fastapi import WebSocketDisconnect

from app.routers import websocket as websocket_router
from app.services.network_stream_hub import (
    NetworkStreamError,
    NetworkStreamHub,
    StreamFrame,
    normalize_stream_url,
)


class FakeCapture:
    def __init__(self, opened: bool = True) -> None:
        self.opened = opened
        self.released = False
        self.release_count = 0
        self._frames: list[np.ndarray] = []
        self._condition = threading.Condition()

    def isOpened(self) -> bool:
        return self.opened and not self.released

    def set(self, *_args) -> bool:
        return True

    def push(self, frame: np.ndarray) -> None:
        with self._condition:
            self._frames.append(frame)
            self._condition.notify_all()

    def read(self):
        with self._condition:
            while not self._frames and not self.released:
                self._condition.wait(0.1)
            if self.released:
                return False, None
            return True, self._frames.pop(0)

    def release(self) -> None:
        with self._condition:
            if not self.released:
                self.release_count += 1
            self.released = True
            self._condition.notify_all()


class CaptureFactory:
    def __init__(self, opened: bool = True) -> None:
        self.opened = opened
        self.calls: list[tuple[str, int, FakeCapture]] = []
        self.created = threading.Event()

    def __call__(self, url: str, backend: int) -> FakeCapture:
        capture = FakeCapture(self.opened)
        self.calls.append((url, backend, capture))
        self.created.set()
        return capture


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_normalize_stream_url_supports_network_and_droidcam_urls():
    assert normalize_stream_url(" HTTP://Camera.LOCAL:80/video#preview ") == (
        "http://camera.local/video"
    )
    assert normalize_stream_url("https://Camera.LOCAL:443/") == "https://camera.local/"
    assert normalize_stream_url("rtsp://camera.local:554/live") == "rtsp://camera.local/live"
    assert normalize_stream_url("rtmp://camera.local:1935/live") == "rtmp://camera.local/live"

    with pytest.raises(ValueError):
        normalize_stream_url("file:///tmp/camera.mp4")
    with pytest.raises(ValueError, match="not allowed"):
        normalize_stream_url("http://169.254.169.254/latest/meta-data")


def test_same_normalized_url_uses_one_capture_until_last_subscriber_closes():
    factory = CaptureFactory()
    hub = NetworkStreamHub(capture_factory=factory)
    first = hub.subscribe("HTTP://Camera.LOCAL:80/video")
    second = hub.subscribe("http://camera.local/video#ignored")
    third = hub.subscribe("http://CAMERA.local:80/video")

    assert factory.created.wait(1.0)
    assert len(factory.calls) == 1
    assert hub.active_stream_count() == 1
    assert hub.subscriber_count("http://camera.local/video") == 3

    capture = factory.calls[0][2]
    original = np.full((2, 3, 3), 7, dtype=np.uint8)
    capture.push(original)
    first_frame = first.next_frame(timeout=1.0)
    second_frame = second.next_frame(timeout=1.0)
    third_frame = third.next_frame(timeout=1.0)
    assert first_frame is not None and second_frame is not None and third_frame is not None
    assert first_frame.sequence == second_frame.sequence == third_frame.sequence
    assert np.array_equal(first_frame.frame, original)
    first_frame.frame[0, 0, 0] = 99
    assert second_frame.frame[0, 0, 0] == 7

    first.close()
    assert not capture.released
    assert hub.subscriber_count("http://camera.local/video") == 2
    second.close()
    assert not capture.released
    assert hub.subscriber_count("http://camera.local/video") == 1
    third.close()
    _wait_until(lambda: capture.released)
    assert capture.release_count == 1
    assert hub.active_stream_count() == 0


def test_different_urls_use_different_captures():
    factory = CaptureFactory()
    hub = NetworkStreamHub(capture_factory=factory)
    first = hub.subscribe("rtsp://camera-a/live")
    second = hub.subscribe("http://camera-b:4747/video")
    _wait_until(lambda: len(factory.calls) == 2)
    assert hub.active_stream_count() == 2
    first.close()
    second.close()


def test_single_transient_read_failure_does_not_drop_all_subscribers():
    class FlakyCapture:
        def __init__(self):
            self.read_count = 0
            self.released = False

        def isOpened(self):
            return True

        def set(self, *_args):
            return True

        def read(self):
            self.read_count += 1
            if self.read_count == 1:
                return False, None
            if self.read_count == 2:
                return True, np.full((2, 2, 3), 9, dtype=np.uint8)
            while not self.released:
                time.sleep(0.01)
            return False, None

        def release(self):
            self.released = True

    capture = FlakyCapture()
    hub = NetworkStreamHub(capture_factory=lambda *_args: capture)
    first = hub.subscribe("rtsp://camera.local/live")
    second = hub.subscribe("rtsp://camera.local/live")

    first_frame = first.next_frame(timeout=1.0)
    second_frame = second.next_frame(timeout=1.0)
    assert first_frame is not None and second_frame is not None
    assert np.all(first_frame.frame == 9)
    assert capture.read_count >= 2

    first.close()
    assert not capture.released
    second.close()
    _wait_until(lambda: capture.released)


def test_network_stream_and_subscriber_limits_prevent_unbounded_workers():
    factory = CaptureFactory()
    hub = NetworkStreamHub(
        capture_factory=factory,
        max_streams=1,
        max_subscribers_per_stream=1,
    )
    first = hub.subscribe("rtsp://camera-a/live")
    with pytest.raises(NetworkStreamError, match="subscribers"):
        hub.subscribe("rtsp://camera-a/live")
    with pytest.raises(NetworkStreamError, match="active"):
        hub.subscribe("rtsp://camera-b/live")
    first.close()


def test_capture_open_failure_is_reported_without_real_camera():
    factory = CaptureFactory(opened=False)
    hub = NetworkStreamHub(capture_factory=factory)
    subscription = hub.subscribe("rtsp://offline-camera/live")
    with pytest.raises(NetworkStreamError, match="unable to open"):
        subscription.next_frame(timeout=1.0)
    subscription.close()
    assert factory.calls[0][2].release_count == 1


def test_log_deduplication_is_scoped_by_user_module_and_source_id(monkeypatch):
    websocket_router._stream_last_logged.clear()
    monkeypatch.setattr(websocket_router.time, "monotonic", lambda: 10.0)
    result = {"gesture": "stop", "confidence": 0.9}

    assert websocket_router._should_log_result("police", "camera-a", result)
    assert not websocket_router._should_log_result("police", "camera-a", result)
    assert websocket_router._should_log_result("police", "camera-b", result)
    assert websocket_router._should_log_result("owner", "camera-a", result)
    assert websocket_router._should_log_result("police", "camera-a", result, user_id=7)
    assert websocket_router._should_log_result("police", "camera-a", result, user_id=8)


def test_empty_frames_do_not_periodically_accumulate_false_failures(monkeypatch):
    websocket_router._stream_last_logged.clear()
    clock = {"value": 10.0}
    monkeypatch.setattr(websocket_router.time, "monotonic", lambda: clock["value"])
    empty_lpr = {"success": False, "plate_count": 0, "plates": []}
    valid_lpr = {"success": True, "plate_count": 1, "plates": [{"plate_number": "粤B12345"}]}

    assert websocket_router._should_log_result("lpr", "camera-a", empty_lpr)
    clock["value"] += 30
    assert not websocket_router._should_log_result("lpr", "camera-a", empty_lpr)
    assert websocket_router._should_log_result("lpr", "camera-a", valid_lpr)
    clock["value"] += websocket_router._LOG_REFRESH_SECONDS + 0.1
    assert websocket_router._should_log_result("lpr", "camera-a", valid_lpr)


def test_source_id_never_echoes_a_credential_bearing_url():
    source_id = websocket_router._normalize_source_id(
        "rtsp://admin:secret@camera.local/live",
        "fallback",
    )
    assert source_id.startswith("source-")
    assert "admin" not in source_id
    assert "secret" not in source_id


class FakeWebSocket:
    def __init__(self, messages: list[dict], token: str | None = None) -> None:
        self._messages = [json_message(message) for message in messages]
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False
        self.query_params = {"token": token} if token else {}

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self.close_code = code

    async def receive_text(self) -> str:
        if not self._messages:
            await asyncio.Future()
        return self._messages.pop(0)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def json_message(payload: dict) -> str:
    import json

    return json.dumps(payload)


def test_websocket_identity_keeps_guests_but_rejects_invalid_tokens():
    assert websocket_router._resolve_websocket_user_id(FakeWebSocket([])) == (True, None)
    assert websocket_router._resolve_websocket_user_id(
        FakeWebSocket([], token="not-a-valid-jwt")
    ) == (False, None)


def test_browser_stream_rejects_invalid_token_before_recognition(monkeypatch):
    websocket = FakeWebSocket([], token="expired")
    monkeypatch.setattr(
        websocket_router,
        "_resolve_websocket_user_id",
        lambda _ws: (False, None),
    )

    asyncio.run(websocket_router.ws_stream(websocket, "lpr"))

    assert websocket.accepted
    assert websocket.closed
    assert websocket.close_code == 1008
    assert websocket.sent == [{"type": "error", "message": "令牌无效或已过期"}]


def test_alert_websocket_registers_in_authenticated_user_scope(monkeypatch):
    websocket = DisconnectingWebSocket([])
    registrations: list[tuple[object, int | None]] = []
    unregistered: list[object] = []
    monkeypatch.setattr(websocket_router, "_resolve_websocket_user_id", lambda _ws: (True, 42))
    monkeypatch.setattr(
        websocket_router.alert_agent,
        "register_ws",
        lambda ws, user_id=None: registrations.append((ws, user_id)),
    )
    monkeypatch.setattr(
        websocket_router.alert_agent,
        "unregister_ws",
        lambda ws: unregistered.append(ws),
    )

    asyncio.run(websocket_router.ws_alerts(websocket))

    assert registrations == [(websocket, 42)]
    assert unregistered == [websocket]


def test_alert_websocket_rejects_invalid_token(monkeypatch):
    websocket = FakeWebSocket([], token="expired")
    registrations: list[object] = []
    monkeypatch.setattr(websocket_router, "_resolve_websocket_user_id", lambda _ws: (False, None))
    monkeypatch.setattr(
        websocket_router.alert_agent,
        "register_ws",
        lambda ws, user_id=None: registrations.append((ws, user_id)),
    )

    asyncio.run(websocket_router.ws_alerts(websocket))

    assert websocket.accepted
    assert websocket.closed
    assert websocket.close_code == 1008
    assert websocket.sent == [{"type": "error", "message": "令牌无效或已过期"}]
    assert registrations == []


def test_browser_stream_echoes_source_id_without_database(monkeypatch):
    encoded = base64.b64encode(b"jpeg").decode("ascii")
    websocket = FakeWebSocket(
        [
            {"type": "frame", "data": encoded, "source_id": "usb-camera-2"},
            {"type": "end", "source_id": "usb-camera-2"},
        ]
    )

    async def fake_recognize(*_args, **_kwargs):
        return {"success": False, "plate_count": 0, "plates": []}

    async def no_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        websocket_router,
        "_decode_jpeg_frame",
        lambda _data: np.zeros((2, 2, 3), dtype=np.uint8),
    )
    monkeypatch.setattr(websocket_router, "_recognize_stream_frame", fake_recognize)
    monkeypatch.setattr(websocket_router, "_log_stream_result", no_log)
    monkeypatch.setattr(websocket_router, "_log_stream_error", no_log)

    asyncio.run(websocket_router.ws_stream(websocket, "lpr"))
    result = next(item for item in websocket.sent if item.get("type") == "result")
    done = next(item for item in websocket.sent if item.get("type") == "done")
    assert result["source_id"] == "usb-camera-2"
    assert done["source_id"] == "usb-camera-2"


class OneFrameSubscription:
    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    def next_frame(self, timeout: float = 1.0):
        self.calls += 1
        if self.calls == 1:
            return StreamFrame(
                sequence=4,
                frame=np.zeros((2, 2, 3), dtype=np.uint8),
                captured_at=123.0,
            )
        raise NetworkStreamError("test stream ended")

    def close(self) -> None:
        self.closed = True


class SlowSubscription:
    def __init__(self) -> None:
        self.closed = False

    def next_frame(self, timeout: float = 1.0):
        time.sleep(min(timeout, 0.2))
        return None

    def close(self) -> None:
        self.closed = True


class DisconnectingWebSocket(FakeWebSocket):
    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0.01)
        raise WebSocketDisconnect()


@pytest.mark.parametrize("module", ["lpr", "police", "owner"])
def test_network_url_route_supports_all_modules_without_camera_or_db(monkeypatch, module):
    websocket = FakeWebSocket(
        [
            {
                "type": "start",
                "url": "http://phone.local:4747/video",
                "source_id": "droidcam-main",
                "target_fps": 10,
            }
        ]
    )
    subscription = OneFrameSubscription()
    received_user_ids: list[int | None] = []

    async def fake_recognize(_module, _frame, _index, _state, user_id):
        received_user_ids.append(user_id)
        return {"gesture": "no_gesture", "confidence": 0.0, "success": False}

    async def no_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        websocket_router.network_stream_hub,
        "subscribe",
        lambda _url: subscription,
    )
    monkeypatch.setattr(websocket_router, "_recognize_stream_frame", fake_recognize)
    monkeypatch.setattr(websocket_router, "_resolve_websocket_user_id", lambda _ws: (True, 42))
    monkeypatch.setattr(websocket_router, "_log_stream_result", no_log)
    monkeypatch.setattr(websocket_router, "_log_stream_error", no_log)
    monkeypatch.setattr(
        websocket_router.police_gesture_service,
        "create_sequence_state",
        lambda: {},
    )

    asyncio.run(websocket_router.ws_stream_url(websocket, module))
    message_types = [item.get("type") for item in websocket.sent]
    assert message_types.index("status") < message_types.index("result")
    result = next(item for item in websocket.sent if item.get("type") == "result")
    assert result["module"] == module
    assert result["source_id"] == "droidcam-main"
    assert result["capture_sequence"] == 4
    assert received_user_ids == [42]
    assert subscription.closed


def test_owner_network_frame_preserves_control_state_with_mock_session(monkeypatch):
    class FakeSession:
        def close(self):
            return None

    applied: dict = {}

    monkeypatch.setattr(websocket_router, "SessionLocal", FakeSession)
    monkeypatch.setattr(websocket_router, "_get_or_create_state", lambda _db, uid: {"uid": uid})
    monkeypatch.setattr(
        websocket_router,
        "_state_to_dict",
        lambda state: {"volume": 50, "uid": state.get("uid")},
    )
    monkeypatch.setattr(
        websocket_router,
        "_owner_recognize_sync",
        lambda _frame, state, _user_id: {"gesture": "thumbs_up", "action": "answer_call", "input": state},
    )

    def fake_apply(_db, uid, action, source):
        applied.update({"uid": uid, "action": action, "source": source})
        return {"phone_status": "in_call"}

    monkeypatch.setattr(websocket_router, "_apply_action_to_db", fake_apply)
    async def run_owner_frame():
        return await websocket_router._recognize_owner_frame(
            asyncio.get_running_loop(),
            np.zeros((2, 2, 3), dtype=np.uint8),
            user_id=42,
        )

    result = asyncio.run(run_owner_frame())
    assert applied == {
        "uid": 42,
        "action": "answer_call",
        "source": "实时网络手势",
    }
    assert result["vehicle_state"] == {"phone_status": "in_call"}


def test_network_route_releases_subscription_when_client_disconnects(monkeypatch):
    websocket = DisconnectingWebSocket([
        {
            "type": "start",
            "url": "rtsp://camera.local/live",
            "source_id": "camera-main",
        }
    ])
    subscription = SlowSubscription()

    monkeypatch.setattr(
        websocket_router.network_stream_hub,
        "subscribe",
        lambda _url: subscription,
    )
    monkeypatch.setattr(websocket_router, "_resolve_websocket_user_id", lambda _ws: (True, None))

    asyncio.run(websocket_router.ws_stream_url(websocket, "lpr"))
    assert subscription.closed
