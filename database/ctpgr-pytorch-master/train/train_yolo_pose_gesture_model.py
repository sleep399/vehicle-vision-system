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

from app.services.ctpgr_pose_adapter import coco_to_ctpgr
from constants.enum_keys import PG
from models.gesture_recognition_model import GestureRecognitionModel
from pgdataset.s3_handcraft import BoneLengthAngle


DATA_ROOT = Path.home() / "PoliceGestureLong"
CACHE_ROOT = ROOT / "generated" / "coords_yolo11s"
MODEL_NAME = "yolo11s-pose.pt"
MODEL_PATH = VEHICLE_ROOT / MODEL_NAME
OUTPUT_PATH = ROOT / "checkpoints" / "lstm_yolo11s.pt"
REPORT_PATH = ROOT / "generated" / "yolo11s_lstm_report.json"
IMAGE_SIZE = (512, 512)
LABEL_DELAY = 15
NUM_CLASSES = 9


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


def prepare(split: str, batch_size: int, device: str) -> None:
    model = YOLO(str(MODEL_PATH) if MODEL_PATH.is_file() else MODEL_NAME)
    videos = sorted((DATA_ROOT / split).glob("*.mp4"))
    if not videos:
        raise FileNotFoundError(f"no videos in {DATA_ROOT / split}")
    for index, video_path in enumerate(videos, 1):
        cache_path = CACHE_ROOT / split / f"{video_path.stem}.npz"
        if cache_path.exists():
            print(f"[{index}/{len(videos)}] cached: {cache_path.name}", flush=True)
            continue
        print(f"[{index}/{len(videos)}] preparing {video_path.name}", flush=True)
        prepare_video(model, video_path, cache_path, batch_size, device)


def delayed_labels(labels: np.ndarray) -> np.ndarray:
    if len(labels) <= LABEL_DELAY:
        return np.zeros_like(labels)
    return np.concatenate((np.zeros(LABEL_DELAY, dtype=labels.dtype), labels[:-LABEL_DELAY]))


def load_cache(split: str) -> list[dict[str, np.ndarray]]:
    bla = BoneLengthAngle()
    items = []
    for path in sorted((CACHE_ROOT / split).glob("*.npz")):
        data = np.load(path)
        coords = data["coord_norm"].astype(np.float32)
        feature_dict = bla.handcrafted_features(coords)
        features = np.concatenate(
            (feature_dict[PG.BONE_LENGTH], feature_dict[PG.BONE_ANGLE_COS], feature_dict[PG.BONE_ANGLE_SIN]),
            axis=1,
        ).astype(np.float32)
        items.append(
            {
                "name": path.stem,
                "features": features,
                "labels": delayed_labels(data["labels"].astype(np.int64)),
                "valid": data["valid"].astype(np.bool_),
            }
        )
    if not items:
        raise FileNotFoundError(f"no caches in {CACHE_ROOT / split}")
    return items


