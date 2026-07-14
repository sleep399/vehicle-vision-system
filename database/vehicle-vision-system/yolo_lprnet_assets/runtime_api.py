"""可被 backend 调用的 YOLO+LPRNet 运行接口。"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch

ASSET_ROOT = Path(__file__).resolve().parent
if str(ASSET_ROOT) not in sys.path:
    sys.path.insert(0, str(ASSET_ROOT))

from app.yolo_lprnet.detector import YOLOPlateDetector
from model.LPRNet import build_lprnet
from app.yolo_lprnet.charset import CHARS
from app.utils.plate_color import resolve_plate_color
from app.utils.plate_number import is_valid_plate_number
from demo_integrated_lpr import greedy_decode


@dataclass
class YoloLprConfig:
    yolo_model: str
    lpr_model: str
    device: str = "cuda" if torch.cuda.is_available() else "cpu"
    yolo_conf: float = 0.4
    yolo_iou: float = 0.5
    yolo_imgsz: int = 960
    yolo_max_det: int = 20
    min_box_width: int = 0
    min_box_height: int = 0
    max_aspect_ratio: float = 6.5
    min_plate_area_ratio: float = 0.0015
    max_plate_area_ratio: float = 0.2
    min_aspect_ratio: float = 1.8
    fallback_yolo_conf: float = 0.3
    fallback_yolo_imgsz: int = 1280


def find_default_models() -> YoloLprConfig:
    yolo_candidates = [
        ASSET_ROOT / "weights" / "best.pt",
        ASSET_ROOT / "weights" / "yolo11n.pt",
        ASSET_ROOT / "yolo11n.pt",
    ]
    lpr_candidates = [
        ASSET_ROOT / "weights" / "Final_LPRNet_model.pth",
        ASSET_ROOT / "weights" / "lprnet.pth",
    ]
    yolo = next((p for p in yolo_candidates if p.exists()), None)
    lpr = next((p for p in lpr_candidates if p.exists()), None)
    if not yolo:
        raise FileNotFoundError(f"未找到 YOLO 权重: {ASSET_ROOT / 'weights'}")
    if not lpr:
        raise FileNotFoundError(f"未找到 LPRNet 权重: {ASSET_ROOT / 'weights'}")
    return YoloLprConfig(str(yolo), str(lpr))


class YoloLprRuntime:
    def __init__(self, config: YoloLprConfig | None = None):
        self.config = config or find_default_models()
        self.detector = YOLOPlateDetector(
            self.config.yolo_model,
            conf_threshold=self.config.yolo_conf,
            iou_threshold=self.config.yolo_iou,
            imgsz=self.config.yolo_imgsz,
            max_det=self.config.yolo_max_det,
            min_box_width=self.config.min_box_width,
            min_box_height=self.config.min_box_height,
            min_plate_area_ratio=self.config.min_plate_area_ratio,
            max_plate_area_ratio=self.config.max_plate_area_ratio,
            min_plate_aspect_ratio=self.config.min_aspect_ratio,
            max_plate_aspect_ratio=self.config.max_aspect_ratio,
        )
        # 同时保留单图链路的 960 近景配置和原视频链路的 1280 小目标配置，
        # 两个检测器复用同一个 YOLO 模型，结果按位置和字符置信度合并。
        self.fallback_detector = YOLOPlateDetector(
            self.config.yolo_model,
            conf_threshold=self.config.fallback_yolo_conf,
            iou_threshold=self.config.yolo_iou,
            imgsz=self.config.fallback_yolo_imgsz,
            max_det=self.config.yolo_max_det,
            min_box_width=18,
            min_box_height=8,
            min_plate_area_ratio=0.0,
            max_plate_area_ratio=1.0,
            min_plate_aspect_ratio=1.5,
            max_plate_aspect_ratio=8.0,
            model=self.detector.model,
        )
        self.recognizer = build_lprnet(lpr_max_len=8, phase=False, class_num=len(CHARS), dropout_rate=0.5)
        state = torch.load(self.config.lpr_model, map_location=self.config.device)
        self.recognizer.load_state_dict(state)
        self.recognizer.to(self.config.device)
        self.recognizer.eval()
        self.min_confidence = 0.35
        self.min_plate_length = 4

    @staticmethod
    def _ctc_recognition_confidence(prebs: torch.Tensor) -> float:
        if prebs.ndim == 2:
            prebs = prebs.unsqueeze(0)
        probabilities = torch.softmax(prebs, dim=1)[0]
        max_probabilities, labels = probabilities.max(dim=0)
        blank_index = len(CHARS) - 1
        previous = blank_index
        character_probabilities: list[float] = []

        for label, probability in zip(labels.tolist(), max_probabilities.tolist()):
            if label == blank_index:
                previous = blank_index
                continue
            if label == previous:
                character_probabilities[-1] = max(character_probabilities[-1], probability)
            else:
                character_probabilities.append(probability)
            previous = label

        if not character_probabilities:
            return 0.0
        log_mean = sum(math.log(max(value, 1e-12)) for value in character_probabilities)
        return float(math.exp(log_mean / len(character_probabilities)))

    def recognize_plate_with_confidence(self, plate_image: np.ndarray) -> tuple[str, float]:
        img = cv2.resize(plate_image, (94, 24))
        img = img.astype("float32")
        img -= 127.5
        img *= 0.0078125
        img = img.transpose(2, 0, 1)
        img = np.expand_dims(img, axis=0)
        img_tensor = torch.from_numpy(img)
        if self.config.device == "cuda" and torch.cuda.is_available():
            img_tensor = img_tensor.cuda()
        with torch.no_grad():
            prebs = self.recognizer(img_tensor)
        return greedy_decode(prebs, CHARS), self._ctc_recognition_confidence(prebs)

    def recognize_plate(self, plate_image: np.ndarray) -> str:
        text, _ = self.recognize_plate_with_confidence(plate_image)
        return text

    def _is_likely_plate_text(self, text: str) -> bool:
        return len(text or "") >= self.min_plate_length and is_valid_plate_number(text)

    def _recognize_with_detector(
        self,
        frame: np.ndarray,
        detector: YOLOPlateDetector,
    ) -> list[dict[str, Any]]:
        plate_results: list[dict[str, Any]] = []
        for x1, y1, x2, y2, conf in detector.detect_plates(frame):
            x1, y1, x2, y2 = map(int, [x1, y1, x2, y2])
            h, w = frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            plate_image = frame[y1:y2, x1:x2]
            plate_text, recognition_confidence = self.recognize_plate_with_confidence(plate_image)
            plate_text_clean = (plate_text or "").replace("无法识别", "").strip()
            if not self._is_likely_plate_text(plate_text_clean):
                continue
            plate_color = resolve_plate_color(frame, [x1, y1, x2, y2])
            plate_results.append({
                "coords": (x1, y1, x2, y2),
                "confidence": float(conf),
                "recognition_confidence": recognition_confidence,
                "text": plate_text_clean,
                "plate_color": plate_color,
            })
        return plate_results

    @staticmethod
    def _box_iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
        x1 = max(first[0], second[0])
        y1 = max(first[1], second[1])
        x2 = min(first[2], second[2])
        y2 = min(first[3], second[3])
        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        if intersection <= 0:
            return 0.0
        first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
        second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
        return intersection / float(max(1, first_area + second_area - intersection))

    @classmethod
    def _merge_multiscale_results(
        cls,
        *result_groups: list[dict[str, Any]],
        iou_threshold: float = 0.45,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for candidate in (item for group in result_groups for item in group):
            best_index = -1
            best_iou = 0.0
            for index, current in enumerate(merged):
                overlap = cls._box_iou(candidate["coords"], current["coords"])
                if overlap > best_iou:
                    best_index = index
                    best_iou = overlap
            if best_index < 0 or best_iou < iou_threshold:
                merged.append(candidate)
                continue

            current = merged[best_index]
            candidate_score = float(candidate.get("recognition_confidence", 0.0))
            current_score = float(current.get("recognition_confidence", 0.0))
            if (
                candidate_score > current_score
                or (
                    candidate_score == current_score
                    and float(candidate.get("confidence", 0.0)) > float(current.get("confidence", 0.0))
                )
            ):
                merged[best_index] = candidate

        merged.sort(
            key=lambda item: (
                float(item.get("recognition_confidence", 0.0)),
                float(item.get("confidence", 0.0)),
            ),
            reverse=True,
        )
        return merged

    def process_frame(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        result_frame = frame.copy()
        primary_results = self._recognize_with_detector(frame, self.detector)
        fallback_results = []
        if self.config.fallback_yolo_imgsz > self.config.yolo_imgsz:
            fallback_results = self._recognize_with_detector(frame, self.fallback_detector)
        plate_results = self._merge_multiscale_results(primary_results, fallback_results)

        for plate in plate_results:
            x1, y1, x2, y2 = plate["coords"]
            plate_text_clean = plate["text"]
            plate_color = plate["plate_color"]
            cv2.rectangle(result_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"{plate_text_clean} ({plate_color})"
            try:
                from PIL import Image, ImageDraw, ImageFont
                pil = Image.fromarray(cv2.cvtColor(result_frame, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(pil)
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 28)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((x1, max(0, y1 - 28)), label, font=font, fill=(255, 0, 0))
                result_frame = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
            except Exception:
                cv2.putText(result_frame, label, (x1, max(0, y1 - 10)), cv2.FONT_HERSHEY_COMPLEX, 0.5, (0, 0, 255), 2)
        return result_frame, plate_results

    def process_image_path(self, image_path: str) -> tuple[np.ndarray, list[dict[str, Any]]]:
        img = cv2.imread(image_path)
        if img is None:
            raise FileNotFoundError(f"无法读取图像: {image_path}")
        return self.process_frame(img)

    def process_video_path(self, video_path: str, sample_interval: int = 1):
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"无法打开视频: {video_path}")
        results = []
        idx = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if idx % sample_interval == 0:
                result_frame, plate_results = self.process_frame(frame)
                results.append({"frame_index": idx, "result_frame": result_frame, "plates": plate_results})
            idx += 1
        cap.release()
        return results
