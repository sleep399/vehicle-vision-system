from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch

from app.config import settings
from app.services.ctpgr_pose_adapter import (
    POSE_REPAIR_CACHE_REVISION,
    POSE_REPAIR_TRAINING_PIPELINE_REVISION,
    apply_gesture_probability_gate,
    coco_to_ctpgr,
    load_arm_pose_prior,
    repair_coco_pose,
    select_person_index,
    select_person_index_with_match,
    sha256_file,
)
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
        self._arm_pose_prior = None
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
            if self.pose_backend == "ctpgr":
                _ = self.predictor
            else:
                self._load_ctpgr_classifier()
        return self._pg

    @property
    def bla(self):
        if self.pose_backend == "ctpgr":
            return self.predictor.bla
        if self._bla is None:
            self._load_ctpgr_classifier()
        return self._bla

    @property
    def g_model(self):
        if self.pose_backend == "ctpgr":
            return self.predictor.g_model
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
            "pose_repair_enabled": settings.police_pose_repair_enabled,
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

            bla = BoneLengthAngle()
            g_model = GestureRecognitionModel(1)
            checkpoint_path = self.ctpgr_root / "checkpoints" / settings.police_gesture_model
            metadata_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".meta.json")
            if settings.police_pose_repair_enabled:
                self._validate_repair_checkpoint(checkpoint_path)
            elif metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("pose_repair_version"):
                    raise ValueError("pose-repair gesture checkpoint requires police_pose_repair_enabled=true")
            g_model.ckpt_path = checkpoint_path
            g_model.load_ckpt(allow_new=False)
            g_model.eval()
            # Publish the classifier only after validation and checkpoint load
            # both succeed; a failed attempt must never cache a random model.
            self._pg = PG
            self._bla = bla
            self._g_model = g_model

    def _validate_repair_checkpoint(self, checkpoint_path: Path) -> None:
        metadata_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".meta.json")
        if not metadata_path.is_file():
            raise FileNotFoundError(f"pose-repair gesture metadata not found: {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        prior = self.arm_pose_prior
        expected = {
            "gesture_model_sha256": sha256_file(checkpoint_path),
            "pose_repair_version": prior.version,
            "profile_fingerprint": prior.fingerprint,
            "pose_model_sha256": prior.pose_model_sha256,
            "repair_cache_revision": POSE_REPAIR_CACHE_REVISION,
            "training_pipeline_revision": POSE_REPAIR_TRAINING_PIPELINE_REVISION,
            "pose_hold_frames": int(settings.police_pose_hold_frames),
        }
        mismatched = [key for key, value in expected.items() if metadata.get(key) != value]
        if mismatched:
            raise ValueError(f"pose-repair gesture checkpoint metadata mismatch: {', '.join(mismatched)}")

    @property
    def yolo_model(self):
        if self._yolo_model is None:
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("YOLO pose backend requires: pip install ultralytics") from exc
            configured_path = settings.police_yolo_pose_model or "yolov8n-pose.pt"
            model_path = (settings.base_dir / configured_path).resolve()
            if not model_path.is_file():
                raise FileNotFoundError(f"police YOLO pose model not found: {model_path}")
            self._yolo_model = YOLO(str(model_path))
        return self._yolo_model

    @property
    def arm_pose_prior(self):
        if self._arm_pose_prior is None:
            profile_path = self.ctpgr_root / settings.police_pose_repair_stats
            if not profile_path.is_file():
                raise FileNotFoundError(f"police pose repair profile not found: {profile_path}")
            prior = load_arm_pose_prior(profile_path)
            model_path = (settings.base_dir / settings.police_yolo_pose_model).resolve()
            model_hash = sha256_file(model_path)
            if prior.pose_model_sha256 and prior.pose_model_sha256 != model_hash:
                raise ValueError("police pose repair profile was fitted with a different YOLO pose model")
            self._arm_pose_prior = prior
        return self._arm_pose_prior

    def _confidence(self, scores: np.ndarray, gesture_id: int) -> float:
        probs = torch.softmax(torch.from_numpy(scores.astype(np.float32)), dim=0).numpy()
        if 0 <= gesture_id < len(probs):
            return float(probs[gesture_id])
        return 0.0

    @staticmethod
    def _apply_gesture_gate(scores: np.ndarray, min_confidence: float, min_margin: float) -> int:
        """Reject uncertain non-zero gestures using validation-calibrated limits."""
        probs = torch.softmax(torch.from_numpy(np.asarray(scores, dtype=np.float32)), dim=0).numpy()
        return int(apply_gesture_probability_gate(probs, min_confidence, min_margin))

    def create_sequence_state(self) -> dict[str, Any]:
        try:
            return {
                "h": torch.zeros_like(self.g_model.h0()),
                "c": torch.zeros_like(self.g_model.c0()),
                "last_coord": None,
                "last_box": None,
                "missed_pose_frames": 0,
                "pose_repair_state": {},
                "classifier_backend": self.pose_backend,
                "pose_backend": self.pose_backend,
            }
        except FileNotFoundError:
            return {
                "h": None,
                "c": None,
                "last_coord": None,
                "last_box": None,
                "missed_pose_frames": 0,
                "pose_repair_state": {},
                "classifier_backend": self.pose_backend,
                "pose_backend": self.pose_backend,
            }

    @staticmethod
    def _select_person_index(boxes: np.ndarray, previous_box: np.ndarray | None = None) -> int:
        """Keep the same person across frames, falling back to the largest box."""
        return select_person_index(boxes, previous_box)

    @staticmethod
    def _reset_pose_track_state(state: dict[str, Any]) -> None:
        state["h"] = None
        state["c"] = None
        state["last_coord"] = None
        state["last_box"] = None
        state["missed_pose_frames"] = 0
        state["pose_repair_state"] = {}

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
            person_index, matched_previous = select_person_index_with_match(xyxy, previous_box)
            if state is not None:
                if previous_box is not None and not matched_previous:
                    self._reset_pose_track_state(state)
                state["last_box"] = xyxy[person_index].copy()

        coco = keypoints_xy[person_index]
        if settings.police_pose_repair_enabled:
            keypoints_confidence = getattr(results[0].keypoints, "conf", None)
            if keypoints_confidence is None:
                confidence = np.ones(17, dtype=np.float32)
            else:
                confidence = keypoints_confidence.cpu().numpy()[person_index]
            repair_state = state.setdefault("pose_repair_state", {}) if state is not None else {}
            repair_result = repair_coco_pose(
                coco,
                confidence,
                state=repair_state,
                prior=self.arm_pose_prior,
                image_size=self.input_size,
                hold_frames=settings.police_pose_hold_frames,
            )
            if state is not None:
                state["pose_quality"] = repair_result.quality
                state["repaired_joints"] = repair_result.repaired.copy()
            if not repair_result.usable:
                if state is not None:
                    state["last_coord"] = None
                    state["missed_pose_frames"] = 0
                raise ValueError("YOLO arm keypoints remained unreliable after repair")
            coco = repair_result.coordinates
        coord_norm = coco_to_ctpgr(coco, self.input_size)
        if state is not None:
            state["last_coord"] = coord_norm.copy()
            state["missed_pose_frames"] = 0
        return coord_norm

    @staticmethod
    def _reuse_recent_pose(state: dict[str, Any] | None) -> np.ndarray:
        hold_frames = max(0, int(settings.police_pose_hold_frames))
        if state is not None:
            missed = int(state.get("missed_pose_frames", 0))
            if state.get("last_coord") is not None and missed < hold_frames:
                state["missed_pose_frames"] = missed + 1
                return np.asarray(state["last_coord"], dtype=np.float32).copy()
            state["missed_pose_frames"] = missed + 1
            if state["missed_pose_frames"] > hold_frames:
                PoliceGestureService._reset_pose_track_state(state)
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
        # Missing/unusable pose is not a confident classifier decision.  A
        # confidence of 1.0 here would dominate video summaries and could make
        # the logging/alert pipeline report "no gesture 100%" merely because a
        # single frame was occluded.
        return self._plain_payload(ctpgr_image, 0, 0.0, None, reason)

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
        if state.get("classifier_backend") != self.pose_backend:
            state["h"] = None
            state["c"] = None
            state["classifier_backend"] = self.pose_backend
        if state.get("h") is None or state.get("c") is None:
            state["h"] = torch.zeros_like(self.g_model.h0())
            state["c"] = torch.zeros_like(self.g_model.c0())
        with self._model_lock, torch.no_grad():
            _, h, c, class_out = self.g_model(features, state["h"], state["c"])
        state["h"], state["c"] = h, c
        scores = class_out[0].cpu().numpy()
        if self.pose_backend == "yolo":
            gesture_id = self._apply_gesture_gate(
                scores,
                settings.police_gesture_min_confidence,
                settings.police_gesture_min_margin,
            )
        else:
            gesture_id = int(np.argmax(scores))
        return {self.pg.OUT_ARGMAX: gesture_id, self.pg.OUT_SCORES: scores, self.pg.COORD_NORM: coord_norm}

    def recognize_prepared_frame_continuous(
        self,
        ctpgr_image: np.ndarray,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state is not None and state.get("pose_backend") != self.pose_backend:
            self._reset_pose_track_state(state)
            state["pose_backend"] = self.pose_backend
            state["classifier_backend"] = self.pose_backend
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
