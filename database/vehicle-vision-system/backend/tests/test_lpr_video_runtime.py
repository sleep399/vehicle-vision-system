import importlib
import sys
from pathlib import Path

import numpy as np
import cv2

from app.services.lpr_video_service import LprVideoService


ASSET_ROOT = Path(__file__).resolve().parents[2] / "yolo_lprnet_assets"


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
    class FakeCapture:
        def set(self, *_args):
            return True

        def isOpened(self):
            return True

        def get(self, prop):
            return {
                cv2.CAP_PROP_FRAME_WIDTH: 1920,
                cv2.CAP_PROP_FRAME_HEIGHT: 1080,
                cv2.CAP_PROP_FPS: 25,
            }.get(prop, 0)

        def release(self):
            pass

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            pass

    service = LprVideoService()
    service._runtime = object()
    service._error = None
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: FakeCapture())
    monkeypatch.setattr("app.services.lpr_video_service.threading.Thread", FakeThread)
    monkeypatch.setattr(
        "app.services.lpr_video_service.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("FFmpeg must not be launched")),
    )

    result = service.start_rtsp_stream("rtsp://example/live", source_name="live1")

    assert result["success"] is True
    assert result["dst_url"] is None
    assert result["preview_url"] == "/api/lpr/preview/live1.mjpg"
