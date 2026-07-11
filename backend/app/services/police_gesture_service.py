import io
from collections import Counter, deque
from typing import Any
import cv2
import numpy as np
import mediapipe as mp
from PIL import Image, ImageSequence
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from app.utils.helpers import ndarray_to_base64
from app.utils.model_loader import get_model_path
from app.services.police_gesture_classifier import PoliceGestureClassifier


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

POSE_CONNECTIONS = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
]


class PoliceGestureService:
    def __init__(self):
        options = vision.PoseLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=get_model_path("pose_landmarker_lite.task")),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=1,
        )
        self.landmarker = vision.PoseLandmarker.create_from_options(options)
        self.classifier = PoliceGestureClassifier()
        self._history: deque[tuple[int, float]] = deque(maxlen=5)

    def _lm(self, landmarks, idx, w, h):
        lm = landmarks[idx]
        return lm.x * w, lm.y * h, lm.visibility

    def _classify_gesture(self, landmarks, w, h) -> tuple[int, float]:
        prediction = self.classifier.classify(landmarks)
        return prediction.gesture_id, prediction.confidence

    def _smooth_prediction(self, gesture_id: int, confidence: float) -> tuple[int, float]:
        """Use a short weighted vote to suppress one-frame pose jitter."""
        if gesture_id == 0:
            self._history.clear()
            return 0, confidence
        self._history.append((gesture_id, confidence))
        votes: dict[int, float] = {}
        for recorded_id, recorded_confidence in self._history:
            votes[recorded_id] = votes.get(recorded_id, 0.0) + recorded_confidence
        voted_id = max(votes, key=votes.get)
        # A single frame is still returned so image uploads remain responsive;
        # video streams become stable as more frames arrive.
        voted_confidence = votes[voted_id] / sum(1 for item in self._history if item[0] == voted_id)
        return voted_id, round(voted_confidence, 3)

    def _draw_skeleton(self, image, landmarks, w, h):
        for a, b in POSE_CONNECTIONS:
            if a < len(landmarks) and b < len(landmarks):
                ax, ay = int(landmarks[a].x * w), int(landmarks[a].y * h)
                bx, by = int(landmarks[b].x * w), int(landmarks[b].y * h)
                cv2.line(image, (ax, ay), (bx, by), (0, 255, 0), 2)
        for lm in landmarks:
            cv2.circle(image, (int(lm.x * w), int(lm.y * h)), 4, (0, 200, 255), -1)

    def _extract_keypoints(self, landmarks, w, h) -> list[dict]:
        return [{"id": i, "x": round(lm.x * w, 2), "y": round(lm.y * h, 2), "z": round(lm.z, 4), "visibility": round(lm.visibility, 3)} for i, lm in enumerate(landmarks)]

    def _detect_best_frame(self, image_bytes: bytes) -> np.ndarray:
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            if getattr(pil_img, "is_animated", False):
                best_frame = None
                best_score = -1.0
                for frame in ImageSequence.Iterator(pil_img):
                    frame_rgb = frame.convert("RGB")
                    frame_np = cv2.cvtColor(np.array(frame_rgb), cv2.COLOR_RGB2BGR)
                    score = cv2.Laplacian(cv2.cvtColor(frame_np, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                    if score > best_score:
                        best_score = score
                        best_frame = frame_np
                if best_frame is not None:
                    return best_frame
            frame_rgb = pil_img.convert("RGB")
            return cv2.cvtColor(np.array(frame_rgb), cv2.COLOR_RGB2BGR)
        except Exception:
            pass
        nparr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解析图像")
        return image

    def recognize(self, image_bytes: bytes) -> dict[str, Any]:
        image = self._detect_best_frame(image_bytes)

        h, w = image.shape[:2]
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self.landmarker.detect(mp_image)
        annotated = image.copy()

        gesture_id, confidence = 0, 0.0
        keypoints = []
        if result.pose_landmarks:
            landmarks = result.pose_landmarks[0]
            self._draw_skeleton(annotated, landmarks, w, h)
            gesture_id, confidence = self._classify_gesture(landmarks, w, h)
            gesture_id, confidence = self._smooth_prediction(gesture_id, confidence)
            keypoints = self._extract_keypoints(landmarks, w, h)

        en, cn = POLICE_GESTURES[gesture_id]
        if gesture_id != 0:
            cv2.putText(annotated, f"{cn} ({confidence:.0%})", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)

        return {
            "gesture": en,
            "gesture_cn": cn,
            "gesture_id": gesture_id,
            "confidence": round(confidence, 3),
            "keypoints": keypoints,
            "annotated_image": ndarray_to_base64(annotated),
            "success": gesture_id > 0,
        }

    def recognize_frame(self, frame: np.ndarray) -> dict[str, Any]:
        _, buf = cv2.imencode(".jpg", frame)
        return self.recognize(buf.tobytes())


police_gesture_service = PoliceGestureService()
