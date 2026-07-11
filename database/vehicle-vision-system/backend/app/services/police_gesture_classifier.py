"""Resolution-independent rules for police-gesture pose landmarks.

The classifier deliberately has no MediaPipe dependency so its behavior can be
tested from landmark-like objects.  All distances are scaled by shoulder width,
which makes an identical pose classify identically at different resolutions.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class GesturePrediction:
    gesture_id: int
    confidence: float


class PoliceGestureClassifier:
    """Classify the eight supported gestures from Pose landmarks.

    Landmark coordinates are expected to be MediaPipe-normalized coordinates
    (``x`` and ``y`` in the image range).  ``visibility`` is used to avoid
    assigning a gesture when an arm is occluded.
    """

    MIN_VISIBILITY = 0.55
    MIN_SHOULDER_WIDTH = 1e-3
    HIGH = 0.45
    LOW = 0.35
    HORIZONTAL = 0.28

    @staticmethod
    def _point(landmarks: Sequence[object], index: int) -> tuple[float, float, float]:
        landmark = landmarks[index]
        return (
            float(landmark.x),
            float(landmark.y),
            float(getattr(landmark, "visibility", 1.0)),
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    def classify(self, landmarks: Sequence[object]) -> GesturePrediction:
        """Return an id from 0--8 and a calibrated rule confidence."""
        if len(landmarks) <= 16:
            return GesturePrediction(0, 0.0)

        ls, rs = self._point(landmarks, 11), self._point(landmarks, 12)
        le, re = self._point(landmarks, 13), self._point(landmarks, 14)
        lw, rw = self._point(landmarks, 15), self._point(landmarks, 16)
        relevant = (ls, rs, le, re, lw, rw)
        if min(point[2] for point in relevant) < self.MIN_VISIBILITY:
            return GesturePrediction(0, 0.0)

        shoulder_width = math.dist(ls[:2], rs[:2])
        if shoulder_width < self.MIN_SHOULDER_WIDTH:
            return GesturePrediction(0, 0.0)

        shoulder_y = (ls[1] + rs[1]) / 2
        # Positive values mean that a wrist is above the shoulders.
        left_up = (shoulder_y - lw[1]) / shoulder_width
        right_up = (shoulder_y - rw[1]) / shoulder_width
        left_out = (ls[0] - lw[0]) / shoulder_width
        right_out = (rw[0] - rs[0]) / shoulder_width

        # These combinations are mutually exclusive and need precedence over
        # one-arm rules below.
        if left_up >= self.HIGH and right_up >= self.HIGH:
            return GesturePrediction(1, round(self._clamp(0.65 + 0.18 * min(left_up, right_up)), 3))
        if left_up >= self.HIGH and right_up <= -self.LOW:
            return GesturePrediction(4, round(self._clamp(0.70 + 0.14 * min(left_up, -right_up, 1.0)), 3))

        scores: dict[int, float] = {}
        if (
            abs(left_up) <= self.HORIZONTAL
            and abs(right_up) <= self.HORIZONTAL
            and left_out >= 0.25
            and right_out >= 0.25
        ):
            level = 1 - max(abs(left_up), abs(right_up)) / self.HORIZONTAL
            scores[2] = self._clamp(0.72 + 0.18 * level)
        if left_up >= self.HIGH and left_out >= 0.15 and right_up < self.LOW:
            scores[3] = self._clamp(0.67 + 0.18 * min(left_up, 1.0))
        if right_up >= self.HIGH and right_out >= 0.15 and left_up < self.LOW:
            scores[5] = self._clamp(0.67 + 0.18 * min(right_up, 1.0))
        if abs(lw[1] - rw[1]) / shoulder_width <= 0.16 and left_out >= 0.2 and right_out >= 0.2:
            scores[6] = self._clamp(0.62 + 0.18 * (1 - abs(lw[1] - rw[1]) / (0.16 * shoulder_width)))
        if left_up <= -self.LOW and right_up <= -self.LOW:
            scores[7] = self._clamp(0.67 + 0.18 * min(-left_up, -right_up, 1.0))
        if max(-left_up, -right_up) >= self.LOW:
            scores[8] = self._clamp(0.58 + 0.18 * max(-left_up, -right_up, 0.0))

        if not scores:
            return GesturePrediction(0, 0.0)
        gesture_id = max(scores, key=scores.get)
        return GesturePrediction(gesture_id, round(scores[gesture_id], 3))