class WindowDataset(Dataset):
    def __init__(self, videos: list[dict[str, np.ndarray]], clip_len: int, stride: int):
        self.videos = videos
        self.windows: list[tuple[int, int]] = []
        for video_index, video in enumerate(videos):
            length = len(video["labels"])
            starts = list(range(0, max(1, length - clip_len + 1), stride))
            final_start = max(0, length - clip_len)
            if not starts or starts[-1] != final_start:
                starts.append(final_start)
            self.windows.extend((video_index, start) for start in starts)
        self.clip_len = clip_len

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, index: int):
        video_index, start = self.windows[index]
        video = self.videos[video_index]
        end = start + self.clip_len
        return (
            torch.from_numpy(video["features"][start:end]),
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
    return {"macro_f1": float(np.mean(f1_values)), "per_class": rows, "confusion_matrix": confusion.tolist()}


@torch.no_grad()
def evaluate_model(model: GestureRecognitionModel, videos: list[dict[str, np.ndarray]], device: torch.device) -> dict:
    model.eval()
    confusion = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
    for video in videos:
        features = torch.from_numpy(video["features"]).to(device).unsqueeze(1)
        h = torch.zeros((1, 1, model.num_hidden), device=device)
        c = torch.zeros_like(h)
        _, _, _, logits = model(features, h, c)
        predictions = logits.argmax(dim=1).cpu().numpy()
        mask = video["valid"]
        targets = video["labels"][mask]
        predictions = predictions[mask]
        np.add.at(confusion, (targets, predictions), 1)
    return confusion_metrics(confusion)


def train(args) -> dict:
    seed_everything(args.seed)
    all_train = load_cache("train")
    validation_count = min(args.validation_videos, max(1, len(all_train) - 1))
    train_videos = all_train[:-validation_count]
    validation_videos = all_train[-validation_count:]
    test_videos = load_cache("test")

    device = torch.device(args.device)
    model = GestureRecognitionModel(args.batch_size).to(device)
    dataset = WindowDataset(train_videos, args.clip_len, args.stride)
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, drop_last=True)

    counts = np.zeros(NUM_CLASSES, dtype=np.int64)
    for video in train_videos:
        counts += np.bincount(video["labels"][video["valid"]], minlength=NUM_CLASSES)
    weights = np.sqrt(counts.sum() / np.maximum(counts, 1))
    weights /= weights.mean()
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32, device=device), reduction="none")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)

    best_f1 = -1.0
    stale_epochs = 0
    history = []
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            loss = frame_loss[valid].mean()
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            losses.append(float(loss.item()))

        validation = evaluate_model(model, validation_videos, device)
        epoch_row = {"epoch": epoch, "loss": float(np.mean(losses)), "val_macro_f1": validation["macro_f1"]}
        history.append(epoch_row)
        print(json.dumps(epoch_row), flush=True)
        if validation["macro_f1"] > best_f1:
            best_f1 = validation["macro_f1"]
            stale_epochs = 0
            torch.save(model.state_dict(), OUTPUT_PATH)
        else:
            stale_epochs += 1
            if stale_epochs >= args.patience:
                print(f"early stopping at epoch {epoch}", flush=True)
                break

    model.load_state_dict(torch.load(OUTPUT_PATH, map_location=device, weights_only=True))
    report = {
        "model": str(OUTPUT_PATH),
        "pose_model": str(MODEL_PATH),
        "train_videos": [video["name"] for video in train_videos],
        "validation_videos": [video["name"] for video in validation_videos],
        "history": history,
        "validation": evaluate_model(model, validation_videos, device),
        "test": evaluate_model(model, test_videos, device),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"best_val_macro_f1": report["validation"]["macro_f1"], "test_macro_f1": report["test"]["macro_f1"]}), flush=True)
    return report


def evaluate_checkpoint(checkpoint: Path, device_name: str) -> dict:
    device = torch.device(device_name)
    model = GestureRecognitionModel(1).to(device)
    model.load_state_dict(torch.load(checkpoint, map_location=device, weights_only=True))
    report = evaluate_model(model, load_cache("test"), device)
    print(json.dumps({"checkpoint": str(checkpoint), **report}, ensure_ascii=False), flush=True)
    return report


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "train", "evaluate", "all"))
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--inference-batch-size", type=int, default=16)
    parser.add_argument("--clip-len", type=int, default=450)
    parser.add_argument("--stride", type=int, default=225)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--patience", type=int, default=8)
    parser.add_argument("--validation-videos", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=20260711)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--checkpoint", type=Path, default=OUTPUT_PATH)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.command in ("prepare", "all"):
        prepare("train", arguments.inference_batch_size, arguments.device)
        prepare("test", arguments.inference_batch_size, arguments.device)
    if arguments.command in ("train", "all"):
        train(arguments)
    if arguments.command == "evaluate":
        evaluate_checkpoint(arguments.checkpoint, arguments.device)
