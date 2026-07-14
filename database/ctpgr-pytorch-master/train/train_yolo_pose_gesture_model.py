"""Prepare YOLO pose caches and train a CTPGR-compatible gesture LSTM.

The script keeps the original CTPGR checkpoints and generated coordinates
untouched.  It is resumable at video granularity.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parents[1]
VEHICLE_ROOT = ROOT.parent / "vehicle-vision-system"
VEHICLE_BACKEND = VEHICLE_ROOT / "backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(VEHICLE_BACKEND) not in sys.path:
    sys.path.insert(0, str(VEHICLE_BACKEND))

from app.services.ctpgr_pose_adapter import (
    POSE_REPAIR_VERSION,
    POSE_REPAIR_CACHE_REVISION,
    POSE_REPAIR_TRAINING_PIPELINE_REVISION,
    apply_gesture_probability_gate,
    ArmPosePrior,
    coco_to_ctpgr,
    fit_arm_pose_prior,
    repair_coco_pose,
    save_arm_pose_prior,
    select_person_index_with_match,
    sha256_file,
)
from constants.enum_keys import PG
from models.gesture_recognition_model import GestureRecognitionModel
from pgdataset.s3_handcraft import BoneLengthAngle


DATA_ROOT = Path.home() / "PoliceGestureLong"
CACHE_ROOT = ROOT / "generated" / "coords_yolo11s"
RAW_REPAIR_CACHE_ROOT = ROOT / "generated" / "coords_yolo11s_arm_repair_raw_v1"
REPAIRED_CACHE_ROOT = ROOT / "generated" / "coords_yolo11s_arm_repair_v1"
REPAIR_STATS_PATH = ROOT / "generated" / "yolo11s_arm_pose_stats.json"
MODEL_NAME = "yolo11s-pose.pt"
MODEL_PATH = VEHICLE_ROOT / MODEL_NAME
OUTPUT_PATH = ROOT / "checkpoints" / "lstm_yolo11s_arm_repair_candidate.pt"
REPORT_PATH = ROOT / "generated" / "yolo11s_arm_repair_report.json"
IMAGE_SIZE = (512, 512)
LABEL_DELAY = 15
NUM_CLASSES = 9
RAW_CACHE_VERSION = "yolo-pose-confidence-track-v2"
REPAIR_CACHE_REVISION = POSE_REPAIR_CACHE_REVISION
PRODUCTION_MIN_CONFIDENCE = 0.35
PRODUCTION_MIN_MARGIN = 0.03


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_labels(path: Path) -> np.ndarray:
    with path.open(newline="") as handle:
        row = next(csv.reader(handle))
    return np.asarray([int(value) for value in row], dtype=np.int64)


def choose_largest_person(result) -> np.ndarray | None:
    if result.keypoints is None or result.keypoints.xy is None or len(result.keypoints.xy) == 0:
        return None
    person_index = 0
    if result.boxes is not None and result.boxes.xyxy is not None and len(result.boxes.xyxy):
        boxes = result.boxes.xyxy.cpu().numpy()
        areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
        person_index = int(np.argmax(areas))
    return result.keypoints.xy[person_index].cpu().numpy()


def choose_tracked_person_pose(
    result,
    previous_box: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, bool] | None:
    """Return coordinates and matching per-joint confidence for one person."""

    if result.keypoints is None or result.keypoints.xy is None or len(result.keypoints.xy) == 0:
        return None
    person_index = 0
    selected_box = None
    matched_previous = True
    if result.boxes is not None and result.boxes.xyxy is not None and len(result.boxes.xyxy):
        boxes = result.boxes.xyxy.cpu().numpy()
        person_index, matched_previous = select_person_index_with_match(boxes, previous_box)
        selected_box = boxes[person_index].astype(np.float32, copy=True)
    xy = result.keypoints.xy[person_index].cpu().numpy().astype(np.float32, copy=False)
    keypoint_confidence = getattr(result.keypoints, "conf", None)
    if keypoint_confidence is None:
        confidence = np.ones(17, dtype=np.float32)
    else:
        confidence = keypoint_confidence[person_index].cpu().numpy().astype(np.float32, copy=False)
    return xy, confidence, selected_box, matched_previous


def _cache_scalar(data, key: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if np.asarray(value).size != 1:
        return None
    return str(np.asarray(value).reshape(-1)[0])


def _raw_cache_is_current(
    path: Path,
    pose_model_sha256: str,
    track_hold_frames: int,
    video_path: Path,
) -> bool:
    if not path.is_file():
        return False
    try:
        with np.load(path) as data:
            return (
                _cache_scalar(data, "cache_version") == RAW_CACHE_VERSION
                and _cache_scalar(data, "pose_model") == MODEL_NAME
                and _cache_scalar(data, "pose_model_sha256") == pose_model_sha256
                and "track_hold_frames" in data
                and int(np.asarray(data["track_hold_frames"]).reshape(-1)[0]) == track_hold_frames
                and "track_switched" in data
                and "sequence_reset" in data
                and int(np.asarray(data["source_video_size"]).reshape(-1)[0]) == video_path.stat().st_size
                and int(np.asarray(data["source_video_mtime_ns"]).reshape(-1)[0]) == video_path.stat().st_mtime_ns
                and _cache_scalar(data, "source_label_sha256") == sha256_file(video_path.with_suffix(".csv"))
                and np.array_equal(data["image_size"], np.asarray(IMAGE_SIZE, dtype=np.int64))
                and data["coco_xy"].ndim == 3
                and data["keypoint_confidence"].shape == data["coco_xy"].shape[:2]
            )
    except (OSError, KeyError, ValueError):
        return False


def _repaired_cache_is_current(
    path: Path,
    prior: ArmPosePrior,
    hold_frames: int,
    raw_fingerprint: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        with np.load(path) as data:
            return (
                _cache_scalar(data, "repair_version") == POSE_REPAIR_VERSION
                and _cache_scalar(data, "repair_cache_revision") == REPAIR_CACHE_REVISION
                and _cache_scalar(data, "profile_fingerprint") == prior.fingerprint
                and _cache_scalar(data, "pose_model") == MODEL_NAME
                and _cache_scalar(data, "pose_model_sha256") == prior.pose_model_sha256
                and int(np.asarray(data["hold_frames"]).reshape(-1)[0]) == hold_frames
                and _cache_scalar(data, "raw_fingerprint") == raw_fingerprint
            )
    except (OSError, KeyError, ValueError):
        return False


def prepare_raw_video(
    model: YOLO,
    video_path: Path,
    cache_path: Path,
    batch_size: int,
    device: str,
    pose_model_sha256: str,
    track_hold_frames: int,
) -> None:
    """Extract resumable raw COCO coordinates and confidence for one video."""

    labels = load_labels(video_path.with_suffix(".csv"))
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"unable to open {video_path}")
    coordinates: list[np.ndarray] = []
    confidences: list[np.ndarray] = []
    valid: list[bool] = []
    track_switched: list[bool] = []
    sequence_reset: list[bool] = []
    frames: list[np.ndarray] = []
    processed = 0
    previous_box: np.ndarray | None = None
    missed_person_frames = 0
    reset_pending = False

    def predict_batch() -> None:
        nonlocal processed, previous_box, missed_person_frames, reset_pending
        if not frames:
            return
        results = model.predict(frames, imgsz=IMAGE_SIZE[0], device=device, verbose=False)
        for result in results:
            pose = choose_tracked_person_pose(result, previous_box)
            if pose is None:
                coordinates.append(np.zeros((17, 2), dtype=np.float32))
                confidences.append(np.zeros(17, dtype=np.float32))
                valid.append(False)
                track_switched.append(False)
                sequence_reset.append(False)
                missed_person_frames += 1
                if missed_person_frames > track_hold_frames:
                    previous_box = None
                    reset_pending = True
            else:
                xy, confidence, selected_box, matched_previous = pose
                coordinates.append(xy.copy())
                confidences.append(confidence.copy())
                valid.append(True)
                switched = previous_box is not None and not matched_previous
                track_switched.append(switched)
                sequence_reset.append(switched or reset_pending)
                missed_person_frames = 0
                reset_pending = False
                if selected_box is not None:
                    previous_box = selected_box
            processed += 1
        print(f"{video_path.name}: raw pose {processed}/{len(labels)}", flush=True)
        frames.clear()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(frame, IMAGE_SIZE, interpolation=cv2.INTER_AREA))
        if len(frames) >= batch_size:
            predict_batch()
    predict_batch()
    cap.release()

    usable = min(len(coordinates), len(labels))
    if usable == 0:
        raise ValueError(f"no frames extracted from {video_path}")
    if len(coordinates) != len(labels):
        print(f"warning: {video_path.name} frames={len(coordinates)} labels={len(labels)}; truncating to {usable}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        coco_xy=np.asarray(coordinates[:usable], dtype=np.float32),
        keypoint_confidence=np.asarray(confidences[:usable], dtype=np.float32),
        labels=labels[:usable],
        valid=np.asarray(valid[:usable], dtype=np.bool_),
        track_switched=np.asarray(track_switched[:usable], dtype=np.bool_),
        sequence_reset=np.asarray(sequence_reset[:usable], dtype=np.bool_),
        cache_version=np.asarray(RAW_CACHE_VERSION),
        pose_model=np.asarray(MODEL_NAME),
        pose_model_sha256=np.asarray(pose_model_sha256),
        image_size=np.asarray(IMAGE_SIZE, dtype=np.int64),
        track_hold_frames=np.asarray(track_hold_frames, dtype=np.int64),
        source_video_size=np.asarray(video_path.stat().st_size, dtype=np.int64),
        source_video_mtime_ns=np.asarray(video_path.stat().st_mtime_ns, dtype=np.int64),
        source_label_sha256=np.asarray(sha256_file(video_path.with_suffix(".csv"))),
    )


def fit_prior_from_raw_cache(
    raw_cache_root: Path,
    stats_path: Path,
    pose_model_sha256: str,
) -> ArmPosePrior:
    coordinate_batches = []
    confidence_batches = []
    sources = []
    for path in sorted((raw_cache_root / "train").glob("*.npz")):
        with np.load(path) as data:
            coordinates = data["coco_xy"].astype(np.float32)
            confidence = data["keypoint_confidence"].astype(np.float32)
            sequence_reset = (
                data["sequence_reset"].astype(np.bool_)
                if "sequence_reset" in data
                else np.zeros(len(coordinates), dtype=np.bool_)
            )
            split_points = np.flatnonzero(sequence_reset)
            split_points = split_points[split_points > 0]
            boundaries = [0, *split_points.tolist(), len(coordinates)]
            for start, end in zip(boundaries[:-1], boundaries[1:]):
                if end > start:
                    # Missing detections keep confidence=0, naturally breaking
                    # temporal deltas without discarding the actual frame gap.
                    coordinate_batches.append(coordinates[start:end])
                    confidence_batches.append(confidence[start:end])
        sources.append(path.stem)
    if not coordinate_batches:
        raise FileNotFoundError(f"no raw training caches in {raw_cache_root / 'train'}")
    prior = fit_arm_pose_prior(
        coordinate_batches,
        confidence_batches,
        image_size=IMAGE_SIZE,
        source=f"CTPGesture train videos: {','.join(sources)}",
        pose_model_sha256=pose_model_sha256,
    )
    save_arm_pose_prior(prior, stats_path)
    print(f"saved arm pose profile {prior.fingerprint[:12]} to {stats_path}", flush=True)
    return prior


def materialize_repaired_cache(
    raw_path: Path,
    cache_path: Path,
    prior: ArmPosePrior,
    hold_frames: int,
    raw_fingerprint: str | None = None,
) -> None:
    raw_fingerprint = raw_fingerprint or sha256_file(raw_path)
    with np.load(raw_path) as data:
        raw_coordinates = data["coco_xy"].astype(np.float32)
        confidence = data["keypoint_confidence"].astype(np.float32)
        detected = data["valid"].astype(np.bool_)
        track_switched = data["track_switched"].astype(np.bool_) if "track_switched" in data else np.zeros_like(detected)
        raw_sequence_reset = (
            data["sequence_reset"].astype(np.bool_)
            if "sequence_reset" in data
            else track_switched.copy()
        )
        labels = data["labels"].astype(np.int64)
    state: dict[str, object] = {}
    coordinates = []
    valid = []
    repaired_masks = []
    pose_quality = []
    sequence_resets = []
    last_coord = np.zeros((1, 2, 14), dtype=np.float32)
    has_last_coord = False
    missed_frames = hold_frames + 1
    cache_reset_pending = False

    for frame_xy, frame_confidence, frame_detected, frame_switched, frame_raw_reset in zip(
        raw_coordinates,
        confidence,
        detected,
        track_switched,
        raw_sequence_reset,
    ):
        if frame_switched or frame_raw_reset:
            state = {}
            last_coord = np.zeros((1, 2, 14), dtype=np.float32)
            has_last_coord = False
            missed_frames = hold_frames + 1
            cache_reset_pending = True
        if frame_detected:
            result = repair_coco_pose(
                frame_xy,
                frame_confidence,
                state=state,
                prior=prior,
                image_size=IMAGE_SIZE,
                hold_frames=hold_frames,
            )
            if result.usable:
                last_coord = coco_to_ctpgr(result.coordinates, IMAGE_SIZE)
                has_last_coord = True
                missed_frames = 0
                frame_valid = True
            else:
                missed_frames = 0
                has_last_coord = False
                last_coord = np.zeros_like(last_coord)
                frame_valid = False
            repaired_masks.append(result.repaired)
            pose_quality.append(result.quality)
        else:
            missed_frames += 1
            frame_valid = bool(has_last_coord and missed_frames <= hold_frames)
            repaired_masks.append(np.zeros(17, dtype=np.bool_))
            pose_quality.append(0.0)
            if missed_frames > hold_frames:
                state = {}
                has_last_coord = False
                cache_reset_pending = True
        if not frame_valid:
            # This coordinate is masked from the loss.  Zero is preferable to
            # holding an invented arm forever and matches runtime rejection.
            coordinate = np.zeros_like(last_coord)
        else:
            coordinate = last_coord
        coordinates.append(coordinate.copy())
        valid.append(frame_valid)
        sequence_resets.append(bool(cache_reset_pending and frame_valid))
        if frame_valid:
            cache_reset_pending = False

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        coord_norm=np.concatenate(coordinates, axis=0),
        labels=labels[: len(coordinates)],
        valid=np.asarray(valid, dtype=np.bool_),
        keypoint_confidence=confidence[: len(coordinates)],
        repaired_mask=np.asarray(repaired_masks, dtype=np.bool_),
        pose_quality=np.asarray(pose_quality, dtype=np.float32),
        sequence_reset=np.asarray(sequence_resets, dtype=np.bool_),
        repair_version=np.asarray(POSE_REPAIR_VERSION),
        repair_cache_revision=np.asarray(REPAIR_CACHE_REVISION),
        profile_fingerprint=np.asarray(prior.fingerprint),
        pose_model=np.asarray(MODEL_NAME),
        pose_model_sha256=np.asarray(prior.pose_model_sha256),
        hold_frames=np.asarray(hold_frames, dtype=np.int64),
        raw_fingerprint=np.asarray(raw_fingerprint),
    )


def prepare_video(model: YOLO, video_path: Path, cache_path: Path, batch_size: int, device: str) -> None:
    labels = load_labels(video_path.with_suffix(".csv"))
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"unable to open {video_path}")

    coordinates: list[np.ndarray] = []
    valid: list[bool] = []
    frames: list[np.ndarray] = []
    processed = 0
    last_valid = np.zeros((1, 2, 14), dtype=np.float32)

    def predict_batch() -> None:
        nonlocal processed, last_valid
        if not frames:
            return
        results = model.predict(frames, imgsz=IMAGE_SIZE[0], device=device, verbose=False)
        for result in results:
            coco = choose_largest_person(result)
            if coco is None:
                coordinates.append(last_valid.copy())
                valid.append(False)
            else:
                last_valid = coco_to_ctpgr(coco, IMAGE_SIZE)
                coordinates.append(last_valid.copy())
                valid.append(True)
            processed += 1
        print(f"{video_path.name}: {processed}/{len(labels)}", flush=True)
        frames.clear()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(frame, IMAGE_SIZE, interpolation=cv2.INTER_AREA))
        if len(frames) >= batch_size:
            predict_batch()
    predict_batch()
    cap.release()

    usable = min(len(coordinates), len(labels))
    if usable == 0:
        raise ValueError(f"no frames extracted from {video_path}")
    if len(coordinates) != len(labels):
        print(f"warning: {video_path.name} frames={len(coordinates)} labels={len(labels)}; truncating to {usable}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        coord_norm=np.concatenate(coordinates[:usable], axis=0),
        labels=labels[:usable],
        valid=np.asarray(valid[:usable], dtype=np.bool_),
    )


def prepare(
    split: str,
    batch_size: int,
    device: str,
    cache_root: Path = CACHE_ROOT,
    force: bool = False,
) -> None:
    model = YOLO(str(MODEL_PATH) if MODEL_PATH.is_file() else MODEL_NAME)
    videos = sorted((DATA_ROOT / split).glob("*.mp4"))
    if not videos:
        raise FileNotFoundError(f"no videos in {DATA_ROOT / split}")
    for index, video_path in enumerate(videos, 1):
        cache_path = cache_root / split / f"{video_path.stem}.npz"
        if cache_path.exists() and not force:
            print(f"[{index}/{len(videos)}] cached: {cache_path.name}", flush=True)
            continue
        print(f"[{index}/{len(videos)}] preparing {video_path.name}", flush=True)
        prepare_video(model, video_path, cache_path, batch_size, device)


def prepare_repaired(
    batch_size: int,
    device: str,
    raw_cache_root: Path = RAW_REPAIR_CACHE_ROOT,
    cache_root: Path = REPAIRED_CACHE_ROOT,
    stats_path: Path = REPAIR_STATS_PATH,
    hold_frames: int = 5,
    force: bool = False,
) -> ArmPosePrior:
    """Build versioned raw caches, fit train-only priors, then repair all splits."""

    if not MODEL_PATH.is_file():
        raise FileNotFoundError(f"pose model not found: {MODEL_PATH}")
    pose_model_sha256 = sha256_file(MODEL_PATH)
    model = YOLO(str(MODEL_PATH))
    for split in ("train", "test"):
        videos = sorted((DATA_ROOT / split).glob("*.mp4"))
        if not videos:
            raise FileNotFoundError(f"no videos in {DATA_ROOT / split}")
        for index, video_path in enumerate(videos, 1):
            raw_path = raw_cache_root / split / f"{video_path.stem}.npz"
            if _raw_cache_is_current(raw_path, pose_model_sha256, hold_frames, video_path) and not force:
                print(f"[{index}/{len(videos)}] raw cached: {raw_path.name}", flush=True)
                continue
            print(f"[{index}/{len(videos)}] extracting raw pose: {video_path.name}", flush=True)
            prepare_raw_video(
                model,
                video_path,
                raw_path,
                batch_size,
                device,
                pose_model_sha256,
                hold_frames,
            )

    prior = fit_prior_from_raw_cache(raw_cache_root, stats_path, pose_model_sha256)
    for split in ("train", "test"):
        raw_paths = sorted((raw_cache_root / split).glob("*.npz"))
        for index, raw_path in enumerate(raw_paths, 1):
            cache_path = cache_root / split / raw_path.name
            raw_fingerprint = sha256_file(raw_path)
            if _repaired_cache_is_current(cache_path, prior, hold_frames, raw_fingerprint) and not force:
                print(f"[{index}/{len(raw_paths)}] repaired cached: {cache_path.name}", flush=True)
                continue
            print(f"[{index}/{len(raw_paths)}] repairing pose: {raw_path.name}", flush=True)
            materialize_repaired_cache(raw_path, cache_path, prior, hold_frames, raw_fingerprint)
    return prior


def delayed_labels(labels: np.ndarray, delay: int = LABEL_DELAY) -> np.ndarray:
    if delay < 0:
        raise ValueError("label delay must be non-negative")
    if delay == 0:
        return labels.copy()
    if len(labels) <= delay:
        return np.zeros_like(labels)
    return np.concatenate((np.zeros(delay, dtype=labels.dtype), labels[:-delay]))


def load_cache(
    split: str,
    label_delay: int = LABEL_DELAY,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, np.ndarray]]:
    bla = BoneLengthAngle()
    items = []
    for path in sorted((cache_root / split).glob("*.npz")):
        data = np.load(path)
        coords = data["coord_norm"].astype(np.float32)
        labels = data["labels"].astype(np.int64)
        valid = data["valid"].astype(np.bool_)
        sequence_reset = (
            data["sequence_reset"].astype(np.bool_)
            if "sequence_reset" in data
            else np.zeros(len(valid), dtype=np.bool_)
        )
        repair_version = _cache_scalar(data, "repair_version")
        profile_fingerprint = _cache_scalar(data, "profile_fingerprint")
        pose_model_sha256 = _cache_scalar(data, "pose_model_sha256")
        repair_cache_revision = _cache_scalar(data, "repair_cache_revision")
        repair_hold_frames = (
            int(np.asarray(data["hold_frames"]).reshape(-1)[0])
            if "hold_frames" in data
            else None
        )
        if not (len(coords) == len(labels) == len(valid) == len(sequence_reset)):
            raise ValueError(f"cache arrays have different frame counts: {path}")
        split_points = np.flatnonzero(sequence_reset)
        split_points = split_points[split_points > 0]
        boundaries = [0, *split_points.tolist(), len(coords)]
        for segment_index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
            if end <= start:
                continue
            timeline_labels = delayed_labels(labels[start:end], label_delay)
            timeline_valid = valid[start:end]
            if repair_version is not None:
                # Runtime returns no_gesture and does not advance the LSTM on
                # an unusable repaired pose.  Delay labels in video-frame time
                # first, then compress only the recurrent input steps.
                timeline_step_indices = np.flatnonzero(timeline_valid)
                segment_coords = coords[start:end][timeline_valid]
                segment_labels = timeline_labels[timeline_valid]
                segment_valid = np.ones(len(segment_labels), dtype=np.bool_)
                evaluation_mask = np.ones(len(timeline_labels), dtype=np.bool_)
            else:
                segment_coords = coords[start:end]
                segment_labels = timeline_labels
                segment_valid = timeline_valid.copy()
                timeline_step_indices = np.arange(len(timeline_labels), dtype=np.int64)
                # Preserve legacy-cache evaluation semantics.  Repaired
                # caches use the complete timeline above because their invalid
                # frames have a defined runtime output (no_gesture).
                evaluation_mask = timeline_valid.copy()
            if len(segment_coords):
                feature_dict = bla.handcrafted_features(segment_coords)
                features = np.concatenate(
                    (
                        feature_dict[PG.BONE_LENGTH],
                        feature_dict[PG.BONE_ANGLE_COS],
                        feature_dict[PG.BONE_ANGLE_SIN],
                    ),
                    axis=1,
                ).astype(np.float32)
            else:
                features = np.empty((0, 25), dtype=np.float32)
            items.append(
                {
                    "name": path.stem if len(boundaries) == 2 else f"{path.stem}#segment{segment_index + 1}",
                    "source_video": path.stem,
                    "features": features,
                    "coords": segment_coords,
                    "labels": segment_labels,
                    "valid": segment_valid,
                    "timeline_labels": timeline_labels,
                    "timeline_step_indices": timeline_step_indices.astype(np.int64, copy=False),
                    "evaluation_mask": evaluation_mask,
                    "repair_version": repair_version,
                    "profile_fingerprint": profile_fingerprint,
                    "pose_model_sha256": pose_model_sha256,
                    "repair_cache_revision": repair_cache_revision,
                    "repair_hold_frames": repair_hold_frames,
                }
            )
    if not items:
        raise FileNotFoundError(f"no caches in {cache_root / split}")
    repair_versions = {item["repair_version"] for item in items if item["repair_version"] is not None}
    profile_fingerprints = {
        item["profile_fingerprint"] for item in items if item["profile_fingerprint"] is not None
    }
    pose_model_hashes = {item["pose_model_sha256"] for item in items if item["pose_model_sha256"] is not None}
    repair_revisions = {item["repair_cache_revision"] for item in items if item["repair_cache_revision"] is not None}
    repair_hold_values = {item["repair_hold_frames"] for item in items if item["repair_hold_frames"] is not None}
    if (
        len(repair_versions) > 1
        or len(profile_fingerprints) > 1
        or len(pose_model_hashes) > 1
        or len(repair_revisions) > 1
        or len(repair_hold_values) > 1
    ):
        raise ValueError(f"mixed pose repair cache profiles in {cache_root / split}")
    if repair_versions and any(item["repair_version"] is None for item in items):
        raise ValueError(f"repaired and legacy pose caches are mixed in {cache_root / split}")
    return items


def _cache_binding(item: dict) -> dict[str, object]:
    return {
        "pose_repair_version": item.get("repair_version"),
        "profile_fingerprint": item.get("profile_fingerprint"),
        "pose_model_sha256": item.get("pose_model_sha256"),
        "repair_cache_revision": item.get("repair_cache_revision"),
        "pose_hold_frames": item.get("repair_hold_frames"),
    }


def assert_matching_cache_bindings(reference: list[dict], *groups: list[dict]) -> None:
    if not reference:
        raise ValueError("reference cache group is empty")
    expected = _cache_binding(reference[0])
    for group in groups:
        if not group:
            continue
        actual = _cache_binding(group[0])
        if actual != expected:
            raise ValueError(f"pose cache binding mismatch: expected {expected}, got {actual}")


def augment_arm_occlusion(
    coordinates: np.ndarray,
    rng: np.random.Generator,
    probability: float,
    max_span: int = 5,
) -> np.ndarray:
    """Simulate residual short arm occlusion after the shared repair layer."""

    augmented = np.asarray(coordinates, dtype=np.float32).copy()
    if probability <= 0 or len(augmented) < 2 or rng.random() >= probability:
        return augmented
    side = int(rng.integers(0, 2))
    shoulder, elbow, wrist = ((0, 1, 2), (3, 4, 5))[side]
    span = int(rng.integers(1, min(max_span, len(augmented) - 1) + 1))
    start = int(rng.integers(1, len(augmented) - span + 1))
    occlude_elbow = bool(rng.random() < 0.35)
    anchor_arm = augmented[start - 1][:, [shoulder, elbow, wrist]].copy()
    for frame_index in range(start, start + span):
        shoulder_shift = augmented[frame_index, :, shoulder] - anchor_arm[:, 0]
        if occlude_elbow:
            augmented[frame_index, :, elbow] = anchor_arm[:, 1] + shoulder_shift
        augmented[frame_index, :, wrist] = anchor_arm[:, 2] + shoulder_shift
    return augmented


class WindowDataset(Dataset):
    def __init__(
        self,
        videos: list[dict[str, np.ndarray]],
        clip_len: int,
        stride: int,
        arm_occlusion_probability: float = 0.0,
        seed: int = 0,
    ):
        self.videos = videos
        self.windows: list[tuple[int, int]] = []
        for video_index, video in enumerate(videos):
            length = len(video["labels"])
            if length < clip_len:
                continue
            starts = list(range(0, max(1, length - clip_len + 1), stride))
            final_start = max(0, length - clip_len)
            if not starts or starts[-1] != final_start:
                starts.append(final_start)
            self.windows.extend((video_index, start) for start in starts)
        self.clip_len = clip_len
        self.arm_occlusion_probability = float(arm_occlusion_probability)
        self.rng = np.random.default_rng(seed)
        self.bla = BoneLengthAngle()
        if not self.windows:
            raise ValueError(f"no training sequence is at least clip_len={clip_len} frames")

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, index: int):
        video_index, start = self.windows[index]
        video = self.videos[video_index]
        end = start + self.clip_len
        if self.arm_occlusion_probability > 0:
            coords = augment_arm_occlusion(
                video["coords"][start:end],
                self.rng,
                self.arm_occlusion_probability,
            )
            feature_dict = self.bla.handcrafted_features(coords)
            features = np.concatenate(
                (
                    feature_dict[PG.BONE_LENGTH],
                    feature_dict[PG.BONE_ANGLE_COS],
                    feature_dict[PG.BONE_ANGLE_SIN],
                ),
                axis=1,
            ).astype(np.float32)
        else:
            features = video["features"][start:end]
        return (
            torch.from_numpy(features),
            torch.from_numpy(video["labels"][start:end]),
            torch.from_numpy(video["valid"][start:end]),
        )


def confusion_metrics(confusion: np.ndarray) -> dict:
    rows = []
    f1_values = []
    for cls in range(NUM_CLASSES):
        tp = int(confusion[cls, cls])
        fp = int(confusion[:, cls].sum() - tp)
        fn = int(confusion[cls, :].sum() - tp)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows.append({"class": cls, "precision": precision, "recall": recall, "f1": f1, "support": int(confusion[cls].sum())})
        f1_values.append(f1)
    no_gesture_total = int(confusion[0].sum())
    false_gesture_rate = (
        float((no_gesture_total - confusion[0, 0]) / no_gesture_total)
        if no_gesture_total
        else 0.0
    )
    return {
        "macro_f1": float(np.mean(f1_values)),
        "gesture_macro_f1": float(np.mean(f1_values[1:])),
        "false_gesture_rate": false_gesture_rate,
        "per_class": rows,
        "confusion_matrix": confusion.tolist(),
    }


def calculate_class_weights(
    counts: np.ndarray,
    power: float = 0.5,
    no_gesture_multiplier: float = 1.0,
) -> np.ndarray:
    if power < 0:
        raise ValueError("class-weight power must be non-negative")
    if no_gesture_multiplier <= 0:
        raise ValueError("no-gesture weight multiplier must be positive")
    counts = np.asarray(counts, dtype=np.float64)
    if counts.ndim != 1 or len(counts) != NUM_CLASSES:
        raise ValueError(f"expected {NUM_CLASSES} class counts")
    weights = np.power(counts.sum() / np.maximum(counts, 1), power)
    weights[0] *= no_gesture_multiplier
    weights /= weights.mean()
    return weights.astype(np.float32)


@torch.no_grad()
def evaluate_model(
    model: GestureRecognitionModel,
    videos: list[dict[str, np.ndarray]],
    device: torch.device,
    min_confidence: float = PRODUCTION_MIN_CONFIDENCE,
    min_margin: float = PRODUCTION_MIN_MARGIN,
) -> dict:
    model.eval()
    confusion = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
    gated_confusion = np.zeros_like(confusion)
    true_probability_sum = np.zeros(NUM_CLASSES, dtype=np.float64)
    true_probability_count = np.zeros(NUM_CLASSES, dtype=np.int64)
    for video in videos:
        step_count = len(video["features"])
        if step_count:
            features = torch.from_numpy(video["features"]).to(device).unsqueeze(1)
            h = torch.zeros((1, 1, model.num_hidden), device=device)
            c = torch.zeros_like(h)
            _, _, _, logits = model(features, h, c)
            probabilities = torch.softmax(logits, dim=1).cpu().numpy()
            step_predictions = probabilities.argmax(axis=1)
            step_gated_predictions = apply_gesture_probability_gate(
                probabilities,
                min_confidence,
                min_margin,
            )
        else:
            probabilities = np.empty((0, NUM_CLASSES), dtype=np.float32)
            step_predictions = np.empty(0, dtype=np.int64)
            step_gated_predictions = np.empty(0, dtype=np.int64)

        timeline_labels = video.get("timeline_labels", video["labels"])
        timeline_step_indices = video.get(
            "timeline_step_indices",
            np.arange(len(video["labels"]), dtype=np.int64),
        )
        if len(timeline_step_indices) != step_count:
            raise ValueError(f"timeline mapping does not match recurrent steps for {video['name']}")
        predictions = np.zeros(len(timeline_labels), dtype=np.int64)
        gated_predictions = np.zeros(len(timeline_labels), dtype=np.int64)
        predictions[timeline_step_indices] = step_predictions
        gated_predictions[timeline_step_indices] = step_gated_predictions
        true_probabilities = np.zeros(len(timeline_labels), dtype=np.float64)
        if step_count:
            step_targets = timeline_labels[timeline_step_indices]
            true_probabilities[timeline_step_indices] = probabilities[
                np.arange(step_count),
                step_targets,
            ]
        mask = video.get("evaluation_mask", video["valid"])
        targets = timeline_labels[mask]
        predictions = predictions[mask]
        gated_predictions = gated_predictions[mask]
        true_probabilities = true_probabilities[mask]
        np.add.at(confusion, (targets, predictions), 1)
        np.add.at(gated_confusion, (targets, gated_predictions), 1)
        if len(targets):
            np.add.at(true_probability_sum, targets, true_probabilities)
            np.add.at(true_probability_count, targets, 1)
    report = confusion_metrics(confusion)
    report["production_gate"] = confusion_metrics(gated_confusion)
    report["mean_true_class_confidence"] = np.divide(
        true_probability_sum,
        true_probability_count,
        out=np.zeros_like(true_probability_sum),
        where=true_probability_count > 0,
    ).tolist()
    report["production_gate_thresholds"] = {
        "min_confidence": min_confidence,
        "min_margin": min_margin,
    }
    return report


def train(args) -> dict:
    seed_everything(args.seed)
    cache_root = Path(getattr(args, "cache_root", CACHE_ROOT))
    arm_occlusion_probability = float(getattr(args, "arm_occlusion_probability", 0.0))
    all_train = load_cache("train", args.label_delay, cache_root)
    source_videos = list(dict.fromkeys(video["source_video"] for video in all_train))
    validation_count = min(args.validation_videos, max(1, len(source_videos) - 1))
    validation_sources = set(source_videos[-validation_count:])
    train_videos = [video for video in all_train if video["source_video"] not in validation_sources]
    validation_label_delay = getattr(args, "validation_label_delay", None)
    if validation_label_delay is None:
        validation_label_delay = args.label_delay
    validation_videos = [
        video
        for video in load_cache("train", validation_label_delay, cache_root)
        if video["source_video"] in validation_sources
    ]
    test_videos = None if args.skip_test else load_cache("test", args.label_delay, cache_root)
    assert_matching_cache_bindings(all_train, validation_videos, *([] if test_videos is None else [test_videos]))

    device = torch.device(args.device)
    model = GestureRecognitionModel(args.batch_size).to(device)
    dataset = WindowDataset(
        train_videos,
        args.clip_len,
        args.stride,
        arm_occlusion_probability=arm_occlusion_probability,
        seed=args.seed,
    )
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, drop_last=True)

    counts = np.zeros(NUM_CLASSES, dtype=np.int64)
    for video in train_videos:
        counts += np.bincount(video["labels"][video["valid"]], minlength=NUM_CLASSES)
    weights = calculate_class_weights(counts, args.class_weight_power, args.no_gesture_weight_multiplier)
    class_weights = torch.tensor(weights, dtype=torch.float32, device=device)
    criterion = nn.CrossEntropyLoss(weight=class_weights, reduction="none")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)

    best_f1 = -1.0
    stale_epochs = 0
    history = []
    output_path = Path(args.output_path)
    report_path = Path(args.report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        losses = []
        for features, labels, valid in loader:
            features = features.to(device).transpose(0, 1)
            labels = labels.to(device).transpose(0, 1).reshape(-1)
            valid = valid.to(device).transpose(0, 1).reshape(-1)
            batch = features.shape[1]
            h = torch.zeros((1, batch, model.num_hidden), device=device)
            c = torch.zeros_like(h)
            _, _, _, logits = model(features, h, c)
            frame_loss = criterion(logits, labels)
            # Keep the effective loss scale comparable across class-weight
            # schemes instead of changing it with the average sample weight.
            valid_weights = class_weights[labels[valid]]
            loss = frame_loss[valid].sum() / valid_weights.sum().clamp_min(1e-12)
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            losses.append(float(loss.item()))

        validation = evaluate_model(model, validation_videos, device)
        gated_validation = validation["production_gate"]
        epoch_row = {
            "epoch": epoch,
            "loss": float(np.mean(losses)),
            "val_macro_f1": validation["macro_f1"],
            "val_gated_macro_f1": gated_validation["macro_f1"],
        }
        history.append(epoch_row)
        print(json.dumps(epoch_row), flush=True)
        if gated_validation["macro_f1"] > best_f1:
            best_f1 = gated_validation["macro_f1"]
            stale_epochs = 0
            torch.save(model.state_dict(), output_path)
        else:
            stale_epochs += 1
            if stale_epochs >= args.patience:
                print(f"early stopping at epoch {epoch}", flush=True)
                break

    model.load_state_dict(torch.load(output_path, map_location=device, weights_only=True))
    report = {
        "model": str(output_path),
        "pose_model": str(MODEL_PATH),
        "configuration": {
            "label_delay": args.label_delay,
            "validation_label_delay": validation_label_delay,
            "class_weight_power": args.class_weight_power,
            "no_gesture_weight_multiplier": args.no_gesture_weight_multiplier,
            "class_weights": weights.tolist(),
            "loss_normalization": "weighted_sum_over_sample_weights",
            "clip_len": args.clip_len,
            "stride": args.stride,
            "learning_rate": args.learning_rate,
            "seed": args.seed,
            "cache_root": str(cache_root),
            "arm_occlusion_probability": arm_occlusion_probability,
            "pose_repair_version": all_train[0].get("repair_version"),
            "pose_repair_profile": all_train[0].get("profile_fingerprint"),
        },
        "train_videos": [video["name"] for video in train_videos],
        "validation_videos": [video["name"] for video in validation_videos],
        "history": history,
        "validation": evaluate_model(model, validation_videos, device),
        "test": evaluate_model(model, test_videos, device) if test_videos is not None else None,
    }
    metadata_path = output_path.with_suffix(output_path.suffix + ".meta.json")
    model_metadata = {
        "gesture_model": str(output_path),
        "gesture_model_sha256": sha256_file(output_path),
        "pose_repair_version": all_train[0].get("repair_version"),
        "profile_fingerprint": all_train[0].get("profile_fingerprint"),
        "pose_model_sha256": all_train[0].get("pose_model_sha256"),
        "repair_cache_revision": all_train[0].get("repair_cache_revision"),
        "training_pipeline_revision": (
            POSE_REPAIR_TRAINING_PIPELINE_REVISION
            if all_train[0].get("repair_version") is not None
            else None
        ),
        "label_delay": int(args.label_delay),
        "pose_hold_frames": all_train[0].get("repair_hold_frames"),
        "cache_root": str(cache_root),
    }
    metadata_path.write_text(json.dumps(model_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    report["model_metadata"] = {**model_metadata, "path": str(metadata_path)}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "best_val_macro_f1": report["validation"]["macro_f1"],
        "best_val_gated_macro_f1": report["validation"]["production_gate"]["macro_f1"],
    }
    if report["test"] is not None:
        summary["test_macro_f1"] = report["test"]["macro_f1"]
    print(json.dumps(summary), flush=True)
    return report


def evaluate_checkpoint(
    checkpoint: Path,
    device_name: str,
    label_delay: int = LABEL_DELAY,
    cache_root: Path = CACHE_ROOT,
) -> dict:
    device = torch.device(device_name)
    videos = load_cache("test", label_delay, cache_root)
    metadata_path = checkpoint.with_suffix(checkpoint.suffix + ".meta.json")
    cache_binding = _cache_binding(videos[0])
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = {
            **cache_binding,
            "gesture_model_sha256": sha256_file(checkpoint),
        }
        if cache_binding["pose_repair_version"] is not None:
            expected.update(
                {
                    "training_pipeline_revision": POSE_REPAIR_TRAINING_PIPELINE_REVISION,
                    "label_delay": int(label_delay),
                }
            )
        mismatched = [key for key, value in expected.items() if metadata.get(key) != value]
        if mismatched:
            raise ValueError(f"checkpoint/cache metadata mismatch: {', '.join(mismatched)}")
    elif cache_binding["pose_repair_version"] is not None:
        raise FileNotFoundError(f"repair checkpoint metadata not found: {metadata_path}")
    model = GestureRecognitionModel(1).to(device)
    model.load_state_dict(torch.load(checkpoint, map_location=device, weights_only=True))
    report = evaluate_model(model, videos, device)
    print(json.dumps({"checkpoint": str(checkpoint), **report}, ensure_ascii=False), flush=True)
    return report


def checkpoint_uses_pose_repair(checkpoint: Path) -> bool:
    """Choose a cache from checkpoint metadata without misrouting legacy models."""

    metadata_path = checkpoint.with_suffix(checkpoint.suffix + ".meta.json")
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return metadata.get("pose_repair_version") is not None
    return checkpoint.resolve() == OUTPUT_PATH.resolve()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "prepare-repaired", "train", "evaluate", "all", "all-repaired"))
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--inference-batch-size", type=int, default=16)
    parser.add_argument("--clip-len", type=int, default=450)
    parser.add_argument("--stride", type=int, default=225)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--patience", type=int, default=8)
    parser.add_argument("--validation-videos", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=20260711)
    parser.add_argument("--label-delay", type=int, default=LABEL_DELAY)
    parser.add_argument("--validation-label-delay", type=int)
    parser.add_argument("--class-weight-power", type=float, default=0.5)
    parser.add_argument("--no-gesture-weight-multiplier", type=float, default=1.0)
    parser.add_argument("--output-path", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--skip-test", action="store_true")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--checkpoint", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--raw-cache-root", type=Path, default=RAW_REPAIR_CACHE_ROOT)
    parser.add_argument("--repair-stats-path", type=Path, default=REPAIR_STATS_PATH)
    parser.add_argument("--pose-hold-frames", type=int, default=5)
    parser.add_argument("--arm-occlusion-probability", type=float, default=0.0)
    parser.add_argument("--force-prepare", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.cache_root is None:
        if arguments.command in ("prepare", "all"):
            arguments.cache_root = CACHE_ROOT
        elif arguments.command == "evaluate":
            arguments.cache_root = (
                REPAIRED_CACHE_ROOT if checkpoint_uses_pose_repair(arguments.checkpoint) else CACHE_ROOT
            )
        else:
            arguments.cache_root = REPAIRED_CACHE_ROOT
    if arguments.command in ("prepare", "all"):
        prepare("train", arguments.inference_batch_size, arguments.device, arguments.cache_root, arguments.force_prepare)
        prepare("test", arguments.inference_batch_size, arguments.device, arguments.cache_root, arguments.force_prepare)
    if arguments.command in ("prepare-repaired", "all-repaired"):
        repaired_cache_root = (
            arguments.cache_root if arguments.cache_root != CACHE_ROOT else REPAIRED_CACHE_ROOT
        )
        prepare_repaired(
            arguments.inference_batch_size,
            arguments.device,
            raw_cache_root=arguments.raw_cache_root,
            cache_root=repaired_cache_root,
            stats_path=arguments.repair_stats_path,
            hold_frames=arguments.pose_hold_frames,
            force=arguments.force_prepare,
        )
        arguments.cache_root = repaired_cache_root
    if arguments.command in ("train", "all", "all-repaired"):
        train(arguments)
    if arguments.command == "evaluate":
        evaluate_checkpoint(arguments.checkpoint, arguments.device, arguments.label_delay, arguments.cache_root)
