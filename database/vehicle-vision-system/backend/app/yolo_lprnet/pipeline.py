"""YOLO + LPRNet 视频单帧处理。"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
import torch

from app.config import settings
from .charset import CHARS
from .detector import YOLOPlateDetector
from .lprnet import build_lprnet

logger = logging.getLogger(__name__)


def _first_existing(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.exists() and path.stat().st_size > 1024:
            return path
    return None


def resolve_yolo_lpr_weights() -> tuple[str, str]:
    root = (settings.base_dir / settings.yolo_lprnet_path).resolve()
    yolo = _first_existing([
        root / "weights" / "best.pt",
        root / "weights" / "yolo_best.pt",
        root / "yolo11n.pt",
        root / "yolov8n.pt",
        root / "runs" / "train" / "yolo_lpr" / "weights" / "best.pt",
        settings.base_dir / "backend" / "weights" / "yolo_best.pt",
    ])
    lpr = _first_existing([
        root / "weights" / "Final_LPRNet_model.pth",
        root / "weights" / "lprnet.pth",
        root / "weights" / "LPRNet_20251014142735.pth",
        settings.base_dir / "backend" / "weights" / "lprnet.pth",
    ])
    if not yolo:
        raise FileNotFoundError(
            f"未找到 YOLO 权重，请将 `best.pt` 放到 `{root / 'weights'}` 或 `{settings.base_dir / 'backend' / 'weights'}`"
        )
    if not lpr:
        raise FileNotFoundError(
            f"未找到 LPRNet 权重，请将 `Final_LPRNet_model.pth` 或 `lprnet.pth` 放到 `{root / 'weights'}` 或 `{settings.base_dir / 'backend' / 'weights'}`"
        )
    return str(yolo), str(lpr)


def _preprocess_lpr_crop(plate_image: np.ndarray) -> np.ndarray:
    img = cv2.resize(plate_image, (94, 24))
    img = img.astype("float32")
    img -= 127.5
    img *= 0.0078125
    img = np.transpose(img, (2, 0, 1))
    return np.expand_dims(img, axis=0)


def greedy_decode(prebs: torch.Tensor) -> str:
    if isinstance(prebs, torch.Tensor):
        prebs = prebs.detach().cpu().numpy()
    if prebs.ndim == 3:
        prebs = prebs[0]
    preb_label = [int(np.argmax(prebs[:, j], axis=0)) for j in range(prebs.shape[1])]
    no_repeat = []
    if preb_label:
        pre_c = preb_label[0]
        if pre_c != len(CHARS) - 1:
            no_repeat.append(pre_c)
        for c in preb_label[1:]:
            if pre_c == c or c == len(CHARS) - 1:
                if c == len(CHARS) - 1:
                    pre_c = c
                continue
            no_repeat.append(c)
            pre_c = c
    plate_str = "".join(CHARS[idx] for idx in no_repeat if 0 <= idx < len(CHARS))
    return plate_str or "无法识别"


class YoloLprPipeline:
    def __init__(self, yolo_path: str | None = None, lpr_path: str | None = None):
        if not yolo_path or not lpr_path:
            yolo_path, lpr_path = resolve_yolo_lpr_weights()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.detector = YOLOPlateDetector(yolo_path)
        self.lprnet = build_lprnet(lpr_max_len=8, phase=False, class_num=len(CHARS), dropout_rate=0.5)
        state = torch.load(lpr_path, map_location=self.device)
        self.lprnet.load_state_dict(state)
        self.lprnet.to(self.device)
        self.lprnet.eval()
        self.yolo_path = yolo_path
        self.lpr_path = lpr_path
        logger.info("YOLO+LPRNet 视频模型已加载 (device=%s)", self.device)

    def recognize_plate_crop(self, plate_image: np.ndarray) -> str:
        if plate_image is None or len(plate_image.shape) != 3:
            return "识别失败"
        img = _preprocess_lpr_crop(plate_image)
        img_tensor = torch.from_numpy(img).to(self.device)
        with torch.no_grad():
            prebs = self.lprnet(img_tensor)
        return greedy_decode(prebs)

    def process_frame(self, frame: np.ndarray) -> tuple[list[dict], np.ndarray]:
        result_frame = frame.copy()
        plate_coords = self.detector.detect_plates(frame)
        plate_results: list[dict] = []
        h, w = frame.shape[:2]
        for x1, y1, x2, y2, conf in plate_coords:
            x1 = max(0, min(x1, w))
            y1 = max(0, min(y1, h))
            x2 = max(0, min(x2, w))
            y2 = max(0, min(y2, h))
            if x2 <= x1 or y2 <= y1:
                continue
            plate_image = frame[y1:y2, x1:x2]
            plate_text = self.recognize_plate_crop(plate_image)
            plate_text_clean = plate_text.replace("无法识别", "").strip()
            if len(plate_text_clean) < 5:
                continue
            plate_results.append({
                "plate_number": plate_text,
                "bbox": [x1, y1, x2, y2],
                "coords": [x1, y1, x2, y2],
                "confidence": float(conf),
                "det_confidence": float(conf),
                "source": "yolo_lprnet",
                "indices": [],
            })
            cv2.rectangle(result_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"{plate_text} ({conf:.2f})"
            try:
                from PIL import Image, ImageDraw, ImageFont
                result_frame_pil = Image.fromarray(cv2.cvtColor(result_frame, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(result_frame_pil)
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 36)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((x1, max(0, y1 - 36)), label, font=font, fill=(255, 0, 0))
                result_frame = cv2.cvtColor(np.array(result_frame_pil), cv2.COLOR_RGB2BGR)
            except Exception:
                cv2.putText(result_frame, label, (x1, max(0, y1 - 10)), cv2.FONT_HERSHEY_COMPLEX, 0.5, (0, 0, 255), 2)
        return plate_results, result_frame
