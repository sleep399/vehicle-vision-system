import asyncio
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.routers.lpr import save_video_history
from app.utils.crypto import decrypt_json
from app.utils.plate_number import is_valid_plate_number


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = PROJECT_ROOT / "yolo_lprnet_assets"


@pytest.fixture(scope="module")
def real_lpr_runtime():
    pytest.importorskip("torch")
    pytest.importorskip("ultralytics")

    yolo_path = ASSET_ROOT / "weights" / "best.pt"
    lpr_path = ASSET_ROOT / "weights" / "Final_LPRNet_model.pth"
    if not yolo_path.is_file() or not lpr_path.is_file():
        pytest.skip("车牌真实权重未包含在当前检出中")

    sys.path.insert(0, str(ASSET_ROOT))
    try:
        from runtime_api import YoloLprRuntime
    finally:
        sys.path.remove(str(ASSET_ROOT))
    return YoloLprRuntime()


@pytest.mark.parametrize(
    ("relative_path", "expected_plate"),
    [
        ("yolo_lprnet_assets/images/test.jpg", "皖AF07000"),
        ("../CCPD-master/rpnet/demo/2.jpg", "皖AT022C"),
        ("../CCPD-master/rpnet/demo/3.jpg", "皖AMK620"),
    ],
)
def test_video_runtime_recognizes_repository_samples(
    real_lpr_runtime,
    relative_path,
    expected_plate,
):
    frame = cv2.imread(str(PROJECT_ROOT / relative_path))
    assert frame is not None

    _, plates = real_lpr_runtime.process_frame(frame)

    assert [plate["text"] for plate in plates] == [expected_plate]


def test_video_runtime_uses_canonical_image_detector_defaults(real_lpr_runtime):
    from app.yolo_lprnet.detector import YOLOPlateDetector

    assert isinstance(real_lpr_runtime.detector, YOLOPlateDetector)
    assert isinstance(real_lpr_runtime.fallback_detector, YOLOPlateDetector)
    assert real_lpr_runtime.detector.model is real_lpr_runtime.fallback_detector.model
    assert real_lpr_runtime.config.yolo_conf == 0.4
    assert real_lpr_runtime.config.yolo_imgsz == 960
    assert real_lpr_runtime.config.fallback_yolo_conf == 0.3
    assert real_lpr_runtime.config.fallback_yolo_imgsz == 1280
    assert real_lpr_runtime.detector.min_plate_area_ratio == 0.0015
    assert real_lpr_runtime.detector.max_plate_area_ratio == 0.2
    assert real_lpr_runtime.detector.min_plate_aspect_ratio == 1.8
    assert real_lpr_runtime.detector.max_plate_aspect_ratio == 6.5
    assert real_lpr_runtime.fallback_detector.min_box_width == 18
    assert real_lpr_runtime.fallback_detector.min_box_height == 8
    assert real_lpr_runtime.fallback_detector.min_plate_aspect_ratio == 1.5
    assert real_lpr_runtime.fallback_detector.max_plate_aspect_ratio == 8.0


def test_video_runtime_runs_both_detection_profiles(
    real_lpr_runtime,
    monkeypatch,
):
    calls = []
    fallback_plate = {
        "coords": (2, 4, 42, 18),
        "confidence": 0.72,
        "text": "京A12345",
        "plate_color": "蓝牌",
    }

    def fake_recognize(_frame, detector):
        calls.append(detector)
        return [] if detector is real_lpr_runtime.detector else [fallback_plate]

    monkeypatch.setattr(real_lpr_runtime, "_recognize_with_detector", fake_recognize)

    _, plates = real_lpr_runtime.process_frame(np.zeros((24, 64, 3), dtype=np.uint8))

    assert calls == [real_lpr_runtime.detector, real_lpr_runtime.fallback_detector]
    assert plates == [fallback_plate]


def test_multiscale_merge_prefers_ocr_confidence_and_keeps_distant_plate(
    real_lpr_runtime,
):
    primary = {
        "coords": (10, 10, 110, 40),
        "confidence": 0.91,
        "recognition_confidence": 0.81,
        "text": "京BF114A",
        "plate_color": "蓝牌",
    }
    corrected = {
        "coords": (8, 9, 112, 41),
        "confidence": 0.62,
        "recognition_confidence": 0.96,
        "text": "京BF1144",
        "plate_color": "蓝牌",
    }
    distant = {
        "coords": (180, 80, 250, 105),
        "confidence": 0.7,
        "recognition_confidence": 0.9,
        "text": "沪A12345",
        "plate_color": "蓝牌",
    }

    merged = real_lpr_runtime._merge_multiscale_results(
        [primary],
        [corrected, distant],
    )

    assert {plate["text"] for plate in merged} == {"京BF1144", "沪A12345"}


@pytest.mark.parametrize(
    "plate",
    ["京A12345", "皖AF07000", "粤BDA1234", "皖A12345F"],
)
def test_supported_plate_lengths_are_valid(plate):
    assert is_valid_plate_number(plate)


@pytest.mark.parametrize(
    "plate",
    [
        "",
        "京A1234",
        "京A1234567",
        "A京12345",
        "测A12345",
        "京I12345",
        "京O12345",
        "皖AB12345",
        "皖AF12A45",
        "皖B06498H",
        "粤BDI1234",
        "粤BDO1234",
    ],
)
def test_malformed_plate_lengths_are_rejected(plate):
    assert not is_valid_plate_number(plate)


def test_video_history_accepts_new_energy_plate():
    class FakeDb:
        record = None

        def add(self, record):
            self.record = record

        def commit(self):
            return None

        @staticmethod
        def refresh(record):
            record.id = 42

    db = FakeDb()
    result = asyncio.run(save_video_history(
        payload={
            "plates": [{
                "plate_number": "皖AF07000",
                "plate_color": "绿牌",
                "confidence": 0.9,
                "frame_index": 8,
            }],
            "source_path": "regression-fixture.mp4",
        },
        db=db,
        user=None,
    ))

    assert result == {"saved": True, "record_id": 42, "plate_count": 1}
    assert db.record is not None
    assert db.record.source_type == "video"
    assert decrypt_json(db.record.plates_json) == {
        "plates": [{
            "plate_number": "皖AF07000",
            "plate_color": "绿牌",
            "confidence": 0.9,
            "frame_index": 8,
            "source": "yolo_lprnet",
        }],
    }
