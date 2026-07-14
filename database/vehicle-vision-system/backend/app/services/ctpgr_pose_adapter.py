"""Adapt and repair COCO pose coordinates for the CTPGR gesture model.

The original adapter deliberately kept every YOLO coordinate, including a
low-confidence argmax produced for an occluded joint.  That behaviour is kept
in :func:`coco_to_ctpgr` for backwards compatibility.  The stateful
``repair_coco_pose`` layer is opt-in and runs before quantization so training
and inference can use the same confidence and body-geometry constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np


CTPGR_HEATMAP_SIZE = 64
POSE_REPAIR_VERSION = "arm-confidence-v1"
POSE_REPAIR_CACHE_REVISION = "3"
POSE_REPAIR_TRAINING_PIPELINE_REVISION = "timeline-delay-v1"
PERSON_TRACK_MIN_IOU = 0.20

# COCO joint order: right arm first here to match the CTPGR 14-joint order.
COCO_ARM_JOINTS = ((6, 8, 10), (5, 7, 9))
ARM_NAMES = ("right", "left")
COCO_TORSO_JOINTS = (5, 6, 11, 12)
DEFAULT_CONFIDENCE_THRESHOLDS = {
    "shoulder": 0.35,
    "elbow": 0.40,
    "wrist": 0.45,
}


def _as_pair(value: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return fallback
    return values if len(values) == 2 and np.isfinite(values).all() else fallback


@dataclass(frozen=True)
class ArmPosePrior:
    """Robust CTPGR-training-set statistics used by the arm repair layer.

    Segment lengths are divided by a body scale: the larger of shoulder width
    and half shoulder-to-hip height.  This remains stable when a side-facing
    officer's apparent shoulder width becomes very small.  Angles are radians.
    """

    shoulder_width_median: float = 0.09
    body_scale_median: float = 0.12
    upper_median: tuple[float, float] = (0.85, 0.90)
    upper_low: tuple[float, float] = (0.15, 0.15)
    upper_high: tuple[float, float] = (4.00, 4.00)
    lower_median: tuple[float, float] = (0.80, 0.82)
    lower_low: tuple[float, float] = (0.15, 0.15)
    lower_high: tuple[float, float] = (4.00, 4.00)
    angle_low: tuple[float, float] = (math.radians(8.0), math.radians(8.0))
    angle_high: tuple[float, float] = (math.pi, math.pi)
    upper_delta_high: tuple[float, float] = (0.28, 0.28)
    lower_delta_high: tuple[float, float] = (0.28, 0.28)
    angle_delta_high: tuple[float, float] = (math.radians(70.0), math.radians(70.0))
    direction_delta_high: tuple[float, float] = (math.radians(65.0), math.radians(65.0))
    sample_count: int = 0
    source: str = "safe-default"
    pose_model_sha256: str = ""
    version: str = POSE_REPAIR_VERSION

    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "sample_count": int(self.sample_count),
            "pose_model_sha256": self.pose_model_sha256,
            "shoulder_width_median": float(self.shoulder_width_median),
            "body_scale_median": float(self.body_scale_median),
            "upper_median": list(self.upper_median),
            "upper_low": list(self.upper_low),
            "upper_high": list(self.upper_high),
            "lower_median": list(self.lower_median),
            "lower_low": list(self.lower_low),
            "lower_high": list(self.lower_high),
            "angle_low": list(self.angle_low),
            "angle_high": list(self.angle_high),
            "upper_delta_high": list(self.upper_delta_high),
            "lower_delta_high": list(self.lower_delta_high),
            "angle_delta_high": list(self.angle_delta_high),
            "direction_delta_high": list(self.direction_delta_high),
            "confidence_thresholds": dict(DEFAULT_CONFIDENCE_THRESHOLDS),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArmPosePrior":
        default = cls()
        prior = cls(
            shoulder_width_median=float(data.get("shoulder_width_median", default.shoulder_width_median)),
            body_scale_median=float(data.get("body_scale_median", default.body_scale_median)),
            upper_median=_as_pair(data.get("upper_median"), default.upper_median),
            upper_low=_as_pair(data.get("upper_low"), default.upper_low),
            upper_high=_as_pair(data.get("upper_high"), default.upper_high),
            lower_median=_as_pair(data.get("lower_median"), default.lower_median),
            lower_low=_as_pair(data.get("lower_low"), default.lower_low),
            lower_high=_as_pair(data.get("lower_high"), default.lower_high),
            angle_low=_as_pair(data.get("angle_low"), default.angle_low),
            angle_high=_as_pair(data.get("angle_high"), default.angle_high),
            upper_delta_high=_as_pair(data.get("upper_delta_high"), default.upper_delta_high),
            lower_delta_high=_as_pair(data.get("lower_delta_high"), default.lower_delta_high),
            angle_delta_high=_as_pair(data.get("angle_delta_high"), default.angle_delta_high),
            direction_delta_high=_as_pair(data.get("direction_delta_high"), default.direction_delta_high),
            sample_count=max(0, int(data.get("sample_count", 0))),
            source=str(data.get("source", "loaded-profile")),
            pose_model_sha256=str(data.get("pose_model_sha256", "")),
            version=str(data.get("version", POSE_REPAIR_VERSION)),
        )
        expected = data.get("fingerprint")
        if expected and expected != prior.fingerprint:
            raise ValueError("arm pose prior fingerprint does not match its contents")
        if prior.version != POSE_REPAIR_VERSION:
            raise ValueError(f"unsupported pose repair profile version: {prior.version}")
        return prior


@dataclass(frozen=True)
class PoseRepairResult:
    coordinates: np.ndarray
    reliable: np.ndarray
    repaired: np.ndarray
    arm_valid: tuple[bool, bool]
    usable: bool
    quality: float


def save_arm_pose_prior(prior: ArmPosePrior, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prior.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_arm_pose_prior(path: Path | str | None) -> ArmPosePrior:
    if path is None:
        return ArmPosePrior()
    path = Path(path)
    if not path.is_file():
        return ArmPosePrior(source=f"safe-default (missing {path.name})")
    return ArmPosePrior.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _robust_summary(
    values: np.ndarray,
    fallback: tuple[float, float, float],
    hard_limits: tuple[float, float],
) -> tuple[float, float, float]:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    values = values[(values >= hard_limits[0]) & (values <= hard_limits[1])]
    if len(values) < 32:
        return fallback
    median = float(np.median(values))
    low, high = np.quantile(values, (0.001, 0.99))
    # Expand the empirical interval slightly so normal fast gestures are not
    # mistaken for occlusion merely because they sit at a training percentile.
    low = max(hard_limits[0], float(low) * 0.80)
    high = min(hard_limits[1], float(high) * 1.15)
    if not low < median < high:
        return fallback
    return median, low, high


def _robust_delta_high(
    values: np.ndarray,
    fallback: float,
    hard_maximum: float,
    minimum: float,
) -> float:
    """Keep normal motion while excluding the extreme jitter tail."""

    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values) & (values >= 0.0) & (values <= hard_maximum)]
    if len(values) < 32:
        return fallback
    median = float(np.median(values))
    robust_sigma = 1.4826 * float(np.median(np.abs(values - median)))
    high = max(float(np.quantile(values, 0.975)) * 1.10, median + 15.0 * robust_sigma, minimum)
    return min(hard_maximum, high)


def fit_arm_pose_prior(
    coordinate_batches: Iterable[np.ndarray],
    confidence_batches: Iterable[np.ndarray],
    image_size: tuple[int, int] = (512, 512),
    source: str = "CTPGR train split",
    pose_model_sha256: str = "",
) -> ArmPosePrior:
    """Fit a robust profile using only confident training-set arm joints."""

    coordinates = [np.asarray(batch, dtype=np.float32) for batch in coordinate_batches]
    confidences = [np.asarray(batch, dtype=np.float32) for batch in confidence_batches]
    if not coordinates or len(coordinates) != len(confidences):
        return ArmPosePrior(source=f"safe-default ({source}: no samples)")
    for xy, conf in zip(coordinates, confidences):
        if xy.ndim != 3 or xy.shape[1:] != (17, 2):
            raise ValueError(f"expected COCO coordinate batches shaped (F, 17, 2), got {xy.shape}")
        if conf.shape != xy.shape[:2]:
            raise ValueError(f"expected confidence batches shaped {xy.shape[:2]}, got {conf.shape}")

    xy = np.concatenate(coordinates, axis=0)
    conf = np.concatenate(confidences, axis=0)
    width, height = image_size
    max_dimension = float(max(width, height))
    finite = np.isfinite(xy).all(axis=2) & np.isfinite(conf)
    inside = (
        (xy[:, :, 0] >= 0)
        & (xy[:, :, 0] <= width)
        & (xy[:, :, 1] >= 0)
        & (xy[:, :, 1] <= height)
    )
    confident = finite & inside & (conf >= 0.60)
    shoulder_ok = confident[:, 5] & confident[:, 6]
    shoulder_width = np.linalg.norm(xy[:, 6] - xy[:, 5], axis=1)
    shoulder_center = (xy[:, 5] + xy[:, 6]) / 2
    hip_center = (xy[:, 11] + xy[:, 12]) / 2
    torso_height = np.linalg.norm(shoulder_center - hip_center, axis=1)
    hips_ok = confident[:, 11] & confident[:, 12]
    body_scale = np.maximum(shoulder_width, np.where(hips_ok, 0.5 * torso_height, 0.0))
    normalized_shoulder = shoulder_width / max_dimension
    normalized_body_scale = body_scale / max_dimension
    shoulder_values = normalized_shoulder[
        shoulder_ok & (normalized_shoulder >= 0.025) & (normalized_shoulder <= 0.35)
    ]
    if len(shoulder_values) >= 32:
        shoulder_median = float(np.median(shoulder_values))
    else:
        shoulder_median = ArmPosePrior().shoulder_width_median
    body_scale_values = normalized_body_scale[
        shoulder_ok & (normalized_body_scale >= 0.025) & (normalized_body_scale <= 0.50)
    ]
    body_scale_median = (
        float(np.median(body_scale_values))
        if len(body_scale_values) >= 32
        else ArmPosePrior().body_scale_median
    )

    upper_rows: list[tuple[float, float, float]] = []
    lower_rows: list[tuple[float, float, float]] = []
    angle_rows: list[tuple[float, float, float]] = []
    upper_delta_values: list[list[np.ndarray]] = [[], []]
    lower_delta_values: list[list[np.ndarray]] = [[], []]
    angle_delta_values: list[list[np.ndarray]] = [[], []]
    direction_delta_values: list[list[np.ndarray]] = [[], []]
    default = ArmPosePrior()
    for batch_xy, batch_conf in zip(coordinates, confidences):
        batch_finite = np.isfinite(batch_xy).all(axis=2) & np.isfinite(batch_conf)
        batch_confident = batch_finite & (batch_conf >= 0.60)
        batch_shoulder_width = np.linalg.norm(batch_xy[:, 6] - batch_xy[:, 5], axis=1)
        batch_shoulder_center = (batch_xy[:, 5] + batch_xy[:, 6]) / 2
        batch_hip_center = (batch_xy[:, 11] + batch_xy[:, 12]) / 2
        batch_torso_height = np.linalg.norm(batch_shoulder_center - batch_hip_center, axis=1)
        batch_hips_ok = batch_confident[:, 11] & batch_confident[:, 12]
        batch_body_scale = np.maximum(
            batch_shoulder_width,
            np.where(batch_hips_ok, 0.5 * batch_torso_height, 0.0),
        )
        batch_scale_ok = (
            batch_confident[:, 5]
            & batch_confident[:, 6]
            & (batch_body_scale >= 0.025 * max_dimension)
            & (batch_body_scale <= 0.50 * max_dimension)
        )
        for side, (shoulder, elbow, wrist) in enumerate(COCO_ARM_JOINTS):
            upper_length = np.linalg.norm(batch_xy[:, shoulder] - batch_xy[:, elbow], axis=1)
            lower_length = np.linalg.norm(batch_xy[:, elbow] - batch_xy[:, wrist], axis=1)
            toward_shoulder = batch_xy[:, shoulder] - batch_xy[:, elbow]
            toward_wrist = batch_xy[:, wrist] - batch_xy[:, elbow]
            denominator = np.linalg.norm(toward_shoulder, axis=1) * np.linalg.norm(toward_wrist, axis=1)
            angle = np.arccos(
                np.clip(np.sum(toward_shoulder * toward_wrist, axis=1) / np.maximum(denominator, 1e-6), -1.0, 1.0)
            )
            upper_ok = batch_scale_ok & batch_confident[:, shoulder] & batch_confident[:, elbow]
            lower_ok = batch_scale_ok & batch_confident[:, elbow] & batch_confident[:, wrist]
            angle_ok = upper_ok & batch_confident[:, wrist]
            if len(batch_xy) > 1:
                scale = np.maximum(batch_body_scale[1:], 1e-6)
                consecutive_upper = upper_ok[1:] & upper_ok[:-1]
                consecutive_lower = lower_ok[1:] & lower_ok[:-1]
                consecutive_angle = angle_ok[1:] & angle_ok[:-1]
                upper_delta_values[side].append(np.abs(np.diff(upper_length))[consecutive_upper] / scale[consecutive_upper])
                lower_delta_values[side].append(np.abs(np.diff(lower_length))[consecutive_lower] / scale[consecutive_lower])
                angle_delta_values[side].append(np.abs(np.diff(angle))[consecutive_angle])
                previous_direction = toward_wrist[:-1]
                current_direction = toward_wrist[1:]
                dot = np.sum(previous_direction * current_direction, axis=1)
                cross = previous_direction[:, 0] * current_direction[:, 1] - previous_direction[:, 1] * current_direction[:, 0]
                direction_delta = np.abs(np.arctan2(cross, dot))
                direction_delta_values[side].append(direction_delta[consecutive_lower])
    for side, (shoulder, elbow, wrist) in enumerate(COCO_ARM_JOINTS):
        scale_ok = shoulder_ok & (body_scale > 1e-6)
        upper_mask = scale_ok & confident[:, shoulder] & confident[:, elbow]
        lower_mask = scale_ok & confident[:, elbow] & confident[:, wrist]
        angle_mask = upper_mask & confident[:, wrist]
        upper_ratio = np.linalg.norm(xy[:, shoulder] - xy[:, elbow], axis=1) / np.maximum(body_scale, 1e-6)
        lower_ratio = np.linalg.norm(xy[:, elbow] - xy[:, wrist], axis=1) / np.maximum(body_scale, 1e-6)
        upper_rows.append(
            _robust_summary(
                upper_ratio[upper_mask],
                (default.upper_median[side], default.upper_low[side], default.upper_high[side]),
                (0.03, 6.00),
            )
        )
        lower_rows.append(
            _robust_summary(
                lower_ratio[lower_mask],
                (default.lower_median[side], default.lower_low[side], default.lower_high[side]),
                (0.03, 6.00),
            )
        )
        toward_shoulder = xy[:, shoulder] - xy[:, elbow]
        toward_wrist = xy[:, wrist] - xy[:, elbow]
        denominator = np.linalg.norm(toward_shoulder, axis=1) * np.linalg.norm(toward_wrist, axis=1)
        cosine = np.sum(toward_shoulder * toward_wrist, axis=1) / np.maximum(denominator, 1e-6)
        angle = np.arccos(np.clip(cosine, -1.0, 1.0))
        angle_rows.append(
            _robust_summary(
                angle[angle_mask],
                (default.angle_high[side] / 2, default.angle_low[side], default.angle_high[side]),
                (math.radians(5.0), math.pi),
            )
        )

    return ArmPosePrior(
        shoulder_width_median=shoulder_median,
        body_scale_median=body_scale_median,
        upper_median=tuple(row[0] for row in upper_rows),
        upper_low=tuple(row[1] for row in upper_rows),
        upper_high=tuple(row[2] for row in upper_rows),
        lower_median=tuple(row[0] for row in lower_rows),
        lower_low=tuple(row[1] for row in lower_rows),
        lower_high=tuple(row[2] for row in lower_rows),
        angle_low=tuple(row[1] for row in angle_rows),
        angle_high=tuple(row[2] for row in angle_rows),
        upper_delta_high=tuple(
            _robust_delta_high(
                np.concatenate(upper_delta_values[side]) if upper_delta_values[side] else np.empty(0),
                default.upper_delta_high[side],
                1.5,
                0.15,
            )
            for side in range(2)
        ),
        lower_delta_high=tuple(
            _robust_delta_high(
                np.concatenate(lower_delta_values[side]) if lower_delta_values[side] else np.empty(0),
                default.lower_delta_high[side],
                1.5,
                0.15,
            )
            for side in range(2)
        ),
        angle_delta_high=tuple(
            _robust_delta_high(
                np.concatenate(angle_delta_values[side]) if angle_delta_values[side] else np.empty(0),
                default.angle_delta_high[side],
                math.pi,
                math.radians(25.0),
            )
            for side in range(2)
        ),
        direction_delta_high=tuple(
            _robust_delta_high(
                np.concatenate(direction_delta_values[side]) if direction_delta_values[side] else np.empty(0),
                default.direction_delta_high[side],
                math.pi,
                math.radians(25.0),
            )
            for side in range(2)
        ),
        sample_count=int(len(xy)),
        source=source,
        pose_model_sha256=pose_model_sha256,
    )


def _unit(vector: np.ndarray, fallback: np.ndarray | None = None) -> np.ndarray:
    length = float(np.linalg.norm(vector))
    if length > 1e-6 and np.isfinite(length):
        return np.asarray(vector, dtype=np.float32) / length
    if fallback is not None:
        return _unit(np.asarray(fallback, dtype=np.float32), np.array([1.0, 0.0], dtype=np.float32))
    return np.array([1.0, 0.0], dtype=np.float32)


def _rotate(vector: np.ndarray, angle: float) -> np.ndarray:
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array(
        [cosine * vector[0] - sine * vector[1], sine * vector[0] + cosine * vector[1]],
        dtype=np.float32,
    )


def _signed_angle(first: np.ndarray, second: np.ndarray) -> float:
    first_unit, second_unit = _unit(first), _unit(second)
    return math.atan2(
        float(first_unit[0] * second_unit[1] - first_unit[1] * second_unit[0]),
        float(np.dot(first_unit, second_unit)),
    )


def _elbow_angle(shoulder: np.ndarray, elbow: np.ndarray, wrist: np.ndarray) -> float:
    first, second = shoulder - elbow, wrist - elbow
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator <= 1e-6:
        return 0.0
    return math.acos(float(np.clip(np.dot(first, second) / denominator, -1.0, 1.0)))


def _circle_intersections(
    first_center: np.ndarray,
    first_radius: float,
    second_center: np.ndarray,
    second_radius: float,
) -> list[np.ndarray]:
    difference = second_center - first_center
    distance = float(np.linalg.norm(difference))
    if distance <= 1e-6 or distance > first_radius + second_radius or distance < abs(first_radius - second_radius):
        return []
    along = (first_radius**2 - second_radius**2 + distance**2) / (2 * distance)
    height_squared = max(0.0, first_radius**2 - along**2)
    midpoint = first_center + difference * (along / distance)
    perpendicular = np.array([-difference[1], difference[0]], dtype=np.float32) / distance
    offset = perpendicular * math.sqrt(height_squared)
    return [midpoint + offset, midpoint - offset]


def select_person_index_with_match(
    boxes: np.ndarray,
    previous_box: np.ndarray | None = None,
) -> tuple[int, bool]:
    """Return selected index and whether it still matches ``previous_box``."""

    boxes = np.asarray(boxes, dtype=np.float32)
    if boxes.ndim != 2 or boxes.shape[1] != 4 or len(boxes) == 0:
        raise ValueError("expected one or more person boxes shaped (N, 4)")
    areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    if previous_box is None:
        return int(np.argmax(areas)), True
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
    if iou[best_match] >= PERSON_TRACK_MIN_IOU:
        return best_match, True
    return int(np.argmax(areas)), False


def select_person_index(boxes: np.ndarray, previous_box: np.ndarray | None = None) -> int:
    """Select a stable person across frames, falling back to the largest box."""

    return select_person_index_with_match(boxes, previous_box)[0]


def apply_gesture_probability_gate(
    probabilities: np.ndarray,
    min_confidence: float,
    min_margin: float,
) -> np.ndarray:
    """Apply the production no-gesture rejection rule to one or more rows."""

    probabilities = np.asarray(probabilities, dtype=np.float32)
    squeeze = probabilities.ndim == 1
    if squeeze:
        probabilities = probabilities[np.newaxis]
    if probabilities.ndim != 2 or probabilities.shape[1] < 2:
        raise ValueError("expected gesture probabilities shaped (N, C) with C >= 2")
    predictions = probabilities.argmax(axis=1)
    ordered = np.partition(probabilities, -2, axis=1)
    confidence = ordered[:, -1]
    margin = confidence - ordered[:, -2]
    rejected = (predictions != 0) & ((confidence < min_confidence) | (margin < min_margin))
    predictions[rejected] = 0
    return predictions[0] if squeeze else predictions


def _constrain_wrist_angle(
    shoulder: np.ndarray,
    elbow: np.ndarray,
    wrist: np.ndarray,
    length: float,
    minimum: float,
    maximum: float,
    reference: np.ndarray | None,
) -> np.ndarray:
    angle = _elbow_angle(shoulder, elbow, wrist)
    if minimum <= angle <= maximum:
        return elbow + _unit(wrist - elbow, elbow - shoulder) * length
    target = float(np.clip(angle, minimum, maximum))
    toward_shoulder = _unit(shoulder - elbow)
    candidates = [
        elbow + _rotate(toward_shoulder, target) * length,
        elbow + _rotate(toward_shoulder, -target) * length,
    ]
    target_point = wrist if reference is None else reference
    return min(candidates, key=lambda point: float(np.linalg.norm(point - target_point)))


def repair_coco_pose(
    coco: np.ndarray,
    confidence: np.ndarray,
    state: dict[str, Any] | None = None,
    prior: ArmPosePrior | None = None,
    image_size: tuple[int, int] = (512, 512),
    hold_frames: int = 5,
) -> PoseRepairResult:
    """Repair unreliable shoulder/elbow/wrist points before CTPGR mapping.

    High-confidence, anatomically valid joints pass through unchanged.  Only a
    low-confidence or geometric outlier is reconstructed and temporally
    smoothed.  ``state`` belongs to one video/stream and must not be shared
    between people.
    """

    xy = np.asarray(coco, dtype=np.float32)
    confidence = np.asarray(confidence, dtype=np.float32)
    if xy.shape != (17, 2):
        raise ValueError(f"expected 17 COCO keypoints, got {xy.shape}")
    if confidence.shape != (17,):
        raise ValueError(f"expected 17 keypoint confidences, got {confidence.shape}")
    if not np.isfinite(xy).all() or not np.isfinite(confidence).all():
        raise ValueError("YOLO pose returned non-finite coordinates or confidences")
    if state is None:
        state = {}
    prior = prior or ArmPosePrior()
    hold_frames = max(0, int(hold_frames))
    width, height = image_size
    max_dimension = float(max(width, height))
    inside = (
        (xy[:, 0] >= -0.05 * width)
        & (xy[:, 0] <= 1.05 * width)
        & (xy[:, 1] >= -0.05 * height)
        & (xy[:, 1] <= 1.05 * height)
    )

    previous = state.get("previous_xy")
    if previous is not None:
        previous = np.asarray(previous, dtype=np.float32)
        if previous.shape != (17, 2):
            previous = None
    previous_reliable = np.asarray(state.get("previous_reliable", np.zeros(17, dtype=np.bool_)), dtype=np.bool_)
    if previous_reliable.shape != (17,):
        previous_reliable = np.zeros(17, dtype=np.bool_)

    torso_differences = []
    if previous is not None:
        for joint in COCO_TORSO_JOINTS:
            if inside[joint] and confidence[joint] >= 0.35 and previous_reliable[joint]:
                torso_differences.append(xy[joint] - previous[joint])
    body_shift = (
        np.median(np.asarray(torso_differences, dtype=np.float32), axis=0)
        if torso_differences
        else np.zeros(2, dtype=np.float32)
    )
    predicted = previous + body_shift if previous is not None else xy.copy()
    repaired_xy = xy.copy()
    repaired_mask = np.zeros(17, dtype=np.bool_)
    reliable = inside & (confidence >= 0.20)
    arm_valid: list[bool] = []

    # Repair shoulders first because they provide the scale and anchor for both
    # distal joints.  A reliable opposite shoulder helps preserve body motion.
    for shoulder in (6, 5):
        shoulder_good = bool(inside[shoulder] and confidence[shoulder] >= DEFAULT_CONFIDENCE_THRESHOLDS["shoulder"])
        if shoulder_good and previous is not None and previous_reliable[shoulder]:
            residual = float(np.linalg.norm(xy[shoulder] - predicted[shoulder]))
            shoulder_good = residual <= max(0.12 * max_dimension, 2.0)
        if not shoulder_good and previous is not None:
            repaired_xy[shoulder] = predicted[shoulder]
            repaired_mask[shoulder] = True
        reliable[shoulder] = shoulder_good

    current_shoulder_width = float(np.linalg.norm(repaired_xy[6] - repaired_xy[5]))
    shoulder_center = (repaired_xy[5] + repaired_xy[6]) / 2
    hips_reliable = bool(
        inside[11]
        and inside[12]
        and confidence[11] >= 0.35
        and confidence[12] >= 0.35
    )
    hip_center = (xy[11] + xy[12]) / 2
    torso_height = float(np.linalg.norm(shoulder_center - hip_center)) if hips_reliable else 0.0
    current_body_scale = max(current_shoulder_width, 0.5 * torso_height)
    last_scale = float(state.get("last_body_scale", 0.0))
    plausible_scale = 0.02 * max_dimension <= current_body_scale <= 0.50 * max_dimension
    scale = current_body_scale if plausible_scale else last_scale
    if scale <= 1e-6:
        scale = prior.body_scale_median * max_dimension
    if plausible_scale and reliable[5] and reliable[6]:
        state["last_shoulder_width"] = current_shoulder_width
        state["last_body_scale"] = current_body_scale

    occluded_counts = list(state.get("arm_occluded_frames", [0, 0]))
    if len(occluded_counts) != 2:
        occluded_counts = [0, 0]
    last_reliable_arms = state.setdefault("last_reliable_arms", {})
    reacquire_state = state.setdefault("arm_reacquire", {})

    for side, (shoulder, elbow, wrist) in enumerate(COCO_ARM_JOINTS):
        saved_arm = last_reliable_arms.get(ARM_NAMES[side])
        if saved_arm is not None:
            saved_arm = np.asarray(saved_arm, dtype=np.float32)
            if saved_arm.shape != (3, 2):
                saved_arm = None
        if saved_arm is not None and saved_arm.shape == (3, 2):
            expected_upper = max(2.0, float(np.linalg.norm(saved_arm[1] - saved_arm[0])))
            expected_lower = max(2.0, float(np.linalg.norm(saved_arm[2] - saved_arm[1])))
        else:
            expected_upper = max(2.0, prior.upper_median[side] * scale)
            expected_lower = max(2.0, prior.lower_median[side] * scale)
        upper_low, upper_high = prior.upper_low[side] * scale, prior.upper_high[side] * scale
        lower_low, lower_high = prior.lower_low[side] * scale, prior.lower_high[side] * scale
        shoulder_point = repaired_xy[shoulder]
        predicted_elbow = predicted[elbow] if previous is not None else xy[elbow]
        predicted_wrist = predicted[wrist] if previous is not None else xy[wrist]
        if previous is not None and previous_reliable[shoulder] and previous_reliable[elbow]:
            anchor_shift = shoulder_point - previous[shoulder]
            predicted_elbow = previous[elbow] + anchor_shift
            predicted_wrist = previous[wrist] + anchor_shift

        upper_length = float(np.linalg.norm(xy[elbow] - shoulder_point))
        upper_geometry_ok = upper_low <= upper_length <= upper_high
        elbow_jump = float(np.linalg.norm(xy[elbow] - predicted_elbow)) if previous is not None else 0.0
        upper_temporal_outlier = False
        if previous is not None and previous_reliable[shoulder] and previous_reliable[elbow]:
            previous_upper_length = float(np.linalg.norm(previous[elbow] - previous[shoulder]))
            upper_temporal_outlier = (
                elbow_jump > 0.70 * scale
                and abs(upper_length - previous_upper_length) > prior.upper_delta_high[side] * scale
            )
        elbow_good = bool(
            inside[elbow]
            and confidence[elbow] >= DEFAULT_CONFIDENCE_THRESHOLDS["elbow"]
            and upper_geometry_ok
            and not upper_temporal_outlier
        )

        raw_lower_length = float(np.linalg.norm(xy[wrist] - xy[elbow]))
        lower_geometry_ok = lower_low <= raw_lower_length <= lower_high
        wrist_jump = float(np.linalg.norm(xy[wrist] - predicted_wrist)) if previous is not None else 0.0
        angle = _elbow_angle(shoulder_point, xy[elbow], xy[wrist])
        angle_ok = prior.angle_low[side] <= angle <= prior.angle_high[side]
        wrist_measurement_good = bool(
            inside[wrist]
            and confidence[wrist] >= DEFAULT_CONFIDENCE_THRESHOLDS["wrist"]
        )
        wrist_temporal_outlier = False
        if (
            previous is not None
            and elbow_good
            and previous_reliable[shoulder]
            and previous_reliable[elbow]
            and previous_reliable[wrist]
        ):
            previous_lower_length = float(np.linalg.norm(previous[wrist] - previous[elbow]))
            previous_angle = _elbow_angle(previous[shoulder], previous[elbow], previous[wrist])
            wrist_temporal_outlier = (
                wrist_jump > 0.85 * scale
                and (
                    abs(raw_lower_length - previous_lower_length) > prior.lower_delta_high[side] * scale
                    or abs(angle - previous_angle) > prior.angle_delta_high[side]
                    or abs(_signed_angle(previous[wrist] - previous[elbow], xy[wrist] - xy[elbow]))
                    > prior.direction_delta_high[side]
                )
            )
        elif previous is not None and previous_reliable[shoulder] and previous_reliable[wrist]:
            previous_reach = previous[wrist] - previous[shoulder]
            current_reach = xy[wrist] - shoulder_point
            wrist_temporal_outlier = (
                wrist_jump > 1.10 * scale
                and (
                    abs(float(np.linalg.norm(current_reach) - np.linalg.norm(previous_reach)))
                    > prior.lower_delta_high[side] * scale
                    or abs(_signed_angle(previous_reach, current_reach)) > prior.direction_delta_high[side]
                )
            )
        if elbow_good:
            wrist_good = bool(wrist_measurement_good and lower_geometry_ok and angle_ok and not wrist_temporal_outlier)
        else:
            shoulder_to_wrist = float(np.linalg.norm(xy[wrist] - shoulder_point))
            reachable_low = max(0.0, abs(expected_upper - expected_lower) - 0.15 * scale)
            reachable_high = expected_upper + expected_lower + 0.15 * scale
            wrist_good = bool(
                wrist_measurement_good
                and reachable_low <= shoulder_to_wrist <= reachable_high
                and not wrist_temporal_outlier
            )

        previous_arm_reliable = bool(
            previous is None
            or (
                previous_reliable[shoulder]
                and previous_reliable[elbow]
                and previous_reliable[wrist]
            )
        )
        raw_arm_measurement_good = bool(
            reliable[shoulder]
            and inside[elbow]
            and inside[wrist]
            and confidence[elbow] >= DEFAULT_CONFIDENCE_THRESHOLDS["elbow"]
            and confidence[wrist] >= DEFAULT_CONFIDENCE_THRESHOLDS["wrist"]
            and upper_geometry_ok
            and lower_geometry_ok
            and angle_ok
        )
        # After an unreliable measurement, require two mutually consistent
        # high-quality raw frames before accepting a potentially new pose.
        # This prevents one stable-looking hallucination from immediately
        # replacing the last reliable arm, while still allowing recovery.
        if not previous_arm_reliable and raw_arm_measurement_good:
            candidate = np.stack((xy[elbow], xy[wrist])).astype(np.float32)
            pending = reacquire_state.get(ARM_NAMES[side])
            consistent = False
            count = 1
            if pending is not None:
                pending_points = np.asarray(pending.get("points"), dtype=np.float32)
                if pending_points.shape == (2, 2):
                    maximum_step = float(np.max(np.linalg.norm(candidate - pending_points, axis=1)))
                    consistent = maximum_step <= max(4.0, 0.25 * scale)
                    count = int(pending.get("count", 0)) + 1 if consistent else 1
            reacquire_state[ARM_NAMES[side]] = {"points": candidate, "count": count}
            if count < 2:
                elbow_good = False
                wrist_good = False
            else:
                reacquire_state.pop(ARM_NAMES[side], None)
        elif elbow_good and wrist_good:
            reacquire_state.pop(ARM_NAMES[side], None)

        if not elbow_good:
            candidates = _circle_intersections(shoulder_point, expected_upper, xy[wrist], expected_lower) if wrist_good else []
            if candidates:
                elbow_point = min(candidates, key=lambda point: float(np.linalg.norm(point - predicted_elbow)))
            else:
                wrist_good = False
                elbow_point = shoulder_point + _unit(predicted_elbow - shoulder_point, xy[elbow] - shoulder_point) * expected_upper
            if previous is not None and not candidates:
                elbow_point = 0.70 * elbow_point + 0.30 * predicted_elbow
                elbow_point = shoulder_point + _unit(elbow_point - shoulder_point) * expected_upper
            repaired_xy[elbow] = elbow_point
            repaired_mask[elbow] = True
        reliable[elbow] = elbow_good

        elbow_point = repaired_xy[elbow]
        if not wrist_good:
            if previous is not None:
                previous_upper = previous[elbow] - previous[shoulder]
                current_upper = elbow_point - shoulder_point
                previous_lower = previous[wrist] - previous[elbow]
                rotated_lower = _rotate(previous_lower, _signed_angle(previous_upper, current_upper))
                wrist_direction = _unit(rotated_lower, xy[wrist] - elbow_point)
            else:
                wrist_direction = _unit(xy[wrist] - elbow_point, elbow_point - shoulder_point)
            wrist_point = elbow_point + wrist_direction * expected_lower
            if previous is not None:
                wrist_point = 0.65 * wrist_point + 0.35 * predicted_wrist
            wrist_point = _constrain_wrist_angle(
                shoulder_point,
                elbow_point,
                wrist_point,
                expected_lower,
                prior.angle_low[side],
                prior.angle_high[side],
                predicted_wrist if previous is not None else xy[wrist],
            )
            repaired_xy[wrist] = wrist_point
            repaired_mask[wrist] = True
        reliable[wrist] = wrist_good

        any_arm_bad = not reliable[shoulder] or not elbow_good or not wrist_good
        if any_arm_bad:
            occluded_counts[side] = int(occluded_counts[side]) + 1
            if not elbow_good and not wrist_good and saved_arm is not None and occluded_counts[side] <= hold_frames:
                anchor_shift = shoulder_point - saved_arm[0]
                repaired_xy[[shoulder, elbow, wrist]] = saved_arm + anchor_shift
                repaired_mask[[shoulder, elbow, wrist]] = True
            shoulder_supported = bool(reliable[shoulder] or previous is not None or saved_arm is not None)
            side_valid = bool(
                occluded_counts[side] <= hold_frames
                and shoulder_supported
                and saved_arm is not None
            )
        else:
            occluded_counts[side] = 0
            side_valid = bool(reliable[shoulder])
        if elbow_good and wrist_good and reliable[shoulder]:
            last_reliable_arms[ARM_NAMES[side]] = repaired_xy[[shoulder, elbow, wrist]].copy()
        arm_valid.append(side_valid)

    state["arm_occluded_frames"] = occluded_counts
    # Non-arm joints are not altered, but reliable torso joints are retained to
    # estimate global translation on the next frame.
    for joint in range(17):
        if joint not in {5, 6, 7, 8, 9, 10}:
            reliable[joint] = bool(inside[joint] and confidence[joint] >= 0.20)
    state["previous_xy"] = repaired_xy.copy()
    state["previous_reliable"] = reliable.copy()
    # The current 25-dimensional CTPGR feature vector has no joint-validity
    # mask.  Once either arm exceeds the repair horizon, rejecting the frame is
    # safer than feeding an unmarked invented arm to the LSTM.
    usable = bool(all(arm_valid))
    arm_joint_indices = np.array([5, 6, 7, 8, 9, 10], dtype=np.int64)
    quality = float(np.clip(np.mean(confidence[arm_joint_indices]), 0.0, 1.0))
    if not all(arm_valid):
        quality *= 0.75
    return PoseRepairResult(
        coordinates=repaired_xy.astype(np.float32),
        reliable=reliable,
        repaired=repaired_mask,
        arm_valid=(bool(arm_valid[0]), bool(arm_valid[1])),
        usable=usable,
        quality=quality,
    )


def _quantize_like_ctpgr(points: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
    width, height = image_size
    normalized = points.astype(np.float32).copy()
    normalized[:, 0] /= width
    normalized[:, 1] /= height
    normalized = np.clip(normalized, 0.0, 1.0)
    # HumanKeypointPredict uses integer heatmap argmax coordinates divided by
    # 64, so the largest representable normalized coordinate is 63/64.
    return np.floor(normalized * CTPGR_HEATMAP_SIZE).clip(0, CTPGR_HEATMAP_SIZE - 1) / CTPGR_HEATMAP_SIZE


def coco_to_ctpgr(coco: np.ndarray, image_size: tuple[int, int] = (512, 512)) -> np.ndarray:
    """Return CTPGR coordinates shaped ``(1, 2, 14)`` from COCO 17 points.

    Confidence is deliberately not used.  This mirrors CTPGR, which always
    emits an argmax point even when a joint is occluded.
    """
    coco = np.asarray(coco, dtype=np.float32)
    if coco.shape != (17, 2):
        raise ValueError(f"expected 17 COCO keypoints, got {coco.shape}")
    if not np.isfinite(coco).all():
        raise ValueError("YOLO pose returned non-finite keypoints")

    right_shoulder, right_elbow, right_wrist = coco[6], coco[8], coco[10]
    left_shoulder, left_elbow, left_wrist = coco[5], coco[7], coco[9]
    right_hip, right_knee, right_ankle = coco[12], coco[14], coco[16]
    left_hip, left_knee, left_ankle = coco[11], coco[13], coco[15]

    neck = (right_shoulder + left_shoulder) / 2
    face = coco[[0, 1, 2, 3, 4]]
    head_x = float(np.mean(face[:, 0]))
    head_y = float(np.min(face[:, 1]))
    shoulder_width = float(np.linalg.norm(left_shoulder - right_shoulder))
    head_top = np.array([head_x, max(0.0, head_y - 0.25 * shoulder_width)], dtype=np.float32)

    points = np.stack(
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
            neck,
        ]
    )
    return _quantize_like_ctpgr(points, image_size).T[np.newaxis].astype(np.float32)
