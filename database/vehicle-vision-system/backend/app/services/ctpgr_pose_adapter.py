"""Adapt COCO pose coordinates to the distribution used by CTPGR.

CTPGR predicts one argmax coordinate for every joint on a 64x64 heatmap,
without applying a visibility threshold.  YOLO coordinates are therefore
mapped to the AIChallenger joint order and quantized to that same grid before
the original CTPGR handcrafted feature extractor sees them.
"""

from __future__ import annotations

import numpy as np


CTPGR_HEATMAP_SIZE = 64


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
