import importlib
import sys
import threading
import time
from pathlib import Path

import numpy as np
import cv2
import pytest

from app.routers import websocket as websocket_router
from app.services import lpr_video_service as lpr_video_service_module
from app.services.lpr_video_service import LprVideoService
from app.services.network_stream_hub import (
    NetworkStreamError,
    NetworkStreamHub,
    StreamFrame,
    StreamInfo,
)


ASSET_ROOT = Path(__file__).resolve().parents[2] / "yolo_lprnet_assets"


def test_lpr_and_gesture_network_routes_use_same_hub_singleton():
    assert lpr_video_service_module.network_stream_hub is websocket_router.network_stream_hub


def test_video_runtime_no_longer_depends_on_removed_data_package():
    sys.path.insert(0, str(ASSET_ROOT))
    try:
        runtime = importlib.import_module("runtime_api")
        demo = importlib.import_module("demo_integrated_lpr")
    finally:
        sys.path.remove(str(ASSET_ROOT))

    assert runtime.CHARS
    assert demo.CHARS == runtime.CHARS


def test_video_service_preserves_detected_plate_color():
    class FakeRuntime:
        @staticmethod
        def process_frame(frame):
            return frame, [{
                "text": "皖AF07000",
                "plate_color": "绿牌",
                "coords": (1, 2, 30, 12),
                "confidence": 0.88,
            }]

    service = LprVideoService()
    service._runtime = FakeRuntime()
    service._error = None
    result = service.recognize_frame(np.zeros((24, 64, 3), dtype=np.uint8))

    assert result["model_available"] is True
    assert result["plates"][0]["plate_color"] == "绿牌"


def test_rtsp_start_does_not_require_external_ffmpeg(monkeypatch):
    class FakeSubscription:
        def __init__(self):
            self.closed = False

        def wait_until_ready(self, timeout):
            assert timeout == 8.5
            return StreamInfo(width=1920, height=1080, fps=25.0)

        def next_frame(self, timeout=1.0):
            return None

        def close(self):
            self.closed = True

    class FakeHub:
        def __init__(self):
            self.urls = []
            self.subscription = FakeSubscription()

        def subscribe(self, url):
            self.urls.append(url)
            return self.subscription

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            pass

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    fake_hub = FakeHub()
    monkeypatch.setattr(lpr_video_service_module, "network_stream_hub", fake_hub)
    monkeypatch.setattr(
        cv2,
        "VideoCapture",
        lambda *_args: (_ for _ in ()).throw(AssertionError("direct capture must not be opened")),
    )
    monkeypatch.setattr("app.services.lpr_video_service.threading.Thread", FakeThread)
    monkeypatch.setattr(
        "app.services.lpr_video_service.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("FFmpeg must not be launched")),
    )

    result = service.start_rtsp_stream("rtsp://example/live", source_name="live1")

    assert result["success"] is True
    assert result["dst_url"] is None
    assert result["preview_url"] == "/api/lpr/preview/live1.mjpg"
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert result["fps"] == 25
    assert fake_hub.urls == ["rtsp://example/live"]


def test_rtsp_jobs_preview_and_stop_are_scoped_by_user(monkeypatch):
    class FakeSubscription:
        def __init__(self):
            self.closed = False

        def wait_until_ready(self, timeout):
            return StreamInfo(width=640, height=360, fps=15.0)

        def close(self):
            self.closed = True

    class FakeHub:
        def __init__(self):
            self.subscriptions = []

        def subscribe(self, _url):
            subscription = FakeSubscription()
            self.subscriptions.append(subscription)
            return subscription

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            return None

        def join(self, timeout=None):
            return None

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    fake_hub = FakeHub()
    monkeypatch.setattr(lpr_video_service_module, "network_stream_hub", fake_hub)
    monkeypatch.setattr("app.services.lpr_video_service.threading.Thread", FakeThread)

    service.start_rtsp_stream(
        "rtsp://camera.local/live",
        source_name="live1",
        user_id=101,
    )
    service.start_rtsp_stream(
        "rtsp://camera.local/live",
        source_name="live1",
        user_id=202,
    )

    assert service.preview_status("live1", user_id=101)["found"] is True
    assert service.preview_status("live1", user_id=202)["found"] is True
    assert service.preview_status("live1", user_id=None)["found"] is False

    stopped = service.stop_rtsp_stream(
        rtsp_url="rtsp://camera.local/live",
        source_name="live1",
        user_id=101,
    )

    assert stopped["stopped"] is True
    assert fake_hub.subscriptions[0].closed is True
    assert fake_hub.subscriptions[1].closed is False
    assert service.preview_status("live1", user_id=101)["running"] is False
    assert service.preview_status("live1", user_id=202)["running"] is True


