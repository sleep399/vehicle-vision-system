from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch

from app.config import settings
from app.services.ctpgr_pose_adapter import coco_to_ctpgr
from app.utils.helpers import ndarray_to_base64
from app.utils.image_draw import draw_cn_text_bgr


POLICE_GESTURES = {
    0: ("no_gesture", "无手势"),
    1: ("stop", "停止"),
    2: ("go_straight", "直行"),
    3: ("turn_left", "左转弯"),
    4: ("left_turn_wait", "左转弯待转"),
    5: ("turn_right", "右转弯"),
    6: ("lane_change", "变道"),
    7: ("slow_down", "减速慢行"),
    8: ("pull_over", "靠边停车"),
}

CTPGR_POSE_CONNECTIONS = [
    (1, 2), (2, 3), (4, 5), (5, 6),
    (14, 1), (14, 4), (1, 7), (4, 10),
    (7, 8), (8, 9), (10, 11), (11, 12), (13, 14),
]


@contextlib.contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PoliceGestureService:
    def __init__(self):
        self.ctpgr_root = (settings.base_dir / settings.ctpgr_data_path).resolve()
        self.input_size = (512, 512)
        self._predictor = None
        self._pg = None
        self._bla = None
        self._g_model = None
        self._yolo_model = None
        self._pose_backend_override: str | None = None
        self._model_lock = threading.RLock()

    @property
    def predictor(self):
        if self._predictor is None:
            self._predictor = self._load_ctpgr_predictor()
        return self._predictor

    @property
    def pg(self):
        if self._pg is None:
            self._load_ctpgr_classifier()
        return self._pg

    @property
    def bla(self):
        if self._bla is None:
            self._load_ctpgr_classifier()
        return self._bla

    @property
    def g_model(self):
        if self._g_model is None:
            self._load_ctpgr_classifier()
        return self._g_model

    @property
    def pose_backend(self) -> str:
        backend = (self._pose_backend_override or settings.police_pose_backend or "ctpgr").strip().lower()
        return backend if backend in {"ctpgr", "yolo"} else "ctpgr"

    def set_pose_backend(self, backend: str) -> dict[str, Any]:
        backend = (backend or "").strip().lower()
        if backend not in {"ctpgr", "yolo"}:
            raise ValueError("pose backend must be 'ctpgr' or 'yolo'")
        if backend == "yolo":
            _ = self.yolo_model
        self._pose_backend_override = backend
        return self.pose_backend_info()

    def pose_backend_info(self) -> dict[str, Any]:
        return {
            "backend": self.pose_backend,
            "available": ["ctpgr", "yolo"],
            "yolo_model": settings.police_yolo_pose_model,
            "yolo_loaded": self._yolo_model is not None,
        }

    def test_yolo_pose_model(self) -> dict[str, Any]:
        model_path = Path(settings.base_dir / settings.police_yolo_pose_model).resolve()
        exists = model_path.is_file()
        load_error = None
        loaded = False
        if exists:
            try:
                _ = self.yolo_model
                loaded = self._yolo_model is not None
            except Exception as exc:
                load_error = str(exc)
        else:
            load_error = f"model file not found: {model_path}"
        return {
            "model_path": str(model_path),
            "exists": exists,
            "loaded": loaded,
            "backend": self.pose_backend,
            "load_error": load_error,
        }

    def _load_ctpgr_predictor(self):
        if not self.ctpgr_root.exists():
            raise FileNotFoundError(f"ctpgr project not found: {self.ctpgr_root}")
        checkpoints = self.ctpgr_root / "checkpoints"
        missing = [name for name in ("pose_model.pt", "lstm.pt", "lstm_yolo11s.pt") if not (checkpoints / name).is_file()]
        if missing:
            raise FileNotFoundError(f"missing ctpgr checkpoints: {', '.join(missing)}")
        root_str = str(self.ctpgr_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        with _working_directory(self.ctpgr_root):
            from constants.enum_keys import PG
            from pred.gesture_pred import GesturePred

            self._pg = PG
            predictor = GesturePred()
            self._bla = predictor.bla
            self._g_model = predictor.g_model
            return predictor

    def _load_ctpgr_classifier(self) -> None:
        if self._g_model is not None and self._bla is not None and self._pg is not None:
            return
        if not self.ctpgr_root.exists():
            raise FileNotFoundError(f"ctpgr project not found: {self.ctpgr_root}")
        root_str = str(self.ctpgr_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        with _working_directory(self.ctpgr_root):
            from constants.enum_keys import PG
            from models.gesture_recognition_model import GestureRecognitionModel
            from pgdataset.s3_handcraft import BoneLengthAngle

            self._pg = PG
            self._bla = BoneLengthAngle()
            self._g_model = GestureRecognitionModel(1)
            self._g_model.ckpt_path = self.ctpgr_root / "checkpoints" / settings.police_gesture_model
            self._g_model.load_ckpt(allow_new=False)
            self._g_model.eval()

    @property
    def yolo_model(self):
        if self._yolo_model is None:
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("YOLO pose backend requires: pip install ultralytics") from exc
            model_path = settings.police_yolo_pose_model or "yolov8n-pose.pt"
            self._yolo_model = YOLO(model_path)
        return self._yolo_model

    def _confidence(self, scores: np.ndarray, gesture_id: int) -> float:
        probs = torch.softmax(torch.from_numpy(scores.astype(np.float32)), dim=0).numpy()
        if 0 <= gesture_id < len(probs):
            return float(probs[gesture_id])
        return 0.0

    def create_sequence_state(self) -> dict[str, Any]:
        try:
            return {
                "h": torch.zeros_like(self.g_model.h0()),
                "c": torch.zeros_like(self.g_model.c0()),
                "last_coord": None,
                "last_box": None,
                "missed_pose_frames": 0,
            }
        except FileNotFoundError:
            return {
                "h": None,
                "c": None,
                "last_coord": None,
                "last_box": None,
                "missed_pose_frames": 0,
            }

    @staticmethod
    def _select_person_index(boxes: np.ndarray, previous_box: np.ndarray | None = None) -> int:
        """Keep the same person across frames, falling back to the largest box."""
        boxes = np.asarray(boxes, dtype=np.float32)
        if boxes.ndim != 2 or boxes.shape[1] != 4 or len(boxes) == 0:
            raise ValueError("expected one or more person boxes shaped (N, 4)")

        areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
        if previous_box is None:
            return int(np.argmax(areas))

        previous_box = np.asarray(previous_box, dtype=np.float32)
        intersection_left_top = np.maximum(boxes[:, :2], previous_box[:2])
        intersection_right_bottom = np.minimum(boxes[:, 2:], previous_box[2:])
        intersection_size = np.maximum(0, intersection_right_bottom - intersection_left_top)
        intersection = intersection_size[:, 0] * intersection_size[:, 1]
        previous_area = max(0.0, float(previous_box[2] - previous_box[0])) * max(
            0.0, float(previous_box[3] - previous_box[1])
        )
        union = areas + previous_area - intersection
        iou = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        best_match = int(np.argmax(iou))
        if iou[best_match] >= 0.05:
            return best_match
        return int(np.argmax(areas))

    def _extract_keypoints(self, coord_norm: np.ndarray) -> list[dict]:
        if coord_norm.ndim == 3:
            coord_norm = coord_norm[0]
        if coord_norm.shape[0] != 2:
            coord_norm = coord_norm.T
        w, h = self.input_size
        return [
            {
                "id": i + 1,
                "x": round(float(coord_norm[0, i] * w), 2),
                "y": round(float(coord_norm[1, i] * h), 2),
                "z": 0.0,
                "visibility": 1.0,
            }
            for i in range(coord_norm.shape[1])
        ]

    def _draw_skeleton(self, image: np.ndarray, keypoints: list[dict]) -> None:
        points = {p["id"]: (int(p["x"]), int(p["y"])) for p in keypoints}
        for a, b in CTPGR_POSE_CONNECTIONS:
            if a in points and b in points:
                cv2.line(image, points[a], points[b], (0, 255, 0), 2)
        for point in points.values():
            cv2.circle(image, point, 4, (0, 200, 255), -1)

    def _coord_from_yolo_pose(
        self,
        ctpgr_image: np.ndarray,
        state: dict[str, Any] | None = None,
    ) -> np.ndarray:
        with self._model_lock:
            results = self.yolo_model.predict(ctpgr_image, verbose=False, imgsz=self.input_size[0], device="cpu")
        if not results or results[0].keypoints is None or results[0].keypoints.xy is None:
            return self._reuse_recent_pose(state)

        keypoints_xy = results[0].keypoints.xy.cpu().numpy()
        if keypoints_xy.size == 0:
            return self._reuse_recent_pose(state)

        person_index = 0
        boxes = getattr(results[0], "boxes", None)
        if boxes is not None and boxes.xyxy is not None and len(boxes.xyxy):
            xyxy = boxes.xyxy.cpu().numpy()
            previous_box = state.get("last_box") if state is not None else None
            person_index = self._select_person_index(xyxy, previous_box)
            if state is not None:
                state["last_box"] = xyxy[person_index].copy()

        coco = keypoints_xy[person_index]
        coord_norm = coco_to_ctpgr(coco, self.input_size)
        if state is not None:
            state["last_coord"] = coord_norm.copy()
            state["missed_pose_frames"] = 0
        return coord_norm

    @staticmethod
    def _reuse_recent_pose(state: dict[str, Any] | None) -> np.ndarray:
        hold_frames = max(0, int(settings.police_pose_hold_frames))
        if state is not None and state.get("last_coord") is not None:
            missed = int(state.get("missed_pose_frames", 0))
            if missed < hold_frames:
                state["missed_pose_frames"] = missed + 1
                return np.asarray(state["last_coord"], dtype=np.float32).copy()
            state["last_box"] = None
        raise ValueError("YOLO pose did not detect a person")

    def _result_payload(self, ctpgr_image: np.ndarray, result) -> dict[str, Any]:
        gesture_id = int(result[self.pg.OUT_ARGMAX])
        scores = result[self.pg.OUT_SCORES]
        confidence = self._confidence(scores, gesture_id)
        keypoints = self._extract_keypoints(result[self.pg.COORD_NORM])
        annotated = ctpgr_image.copy()
        self._draw_skeleton(annotated, keypoints)
        en, cn = POLICE_GESTURES.get(gesture_id, POLICE_GESTURES[0])
        annotated = draw_cn_text_bgr(annotated, f"{cn}（{confidence:.0%}）", (20, 48), (0, 0, 255), 30)
        return {
            "gesture": en,
            "gesture_cn": cn,
            "gesture_id": gesture_id,
            "confidence": round(confidence, 3),
            "pose_backend": self.pose_backend,
            "keypoints": keypoints,
            "annotated_image": ndarray_to_base64(annotated),
            "success": gesture_id > 0,
        }

    def _plain_payload(
        self,
        ctpgr_image: np.ndarray,
        gesture_id: int,
        confidence: float,
        coord_norm: np.ndarray | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        annotated = ctpgr_image.copy()
        keypoints = []
        if coord_norm is not None:
            keypoints = self._extract_keypoints(coord_norm)
            self._draw_skeleton(annotated, keypoints)
        en, cn = POLICE_GESTURES.get(gesture_id, POLICE_GESTURES[0])
        annotated = draw_cn_text_bgr(annotated, f"{cn}（{confidence:.0%}）", (20, 48), (0, 0, 255), 30)
        payload = {
            "gesture": en,
            "gesture_cn": cn,
            "gesture_id": gesture_id,
            "confidence": round(float(confidence), 3),
            "pose_backend": self.pose_backend,
            "keypoints": keypoints,
            "annotated_image": ndarray_to_base64(annotated),
            "success": gesture_id > 0,
        }
        if reason:
            payload["reason"] = reason
        return payload

    def _no_gesture_payload(self, ctpgr_image: np.ndarray, reason: str | None = None) -> dict[str, Any]:
        return self._plain_payload(ctpgr_image, 0, 1.0, None, reason)

    def _coord_from_prepared_image(
        self,
        ctpgr_image: np.ndarray,
        state: dict[str, Any] | None = None,
    ) -> np.ndarray:
        if self.pose_backend == "yolo":
            return self._coord_from_yolo_pose(ctpgr_image, state)
        with self._model_lock:
            pose = self.predictor.p_predictor.get_coordinates(ctpgr_image)
        return pose[self.pg.COORD_NORM][np.newaxis]

    def _classify_coord(self, coord_norm: np.ndarray, state: dict[str, Any] | None = None):
        features_dict = self.bla.handcrafted_features(coord_norm)
        features = np.concatenate(
            (
                features_dict[self.pg.BONE_LENGTH],
                features_dict[self.pg.BONE_ANGLE_COS],
                features_dict[self.pg.BONE_ANGLE_SIN],
            ),
            axis=1,
        )
        features = features[np.newaxis].transpose((1, 0, 2))
        features = torch.from_numpy(features).to(self.g_model.device, dtype=torch.float32)
        if state is None:
            state = self.create_sequence_state()
        if state.get("h") is None or state.get("c") is None:
            state["h"] = torch.zeros_like(self.g_model.h0())
            state["c"] = torch.zeros_like(self.g_model.c0())
        with self._model_lock, torch.no_grad():
            _, h, c, class_out = self.g_model(features, state["h"], state["c"])
        state["h"], state["c"] = h, c
        scores = class_out[0].cpu().numpy()
        return {self.pg.OUT_ARGMAX: int(np.argmax(scores)), self.pg.OUT_SCORES: scores, self.pg.COORD_NORM: coord_norm}

    def recognize_prepared_frame_continuous(
        self,
        ctpgr_image: np.ndarray,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            coord_norm = self._coord_from_prepared_image(ctpgr_image, state)
        except ValueError as exc:
            return self._no_gesture_payload(ctpgr_image, str(exc))
        result = self._classify_coord(coord_norm, state)
        return self._result_payload(ctpgr_image, result)

    def recognize_frame_continuous(
        self,
        frame: np.ndarray,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctpgr_image = cv2.resize(frame, self.input_size, interpolation=cv2.INTER_AREA)
        return self.recognize_prepared_frame_continuous(ctpgr_image, state)


police_gesture_service = PoliceGestureService()
