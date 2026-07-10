from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2


ALLOWED_STREAM_SCHEMES = {"rtsp", "http", "https", "rtmp"}


def validate_stream_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_STREAM_SCHEMES or not parsed.netloc:
        raise ValueError("Only rtsp/http/https/rtmp stream URLs are supported")
    return parsed.geturl()


def process_video_file(
    service: Any,
    video_path: Path,
    sample_interval: int = 15,
    max_results: int = 60,
    max_sampled_frames: int = 120,
) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    results = []
    changes = []
    hit_count = 0
    total_frames = 0
    sampled_frames = 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    last_gesture = None
    sequence_state = service.create_sequence_state() if hasattr(service, "create_sequence_state") else None
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if total_frames % sample_interval == 0:
                if sampled_frames >= max_sampled_frames:
                    break
                sampled_frames += 1
                if hasattr(service, "recognize_frame_continuous"):
                    result = service.recognize_frame_continuous(frame, sequence_state)
                else:
                    result = service.recognize_frame(frame)
                row = {"frame": total_frames, "time_sec": round(total_frames / fps, 2) if fps else None, **result}
                results.append(row)
                if result.get("gesture") != last_gesture:
                    changes.append(row)
                    last_gesture = result.get("gesture")
                if result.get("success"):
                    hit_count += 1
                if hit_count >= max_results:
                    break
            total_frames += 1
    finally:
        cap.release()

    return {
        "frame_count": total_frames,
        "sampled_frames": sampled_frames,
        "result_count": hit_count,
        "results": results,
        "changes": changes,
    }
