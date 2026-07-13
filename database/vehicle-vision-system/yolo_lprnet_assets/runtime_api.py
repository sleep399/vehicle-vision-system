"""可被 backend 调用的 YOLO+LPRNet 运行接口。"""

from __future__ import annotations

import os
import re
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

from yolo_utils import YOLOPlateDetector
from model.LPRNet import build_lprnet
from app.yolo_lprnet.charset import CHARS
from app.utils.plate_color import resolve_plate_color
from demo_integrated_lpr import greedy_decode

PLATE_RE = re.compile(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,6}$")


@dataclass
class YoloLprConfig:
    yolo_model: str
    lpr_model: str
    device: str = "cuda" if torch.cuda.is_available() else "cpu"
    yolo_conf: float = 0.3
    yolo_iou: float = 0.5
    yolo_imgsz: int = 1280
    yolo_max_det: int = 20
    min_box_width: int = 18
    min_box_height: int = 8
    max_aspect_ratio: float = 8.0


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
            max_aspect_ratio=self.config.max_aspect_ratio,
        )
        self.recognizer = build_lprnet(lpr_max_len=8, phase=False, class_num=len(CHARS), dropout_rate=0.5)
        state = torch.load(self.config.lpr_model, map_location=self.config.device)
        self.recognizer.load_state_dict(state)
        self.recognizer.to(self.config.device)
        self.recognizer.eval()
        self.min_confidence = 0.35
        self.min_plate_length = 4

    def recognize_plate(self, plate_image: np.ndarray) -> str:
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
        return greedy_decode(prebs, CHARS)

    def _is_likely_plate_text(self, text: str) -> bool:
        if not text:
            return False
        if len(text) < self.min_plate_length:
            return False
        if PLATE_RE.match(text):
            return True
        if len(text) == 7 and text[0] in CHARS[:31] and text[1].isalpha():
            return True
        return False

    def process_frame(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        result_frame = frame.copy()
        plate_results: list[dict[str, Any]] = []
        for x1, y1, x2, y2, conf in self.detector.detect_plates(frame):
            x1, y1, x2, y2 = map(int, [x1, y1, x2, y2])
            h, w = frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            plate_image = frame[y1:y2, x1:x2]
            plate_text = self.recognize_plate(plate_image)
            plate_text_clean = (plate_text or "").replace("无法识别", "").strip()
            if not self._is_likely_plate_text(plate_text_clean):
                continue
            plate_color = resolve_plate_color(frame, [x1, y1, x2, y2])
            plate_results.append({
                "coords": (x1, y1, x2, y2),
                "confidence": float(conf),
                "text": plate_text_clean,
                "plate_color": plate_color,
            })
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
