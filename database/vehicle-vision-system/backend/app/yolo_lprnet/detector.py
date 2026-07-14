"""YOLO 车牌检测，按参考项目的检测方式实现。"""

from __future__ import annotations

import os

import cv2
import numpy as np
from ultralytics import YOLO


class YOLOPlateDetector:
    def __init__(
        self,
        model_path,
        conf_threshold=0.4,
        iou_threshold=0.5,
        imgsz=960,
        max_det=20,
        min_box_width=0,
        min_box_height=0,
        min_plate_area_ratio=0.0015,
        max_plate_area_ratio=0.2,
        min_plate_aspect_ratio=1.8,
        max_plate_aspect_ratio=6.5,
        model=None,
    ):
        if model is None:
            if not os.path.exists(model_path):
                raise FileNotFoundError(f"未找到YOLO模型文件: {model_path}")
            model = YOLO(model_path)
        self.model = model
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.imgsz = imgsz
        self.max_det = max_det
        self.min_box_width = min_box_width
        self.min_box_height = min_box_height
        self.min_plate_area_ratio = min_plate_area_ratio
        self.max_plate_area_ratio = max_plate_area_ratio
        self.min_plate_aspect_ratio = min_plate_aspect_ratio
        self.max_plate_aspect_ratio = max_plate_aspect_ratio

    def _looks_like_plate(self, box, image_shape) -> bool:
        h, w = image_shape[:2]
        x1, y1, x2, y2 = box
        bw = max(0, x2 - x1)
        bh = max(0, y2 - y1)
        if bw <= 0 or bh <= 0:
            return False
        if bw < self.min_box_width or bh < self.min_box_height:
            return False
        area_ratio = (bw * bh) / float(max(1, w * h))
        aspect_ratio = bw / float(max(1, bh))
        if area_ratio < self.min_plate_area_ratio or area_ratio > self.max_plate_area_ratio:
            return False
        if aspect_ratio < self.min_plate_aspect_ratio or aspect_ratio > self.max_plate_aspect_ratio:
            return False
        return True

    def detect_plates(self, image, return_image=False):
        if isinstance(image, str):
            img = cv2.imread(image)
            if img is None:
                raise FileNotFoundError(f"无法读取图像: {image}")
        else:
            img = image.copy()

        results = self.model(img, conf=self.conf_threshold, iou=self.iou_threshold, imgsz=self.imgsz, max_det=self.max_det, verbose=False)
        plates = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            for box in boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                conf = box.conf[0].item()
                cls = box.cls[0].item()
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                if cls == 0 and self._looks_like_plate((x1, y1, x2, y2), img.shape):
                    plates.append([x1, y1, x2, y2, conf])
                    if return_image:
                        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                        label = f"车牌: {conf:.2f}"
                        try:
                            from PIL import Image, ImageDraw, ImageFont
                            img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                            draw = ImageDraw.Draw(img_pil)
                            try:
                                font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 16)
                            except:
                                font = ImageFont.load_default()
                            draw.text((x1, max(0, y1 - 20)), label, font=font, fill=(0, 255, 0))
                            img = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
                        except:
                            cv2.putText(img, label, (x1, max(0, y1 - 10)),
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        if return_image:
            return plates, img
        return plates
