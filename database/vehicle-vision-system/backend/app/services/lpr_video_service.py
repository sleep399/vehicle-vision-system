"""视频/实时流车牌识别，直接调用 `yolo_lprnet_assets.runtime_api`。"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.config import settings
from app.utils.helpers import ndarray_to_base64

logger = logging.getLogger(__name__)
ASSET_ROOT = (settings.base_dir / "yolo_lprnet_assets").resolve()
if str(ASSET_ROOT) not in sys.path:
    sys.path.insert(0, str(ASSET_ROOT))


class LprVideoService:
    """把后端输入封装成 `yolo_lprnet_assets` 的输入，再把结果转回后端格式。"""

    def __init__(self) -> None:
        self._error: str | None = None
        self._runtime = None
        self._yolo_path: str | None = None
        self._lpr_path: str | None = None
        self._stream_jobs: dict[str, dict[str, Any]] = {}
        self._stream_lock = threading.Lock()
        self._preview_jobs: dict[str, dict[str, Any]] = {}
        self._rtsp_history_ready: set[str] = set()

    def _resolve_weights(self) -> tuple[str, str]:
        from runtime_api import find_default_models
        cfg = find_default_models()
        return cfg.yolo_model, cfg.lpr_model

    def _load_runtime(self):
        if self._runtime is not None and self._error is None:
            return
        if self._error is not None:
            return
        try:
            from runtime_api import YoloLprRuntime, YoloLprConfig
            yolo_path, lpr_path = self._resolve_weights()
            self._yolo_path, self._lpr_path = yolo_path, lpr_path
            self._runtime = YoloLprRuntime(YoloLprConfig(yolo_model=yolo_path, lpr_model=lpr_path))
        except Exception as exc:
            self._error = str(exc)
            logger.exception("加载 yolo_lprnet_assets runtime 失败: %s", exc)

    def model_available(self) -> bool:
        self._load_runtime()
        return self._error is None and self._runtime is not None

    def model_status(self) -> dict[str, Any]:
        if self.model_available():
            return {
                "model_available": True,
                "engine": "yolo_lprnet",
                "yolo_path": self._yolo_path,
                "lpr_path": self._lpr_path,
                "message": "YOLO+LPRNet 视频识别已就绪",
            }
        return {
            "model_available": False,
            "engine": "yolo_lprnet",
            "message": self._error or f"请将权重放到 `{ASSET_ROOT / 'weights'}`",
        }

    def _is_valid_plate_text(self, text: str) -> bool:
        text = (text or "").replace("无法识别", "").strip()
        if len(text) < 4:
            return False
        return True

    def _extract_plate_identity(self, plate: dict[str, Any]) -> str:
        return str(plate.get("text") or plate.get("plate_number") or "").replace("无法识别", "").strip()

    def _filter_and_fuse_plate_results(self, plate_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fused: dict[str, dict[str, Any]] = {}
        for p in plate_results:
            plate_text = self._extract_plate_identity(p)
            if not self._is_valid_plate_text(plate_text):
                continue
            plate_color = str(p.get("plate_color") or "蓝牌")
            key = f"{plate_text}|{plate_color}"
            agg = fused.setdefault(key, {
                "plate_number": plate_text,
                "plate_color": plate_color,
                "confidence_sum": 0.0,
                "hit_count": 0,
                "max_confidence": 0.0,
                "coords": p.get("coords", (0, 0, 0, 0)),
            })
            confidence = float(p.get("confidence", 0.0))
            agg["confidence_sum"] += confidence
            agg["hit_count"] += 1
            agg["max_confidence"] = max(agg["max_confidence"], confidence)
        filtered = []
        for agg in fused.values():
            if agg["hit_count"] < 2 and agg["max_confidence"] < 0.55:
                continue
            filtered.append({
                "text": agg["plate_number"],
                "plate_color": agg["plate_color"],
                "confidence": round(agg["confidence_sum"] / max(agg["hit_count"], 1), 3),
                "max_confidence": round(agg["max_confidence"], 3),
                "hit_count": agg["hit_count"],
                "coords": agg["coords"],
            })
        return filtered

    def recognize_frame(self, frame: np.ndarray, frame_index: int = 0) -> dict[str, Any]:
        self._load_runtime()
        if not self.model_available():
            return {
                "plates": [],
                "plate_count": 0,
                "annotated_image": ndarray_to_base64(frame),
                "success": False,
                "source": "yolo_lprnet",
                "model_available": False,
                "frame": frame_index,
                "error": self._error,
            }
        logger.info("[LPR-FRAME] start frame=%s shape=%s dtype=%s min=%s max=%s", frame_index, getattr(frame, "shape", None), getattr(frame, "dtype", None), int(frame.min()) if frame is not None and frame.size else None, int(frame.max()) if frame is not None and frame.size else None)
        result_frame, plate_results = self._runtime.process_frame(frame)
        logger.info("[LPR-FRAME] raw_candidates frame=%s count=%s", frame_index, len(plate_results))
        filtered_results = self._filter_and_fuse_plate_results(plate_results)
        for idx, p in enumerate(filtered_results):
            logger.info(
                "[LPR-FRAME] candidate frame=%s idx=%s text=%s conf=%.3f hit=%s coords=%s",
                frame_index,
                idx,
                p.get("text", ""),
                float(p.get("confidence", 0.0)),
                p.get("hit_count", 0),
                p.get("coords", None),
            )
        result = {
            "plates": [
                {
                    "plate_number": p.get("text", "无法识别"),
                    "plate_color": p.get("plate_color", "蓝牌"),
                    "bbox": list(p.get("coords", (0, 0, 0, 0))),
                    "indices": [],
                    "confidence": float(p.get("confidence", 0.0)),
                    "hit_count": int(p.get("hit_count", 0)),
                    "max_confidence": float(p.get("max_confidence", 0.0)),
                    "source": "yolo_lprnet",
                }
                for p in filtered_results
            ],
            "plate_count": len(filtered_results),
            "annotated_image": ndarray_to_base64(result_frame),
            "success": len(filtered_results) > 0,
            "source": "yolo_lprnet",
            "model_available": True,
            "frame": frame_index,
            "message": "YOLO+LPRNet 视频实时识别",
        }
        logger.info("[LPR-FRAME] done frame=%s plates=%s", frame_index, result.get("plate_count"))
        return result

    def recognize_bytes(self, image_bytes: bytes, frame_index: int = 0) -> dict[str, Any]:
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("无法解析视频帧")
        return self.recognize_frame(frame, frame_index)

    def _find_ffmpeg_bin(self) -> str:
        candidates = [
            settings.base_dir.parent / "ffmpeg-master-latest-win64-gpl-shared" / "ffmpeg-master-latest-win64-gpl-shared" / "bin" / "ffmpeg.exe",
            settings.base_dir / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(r"D:\pratical trainning2\database\ffmpeg-master-latest-win64-gpl-shared\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"),
            Path("ffmpeg.exe"),
            Path("ffmpeg"),
            Path(r"C:\\ffmpeg\\bin\\ffmpeg.exe"),
            Path(r"C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe"),
        ]
        for item in candidates:
            if item.exists():
                return str(item)
        return "ffmpeg.exe"

    def _build_ffmpeg_command(self, width: int, height: int, fps: int, dst_url: str) -> list[str]:
        return [
            self._find_ffmpeg_bin(),
            "-y",
            "-loglevel", "info",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-an",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", "2M",
            "-f", "rtsp",
            dst_url,
        ]

    def _build_ffmpeg_file_command(self, video_path: Path, dst_url: str, fps: int | None = None) -> list[str]:
        command = [
            self._find_ffmpeg_bin(),
            "-y",
            "-loglevel", "info",
            "-re",
            "-stream_loop", "-1",
            "-i", str(video_path),
            "-an",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", "2M",
        ]
        if fps:
            command += ["-r", str(fps)]
        command += ["-f", "rtsp", dst_url]
        return command

    def _frame_to_mjpeg_chunk(self, frame: np.ndarray) -> bytes:
        ok, buf = cv2.imencode('.jpg', frame)
        if not ok:
            raise RuntimeError('JPEG 编码失败')
        return (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        )

    def _make_rtsp_worker(self, rtsp_url: str, source_name: str, stop_event: threading.Event, state: dict[str, Any]):
        def worker():
            cap2 = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            cap2.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            frame_index = 0
            logger.info("[LPR-RTSP] worker started input=%s source=%s", rtsp_url, source_name)
            try:
                while not stop_event.is_set():
                    ok, frame = cap2.read()
                    if not ok:
                        logger.info("[LPR-RTSP] frame read failed, retrying...")
                        time.sleep(0.3)
                        continue
                    if frame_index % 20 == 0:
                        logger.info("[LPR-RTSP] frame=%s shape=%s time=%s", frame_index, getattr(frame, 'shape', None), datetime.now().isoformat(timespec='seconds'))
                    result = self.recognize_frame(frame, frame_index)
                    frame_index += 1
                    state["latest"] = result
                    state["frame_index"] = frame_index
                    state["last_update"] = datetime.now().isoformat(timespec='seconds')
                    state["latest_frame"] = frame
                    fused_map = state.setdefault("fused_map", {})
                    for p in result.get("plates", []):
                        plate_number = (p.get("plate_number") or "").strip()
                        confidence = float(p.get("confidence", 0.0))
                        if not plate_number:
                            continue
                        key = (plate_number, p.get("plate_color", "蓝牌"))
                        agg = fused_map.setdefault(key, {
                            "plate_number": plate_number,
                            "plate_color": p.get("plate_color", "蓝牌"),
                            "confidence_sum": 0.0,
                            "hit_count": 0,
                            "max_confidence": 0.0,
                            "frames": [],
                            "source": "yolo_lprnet",
                        })
                        agg["confidence_sum"] += confidence
                        agg["hit_count"] += 1
                        agg["max_confidence"] = max(agg["max_confidence"], confidence)
                        agg["frames"].append(frame_index)
            finally:
                cap2.release()
                with self._stream_lock:
                    state["running"] = False
        return worker

    def start_rtsp_stream(self, rtsp_url: str, source_name: str = "live1", label: str = "") -> dict[str, Any]:
        self._load_runtime()
        if not self.model_available():
            raise RuntimeError(self._error or "YOLO+LPRNet 模型未加载")

        with self._stream_lock:
            job = self._stream_jobs.get(rtsp_url)
            if job and job.get("running"):
                return {"success": True, **job["meta"], "message": "RTSP 识别任务已在运行"}

            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                raise RuntimeError(f"无法打开 RTSP 流: {rtsp_url}")

            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
            fps = int(cap.get(cv2.CAP_PROP_FPS) or 25)
            cap.release()

            logger.info("[LPR-RTSP] launch direct input=%s size=%sx%s fps=%s", rtsp_url, width, height, fps)
            meta = {
                "success": True,
                "source": "yolo_lprnet",
                "model_available": True,
                "rtsp_url": rtsp_url,
                "dst_url": None,
                "source_name": source_name,
                "label": label,
                "message": f"RTSP 识别任务已启动：{label or source_name}",
                "width": width,
                "height": height,
                "fps": fps,
                "annotated_image": None,
                "plates": [],
                "plate_count": 0,
            }

            stop_event = threading.Event()
            state = {"running": True, "proc": None, "thread": None, "stop_event": stop_event, "meta": meta, "latest": None, "rtsp_url": rtsp_url, "source_name": source_name, "latest_frame": None, "frame_index": 0, "last_update": None, "fused_map": {}}
            worker = self._make_rtsp_worker(rtsp_url, source_name, stop_event, state)
            thread = threading.Thread(target=worker, daemon=True)
            state["thread"] = thread
            self._stream_jobs[rtsp_url] = state
            self._preview_jobs[source_name] = state
            thread.start()
            return {**meta, "preview_url": f"/api/lpr/preview/{source_name}.mjpg"}

    def stop_rtsp_stream(self, rtsp_url: str = "", source_name: str = "") -> dict[str, Any]:
        stopped_any = False
        history: dict[str, Any] | None = None
        with self._stream_lock:
            if rtsp_url and rtsp_url in self._stream_jobs:
                job = self._stream_jobs.get(rtsp_url)
                if job:
                    job["stop_event"].set()
                    history = self._build_history_record(job, source_type="rtsp")
                    try:
                        if job.get("proc") and job["proc"].poll() is None:
                            job["proc"].terminate()
                    except Exception:
                        pass
                    job["running"] = False
                    stopped_any = True
            if source_name and source_name in self._preview_jobs:
                job = self._preview_jobs.get(source_name)
                if job:
                    job["stop_event"].set()
                    history = self._build_history_record(job, source_type="rtsp")
                    try:
                        if job.get("proc") and job["proc"].poll() is None:
                            job["proc"].terminate()
                    except Exception:
                        pass
                    job["running"] = False
                    stopped_any = True
        return {"stopped": stopped_any, "message": "任务已停止" if stopped_any else "未找到对应任务", "history": history}

    def start_video_file_stream(self, video_path: Path, source_name: str = "video30") -> dict[str, Any]:
        self._load_runtime()
        if not self.model_available():
            raise RuntimeError(self._error or "YOLO+LPRNet 模型未加载")

        if not video_path.exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        cap = cv2.VideoCapture(str(video_path))
        fps = int(cap.get(cv2.CAP_PROP_FPS) or 25)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
        cap.release()
        dst_url = f"rtsp://127.0.0.1:8554/{source_name}"
        command = self._build_ffmpeg_file_command(video_path, dst_url, fps=fps)
        logger.info("[LPR-FILE] launch input=%s dst=%s size=%sx%s fps=%s cmd=%s", video_path, dst_url, width, height, fps, command)
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=False)
        try:
            preview = proc.stderr.readline().decode(errors='ignore').strip() if proc.stderr else ''
            if preview:
                logger.info("[LPR-FILE] ffmpeg first stderr: %s", preview)
        except Exception as exc:
            logger.warning("[LPR-FILE] unable to read ffmpeg stderr preview: %s", exc)

        stop_event = threading.Event()
        preview_state: dict[str, Any] = {"running": True, "latest": None, "frame_index": 0, "last_update": None}

        def worker():
            cap2 = cv2.VideoCapture(str(video_path))
            cap2.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            frame_index = 0
            logger.info("[LPR-PREVIEW] worker started input=%s", video_path)
            try:
                while not stop_event.is_set():
                    if proc.poll() is not None:
                        logger.error("[LPR-PREVIEW] ffmpeg exited early code=%s", proc.returncode)
                        break
                    ok, frame = cap2.read()
                    if not ok:
                        logger.info("[LPR-PREVIEW] frame read failed, looping from start")
                        cap2.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    if frame_index % 30 == 0:
                        logger.info("[LPR-PREVIEW] frame=%s shape=%s", frame_index, getattr(frame, 'shape', None))
                    result = self.recognize_frame(frame, frame_index)
                    preview_state["latest"] = result
                    preview_state["frame_index"] = frame_index
                    preview_state["last_update"] = datetime.now().isoformat(timespec='seconds')
                    frame_index += 1
                    time.sleep(max(0.0, 1.0 / max(fps, 1)))
            finally:
                cap2.release()
                try:
                    if proc.stderr:
                        tail = proc.stderr.read().decode(errors='ignore')
                        if tail:
                            logger.info("[LPR-PREVIEW] ffmpeg stderr tail: %s", tail[-1200:])
                except Exception as exc:
                    logger.warning("[LPR-PREVIEW] unable to read ffmpeg stderr tail: %s", exc)
                with self._stream_lock:
                    preview_state["running"] = False

        thread = threading.Thread(target=worker, daemon=True)
        preview_state.update({"proc": proc, "thread": thread, "stop_event": stop_event, "input_path": str(video_path), "dst_url": dst_url, "source_name": source_name, "hls_url": f"http://127.0.0.1:8888/{source_name}/index.m3u8"})
        self._preview_jobs[source_name] = preview_state
        thread.start()
        return {
            "success": True,
            "source": "video_file",
            "source_name": source_name,
            "message": f"视频推流已启动：{video_path.name}",
            "input_path": str(video_path),
            "dst_url": dst_url,
            "width": width,
            "height": height,
            "fps": fps,
            "proc_pid": proc.pid,
            "hls_url": f"http://127.0.0.1:8888/{source_name}/index.m3u8",
            "preview_url": f"/api/lpr/preview/{source_name}.mjpg",
        }

    def preview_frame_generator(self, source_name: str):
        job = self._preview_jobs.get(source_name)
        if not job:
            raise FileNotFoundError(f"预览任务不存在: {source_name}")
        boundary = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
        while job.get("running") or job.get("latest") is not None:
            latest = job.get("latest")
            if latest and latest.get("annotated_image"):
                img_bytes = base64.b64decode(latest["annotated_image"].split(",", 1)[-1])
                yield boundary + img_bytes + b'\r\n'
            else:
                blank = np.zeros((720, 1280, 3), dtype=np.uint8)
                cv2.putText(blank, "Waiting for video...", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
                ok, buf = cv2.imencode('.jpg', blank)
                if ok:
                    yield boundary + buf.tobytes() + b'\r\n'
            time.sleep(0.12)

    def stop_preview_stream(self, source_name: str) -> dict[str, Any]:
        job = self._preview_jobs.get(source_name)
        if not job:
            return {"stopped": False, "message": "未找到对应预览任务"}
        job["stop_event"].set()
        try:
            if job.get("proc") and job["proc"].poll() is None:
                job["proc"].terminate()
        except Exception:
            pass
        job["running"] = False
        return {"stopped": True, "message": "预览任务已停止"}

    def preview_status(self, source_name: str) -> dict[str, Any]:
        job = self._preview_jobs.get(source_name)
        if not job:
            return {"running": False, "found": False, "source_name": source_name}
        latest = job.get("latest") or {}
        return {
            "found": True,
            "running": bool(job.get("running")),
            "source_name": source_name,
            "frame_index": job.get("frame_index"),
            "last_update": job.get("last_update"),
            "dst_url": job.get("dst_url"),
            "hls_url": job.get("hls_url"),
            "plate_count": latest.get("plate_count", 0),
            "plates": latest.get("plates", []),
            "history": self._build_history_record(job, source_type="video", dry_run=True),
        }

    def _build_history_record(self, job: dict[str, Any], source_type: str = "video", dry_run: bool = False) -> dict[str, Any]:
        fused_map = job.get("fused_map") or {}
        records = []
        for agg in fused_map.values():
            hit_count = int(agg.get("hit_count", 0))
            if hit_count <= 0:
                continue
            records.append({
                "plate_number": agg.get("plate_number", "未识别"),
                "plate_color": agg.get("plate_color", "蓝牌"),
                "confidence": round((float(agg.get("confidence_sum", 0.0)) / max(hit_count, 1)), 3),
                "max_confidence": round(float(agg.get("max_confidence", 0.0)), 3),
                "hit_count": hit_count,
                "frames": sorted(set(agg.get("frames", []))),
                "frame_index": agg.get("frames", [None])[0],
                "source": agg.get("source", "yolo_lprnet"),
            })
        records.sort(key=lambda x: (x.get("hit_count", 0), x.get("max_confidence", 0)), reverse=True)
        if not records:
            records = [{"plate_number": "未识别", "plate_color": "无", "confidence": 0.0, "max_confidence": 0.0, "hit_count": 0, "frames": [], "frame_index": None, "source": "yolo_lprnet"}]
        payload = {
            "source_type": source_type,
            "plates": records,
            "plate_count": 0 if records[0].get("plate_number") == "未识别" else len(records),
            "frame_index": job.get("frame_index"),
            "last_update": job.get("last_update"),
            "source_name": job.get("source_name"),
            "rtsp_url": job.get("rtsp_url"),
        }
        if not dry_run:
            job["history"] = payload
        return payload

    def process_video(self, video_path: Path, sample_interval: int = 5) -> dict[str, Any]:
        self._load_runtime()
        logger.info("[LPR-VIDEO] process_video start path=%s interval=%s runtime=%s error=%s", video_path, sample_interval, bool(self._runtime), self._error)
        if not self.model_available():
            logger.error("[LPR-VIDEO] runtime unavailable error=%s", self._error)
            raise RuntimeError(self._error or "YOLO+LPRNet 模型未加载")

        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        logger.info("[LPR-VIDEO] video opened fps=%s total_frames=%s", fps, total_frames)

        video_results = self._runtime.process_video_path(str(video_path), sample_interval=max(2, sample_interval))
        logger.info("[LPR-VIDEO] runtime returned frames=%s", len(video_results))
        results: list[dict[str, Any]] = []
        annotated_paths: list[Path] = []
        for item in video_results:
            result_frame = item.get("result_frame")
            plates = item.get("plates", [])
            frame_idx = int(item.get("frame_index", 0))
            frame_path = video_path.parent / f"{video_path.stem}_annotated_{frame_idx:06d}.jpg"
            if result_frame is not None:
                ok = cv2.imwrite(str(frame_path), result_frame)
                logger.info("[LPR-VIDEO] frame saved idx=%s path=%s ok=%s", frame_idx, frame_path, ok)
                annotated_paths.append(frame_path)
            results.append({
                "frame_index": frame_idx,
                "plates": [
                    {
                        "plate_number": p.get("text", "无法识别"),
                        "plate_color": p.get("plate_color", "蓝牌"),
                        "bbox": list(p.get("coords", (0, 0, 0, 0))),
                        "indices": [],
                        "confidence": float(p.get("confidence", 0.0)),
                        "source": "yolo_lprnet",
                    }
                    for p in plates
                ],
                "plate_count": len(plates),
                "annotated_image": ndarray_to_base64(result_frame) if result_frame is not None else None,
                "success": len(plates) > 0,
                "source": "yolo_lprnet",
                "model_available": True,
            })

        best = max(results, key=lambda item: sum(p.get("confidence", 0) for p in item.get("plates", []))) if results else None
        annotated_video_path = None
        if annotated_paths:
            first_frame = cv2.imread(str(annotated_paths[0]))
            if first_frame is not None:
                h, w = first_frame.shape[:2]
                annotated_video_path = video_path.parent / f"{video_path.stem}_annotated.mp4"
                writer = cv2.VideoWriter(str(annotated_video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
                logger.info("[LPR-VIDEO] writing annotated video path=%s size=%sx%s frames=%s", annotated_video_path, w, h, len(annotated_paths))
                for p in annotated_paths:
                    img = cv2.imread(str(p))
                    if img is not None:
                        writer.write(img)
                writer.release()
                logger.info("[LPR-VIDEO] annotated video written exists=%s", annotated_video_path.exists())
        logger.info("[LPR-VIDEO] process_video done best=%s annotated=%s", bool(best), annotated_video_path)
        return {
            "frame_count": len(results),
            "total_frames": len(video_results),
            "results": results,
            "best": best,
            "annotated_frames": [str(p) for p in annotated_paths],
            "annotated_video_path": str(annotated_video_path) if annotated_video_path else None,
            "model_available": True,
        }


lpr_video_service = LprVideoService()
