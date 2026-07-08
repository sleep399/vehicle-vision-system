from __future__ import annotations
import contextlib
import io
import os
import sys
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from PIL import Image, ImageSequence

from app.config import settings
from app.utils.helpers import ndarray_to_base64


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
        self.ctpgr_root = settings.base_dir.parent / "ctpgr-pytorch-master"
        self.input_size = (512, 512)
        self.sequence_steps = 30
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

    def _load_ctpgr_predictor(self):
        if not self.ctpgr_root.exists():
            raise FileNotFoundError(f"ctpgr project not found: {self.ctpgr_root}")
        checkpoints = self.ctpgr_root / "checkpoints"
        missing = [name for name in ("pose_model.pt", "lstm.pt") if not (checkpoints / name).is_file()]
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
        if self.ctpgr_root.exists():
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
            self._g_model.load_ckpt(allow_new=False)
            self._g_model.eval()

    @property
    def pose_backend(self) -> str:
        backend = (self._pose_backend_override or settings.police_pose_backend or "ctpgr").strip().lower()
        if backend not in {"ctpgr", "yolo"}:
            return "ctpgr"
        return backend

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

    def _detect_best_frame(self, image_bytes: bytes) -> np.ndarray:
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            if getattr(pil_img, "is_animated", False):
                best_frame = None
                best_score = -1.0
                for frame in ImageSequence.Iterator(pil_img):
                    frame_np = cv2.cvtColor(np.array(frame.convert("RGB")), cv2.COLOR_RGB2BGR)
                    score = cv2.Laplacian(cv2.cvtColor(frame_np, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                    if score > best_score:
                        best_score = score
                        best_frame = frame_np
                if best_frame is not None:
                    return best_frame
            return cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception:
            pass

        nparr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Unable to parse image")
        return image

    def _confidence(self, scores: np.ndarray, gesture_id: int) -> float:
        probs = torch.softmax(torch.from_numpy(scores.astype(np.float32)), dim=0).numpy()
        if 0 <= gesture_id < len(probs):
            return float(probs[gesture_id])
        return 0.0

    def create_sequence_state(self) -> dict[str, torch.Tensor]:
        return {
            "h": torch.zeros_like(self.g_model.h0()),
            "c": torch.zeros_like(self.g_model.c0()),
        }

    def reset_sequence_state(self) -> None:
        self.predictor.h = torch.zeros_like(self.predictor.h)
        self.predictor.c = torch.zeros_like(self.predictor.c)

    def _extract_keypoints(self, coord_norm: np.ndarray) -> list[dict]:
        if coord_norm.ndim == 3:
            coord_norm = coord_norm[0]
        if coord_norm.shape[0] != 2:
            coord_norm = coord_norm.T
        w, h = self.input_size
        return [
            {"id": i + 1, "x": round(float(coord_norm[0, i] * w), 2), "y": round(float(coord_norm[1, i] * h), 2), "z": 0.0, "visibility": 1.0}
            for i in range(coord_norm.shape[1])
        ]

    def _draw_skeleton(self, image: np.ndarray, keypoints: list[dict]) -> None:
        points = {p["id"]: (int(p["x"]), int(p["y"])) for p in keypoints}
        for a, b in CTPGR_POSE_CONNECTIONS:
            if a in points and b in points:
                cv2.line(image, points[a], points[b], (0, 255, 0), 2)
        for point in points.values():
            cv2.circle(image, point, 4, (0, 200, 255), -1)

    def _coord_from_yolo_pose(self, ctpgr_image: np.ndarray) -> np.ndarray:
        with self._model_lock:
            results = self.yolo_model.predict(ctpgr_image, verbose=False, imgsz=self.input_size[0], device="cpu")
        if not results or results[0].keypoints is None or results[0].keypoints.xy is None:
            raise ValueError("YOLO pose did not detect a person")

        keypoints_xy = results[0].keypoints.xy.cpu().numpy()
        keypoints_conf = results[0].keypoints.conf
        keypoints_conf = keypoints_conf.cpu().numpy() if keypoints_conf is not None else np.ones(keypoints_xy.shape[:2], dtype=np.float32)
        if keypoints_xy.size == 0:
            raise ValueError("YOLO pose did not return keypoints")

        person_index = 0
        boxes = getattr(results[0], "boxes", None)
        if boxes is not None and boxes.xyxy is not None and len(boxes.xyxy):
            xyxy = boxes.xyxy.cpu().numpy()
            areas = np.maximum(0, xyxy[:, 2] - xyxy[:, 0]) * np.maximum(0, xyxy[:, 3] - xyxy[:, 1])
            person_index = int(np.argmax(areas))

        coco = keypoints_xy[person_index]
        conf = keypoints_conf[person_index]
        min_conf = float(settings.police_yolo_keypoint_conf)

        def pick(index: int) -> np.ndarray:
            point = coco[index].astype(np.float32)
            if conf[index] < min_conf or np.allclose(point, 0):
                return np.array([np.nan, np.nan], dtype=np.float32)
            return point

        right_shoulder = pick(6)
        right_elbow = pick(8)
        right_wrist = pick(10)
        left_shoulder = pick(5)
        left_elbow = pick(7)
        left_wrist = pick(9)
        right_hip = pick(12)
        right_knee = pick(14)
        right_ankle = pick(16)
        left_hip = pick(11)
        left_knee = pick(13)
        left_ankle = pick(15)

        shoulder_points = np.stack([right_shoulder, left_shoulder])
        neck = np.nanmean(shoulder_points, axis=0)
        if np.isnan(neck).any():
            raise ValueError("YOLO pose shoulders are not reliable enough")

        head_candidates = np.stack([pick(0), pick(1), pick(2), pick(3), pick(4)])
        valid_head = head_candidates[~np.isnan(head_candidates).any(axis=1)]
        if len(valid_head):
            head_x = float(np.nanmean(valid_head[:, 0]))
            head_y = float(np.nanmin(valid_head[:, 1]))
        else:
            head_x, head_y = float(neck[0]), float(neck[1])
        shoulder_width = float(np.linalg.norm(left_shoulder - right_shoulder)) if not np.isnan(shoulder_points).any() else 40.0
        head_top = np.array([head_x, max(0.0, head_y - 0.25 * shoulder_width)], dtype=np.float32)

        ctpgr_points = np.stack(
            [
                right_shoulder,
                right_elbow,
                right_wrist,
                left_shoulder,
                left_elbow,
                left_wrist,
                right_hip,
                right_knee,
                right_ankle,
                left_hip,
                left_knee,
                left_ankle,
                head_top,
                neck.astype(np.float32),
            ],
            axis=0,
        )
        if np.isnan(ctpgr_points).any():
            raise ValueError("YOLO pose missing required body keypoints")
        w, h = self.input_size
        ctpgr_points[:, 0] = np.clip(ctpgr_points[:, 0] / w, 0.0, 1.0)
        ctpgr_points[:, 1] = np.clip(ctpgr_points[:, 1] / h, 0.0, 1.0)
        return ctpgr_points.T[np.newaxis].astype(np.float32)

    def _result_payload(self, ctpgr_image: np.ndarray, result) -> dict[str, Any]:
        gesture_id = int(result[self.pg.OUT_ARGMAX])
        scores = result[self.pg.OUT_SCORES]
        confidence = self._confidence(scores, gesture_id)
        keypoints = self._extract_keypoints(result[self.pg.COORD_NORM])
        annotated = ctpgr_image.copy()
        self._draw_skeleton(annotated, keypoints)
        en, cn = POLICE_GESTURES.get(gesture_id, POLICE_GESTURES[0])
        cv2.putText(annotated, f"{en} ({confidence:.0%})", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
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

    def _coord_from_prepared_image(self, ctpgr_image: np.ndarray) -> np.ndarray:
        if self.pose_backend == "yolo":
            return self._coord_from_yolo_pose(ctpgr_image)
        with self._model_lock:
            pose = self.predictor.p_predictor.get_coordinates(ctpgr_image)
        return pose[self.pg.COORD_NORM][np.newaxis]

    def _classify_coord(self, coord_norm: np.ndarray, state: dict[str, torch.Tensor] | None = None):
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
        with self._model_lock, torch.no_grad():
            _, h, c, class_out = self.g_model(features, state["h"], state["c"])
        state["h"], state["c"] = h, c
        scores = class_out[0].cpu().numpy()
        return {self.pg.OUT_ARGMAX: int(np.argmax(scores)), self.pg.OUT_SCORES: scores, self.pg.COORD_NORM: coord_norm}

    def _classify_prepared_image(self, ctpgr_image: np.ndarray) -> dict[str, Any]:
        coord_norm = self._coord_from_prepared_image(ctpgr_image)
        state = self.create_sequence_state()

        sequence_results = []
        for _ in range(self.sequence_steps):
            sequence_results.append(self._classify_coord(coord_norm, state))

        tail = sequence_results[-8:]
        nonzero_tail = [r for r in tail if int(r[self.pg.OUT_ARGMAX]) > 0]
        if nonzero_tail:
            result = max(nonzero_tail, key=lambda r: self._confidence(r[self.pg.OUT_SCORES], int(r[self.pg.OUT_ARGMAX])))
        else:
            result = sequence_results[-1]
        return self._result_payload(ctpgr_image, result)

    def recognize_prepared_frame_continuous(self, ctpgr_image: np.ndarray, state: dict[str, torch.Tensor] | None = None) -> dict[str, Any]:
        coord_norm = self._coord_from_prepared_image(ctpgr_image)
        result = self._classify_coord(coord_norm, state)
        return self._result_payload(ctpgr_image, result)

    def recognize_image(self, image: np.ndarray) -> dict[str, Any]:
        ctpgr_image = cv2.resize(image, self.input_size, interpolation=cv2.INTER_AREA)
        return self._classify_prepared_image(ctpgr_image)

    def recognize(self, image_bytes: bytes) -> dict[str, Any]:
        image = self._detect_best_frame(image_bytes)
        return self.recognize_image(image)

    def recognize_frame(self, frame: np.ndarray) -> dict[str, Any]:
        return self.recognize_image(frame)

    def recognize_frame_continuous(self, frame: np.ndarray, state: dict[str, torch.Tensor] | None = None) -> dict[str, Any]:
        ctpgr_image = cv2.resize(frame, self.input_size, interpolation=cv2.INTER_AREA)
        return self.recognize_prepared_frame_continuous(ctpgr_image, state)


police_gesture_service = PoliceGestureService()