def test_lpr_rtsp_shares_capture_and_stop_releases_only_lpr_subscription(monkeypatch):
    class SharedCapture:
        def __init__(self):
            self.released = False
            self.release_count = 0
            self._frames = []
            self._condition = threading.Condition()

        def isOpened(self):
            return not self.released

        def set(self, *_args):
            return True

        def get(self, prop):
            return {
                cv2.CAP_PROP_FRAME_WIDTH: 640,
                cv2.CAP_PROP_FRAME_HEIGHT: 360,
                cv2.CAP_PROP_FPS: 15,
            }.get(prop, 0)

        def push(self, frame):
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

        def release(self):
            with self._condition:
                if not self.released:
                    self.release_count += 1
                self.released = True
                self._condition.notify_all()

    captures = []

    def capture_factory(_url, _backend):
        capture = SharedCapture()
        captures.append(capture)
        return capture

    def wait_until(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached")

    hub = NetworkStreamHub(capture_factory=capture_factory)
    other_module = hub.subscribe("RTSP://Camera.Local:554/live")
    other_module.wait_until_ready(timeout=1.0)

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    monkeypatch.setattr(lpr_video_service_module, "network_stream_hub", hub)
    monkeypatch.setattr(
        service,
        "recognize_frame",
        lambda _frame, frame_index=0: {
            "success": True,
            "plates": [{
                "plate_number": "京A12345",
                "plate_color": "蓝牌",
                "confidence": 0.91,
            }],
            "plate_count": 1,
            "annotated_image": "data:image/jpeg;base64,anBlZw==",
            "frame": frame_index,
        },
    )

    result = service.start_rtsp_stream(
        "rtsp://camera.local/live",
        source_name="live1",
    )
    assert result["preview_url"] == "/api/lpr/preview/live1.mjpg"
    assert result["width"] == 640
    assert result["height"] == 360
    assert result["fps"] == 15
    assert len(captures) == 1
    assert hub.subscriber_count("rtsp://camera.local/live") == 2

    captures[0].push(np.zeros((360, 640, 3), dtype=np.uint8))
    wait_until(lambda: service.preview_status("live1")["plate_count"] == 1)
    status = service.preview_status("live1")
    assert status["running"] is True
    assert status["plates"][0]["plate_number"] == "京A12345"

    stopped = service.stop_rtsp_stream(
        rtsp_url="rtsp://camera.local/live",
        source_name="live1",
    )
    assert stopped["stopped"] is True
    assert stopped["history"]["plates"][0]["plate_number"] == "京A12345"
    assert hub.subscriber_count("rtsp://camera.local/live") == 1
    assert captures[0].released is False

    shared_frame = other_module.next_frame(timeout=1.0)
    assert shared_frame is not None
    other_module.close()
    wait_until(lambda: captures[0].released)
    assert captures[0].release_count == 1
    assert hub.active_stream_count() == 0


def test_lpr_rtsp_start_failure_releases_its_subscription(monkeypatch):
    class FailingSubscription:
        def __init__(self):
            self.closed = False

        def wait_until_ready(self, timeout):
            raise NetworkStreamError("unable to open network camera stream")

        def close(self):
            self.closed = True

    subscription = FailingSubscription()

    class FakeHub:
        @staticmethod
        def subscribe(_url):
            return subscription

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    monkeypatch.setattr(lpr_video_service_module, "network_stream_hub", FakeHub())

    with pytest.raises(RuntimeError, match="无法打开网络视频流"):
        service.start_rtsp_stream("rtsp://offline.local/live")

    assert subscription.closed is True


def test_lpr_stop_during_inference_does_not_publish_a_late_result(monkeypatch):
    class BlockingSubscription:
        def __init__(self):
            self.delivered = False
            self.closed = threading.Event()

        def wait_until_ready(self, timeout):
            return StreamInfo(width=640, height=360, fps=15.0)

        def next_frame(self, timeout=1.0):
            if not self.delivered:
                self.delivered = True
                return StreamFrame(
                    sequence=1,
                    frame=np.zeros((360, 640, 3), dtype=np.uint8),
                    captured_at=time.time(),
                )
            self.closed.wait(timeout)
            if self.closed.is_set():
                raise NetworkStreamError("subscription is closed")
            return None

        def close(self):
            self.closed.set()

    subscription = BlockingSubscription()

    class FakeHub:
        @staticmethod
        def subscribe(_url):
            return subscription

    inference_started = threading.Event()
    finish_inference = threading.Event()

    def recognize(_frame, frame_index=0):
        inference_started.set()
        assert finish_inference.wait(1.0)
        return {
            "success": True,
            "plates": [{
                "plate_number": "京A12345",
                "plate_color": "蓝牌",
                "confidence": 0.91,
            }],
            "plate_count": 1,
            "frame": frame_index,
        }

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    monkeypatch.setattr(lpr_video_service_module, "network_stream_hub", FakeHub())
    monkeypatch.setattr(service, "recognize_frame", recognize)

    service.start_rtsp_stream("rtsp://camera.local/live", source_name="live1")
    assert inference_started.wait(1.0)
    timer = threading.Timer(0.05, finish_inference.set)
    timer.start()
    stopped = service.stop_rtsp_stream(
        rtsp_url="rtsp://camera.local/live",
        source_name="live1",
    )
    timer.join(timeout=1.0)

    assert stopped["stopped"] is True
    assert stopped["history"]["plate_count"] == 0
    status = service.preview_status("live1")
    assert status["running"] is False
    assert status["plate_count"] == 0
