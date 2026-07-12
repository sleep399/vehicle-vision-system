import io
import math
import time
from pathlib import Path
from collections import Counter, deque
from typing import Any, Tuple, List
import cv2
import numpy as np
import mediapipe as mp
from PIL import Image, ImageSequence
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from app.config import settings
from app.utils.helpers import ndarray_to_base64
from app.utils.model_loader import get_model_path

# 8 种手势 + 无手势, 全部支持中英文与对应车辆控制动作
OWNER_GESTURES = {
    "no_gesture":   ("no_gesture",  "无手势",    None),
    "palm_open":    ("palm_open",   "手掌张开",  "wake"),
    "fist":         ("fist",        "握拳",      "confirm"),
    "circle":       ("circle",      "单指画圈",  "volume_adjust"),
    "point_left":   ("point_left",  "单指指左",  "prev_page"),
    "point_right":  ("point_right", "单指指右",  "next_page"),
    "thumb_up":     ("thumb_up",    "拇指向上",  "answer_call"),
    "thumb_down":   ("thumb_down",  "拇指向下",  "hang_up"),
    "wave":         ("wave",        "挥手",      "go_home"),
}

# 需要“低置信度二次确认”的动作 (requirement 4: 复合确认降低误操作)
CONFIRM_REQUIRED_ACTIONS = {"confirm"}
# 低于该置信度时, 触发确认流程而非直接执行
CONFIRM_CONFIDENCE_THRESHOLD = 0.85
ACTION_CONFIDENCE_THRESHOLD = 0.78
MOTION_CONFIDENCE_THRESHOLD = 0.72


class HandLandmark:
    WRIST = 0
    THUMB_CMC = 1
    THUMB_MCP = 2
    THUMB_IP = 3
    THUMB_TIP = 4
    INDEX_FINGER_MCP = 5
    INDEX_FINGER_PIP = 6
    INDEX_FINGER_DIP = 7
    INDEX_FINGER_TIP = 8
    MIDDLE_FINGER_MCP = 9
    MIDDLE_FINGER_PIP = 10
    MIDDLE_FINGER_DIP = 11
    MIDDLE_FINGER_TIP = 12
    RING_FINGER_MCP = 13
    RING_FINGER_PIP = 14
    RING_FINGER_DIP = 15
    RING_FINGER_TIP = 16
    PINKY_MCP = 17
    PINKY_PIP = 18
    PINKY_DIP = 19
    PINKY_TIP = 20

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12), (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20), (5, 9), (9, 13), (13, 17),
]

class OwnerGestureService:
    """车主手势控车: MediaPipe Hand Landmarker + 启发式规则 + 持续时间/复合确认防抖"""

    # 浏览器预览和上传到后端的画面都未做水平翻转，直接采用图像坐标方向。
    # 若此处再次镜像，会造成“向左指识别为向右指”。
    MIRROR_POINT_DIRECTIONS = False
    REALTIME_DYNAMIC_WINDOW_SEC = 1.0
    REALTIME_DYNAMIC_MIN_SCORE = {
        "wave": 1.05,
        "circle": 1.15,
        "point_left": 1.6,
        "point_right": 1.6,
    }
    REALTIME_DYNAMIC_MIN_COUNT = {
        "wave": 2,
        "circle": 2,
        "point_left": 2,
        "point_right": 2,
    }
    REALTIME_STATIC_MIN_SCORE = {
        "fist": 1.35,
        "thumb_up": 1.45,
        "thumb_down": 1.45,
        "palm_open": 2.25,
        "point_left": 2.3,
        "point_right": 2.3,
    }
    REALTIME_STATIC_MIN_COUNT = {
        "fist": 2,
        "thumb_up": 2,
        "thumb_down": 2,
        "palm_open": 3,
        "point_left": 3,
        "point_right": 3,
    }
    REALTIME_CONFIRM_SEC = {
        "fist": 0.18,
        "palm_open": 0.26,
        "circle": 0.22,
        "point_left": 0.18,
        "point_right": 0.18,
        "wave": 0.12,
        "thumb_up": 0.22,
        "thumb_down": 0.22,
    }
    REALTIME_SWITCH_CONFIRM_SEC = {
        "fist": 0.14,
        "palm_open": 0.26,
        "circle": 0.24,
        "point_left": 0.20,
        "point_right": 0.20,
        "wave": 0.10,
        "thumb_up": 0.22,
        "thumb_down": 0.22,
    }
    REALTIME_KEEP_SEC = 0.18
    REALTIME_KEEP_SEC_BY_GESTURE = {
        "fist": 0.34,
        "palm_open": 0.22,
        "point_left": 0.24,
        "point_right": 0.24,
    }
    HOLD_REQUIRED = {
        "palm_open":   1,
        "fist":        1,
        "thumb_up":    1,
        "thumb_down":  1,
        "circle":      1,
        "point_left":   1,
        "point_right":  1,
        "wave":        1,
    }
    COOLDOWN_SEC = 0.45
    GESTURE_COOLDOWN_SEC = {
        "wave": 0.12,
    }

    def __init__(self):
        self.landmarker = None
        self._use_tasks_landmarker = False
        self._hands_fallback = None
        self._init_error = None

        try:
            options = vision.HandLandmarkerOptions(
                base_options=python.BaseOptions(
                    model_asset_path=get_model_path("hand_landmarker.task")),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=1,
                min_hand_detection_confidence=0.3,
                min_hand_presence_confidence=0.3,
                min_tracking_confidence=0.3,
            )
            self.landmarker = vision.HandLandmarker.create_from_options(options)
            self._use_tasks_landmarker = True
        except Exception as exc:
            self._init_error = str(exc)
            try:
                self._hands_fallback = mp.solutions.hands.Hands(
                    static_image_mode=True,
                    max_num_hands=1,
                    model_complexity=1,
                    min_detection_confidence=0.3,
                    min_tracking_confidence=0.3,
                )
            except Exception as exc2:
                self._init_error = f"{self._init_error}; fallback failed: {exc2}"
                self._hands_fallback = None

        self._position_history: deque = deque(maxlen=30)
        self._hand_center_history: deque = deque(maxlen=30)
        self._circle_points: deque = deque(maxlen=40)
        self._v_sign_history: deque = deque(maxlen=8)
        self._hold_counter: dict = {}
        self._last_emitted: str = "no_gesture"
        self._last_action_time: float = 0.0
        self._last_frame_time: float = time.time()
        self._motion_lock_until: float = 0.0
        self._swipe_mode_until: float = 0.0
        self._swipe_start_center: tuple[float, float] | None = None
        self._session_awake: bool = False
        self._last_circle_debug: dict[str, Any] = {}
        self._circle_hold_until: float = 0.0
        self._last_hand_seen_time: float = 0.0
        self._realtime_history: deque = deque(maxlen=24)
        self._realtime_candidate_gesture: str = "no_gesture"
        self._realtime_candidate_confidence: float = 0.0
        self._realtime_candidate_since: float = 0.0
        self._realtime_confirmed_gesture: str = "no_gesture"
        self._realtime_confirmed_confidence: float = 0.0
        self._realtime_confirmed_at: float = 0.0
        self._wake_lock_until: float = 0.0
        # 等待用户二次确认的挂起动作 (低置信度 confirm 流程)
        self._pending_confirm: dict | None = None

    def _reset_runtime_state(self, clear_confirmation: bool = True) -> None:
        self._position_history.clear()
        self._hand_center_history.clear()
        self._circle_points.clear()
        self._v_sign_history.clear()
        self._hold_counter.clear()
        self._last_emitted = "no_gesture"
        self._last_action_time = 0.0
        self._last_frame_time = time.time()
        self._motion_lock_until = 0.0
        self._swipe_mode_until = 0.0
        self._swipe_start_center = None
        self._session_awake = False
        self._last_circle_debug = {}
        self._circle_hold_until = 0.0
        self._last_hand_seen_time = 0.0
        self._realtime_history.clear()
        self._realtime_candidate_gesture = "no_gesture"
        self._realtime_candidate_confidence = 0.0
        self._realtime_candidate_since = 0.0
        self._realtime_confirmed_gesture = "no_gesture"
        self._realtime_confirmed_confidence = 0.0
        self._realtime_confirmed_at = 0.0
        self._wake_lock_until = 0.0
        if clear_confirmation:
            self._pending_confirm = None

    # ---------------------- 基础几何 ----------------------
    @staticmethod
    def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    @staticmethod
    def _get_finger_angle(landmarks, tip_idx: int, pip_idx: int, mcp_idx: int) -> float:
        tip = (landmarks[tip_idx].x, landmarks[tip_idx].y)
        pip = (landmarks[pip_idx].x, landmarks[pip_idx].y)
        mcp = (landmarks[mcp_idx].x, landmarks[mcp_idx].y)
        v1 = (tip[0] - pip[0], tip[1] - pip[1])
        v2 = (mcp[0] - pip[0], mcp[1] - pip[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            return 180.0
        cos_a = max(-1.0, min(1.0, dot / (n1 * n2)))
        return math.degrees(math.acos(cos_a))

    @staticmethod
    def _finger_tip_above_pip(landmarks, tip_idx: int, pip_idx: int, margin: float = 0.015) -> bool:
        return landmarks[tip_idx].y < landmarks[pip_idx].y - margin

    def _is_finger_extended(self, landmarks, tip_idx, pip_idx, mcp_idx, threshold: float = 145.0) -> bool:
        angle_extended = self._get_finger_angle(landmarks, tip_idx, pip_idx, mcp_idx) > threshold
        vertical_extended = self._finger_tip_above_pip(landmarks, tip_idx, pip_idx)
        return angle_extended or vertical_extended

    def _get_fingers_extended(self, landmarks) -> dict:
        return {
            "thumb":  self._is_thumb_extended(landmarks),
            "index":  self._is_finger_extended(
                landmarks,
                HandLandmark.INDEX_FINGER_TIP, HandLandmark.INDEX_FINGER_PIP, HandLandmark.INDEX_FINGER_MCP),
            "middle": self._is_finger_extended(
                landmarks,
                HandLandmark.MIDDLE_FINGER_TIP, HandLandmark.MIDDLE_FINGER_PIP, HandLandmark.MIDDLE_FINGER_MCP),
            "ring":   self._is_finger_extended(
                landmarks,
                HandLandmark.RING_FINGER_TIP, HandLandmark.RING_FINGER_PIP, HandLandmark.RING_FINGER_MCP),
            "pinky":  self._is_finger_extended(
                landmarks,
                HandLandmark.PINKY_TIP, HandLandmark.PINKY_PIP, HandLandmark.PINKY_MCP),
        }

    def _normalize_point_direction(self, gesture: str) -> str:
        if not self.MIRROR_POINT_DIRECTIONS:
            return gesture
        if gesture == "point_left":
            return "point_right"
        if gesture == "point_right":
            return "point_left"
        return gesture

    def _get_realtime_keep_sec(self, gesture: str) -> float:
        return self.REALTIME_KEEP_SEC_BY_GESTURE.get(gesture, self.REALTIME_KEEP_SEC)

    def _is_v_sign(self, landmarks) -> bool:
        ext = self._get_fingers_extended(landmarks)
        if not (ext["index"] and ext["middle"] and not ext["ring"] and not ext["pinky"]):
            return False
        index_tip = (landmarks[HandLandmark.INDEX_FINGER_TIP].x, landmarks[HandLandmark.INDEX_FINGER_TIP].y)
        middle_tip = (landmarks[HandLandmark.MIDDLE_FINGER_TIP].x, landmarks[HandLandmark.MIDDLE_FINGER_TIP].y)
        ring_tip = (landmarks[HandLandmark.RING_FINGER_TIP].x, landmarks[HandLandmark.RING_FINGER_TIP].y)
        pinky_tip = (landmarks[HandLandmark.PINKY_TIP].x, landmarks[HandLandmark.PINKY_TIP].y)
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return False
        middle_mcp = (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x, landmarks[HandLandmark.MIDDLE_FINGER_MCP].y)
        ring_mcp = (landmarks[HandLandmark.RING_FINGER_MCP].x, landmarks[HandLandmark.RING_FINGER_MCP].y)
        index_middle_gap = self._distance(index_tip, middle_tip) / palm_width
        two_fingers_level = abs(index_tip[1] - middle_tip[1]) < palm_width * 0.45
        ring_curled = self._distance(ring_tip, ring_mcp) < palm_width * 0.70
        pinky_curled = self._distance(pinky_tip, pinky_mcp) < palm_width * 0.70
        index_extended_len = self._distance(index_tip, index_mcp) > palm_width * 0.85
        middle_extended_len = self._distance(middle_tip, middle_mcp) > palm_width * 0.85
        return (index_middle_gap > 0.32 and two_fingers_level and ring_curled and pinky_curled
                and index_extended_len and middle_extended_len)

    def _is_thumb_extended(self, landmarks) -> bool:
        thumb_tip = (landmarks[HandLandmark.THUMB_TIP].x, landmarks[HandLandmark.THUMB_TIP].y)
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return False
        return self._distance(thumb_tip, index_mcp) / palm_width > 0.55

    def _is_thumb_up(self, landmarks) -> Tuple[bool, float]:
        thumb_tip = (landmarks[HandLandmark.THUMB_TIP].x, landmarks[HandLandmark.THUMB_TIP].y)
        thumb_ip  = (landmarks[HandLandmark.THUMB_IP].x, landmarks[HandLandmark.THUMB_IP].y)
        thumb_mcp = (landmarks[HandLandmark.THUMB_MCP].x, landmarks[HandLandmark.THUMB_MCP].y)
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        index_tip = (landmarks[HandLandmark.INDEX_FINGER_TIP].x, landmarks[HandLandmark.INDEX_FINGER_TIP].y)
        middle_tip= (landmarks[HandLandmark.MIDDLE_FINGER_TIP].x, landmarks[HandLandmark.MIDDLE_FINGER_TIP].y)
        ring_tip  = (landmarks[HandLandmark.RING_FINGER_TIP].x, landmarks[HandLandmark.RING_FINGER_TIP].y)
        pinky_tip = (landmarks[HandLandmark.PINKY_TIP].x, landmarks[HandLandmark.PINKY_TIP].y)

        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return False, 0.0
        if self._distance(thumb_tip, index_mcp) / palm_width < 0.55:
            return False, 0.0
        ext = self._get_fingers_extended(landmarks)
        non_thumb_extended_count = sum(1 for name in ("index", "middle", "ring", "pinky") if ext[name])
        if non_thumb_extended_count > 2:
            middle_mcp = (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x, landmarks[HandLandmark.MIDDLE_FINGER_MCP].y)
            ring_mcp = (landmarks[HandLandmark.RING_FINGER_MCP].x, landmarks[HandLandmark.RING_FINGER_MCP].y)
            non_thumb_tip_points = [index_tip, middle_tip, ring_tip, pinky_tip]
            non_thumb_mcp_points = [index_mcp, middle_mcp, ring_mcp, pinky_mcp]
            x_span = (max(p[0] for p in non_thumb_tip_points) - min(p[0] for p in non_thumb_tip_points)) / palm_width
            avg_tip_to_mcp = sum(
                self._distance(non_thumb_tip_points[i], non_thumb_mcp_points[i]) / palm_width
                for i in range(4)
            ) / 4
            thumb_dx = (thumb_tip[0] - thumb_mcp[0]) / palm_width
            thumb_dy = (thumb_tip[1] - thumb_mcp[1]) / palm_width
            thumb_avg_y = (thumb_mcp[1] + thumb_ip[1] + thumb_tip[1]) / 3
            fingers_avg_y = (index_tip[1] + middle_tip[1] + ring_tip[1] + pinky_tip[1]) / 4
            height_diff = (fingers_avg_y - thumb_avg_y) / palm_width
            side_profile_ok = (
                x_span < 0.32
                and avg_tip_to_mcp < 0.76
                and thumb_dy < -0.18
                and abs(thumb_dy) > abs(thumb_dx) * 0.55
                and height_diff > 0.38
            )
            if side_profile_ok:
                confidence = 0.78 + min(max(height_diff - 0.38, 0.0), 0.9) * 0.10
                return True, min(0.9, confidence)
            return False, 0.0
        thumb_dx = (thumb_tip[0] - thumb_mcp[0]) / palm_width
        thumb_dy = (thumb_tip[1] - thumb_mcp[1]) / palm_width
        if thumb_dy > -0.42:
            return False, 0.0
        if abs(thumb_dy) < abs(thumb_dx) * 1.25:
            return False, 0.0
        thumb_avg_y = (thumb_mcp[1] + thumb_ip[1] + thumb_tip[1]) / 3
        fingers_avg_y = (index_tip[1] + middle_tip[1] + ring_tip[1] + pinky_tip[1]) / 4
        height_diff = (fingers_avg_y - thumb_avg_y) / palm_width
        if height_diff < 0.65:
            return False, 0.0
        index_curled = self._distance(index_tip, index_mcp) < palm_width * 0.75
        middle_curled = self._distance(middle_tip, (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x,
                                                    landmarks[HandLandmark.MIDDLE_FINGER_MCP].y)) < palm_width * 0.75
        ring_curled = self._distance(ring_tip, (landmarks[HandLandmark.RING_FINGER_MCP].x,
                                                landmarks[HandLandmark.RING_FINGER_MCP].y)) < palm_width * 0.75
        pinky_curled = self._distance(pinky_tip, pinky_mcp) < palm_width * 0.75
        if not (index_curled and middle_curled and ring_curled and pinky_curled):
            return False, 0.0
        return True, min(0.95, 0.70 + min(height_diff, 1.2) * 0.18)

    def _is_thumb_down(self, landmarks) -> Tuple[bool, float]:
        thumb_tip = (landmarks[HandLandmark.THUMB_TIP].x, landmarks[HandLandmark.THUMB_TIP].y)
        thumb_ip  = (landmarks[HandLandmark.THUMB_IP].x, landmarks[HandLandmark.THUMB_IP].y)
        thumb_mcp = (landmarks[HandLandmark.THUMB_MCP].x, landmarks[HandLandmark.THUMB_MCP].y)
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        index_tip = (landmarks[HandLandmark.INDEX_FINGER_TIP].x, landmarks[HandLandmark.INDEX_FINGER_TIP].y)
        middle_tip= (landmarks[HandLandmark.MIDDLE_FINGER_TIP].x, landmarks[HandLandmark.MIDDLE_FINGER_TIP].y)
        ring_tip  = (landmarks[HandLandmark.RING_FINGER_TIP].x, landmarks[HandLandmark.RING_FINGER_TIP].y)
        pinky_tip = (landmarks[HandLandmark.PINKY_TIP].x, landmarks[HandLandmark.PINKY_TIP].y)

        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return False, 0.0
        if self._distance(thumb_tip, index_mcp) / palm_width < 0.55:
            return False, 0.0
        ext = self._get_fingers_extended(landmarks)
        non_thumb_extended_count = sum(1 for name in ("index", "middle", "ring", "pinky") if ext[name])
        if non_thumb_extended_count > 2:
            middle_mcp = (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x, landmarks[HandLandmark.MIDDLE_FINGER_MCP].y)
            ring_mcp = (landmarks[HandLandmark.RING_FINGER_MCP].x, landmarks[HandLandmark.RING_FINGER_MCP].y)
            non_thumb_tip_points = [index_tip, middle_tip, ring_tip, pinky_tip]
            non_thumb_mcp_points = [index_mcp, middle_mcp, ring_mcp, pinky_mcp]
            x_span = (max(p[0] for p in non_thumb_tip_points) - min(p[0] for p in non_thumb_tip_points)) / palm_width
            avg_tip_to_mcp = sum(
                self._distance(non_thumb_tip_points[i], non_thumb_mcp_points[i]) / palm_width
                for i in range(4)
            ) / 4
            thumb_dx = (thumb_tip[0] - thumb_mcp[0]) / palm_width
            thumb_dy = (thumb_tip[1] - thumb_mcp[1]) / palm_width
            thumb_avg_y = (thumb_mcp[1] + thumb_ip[1] + thumb_tip[1]) / 3
            fingers_avg_y = (index_tip[1] + middle_tip[1] + ring_tip[1] + pinky_tip[1]) / 4
            height_diff = (thumb_avg_y - fingers_avg_y) / palm_width
            side_profile_ok = (
                x_span < 0.32
                and avg_tip_to_mcp < 0.76
                and thumb_dy > 0.18
                and abs(thumb_dy) > abs(thumb_dx) * 0.55
                and height_diff > 0.38
            )
            if side_profile_ok:
                confidence = 0.78 + min(max(height_diff - 0.38, 0.0), 0.9) * 0.10
                return True, min(0.9, confidence)
            return False, 0.0
        thumb_dx = (thumb_tip[0] - thumb_mcp[0]) / palm_width
        thumb_dy = (thumb_tip[1] - thumb_mcp[1]) / palm_width
        if thumb_dy < 0.42:
            return False, 0.0
        if abs(thumb_dy) < abs(thumb_dx) * 1.25:
            return False, 0.0
        thumb_avg_y = (thumb_mcp[1] + thumb_ip[1] + thumb_tip[1]) / 3
        fingers_avg_y = (index_tip[1] + middle_tip[1] + ring_tip[1] + pinky_tip[1]) / 4
        height_diff = (thumb_avg_y - fingers_avg_y) / palm_width
        if height_diff < 0.65:
            return False, 0.0
        index_curled = self._distance(index_tip, index_mcp) < palm_width * 0.75
        middle_curled = self._distance(middle_tip, (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x,
                                                    landmarks[HandLandmark.MIDDLE_FINGER_MCP].y)) < palm_width * 0.75
        ring_curled = self._distance(ring_tip, (landmarks[HandLandmark.RING_FINGER_MCP].x,
                                                landmarks[HandLandmark.RING_FINGER_MCP].y)) < palm_width * 0.75
        pinky_curled = self._distance(pinky_tip, pinky_mcp) < palm_width * 0.75
        if not (index_curled and middle_curled and ring_curled and pinky_curled):
            return False, 0.0
        return True, min(0.95, 0.70 + min(height_diff, 1.2) * 0.18)

    # ---------------------- 静态手势分类 ----------------------
    def _fist_confidence(self, landmarks) -> float:
        """握拳置信度: 四指指尖越贴近掌心, 置信度越高 (松散握拳 -> 低置信度需二次确认)。"""
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return 0.6
        wrist = (landmarks[HandLandmark.WRIST].x, landmarks[HandLandmark.WRIST].y)
        palm_center = ((index_mcp[0] + pinky_mcp[0] + wrist[0]) / 3,
                       (index_mcp[1] + pinky_mcp[1] + wrist[1]) / 3)
        tips = [HandLandmark.INDEX_FINGER_TIP, HandLandmark.MIDDLE_FINGER_TIP,
                HandLandmark.RING_FINGER_TIP, HandLandmark.PINKY_TIP]
        ratios = [self._distance((landmarks[t].x, landmarks[t].y), palm_center) / palm_width for t in tips]
        avg_ratio = sum(ratios) / len(ratios)
        # avg_ratio 越小(指尖越靠掌心)越标准; 映射到 [0.55, 0.95]
        conf = 1.0 - min(1.0, max(0.0, (avg_ratio - 0.35) / 0.65))
        return round(max(0.55, min(0.95, conf)), 3)

    # ---------------------- 静态手势分类 ----------------------
    def _classify_static_gesture(self, landmarks, w: int, h: int) -> Tuple[str, float]:
        ext = self._get_fingers_extended(landmarks)
        non_thumb = [ext["index"], ext["middle"], ext["ring"], ext["pinky"]]
        non_thumb_count = sum(1 for v in non_thumb if v)

        # 点赞/点踩要先于开掌判断, 避免侧视图里折叠手指被误当成张开手掌。
        up_ok, up_conf = self._is_thumb_up(landmarks)
        if up_ok:
            return "thumb_up", up_conf
        down_ok, down_conf = self._is_thumb_down(landmarks)
        if down_ok:
            return "thumb_down", down_conf

        if all([ext["thumb"], ext["index"], ext["middle"], ext["ring"], ext["pinky"]]):
            return "palm_open", 0.94
        if non_thumb_count >= 4 and ext["thumb"]:
            return "palm_open", 0.90
        if non_thumb_count >= 4:
            return "palm_open", 0.86

        index_tip = landmarks[HandLandmark.INDEX_FINGER_TIP]
        index_mcp = landmarks[HandLandmark.INDEX_FINGER_MCP]
        wrist = landmarks[HandLandmark.WRIST]
        middle_tip = landmarks[HandLandmark.MIDDLE_FINGER_TIP]
        ring_tip = landmarks[HandLandmark.RING_FINGER_TIP]
        pinky_tip = landmarks[HandLandmark.PINKY_TIP]
        other_tips = [middle_tip, ring_tip, pinky_tip]
        dx = index_tip.x - wrist.x
        dy = index_tip.y - wrist.y
        palm_ref = self._distance(
            (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y),
            (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y),
        )
        fist_conf = self._fist_confidence(landmarks)
        point_shape_clear = False
        if palm_ref > 1e-3:
            index_reach = self._distance((index_tip.x, index_tip.y), (index_mcp.x, index_mcp.y)) / palm_ref
            curled_other_avg = (
                self._distance((middle_tip.x, middle_tip.y), (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x, landmarks[HandLandmark.MIDDLE_FINGER_MCP].y))
                + self._distance((ring_tip.x, ring_tip.y), (landmarks[HandLandmark.RING_FINGER_MCP].x, landmarks[HandLandmark.RING_FINGER_MCP].y))
                + self._distance((pinky_tip.x, pinky_tip.y), (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y))
            ) / (3 * palm_ref)
            point_shape_clear = (
                ext["index"]
                and not ext["middle"]
                and not ext["ring"]
                and not ext["pinky"]
                and index_reach >= 0.82
                and curled_other_avg <= 0.82
                and fist_conf < 0.84
            )
        if point_shape_clear and abs(dx) > abs(dy) * 0.75:
            if dx < -0.08 and index_tip.x < min(t.x for t in other_tips) - 0.02:
                return self._normalize_point_direction("point_left"), 0.88
            if dx > 0.08 and index_tip.x > max(t.x for t in other_tips) + 0.02:
                return self._normalize_point_direction("point_right"), 0.88

        if non_thumb_count == 0:
            return "fist", fist_conf
        if (
            non_thumb_count <= 1
            and not ext["middle"]
            and not ext["ring"]
            and not ext["pinky"]
            and fist_conf >= 0.84
            and not point_shape_clear
        ):
            return "fist", fist_conf

        if ext["index"] and non_thumb_count <= 2 and not ext["ring"] and not ext["pinky"]:
            dx = index_tip.x - wrist.x
            dy = index_tip.y - wrist.y
            finger_len = self._distance((index_tip.x, index_tip.y), (index_mcp.x, index_mcp.y))
            if point_shape_clear and palm_ref > 1e-3 and finger_len > palm_ref * 0.78 and abs(dx) > abs(dy) * 0.75:
                gesture = "point_right" if dx > 0 else "point_left"
                return self._normalize_point_direction(gesture), 0.86
            return "point", 0.75

        return "no_gesture", 0.0

    def _detect_realtime_fist_override(self, landmarks) -> Tuple[bool, float]:
        index_mcp = (landmarks[HandLandmark.INDEX_FINGER_MCP].x, landmarks[HandLandmark.INDEX_FINGER_MCP].y)
        pinky_mcp = (landmarks[HandLandmark.PINKY_MCP].x, landmarks[HandLandmark.PINKY_MCP].y)
        wrist = (landmarks[HandLandmark.WRIST].x, landmarks[HandLandmark.WRIST].y)
        palm_width = self._distance(index_mcp, pinky_mcp)
        if palm_width < 1e-3:
            return False, 0.0

        palm_center = (
            (index_mcp[0] + pinky_mcp[0] + wrist[0]) / 3,
            (index_mcp[1] + pinky_mcp[1] + wrist[1]) / 3,
        )
        tip_indices = [
            HandLandmark.INDEX_FINGER_TIP,
            HandLandmark.MIDDLE_FINGER_TIP,
            HandLandmark.RING_FINGER_TIP,
            HandLandmark.PINKY_TIP,
        ]
        mcp_indices = [
            HandLandmark.INDEX_FINGER_MCP,
            HandLandmark.MIDDLE_FINGER_MCP,
            HandLandmark.RING_FINGER_MCP,
            HandLandmark.PINKY_MCP,
        ]
        tip_points = [(landmarks[i].x, landmarks[i].y) for i in tip_indices]
        mcp_points = [(landmarks[i].x, landmarks[i].y) for i in mcp_indices]
        avg_tip_to_palm = (
            sum(self._distance(point, palm_center) for point in tip_points) / len(tip_points) / palm_width
        )
        avg_tip_to_mcp = (
            sum(self._distance(tip_points[i], mcp_points[i]) for i in range(len(tip_points))) / len(tip_points) / palm_width
        )
        tip_x_span = (max(point[0] for point in tip_points) - min(point[0] for point in tip_points)) / palm_width
        tip_y_span = (max(point[1] for point in tip_points) - min(point[1] for point in tip_points)) / palm_width
        thumb_tip = (landmarks[HandLandmark.THUMB_TIP].x, landmarks[HandLandmark.THUMB_TIP].y)
        thumb_mcp = (landmarks[HandLandmark.THUMB_MCP].x, landmarks[HandLandmark.THUMB_MCP].y)
        thumb_to_index = self._distance(thumb_tip, index_mcp) / palm_width
        thumb_dx = (thumb_tip[0] - thumb_mcp[0]) / palm_width
        thumb_dy = (thumb_tip[1] - thumb_mcp[1]) / palm_width

        fist_like = (
            avg_tip_to_palm >= 1.90
            and avg_tip_to_mcp >= 1.35
            and tip_y_span <= 1.60
            and thumb_to_index >= 1.05
            and abs(thumb_dx) >= 0.52
            and abs(thumb_dy) <= 0.95
        )
        if not fist_like:
            return False, 0.0

        confidence = 0.80
        confidence += min(max(avg_tip_to_palm - 1.90, 0.0), 0.9) * 0.10
        confidence += min(max(avg_tip_to_mcp - 1.35, 0.0), 0.9) * 0.08
        confidence += min(max(1.60 - tip_y_span, 0.0), 0.6) * 0.06
        if tip_x_span <= 1.25:
            confidence += 0.04
        if thumb_to_index >= 1.12:
            confidence += 0.03
        return True, min(0.93, confidence)

    # ---------------------- 动态手势 ----------------------
    def _detect_motion_gesture(self, landmarks, w: int, h: int, static_gesture: str) -> Tuple[str, float]:
        index_tip = (landmarks[HandLandmark.INDEX_FINGER_TIP].x * w,
                     landmarks[HandLandmark.INDEX_FINGER_TIP].y * h)
        wrist = (landmarks[HandLandmark.WRIST].x * w, landmarks[HandLandmark.WRIST].y * h)
        middle_mcp = (landmarks[HandLandmark.MIDDLE_FINGER_MCP].x * w,
                      landmarks[HandLandmark.MIDDLE_FINGER_MCP].y * h)
        hand_center = ((wrist[0] + middle_mcp[0]) / 2, (wrist[1] + middle_mcp[1]) / 2)
        self._position_history.append(index_tip)
        self._hand_center_history.append(hand_center)
        self._circle_points.append(index_tip)
        upper_gesture_zone = hand_center[1] < h * 0.50
        lower_gesture_zone = hand_center[1] >= h * 0.50

        now = time.time()
        swipe_mode = True
        v_swipe_ready = False
        now = time.time()
        if v_swipe_ready and now > self._swipe_mode_until:
            self._swipe_mode_until = now + 2.0
            self._swipe_start_center = hand_center
        swipe_mode = True
        if len(self._hand_center_history) < 4:
            return "no_gesture", 0.0

        pts = list(self._hand_center_history)[-10:]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        dx_net = xs[-1] - xs[0]
        dy_net = ys[-1] - ys[0]
        x_range = max(xs) - min(xs)
        y_range = max(ys) - min(ys)

        x_diffs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        x_sign_changes = sum(
            1 for i in range(1, len(x_diffs))
            if x_diffs[i] * x_diffs[i - 1] < 0 and abs(x_diffs[i]) > w * 0.01
        )
        path_len = sum(self._distance(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        swipe_dx = dx_net
        swipe_dy = dy_net
        center_y_ratio = hand_center[1] / max(h, 1)
        recent_pts = pts[-5:]
        recent_xs = [p[0] for p in recent_pts]
        recent_ys = [p[1] for p in recent_pts]
        recent_hand_moving = (
            (max(recent_xs) - min(recent_xs)) > w * 0.045
            or (max(recent_ys) - min(recent_ys)) > h * 0.045
        )
        net_ratio = abs(dx_net) / max(x_range, 1.0)
        diagonal_ratio = y_range / max(x_range, 1.0)

        # 1) 挥手: 横向往返摆动 (允许位于画面上半区, 运动中短暂丢失静态类别也可判定)
        wave_static_ok = static_gesture in ("palm_open", "no_gesture")
        wave_motion_ok = x_range > w * 0.065 and x_range > y_range * 1.05
        wave_pattern_ok = x_sign_changes >= 1 and abs(dx_net) < x_range * 0.85 and path_len > x_range * 1.25
        wave_zone_ok = lower_gesture_zone or hand_center[1] < h * 0.62
        oscillating_wave_ok = x_sign_changes >= 2 or net_ratio < 0.55
        sweeping_wave_ok = recent_hand_moving and center_y_ratio < 0.56 and diagonal_ratio > 0.42
        if wave_static_ok and wave_zone_ok and wave_motion_ok and wave_pattern_ok and (
            oscillating_wave_ok or sweeping_wave_ok
        ):
            confidence = min(
                0.93,
                0.70
                + min(x_range / max(w * 0.18, 1.0), 1.0) * 0.10
                + min(x_sign_changes + 1, 4) * 0.03,
            )
            return "wave", confidence
        broad_wave_ok = (
            static_gesture == "palm_open"
            and wave_zone_ok
            and recent_hand_moving
            and center_y_ratio < 0.56
            and x_range > w * 0.075
            and diagonal_ratio > 0.40
            and path_len > x_range * 1.22
            and abs(dx_net) > w * 0.045
        )
        if broad_wave_ok:
            confidence = min(0.86, 0.72 + min(x_range / max(w * 0.18, 1.0), 1.0) * 0.08)
            return "wave", confidence

        # 2) 横向滑动: 单向、几乎无反转、净位移主导且轨迹接近直线
        if (static_gesture in ("point", "point_left", "point_right")
                and upper_gesture_zone and swipe_mode and abs(swipe_dx) > w * 0.055 and abs(swipe_dx) > abs(swipe_dy) * 1.15
                and x_sign_changes <= 2 and path_len < max(abs(swipe_dx), 1.0) * 3.2):
            self._swipe_mode_until = 0.0
            self._swipe_start_center = None
            confidence = min(0.92, 0.74 + min(abs(swipe_dx) / max(w * 0.16, 1.0), 1.0) * 0.16)
            gesture = "point_right" if swipe_dx > 0 else "point_left"
            return self._normalize_point_direction(gesture), confidence

        # 3) 画圈: 仅在单指类姿态下尝试, 避免张开手掌被误判
        if static_gesture in ("point", "point_left", "point_right", "no_gesture"):
            circle_res = self._detect_circle(w, h)
            if circle_res is not None:
                return circle_res

        return "no_gesture", 0.0

    def _detect_circle(self, w: int, h: int):
        """画圈: 轨迹闭合、累计转角接近一圈、半径稳定。返回 (gesture, conf) 或 None。"""
        self._last_circle_debug = {"hit": False, "reason": "too_few_points", "points": len(self._circle_points)}
        if len(self._circle_points) < 6:
            return None
        pts = list(self._circle_points)[-20:]
        hpts = list(self._hand_center_history)[-20:]
        if len(hpts) >= 6:
            hxs = [p[0] for p in hpts]
            hys = [p[1] for p in hpts]
            if (max(hxs) - min(hxs)) > w * 0.16 or (max(hys) - min(hys)) > h * 0.16:
                self._last_circle_debug = {"hit": False, "reason": "hand_center_move", "points": len(pts)}
                return None
        pxs = [p[0] for p in pts]
        pys = [p[1] for p in pts]
        p_x_range = max(pxs) - min(pxs)
        p_y_range = max(pys) - min(pys)
        if p_x_range < w * 0.020 or p_y_range < h * 0.020:
            self._last_circle_debug = {"hit": False, "reason": "range_small", "points": len(pts)}
            return None
        if p_x_range > p_y_range * 2.8 or p_y_range > p_x_range * 2.8:
            self._last_circle_debug = {"hit": False, "reason": "aspect_bad", "points": len(pts)}
            return None
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        distances = [self._distance(p, (cx, cy)) for p in pts]
        avg_r = sum(distances) / len(distances)
        if avg_r < w * 0.010:
            self._last_circle_debug = {"hit": False, "reason": "radius_small", "points": len(pts)}
            return None
        var = sum((d - avg_r) ** 2 for d in distances) / len(distances)
        radius_std = math.sqrt(var)
        if radius_std > avg_r * 0.92:
            self._last_circle_debug = {"hit": False, "reason": "radius_unstable", "points": len(pts)}
            return None
        end_gap = self._distance(pts[0], pts[-1])
        angles = [math.atan2(p[1] - cy, p[0] - cx) for p in pts]
        unwrapped = [angles[0]]
        for a in angles[1:]:
            last = unwrapped[-1]
            while a - last > math.pi:
                a -= 2 * math.pi
            while a - last < -math.pi:
                a += 2 * math.pi
            unwrapped.append(a)
        span = abs(unwrapped[-1] - unwrapped[0])
        total_rot = sum(abs(unwrapped[i + 1] - unwrapped[i]) for i in range(len(unwrapped) - 1))
        x_diffs = [pxs[i + 1] - pxs[i] for i in range(len(pxs) - 1)]
        y_diffs = [pys[i + 1] - pys[i] for i in range(len(pys) - 1)]
        x_sign_changes = sum(1 for i in range(1, len(x_diffs)) if x_diffs[i] * x_diffs[i - 1] < 0)
        y_sign_changes = sum(1 for i in range(1, len(y_diffs)) if y_diffs[i] * y_diffs[i - 1] < 0)
        path_len = sum(self._distance(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

        strict_hit = (
            total_rot > math.pi * 0.95
            and span > math.pi * 0.75
            and total_rot < span * 1.8
            and end_gap <= avg_r * 2.6
        )
        loose_hit = (
            total_rot > math.pi * 0.55
            and span > math.pi * 0.35
            and path_len > max(p_x_range, p_y_range) * 2.2
            and x_sign_changes >= 1
            and y_sign_changes >= 1
            and end_gap <= max(p_x_range, p_y_range) * 1.15
        )
        if strict_hit or loose_hit:
            self._last_circle_debug = {
                "points": len(pts),
                "x_range": round(p_x_range / w, 3),
                "y_range": round(p_y_range / h, 3),
                "avg_r": round(avg_r / w, 3),
                "radius_std": round(radius_std / max(avg_r, 1e-6), 3),
                "span": round(span / math.pi, 2),
                "total_rot": round(total_rot / math.pi, 2),
                "end_gap": round(end_gap / max(avg_r, 1e-6), 3),
                "x_sign_changes": x_sign_changes,
                "y_sign_changes": y_sign_changes,
                "path_len": round(path_len / max(max(p_x_range, p_y_range), 1e-6), 2),
                "mode": "strict" if strict_hit else "loose",
                "hit": True,
            }
            self._circle_hold_until = time.time() + 0.6
            base_conf = 0.76 if loose_hit and not strict_hit else 0.82
            return "circle", min(0.95, base_conf + total_rot * 0.06)
        self._last_circle_debug = {
            "hit": False,
            "reason": "rotation_insufficient",
            "points": len(pts),
            "span": round(span / math.pi, 2),
            "total_rot": round(total_rot / math.pi, 2),
            "x_sign_changes": x_sign_changes,
            "y_sign_changes": y_sign_changes,
            "end_gap": round(end_gap / max(avg_r, 1e-6), 3),
        }
        return None

    def _is_hand_moving(self, w: int, h: int) -> bool:
        """手部正在明显移动时, 暂停静态手势执行, 等待动态手势完成。"""
        if len(self._hand_center_history) < 3:
            return False
        pts = list(self._hand_center_history)[-5:]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (max(xs) - min(xs)) > w * 0.045 or (max(ys) - min(ys)) > h * 0.045

    def _is_index_tip_moving(self, w: int, h: int) -> bool:
        if len(self._position_history) < 4:
            return False
        pts = list(self._position_history)[-5:]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (max(xs) - min(xs)) > w * 0.06 or (max(ys) - min(ys)) > h * 0.06

    # ---------------------- 防抖 (持续时间阈值 + 冷却) ----------------------
    def _apply_hold_and_cooldown(self, gesture: str, confidence: float) -> Tuple[str, float, str | None]:
        if gesture == "no_gesture":
            self._hold_counter.clear()
            return "no_gesture", confidence, None

        if self._hold_counter.get("current") == gesture:
            self._hold_counter["count"] = self._hold_counter.get("count", 0) + 1
        else:
            self._hold_counter["current"] = gesture
            self._hold_counter["count"] = 1

        required = self.HOLD_REQUIRED.get(gesture, 1)
        if self._hold_counter["count"] < required:
            return "no_gesture", confidence, None

        now = time.time()
        cooldown = self.GESTURE_COOLDOWN_SEC.get(gesture, self.COOLDOWN_SEC)
        if self._last_emitted == gesture and now - self._last_action_time < cooldown:
            return "no_gesture", confidence, None

        self._last_emitted = gesture
        self._last_action_time = now
        _, _, action = OWNER_GESTURES.get(gesture, OWNER_GESTURES["no_gesture"])
        return gesture, confidence, action

    def _apply_realtime_confirmation(
        self,
        gesture: str,
        confidence: float,
        now: float,
    ) -> tuple[str, float, dict[str, Any]]:
        debug: dict[str, Any] = {
            "candidate": gesture,
            "candidate_confidence": round(confidence, 3),
            "confirmed": self._realtime_confirmed_gesture,
        }
        if gesture == "no_gesture":
            if (
                self._realtime_confirmed_gesture != "no_gesture"
                and now - self._realtime_confirmed_at <= self._get_realtime_keep_sec(self._realtime_confirmed_gesture)
            ):
                debug["hold"] = "keep_confirmed"
                return self._realtime_confirmed_gesture, self._realtime_confirmed_confidence, debug
            if (
                self._realtime_candidate_gesture != "no_gesture"
                and now - self._realtime_candidate_since <= self._get_realtime_keep_sec(self._realtime_candidate_gesture)
            ):
                debug["hold"] = "keep_candidate_gap"
                return "no_gesture", 0.0, debug
            self._realtime_candidate_gesture = "no_gesture"
            self._realtime_candidate_confidence = 0.0
            self._realtime_candidate_since = 0.0
            self._realtime_confirmed_gesture = "no_gesture"
            self._realtime_confirmed_confidence = 0.0
            self._realtime_confirmed_at = 0.0
            debug["hold"] = "clear"
            return "no_gesture", 0.0, debug

        if self._realtime_confirmed_gesture == gesture:
            self._realtime_confirmed_confidence = max(self._realtime_confirmed_confidence, confidence)
            self._realtime_confirmed_at = now
            self._realtime_candidate_gesture = gesture
            self._realtime_candidate_confidence = confidence
            if self._realtime_candidate_since == 0.0:
                self._realtime_candidate_since = now
            debug["hold"] = "confirmed_same"
            return gesture, self._realtime_confirmed_confidence, debug

        if self._realtime_candidate_gesture != gesture:
            self._realtime_candidate_gesture = gesture
            self._realtime_candidate_confidence = confidence
            self._realtime_candidate_since = now
        else:
            self._realtime_candidate_confidence = max(self._realtime_candidate_confidence, confidence)

        candidate_age = now - self._realtime_candidate_since
        required = self.REALTIME_CONFIRM_SEC.get(gesture, 0.22)
        if self._realtime_confirmed_gesture != "no_gesture":
            required = self.REALTIME_SWITCH_CONFIRM_SEC.get(gesture, required)
        debug["candidate_age"] = round(candidate_age, 3)
        debug["required"] = required

        if candidate_age >= required:
            self._realtime_confirmed_gesture = gesture
            self._realtime_confirmed_confidence = self._realtime_candidate_confidence
            self._realtime_confirmed_at = now
            debug["hold"] = "candidate_confirmed"
            return gesture, self._realtime_confirmed_confidence, debug

        if (
            self._realtime_confirmed_gesture != "no_gesture"
            and now - self._realtime_confirmed_at <= self._get_realtime_keep_sec(self._realtime_confirmed_gesture)
        ):
            debug["hold"] = "keep_previous"
            return self._realtime_confirmed_gesture, self._realtime_confirmed_confidence, debug

        debug["hold"] = "wait_candidate"
        return "no_gesture", 0.0, debug

    def _stabilize_realtime_dynamic_gesture(
        self,
        gesture: str,
        confidence: float,
        static_gesture: str,
        static_confidence: float,
        motion_gesture: str,
        motion_confidence: float,
        is_moving: bool,
        index_tip_moving: bool,
        now: float,
        circle_debug: dict[str, Any] | None = None,
    ) -> tuple[str, float, dict[str, Any]]:
        dynamic_gestures = {"wave", "circle", "point_left", "point_right"}
        circle_debug = circle_debug or {}
        circle_hit = bool(circle_debug.get("hit"))
        circle_total_rot = float(circle_debug.get("total_rot", 0.0) or 0.0)
        circle_span = float(circle_debug.get("span", 0.0) or 0.0)
        circle_x_sign_changes = int(circle_debug.get("x_sign_changes", 0) or 0)
        circle_y_sign_changes = int(circle_debug.get("y_sign_changes", 0) or 0)
        strong_circle_frame = circle_hit and (
            circle_debug.get("mode") == "strict"
            or (
                circle_total_rot >= 1.05
                and circle_span >= 0.80
                and circle_x_sign_changes >= 2
                and circle_y_sign_changes >= 2
            )
        )
        candidate = motion_gesture if motion_gesture in dynamic_gestures else (gesture if gesture in dynamic_gestures else "no_gesture")
        moving_now = is_moving or index_tip_moving
        self._realtime_history.append({
            "time": now,
            "gesture": gesture,
            "confidence": float(confidence),
            "static_gesture": static_gesture,
            "static_confidence": float(static_confidence),
            "motion_gesture": motion_gesture,
            "motion_confidence": float(motion_confidence),
            "candidate": candidate,
            "moving": moving_now,
            "index_tip_moving": bool(index_tip_moving),
            "circle_hit": circle_hit,
            "strong_circle_frame": strong_circle_frame,
        })
        cutoff = now - self.REALTIME_DYNAMIC_WINDOW_SEC
        while self._realtime_history and self._realtime_history[0]["time"] < cutoff:
            self._realtime_history.popleft()

        recent = list(self._realtime_history)
        recent_dynamic_tail = recent[-8:]
        recent_strong_circle_tail = sum(
            1 for entry in recent_dynamic_tail if entry["strong_circle_frame"]
        )
        recent_circle_tail = sum(
            1 for entry in recent_dynamic_tail if entry["circle_hit"]
        )
        recent_fist_tail = sum(
            1 for entry in recent_dynamic_tail
            if entry["static_gesture"] == "fist" and entry["static_confidence"] >= 0.72
        )
        strong_fist_now = (
            static_gesture == "fist"
            and static_confidence >= 0.80
            and motion_gesture != "circle"
            and not circle_hit
            and not index_tip_moving
        )
        strong_open_now = (
            static_gesture == "palm_open"
            and static_confidence >= 0.90
            and motion_gesture not in ("circle", "wave")
            and not circle_hit
            and not moving_now
        )
        strong_point_now = (
            static_gesture in ("point_left", "point_right")
            and static_confidence >= 0.86
            and motion_gesture != "circle"
            and not circle_hit
        )
        if recent_strong_circle_tail >= 1 and (strong_fist_now or strong_open_now):
            self._circle_hold_until = 0.0
            self._circle_points.clear()
            self._position_history.clear()
            self._hand_center_history.clear()
            latest_entry = self._realtime_history[-1]
            self._realtime_history = deque([latest_entry], maxlen=self._realtime_history.maxlen)
            if strong_fist_now:
                return "fist", round(static_confidence, 3), {
                    "active": False,
                    "candidate": candidate,
                    "window_size": 1,
                    "transition_reset": "circle_to_fist",
                    "recent_circle_debug_hits": recent_circle_tail,
                    "recent_strong_circle_hits": recent_strong_circle_tail,
                }
            self._motion_lock_until = max(self._motion_lock_until, now + 0.25)
            return "no_gesture", 0.0, {
                "active": False,
                "candidate": candidate,
                "window_size": 1,
                "transition_reset": "circle_to_open_wait",
                "recent_circle_debug_hits": recent_circle_tail,
                "recent_strong_circle_hits": recent_strong_circle_tail,
            }
        if recent_strong_circle_tail >= 1 and strong_point_now:
            self._circle_hold_until = 0.0
            self._circle_points.clear()
            self._position_history.clear()
            self._hand_center_history.clear()
            latest_entry = self._realtime_history[-1]
            self._realtime_history = deque([latest_entry], maxlen=self._realtime_history.maxlen)
            return static_gesture, round(static_confidence, 3), {
                "active": False,
                "candidate": candidate,
                "window_size": 1,
                "transition_reset": "circle_to_point",
                "recent_circle_debug_hits": recent_circle_tail,
                "recent_strong_circle_hits": recent_strong_circle_tail,
            }
        if (
            static_gesture == "palm_open"
            and static_confidence >= 0.84
            and motion_gesture == "no_gesture"
            and not moving_now
            and recent_fist_tail >= 2
        ):
            latest_entry = self._realtime_history[-1]
            latest_entry["static_gesture"] = "fist"
            latest_entry["static_confidence"] = max(float(latest_entry["static_confidence"]), 0.90)
            return "fist", round(max(0.90, static_confidence), 3), {
                "active": False,
                "candidate": candidate,
                "window_size": len(recent),
                "transition_reset": "palm_to_fist_hold",
                "recent_fist_hits": recent_fist_tail,
            }
        active_dynamic_window = any(
            entry["moving"]
            or entry["motion_gesture"] in dynamic_gestures
            or entry["candidate"] in dynamic_gestures
            for entry in recent
        )
        debug = {
            "active": active_dynamic_window,
            "candidate": candidate,
            "window_size": len(recent),
        }
        if not active_dynamic_window:
            return gesture, confidence, debug

        scores = {name: 0.0 for name in dynamic_gestures}
        counts = {name: 0 for name in dynamic_gestures}
        best_conf = {name: 0.0 for name in dynamic_gestures}
        for entry in recent:
            candidate_name = entry["candidate"]
            if candidate_name not in dynamic_gestures:
                continue
            weight = max(0.35, entry["confidence"])
            if entry["motion_gesture"] == candidate_name:
                weight += 0.25
            if entry["moving"]:
                weight += 0.15
            if candidate_name == "wave" and (
                entry["motion_gesture"] == "wave" or entry["static_gesture"] == "palm_open"
            ):
                weight += 0.16
            if candidate_name == "circle" and entry["circle_hit"]:
                weight += 0.28
            if candidate_name == "circle" and entry["strong_circle_frame"]:
                weight += 0.18
            if candidate_name == "circle" and entry["static_gesture"] in ("palm_open", "fist"):
                weight -= 0.18
            if candidate_name == "circle" and entry["static_gesture"] in ("point_left", "point_right"):
                weight -= 0.18
            if candidate_name in ("point_left", "point_right") and entry["static_gesture"] in ("palm_open", "fist"):
                weight -= 0.22
            scores[candidate_name] += weight
            counts[candidate_name] += 1
            best_conf[candidate_name] = max(best_conf[candidate_name], entry["confidence"], entry["motion_confidence"])

        nonzero_scores = {name: score for name, score in scores.items() if score > 0}
        debug["scores"] = {name: round(score, 3) for name, score in nonzero_scores.items()}
        debug["counts"] = {name: counts[name] for name in counts if counts[name] > 0}
        if nonzero_scores:
            ranked = sorted(nonzero_scores.items(), key=lambda item: item[1], reverse=True)
            top_gesture, top_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            debug["top"] = top_gesture
            debug["top_score"] = round(top_score, 3)
            min_score = self.REALTIME_DYNAMIC_MIN_SCORE.get(top_gesture, 1.4)
            min_count = self.REALTIME_DYNAMIC_MIN_COUNT.get(top_gesture, 2)
            strong_dynamic_hit = any(
                entry["motion_gesture"] == top_gesture and entry["motion_confidence"] >= 0.86
                for entry in recent
            )
            recent_wave_motion_hits = sum(
                1 for entry in recent[-8:]
                if entry["motion_gesture"] == "wave" and entry["motion_confidence"] >= 0.72
            )
            recent_circle_motion_hits = sum(
                1 for entry in recent[-8:]
                if entry["motion_gesture"] == "circle" and entry["motion_confidence"] >= 0.80
            )
            recent_circle_debug_hits = sum(
                1 for entry in recent[-8:]
                if entry["circle_hit"]
            )
            recent_strong_circle_hits = sum(
                1 for entry in recent[-8:]
                if entry["strong_circle_frame"]
            )
            recent_point_like_hits = sum(
                1 for entry in recent[-8:]
                if entry["static_gesture"] in ("point", "point_left", "point_right")
            )
            recent_point_static_hits = sum(
                1 for entry in recent[-8:]
                if entry["static_gesture"] == "point"
            )
            recent_open_like_hits = sum(
                1 for entry in recent[-8:]
                if entry["static_gesture"] == "palm_open"
            )
            recent_fist_like_hits = sum(
                1 for entry in recent[-8:]
                if entry["static_gesture"] == "fist" and entry["static_confidence"] >= 0.72
            )
            debug["recent_wave_motion_hits"] = recent_wave_motion_hits
            debug["recent_circle_motion_hits"] = recent_circle_motion_hits
            debug["recent_circle_debug_hits"] = recent_circle_debug_hits
            debug["recent_strong_circle_hits"] = recent_strong_circle_hits
            debug["recent_point_static_hits"] = recent_point_static_hits
            if strong_point_now and recent_strong_circle_hits == 0 and recent_circle_motion_hits < 2:
                self._circle_hold_until = 0.0
                self._circle_points.clear()
                self._position_history.clear()
                self._hand_center_history.clear()
                latest_entry = self._realtime_history[-1]
                self._realtime_history = deque([latest_entry], maxlen=self._realtime_history.maxlen)
                debug["transition_reset"] = "point_override"
                return static_gesture, round(static_confidence, 3), debug
            if strong_fist_now and recent_fist_like_hits >= 2:
                self._circle_hold_until = 0.0
                self._circle_points.clear()
                self._position_history.clear()
                self._hand_center_history.clear()
                latest_entry = self._realtime_history[-1]
                self._realtime_history = deque([latest_entry], maxlen=self._realtime_history.maxlen)
                debug["transition_reset"] = "fist_override"
                return "fist", round(static_confidence, 3), debug
            if (
                counts[top_gesture] >= min_count
                and top_score >= min_score
                and top_score >= second_score * 1.18
            ):
                if top_gesture == "circle":
                    if recent_wave_motion_hits >= 1 and recent_point_static_hits < 1 and recent_strong_circle_hits < 2:
                        return "no_gesture", 0.0, debug
                    if recent_strong_circle_hits == 0 and recent_circle_motion_hits < 2:
                        return "no_gesture", 0.0, debug
                if top_gesture in ("point_left", "point_right") and (
                    recent_fist_like_hits >= 1
                    or recent_open_like_hits >= 2
                    or recent_point_like_hits < 2
                    or recent_circle_motion_hits >= 1
                    or recent_strong_circle_hits >= 1
                ):
                    return "no_gesture", 0.0, debug
                return top_gesture, round(best_conf[top_gesture], 3), debug
            if (
                top_gesture == "wave"
                and recent_wave_motion_hits >= 1
                and recent_open_like_hits >= 1
                and recent_circle_motion_hits == 0
                and recent_strong_circle_hits == 0
                and recent_point_static_hits == 0
                and top_score >= 1.0
            ):
                return "wave", round(best_conf["wave"], 3), debug
            if (
                top_gesture == "circle"
                and recent_strong_circle_hits >= 1
                and recent_wave_motion_hits == 0
                and not strong_point_now
                and top_score >= 1.0
            ):
                return "circle", round(best_conf["circle"], 3), debug
            if top_gesture == "circle" and strong_dynamic_hit and top_score >= 0.95:
                if (
                    (recent_wave_motion_hits >= 1 and recent_point_static_hits == 0 and recent_strong_circle_hits < 2)
                    or (recent_circle_motion_hits < 2 and recent_strong_circle_hits == 0)
                ):
                    return "no_gesture", 0.0, debug
                return top_gesture, round(best_conf[top_gesture], 3), debug
            if (
                top_gesture == "circle"
                and (recent_circle_motion_hits >= 1 or recent_circle_debug_hits >= 1)
                and (recent_point_static_hits >= 1 or recent_strong_circle_hits >= 1)
                and not strong_point_now
                and top_score >= 0.95
            ):
                return "circle", round(best_conf["circle"], 3), debug
            if (
                top_gesture == "circle"
                and now < self._circle_hold_until
                and recent_circle_debug_hits >= 1
                and recent_wave_motion_hits == 0
                and not strong_point_now
                and top_score >= 0.85
            ):
                return "circle", round(best_conf["circle"], 3), debug

        static_gestures = {"fist", "thumb_up", "thumb_down", "palm_open", "point_left", "point_right"}
        static_scores = {name: 0.0 for name in static_gestures}
        static_counts = {name: 0 for name in static_gestures}
        static_best_conf = {name: 0.0 for name in static_gestures}
        recent_static_frames = recent[-6:]
        recent_fist_hits = 0
        recent_open_hits = 0
        recent_nonmoving = sum(1 for entry in recent_static_frames if not entry["moving"])
        recent_point_motion = sum(1 for entry in recent_static_frames if entry["index_tip_moving"])
        for entry in recent:
            candidate_name = entry["static_gesture"]
            if candidate_name not in static_gestures:
                continue
            weight = max(0.32, entry["static_confidence"])
            if not entry["moving"]:
                weight += 0.12
            if candidate_name == "fist":
                weight += 0.12
            if candidate_name in ("point_left", "point_right") and entry["index_tip_moving"]:
                weight -= 0.18
            static_scores[candidate_name] += weight
            static_counts[candidate_name] += 1
            static_best_conf[candidate_name] = max(static_best_conf[candidate_name], entry["static_confidence"])
            if candidate_name == "fist" and entry["static_confidence"] >= 0.72:
                recent_fist_hits += 1
            if candidate_name == "palm_open" and entry["static_confidence"] >= 0.84:
                recent_open_hits += 1

        nonzero_static_scores = {name: score for name, score in static_scores.items() if score > 0}
        debug["static_scores"] = {name: round(score, 3) for name, score in nonzero_static_scores.items()}
        debug["static_counts"] = {name: static_counts[name] for name in static_counts if static_counts[name] > 0}
        debug["recent_fist_hits"] = recent_fist_hits
        debug["recent_open_hits"] = recent_open_hits
        if strong_fist_now and recent_fist_hits >= 2:
            self._circle_hold_until = 0.0
            self._circle_points.clear()
            self._position_history.clear()
            self._hand_center_history.clear()
            latest_entry = self._realtime_history[-1]
            self._realtime_history = deque([latest_entry], maxlen=self._realtime_history.maxlen)
            debug["transition_reset"] = "fist_static_override"
            return "fist", round(max(static_best_conf["fist"], static_confidence), 3), debug
        if nonzero_static_scores:
            ranked_static = sorted(nonzero_static_scores.items(), key=lambda item: item[1], reverse=True)
            top_static, top_static_score = ranked_static[0]
            second_static_score = ranked_static[1][1] if len(ranked_static) > 1 else 0.0
            static_min_score = self.REALTIME_STATIC_MIN_SCORE.get(top_static, 1.5)
            static_min_count = self.REALTIME_STATIC_MIN_COUNT.get(top_static, 2)
            debug["top_static"] = top_static
            debug["top_static_score"] = round(top_static_score, 3)
            if top_static == "fist" and recent_fist_hits >= 2 and recent_nonmoving >= 2:
                return "fist", round(static_best_conf["fist"], 3), debug
            if (
                static_counts[top_static] >= static_min_count
                and top_static_score >= static_min_score
                and top_static_score >= second_static_score * 1.12
            ):
                if top_static in ("point_left", "point_right"):
                    if (
                        active_dynamic_window
                        or recent_point_motion >= 2
                        or recent_fist_hits >= 1
                        or recent_open_hits >= 2
                        or static_counts[top_static] < 4
                        or recent_nonmoving < 4
                    ):
                        return "no_gesture", 0.0, debug
                if top_static == "palm_open":
                    if active_dynamic_window or recent_fist_hits >= 2:
                        return "no_gesture", 0.0, debug
                if top_static == "fist" and recent_nonmoving >= 2:
                    return "fist", round(static_best_conf["fist"], 3), debug
                if top_static in ("thumb_up", "thumb_down") and recent_nonmoving >= 2:
                    return top_static, round(static_best_conf[top_static], 3), debug
                if top_static == "palm_open" and recent_nonmoving >= 3:
                    return "palm_open", round(static_best_conf["palm_open"], 3), debug
                if top_static in ("point_left", "point_right") and recent_nonmoving >= 3:
                    return top_static, round(static_best_conf[top_static], 3), debug

        return "no_gesture", 0.0, debug

    # ---------------------- 画骨架 ----------------------
    def _draw_hand(self, image, landmarks, w: int, h: int):
        pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
        for a, b in HAND_CONNECTIONS:
            if a < len(pts) and b < len(pts):
                cv2.line(image, pts[a], pts[b], (255, 128, 0), 2)
        for i, p in enumerate(pts):
            if i <= 4:
                color = (0, 0, 255)
            elif i <= 8:
                color = (0, 255, 0)
            elif i <= 12:
                color = (255, 0, 0)
            elif i <= 16:
                color = (0, 255, 255)
            else:
                color = (255, 0, 255)
            cv2.circle(image, p, 5, color, -1)

    # ---------------------- 图像解码 ----------------------
    def _detect_best_frame(self, image_bytes: bytes) -> np.ndarray:
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            if getattr(pil_img, "is_animated", False):
                best_frame = None
                best_score = -1.0
                for frame in ImageSequence.Iterator(pil_img):
                    frame_rgb = frame.convert("RGB")
                    frame_np = cv2.cvtColor(np.array(frame_rgb), cv2.COLOR_RGB2BGR)
                    gray = cv2.cvtColor(frame_np, cv2.COLOR_BGR2GRAY)
                    score = cv2.Laplacian(gray, cv2.CV_64F).var()
                    if score > best_score:
                        best_score = score
                        best_frame = frame_np
                if best_frame is not None:
                    return best_frame
            frame_rgb = pil_img.convert("RGB")
            return cv2.cvtColor(np.array(frame_rgb), cv2.COLOR_RGB2BGR)
        except Exception:
            pass
        nparr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解析图像")
        return image

    def _detect_hands(self, rgb: np.ndarray):
        if self._use_tasks_landmarker and self.landmarker is not None:
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = self.landmarker.detect(mp_image)
            return result.hand_landmarks or []
        if self._hands_fallback is not None:
            fallback_result = self._hands_fallback.process(rgb)
            return fallback_result.multi_hand_landmarks or []
        return []

    # ---------------------- 确认流程 ----------------------
    def confirm_pending(self, accept: bool) -> dict | None:
        """用户对低置信度动作做出确认/取消。返回确认后的动作信息或 None。"""
        pending = self._pending_confirm
        self._pending_confirm = None
        if not pending:
            return None
        if accept:
            self._last_emitted = pending["gesture"]
            self._last_action_time = time.time()
            return pending
        return None

    def has_pending_confirm(self) -> bool:
        return self._pending_confirm is not None

    # ---------------------- 主入口 ----------------------
    def recognize(
        self,
        image_bytes: bytes,
        timestamp_ms: int | None = None,
        apply_debounce: bool = True,
        vehicle_state: dict | None = None,
        respect_standby: bool = False,
        realtime_mode: bool = False,
    ) -> dict[str, Any]:
        image = self._detect_best_frame(image_bytes)
        h, w = image.shape[:2]
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        now = time.time()
        if now - self._last_frame_time > 0.5:
            self._position_history.clear()
            self._hand_center_history.clear()
            self._circle_points.clear()
            self._hold_counter.clear()
            self._realtime_history.clear()
        self._last_frame_time = now

        if not self._use_tasks_landmarker and self._hands_fallback is None:
            return self._empty_result(image, error=f"手部检测模型初始化失败: {self._init_error}")

        try:
            detected_hands = self._detect_hands(rgb)
        except Exception as e:
            return self._empty_result(image, error=str(e))

        annotated = image.copy()
        gesture, confidence = "no_gesture", 0.0
        action = None
        keypoints = []
        debug_info: dict[str, Any] = {"stage": "no_hand"}
        confirmation_resolved = False
        confirmation_accepted = False
        needs_confirmation = False
        vehicle_state = vehicle_state or {}
        is_standby = bool(respect_standby and not vehicle_state.get("is_awake"))
        if is_standby:
            self._session_awake = False
        elif vehicle_state.get("is_awake"):
            self._session_awake = True
        confirm_mode = self._pending_confirm is not None

        if detected_hands:
            self._last_hand_seen_time = time.time()
            hand = detected_hands[0]
            self._draw_hand(annotated, hand, w, h)

            static_g, static_c = self._classify_static_gesture(hand, w, h)
            motion_g, motion_c = self._detect_motion_gesture(hand, w, h, static_g)

            is_moving = self._is_hand_moving(w, h)
            index_tip_moving = self._is_index_tip_moving(w, h)
            if (
                static_g in ("palm_open", "point", "point_left", "point_right")
                and motion_g in ("no_gesture", "point_left", "point_right")
            ):
                fist_override, fist_override_conf = self._detect_realtime_fist_override(hand)
                allow_fist_override = (
                    realtime_mode
                    or fist_override_conf >= 0.84
                    or (static_g == "palm_open" and static_c >= 0.84)
                )
                if fist_override and allow_fist_override:
                    static_g, static_c = "fist", max(static_c, fist_override_conf)
                    if motion_g in ("point_left", "point_right"):
                        motion_g, motion_c = "no_gesture", 0.0
            debug_info = {
                "stage": "hand_detected",
                "mode": "confirm" if confirm_mode else ("standby" if is_standby else "normal"),
                "current_page": vehicle_state.get("current_page"),
                "is_awake": vehicle_state.get("is_awake"),
                "static_gesture": static_g,
                "static_confidence": round(static_c, 3),
                "motion_gesture": motion_g,
                "motion_confidence": round(motion_c, 3),
                "hand_moving": is_moving,
                "index_tip_moving": index_tip_moving,
                "motion_locked": time.time() < self._motion_lock_until,
                "session_awake": self._session_awake,
                "circle_debug": self._last_circle_debug,
            }
            motion_locked = time.time() < self._motion_lock_until
            if motion_locked and not confirm_mode:
                gesture, confidence = "no_gesture", 0.0
            elif confirm_mode:
                if static_g == "fist" and static_c >= 0.72:
                    gesture, confidence = "fist", static_c
                    action = "confirm_pending_accept"
                elif static_g == "palm_open" and static_c >= 0.72:
                    gesture, confidence = "palm_open", static_c
                    action = "confirm_pending_cancel"
                else:
                    gesture, confidence = "no_gesture", 0.0
            elif motion_g == "wave" and motion_c >= 0.68:
                gesture, confidence = motion_g, motion_c
                self._position_history.clear()
                self._hand_center_history.clear()
                self._circle_points.clear()
                self._v_sign_history.clear()
                self._swipe_mode_until = 0.0
                self._swipe_start_center = None
                self._motion_lock_until = time.time() + 0.08
            elif static_g == "palm_open" and static_c >= 0.84:
                gesture, confidence = static_g, static_c
            elif motion_g == "circle" and motion_c >= 0.80:
                gesture, confidence = motion_g, motion_c
                self._position_history.clear()
                self._hand_center_history.clear()
                self._circle_points.clear()
                self._v_sign_history.clear()
                self._swipe_mode_until = 0.0
                self._swipe_start_center = None
                self._motion_lock_until = time.time() + 0.35
            elif motion_g in ("point_left", "point_right") and motion_c >= 0.74:
                gesture, confidence = motion_g, motion_c
                self._position_history.clear()
                self._hand_center_history.clear()
                self._circle_points.clear()
                self._v_sign_history.clear()
                self._swipe_mode_until = 0.0
                self._swipe_start_center = None
                self._motion_lock_until = time.time() + 0.25
            elif static_g in ("point_left", "point_right") and not is_moving and not index_tip_moving:
                gesture, confidence = static_g, static_c
            elif static_g in ("thumb_up", "thumb_down") and not is_moving:
                gesture, confidence = static_g, static_c
            elif static_g == "fist" and static_c >= 0.72:
                gesture, confidence = static_g, static_c
            elif is_moving:
                gesture, confidence = "no_gesture", 0.0
            else:
                gesture, confidence = "no_gesture", 0.0

            if realtime_mode and not confirm_mode:
                if respect_standby and is_standby and gesture == "palm_open" and confidence >= 0.84:
                    realtime_debug = {
                        "active": False,
                        "candidate": "palm_open",
                        "window_size": 0,
                        "standby_wake_bypass": True,
                    }
                else:
                    gesture, confidence, realtime_debug = self._stabilize_realtime_dynamic_gesture(
                        gesture,
                        confidence,
                        static_g,
                        static_c,
                        motion_g,
                        motion_c,
                        is_moving,
                        index_tip_moving,
                        now,
                        self._last_circle_debug,
                    )
                debug_info["realtime_smoothing"] = realtime_debug
                gesture, confidence, confirmation_debug = self._apply_realtime_confirmation(
                    gesture,
                    confidence,
                    now,
                )
                debug_info["realtime_confirmation"] = confirmation_debug

            keypoints = [
                {"id": i,
                 "x": round(lm.x * w, 2),
                 "y": round(lm.y * h, 2),
                 "z": round(getattr(lm, "z", 0.0), 4)}
                for i, lm in enumerate(hand)
            ]

            if action == "confirm_pending_accept":
                pending = self.confirm_pending(True)
                action = pending["action"] if pending else None
                confirmation_resolved = True
                confirmation_accepted = True
                needs_confirmation = False
            elif action == "confirm_pending_cancel":
                self.confirm_pending(False)
                action = None
                confirmation_resolved = True
                confirmation_accepted = False
                needs_confirmation = False
            elif apply_debounce:
                raw_gesture, raw_confidence = gesture, confidence
                debounced_gesture, debounced_confidence, action = self._apply_hold_and_cooldown(gesture, confidence)
                if debounced_gesture != "no_gesture":
                    gesture, confidence = debounced_gesture, debounced_confidence
                    action, needs_confirmation = self._maybe_defer_for_confirmation(
                        gesture, confidence, action)
                else:
                    gesture, confidence = raw_gesture, raw_confidence
            else:
                _, _, action = OWNER_GESTURES.get(gesture, OWNER_GESTURES["no_gesture"])
                action, needs_confirmation = self._maybe_defer_for_confirmation(
                    gesture, confidence, action)

            if respect_standby and not confirm_mode:
                if not vehicle_state.get("is_awake") and action and action != "wake":
                    debug_info["blocked_action_in_standby"] = action
                    action = None
                    needs_confirmation = False
                if action == "wake" and now < self._wake_lock_until:
                    debug_info["blocked_wake_lock"] = round(self._wake_lock_until - now, 3)
                    action = None
                    needs_confirmation = False
                if action == "wake":
                    self._session_awake = True
                elif action == "go_home":
                    self._session_awake = False
                    self._wake_lock_until = now + 1.0
        else:
            self._hold_counter.clear()
            if time.time() - self._last_hand_seen_time > 0.5:
                self._position_history.clear()
                self._hand_center_history.clear()
                self._circle_points.clear()
                self._circle_hold_until = 0.0
                self._realtime_history.clear()
                self._wake_lock_until = 0.0
            self._last_circle_debug = {"hit": False, "reason": "no_hand", "points": len(self._circle_points)}
            debug_info = {
                "stage": "no_hand",
                "circle_debug": self._last_circle_debug,
            }
            if realtime_mode and not confirm_mode:
                gesture, confidence, confirmation_debug = self._apply_realtime_confirmation(
                    "no_gesture",
                    0.0,
                    now,
                )
                debug_info["realtime_confirmation"] = confirmation_debug

        debug_info.update({
            "final_gesture": gesture,
            "final_confidence": round(confidence, 3),
            "final_action": action,
            "needs_confirmation": needs_confirmation,
        })
        en, cn, _ = OWNER_GESTURES.get(gesture, OWNER_GESTURES["no_gesture"])
        self._annotate(annotated, en, confidence, action, needs_confirmation)

        return {
            "gesture": en,
            "gesture_cn": cn,
            "confidence": round(confidence, 3),
            "action": action,
            "needs_confirmation": needs_confirmation,
            "debug_info": debug_info,
            "confirmation_resolved": confirmation_resolved,
            "confirmation_accepted": confirmation_accepted,
            "confirm_prompt": (f"检测到“{cn}”置信度较低 ({confidence:.0%})，是否确认执行？"
                               if needs_confirmation else None),
            "keypoints": keypoints,
            "annotated_image": ndarray_to_base64(annotated),
            "success": action is not None or gesture != "no_gesture",
        }

    def _maybe_defer_for_confirmation(self, gesture: str, confidence: float, action: str | None):
        """低置信度且需确认的动作: 暂不执行, 挂起等待用户确认。返回 (生效动作, 是否需确认)。"""
        if action in CONFIRM_REQUIRED_ACTIONS and confidence < CONFIRM_CONFIDENCE_THRESHOLD:
            _, gesture_cn, _ = OWNER_GESTURES.get(gesture, OWNER_GESTURES["no_gesture"])
            self._pending_confirm = {
                "gesture": gesture,
                "gesture_cn": gesture_cn,
                "confidence": float(confidence),
                "action": action,
            }
            return None, True
        return action, False

    def _annotate(self, annotated, en, confidence, action, needs_confirmation):
        return

    def _empty_result(self, image, error: str | None = None) -> dict[str, Any]:
        return {
            "gesture": "no_gesture",
            "gesture_cn": "无手势",
            "confidence": 0.0,
            "action": None,
            "needs_confirmation": False,
            "confirmation_resolved": False,
            "confirmation_accepted": False,
            "confirm_prompt": None,
            "debug_info": {"stage": "empty", "error": error},
            "keypoints": [],
            "annotated_image": ndarray_to_base64(image),
            "success": False,
            "error": error,
        }

    def recognize_frame(
        self,
        frame: np.ndarray,
        vehicle_state: dict | None = None,
        respect_standby: bool = False,
        realtime_mode: bool = False,
    ) -> dict[str, Any]:
        _, buf = cv2.imencode(".jpg", frame)
        return self.recognize(
            buf.tobytes(),
            apply_debounce=True,
            vehicle_state=vehicle_state,
            respect_standby=respect_standby,
            realtime_mode=realtime_mode,
        )

    @staticmethod
    def _score_video_result(result: dict[str, Any]) -> tuple[int, float]:
        gesture = result.get("gesture", "no_gesture")
        action = result.get("action")
        confidence = float(result.get("confidence", 0.0))
        if action:
            return 3, confidence
        if gesture != "no_gesture":
            return 2, confidence
        return 1, confidence

    @staticmethod
    def _build_result_segments(results: list[dict[str, Any]], max_gap: int = 4) -> list[dict[str, Any]]:
        if not results:
            return []
        ordered = sorted(results, key=lambda item: int(item.get("frame", -1)))
        segments: list[dict[str, Any]] = []
        current_items: list[dict[str, Any]] = []
        current_gesture: str | None = None
        last_frame = -10_000

        for item in ordered:
            gesture = item.get("gesture", "no_gesture")
            frame = int(item.get("frame", -1))
            if current_items and (gesture != current_gesture or frame - last_frame > max_gap):
                segments.append({
                    "gesture": current_gesture,
                    "items": current_items,
                    "start_frame": int(current_items[0].get("frame", -1)),
                    "end_frame": int(current_items[-1].get("frame", -1)),
                })
                current_items = []
            current_gesture = gesture
            current_items.append(item)
            last_frame = frame

        if current_items:
            segments.append({
                "gesture": current_gesture,
                "items": current_items,
                "start_frame": int(current_items[0].get("frame", -1)),
                "end_frame": int(current_items[-1].get("frame", -1)),
            })
        return segments

    def _select_video_best_result(self, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not results:
            return None
        segments = self._build_result_segments(results)
        nonwake_segments = []
        for segment in segments:
            actionable_items = [
                item for item in segment["items"]
                if item.get("action") and item.get("action") != "wake"
            ]
            if not actionable_items:
                continue
            nonwake_segments.append({
                **segment,
                "actionable_items": actionable_items,
                "max_conf": max(float(item.get("confidence", 0.0)) for item in segment["items"]),
                "length": len(segment["items"]),
            })
        if nonwake_segments:
            best_conf = max(segment["max_conf"] for segment in nonwake_segments)
            candidate_segments = [
                segment for segment in nonwake_segments
                if segment["max_conf"] >= best_conf - 0.12
            ]
            chosen_segment = max(
                candidate_segments,
                key=lambda segment: (
                    segment["length"],
                    len(segment["actionable_items"]),
                    segment["end_frame"],
                    segment["max_conf"],
                ),
            )
            return max(
                chosen_segment["actionable_items"],
                key=lambda item: (float(item.get("confidence", 0.0)), int(item.get("frame", -1))),
            )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for result in results:
            gesture = result.get("gesture", "no_gesture")
            grouped.setdefault(gesture, []).append(result)

        stable_action_groups: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
        for gesture, items in grouped.items():
            actionable_items = [
                item for item in items
                if item.get("action") and item.get("action") != "wake"
            ]
            if len(actionable_items) >= 2:
                stable_action_groups.append((gesture, items, actionable_items))
        if stable_action_groups:
            stable_action_groups.sort(
                key=lambda entry: (
                    len(entry[2]),
                    len(entry[1]),
                    max(int(item.get("frame", -1)) for item in entry[2]),
                    max(float(item.get("confidence", 0.0)) for item in entry[2]),
                ),
                reverse=True,
            )
            _, items, actionable_items = stable_action_groups[0]
            return max(
                actionable_items,
                key=lambda item: (float(item.get("confidence", 0.0)), int(item.get("frame", -1))),
            )

        wave_items = [
            item for item in grouped.get("wave", [])
            if item.get("action") == "go_home" and float(item.get("confidence", 0.0)) >= 0.74
        ]
        if wave_items:
            return max(
                wave_items,
                key=lambda item: (float(item.get("confidence", 0.0)), int(item.get("frame", -1))),
            )

        recent_gestures = [result.get("gesture", "no_gesture") for result in results[-8:]]

        def group_score(item: tuple[str, list[dict[str, Any]]]):
            gesture, items = item
            top_rank = max(self._score_video_result(entry)[0] for entry in items)
            avg_conf = sum(float(entry.get("confidence", 0.0)) for entry in items) / len(items)
            max_conf = max(float(entry.get("confidence", 0.0)) for entry in items)
            recent_count = sum(1 for recent_gesture in recent_gestures if recent_gesture == gesture)
            action_count = sum(
                1 for entry in items
                if entry.get("action") and entry.get("action") != "wake"
            )
            score = len(items) + action_count * 15 + recent_count * 3
            return score, top_rank, avg_conf, max_conf

        dominant_gesture, dominant_items = max(grouped.items(), key=group_score)
        actionable_items = [
            item for item in dominant_items
            if item.get("action") and item.get("action") != "wake"
        ]
        if actionable_items:
            return max(
                actionable_items,
                key=lambda item: (float(item.get("confidence", 0.0)), int(item.get("frame", -1))),
            )
        return max(
            dominant_items,
            key=lambda item: (self._score_video_result(item), float(item.get("confidence", 0.0))),
        )

    def process_video(
        self,
        video_path: Path,
        sample_interval: int = 1,
        vehicle_state: dict | None = None,
        respect_standby: bool = False,
    ) -> dict[str, Any]:
        cap = cv2.VideoCapture(str(video_path))
        results: list[dict[str, Any]] = []
        sampled_frames = 0
        frame_index = 0
        last_result: dict[str, Any] | None = None
        initial_state = dict(vehicle_state or {
            "volume": 50,
            "temperature": 24,
            "phone_status": "idle",
            "current_page": "standby",
            "is_awake": 0,
        })
        local_state = dict(initial_state)

        self._reset_runtime_state()
        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break
                if frame_index % sample_interval == 0:
                    sampled_frames += 1
                    result = self.recognize_frame(
                        frame,
                        vehicle_state=local_state,
                        respect_standby=respect_standby,
                    )
                    last_result = result
                    if result.get("action"):
                        local_state = self.apply_action_to_state(result["action"], dict(local_state))
                        result["vehicle_state"] = dict(local_state)
                    if result.get("gesture") != "no_gesture":
                        results.append({
                            "frame": frame_index,
                            "time_sec": round(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0, 2),
                            **result,
                        })
                frame_index += 1
        finally:
            cap.release()
            self._reset_runtime_state()

        best_result = self._select_video_best_result(results)
        final_state = dict(local_state)
        if best_result:
            resolved_state = dict(initial_state)
            best_action = best_result.get("action")
            if respect_standby and not initial_state.get("is_awake") and best_action != "wake":
                best_result = dict(best_result)
                best_result["action"] = None
                best_result["vehicle_state"] = dict(resolved_state)
                final_state = dict(resolved_state)
            else:
                if best_action:
                    resolved_state = self.apply_action_to_state(best_action, resolved_state)
                best_result = dict(best_result)
                best_result["vehicle_state"] = dict(resolved_state)
                final_state = dict(resolved_state)
        return {
            "frame_count": frame_index,
            "sampled_frames": sampled_frames,
            "recognized_frames": len(results),
            "success": best_result is not None,
            "best_result": best_result,
            "preview_result": last_result,
            "results": results,
            "final_vehicle_state": dict(final_state),
        }

    # ---------------------- 车辆控制 ----------------------
    def apply_action_to_state(self, action: str, state: dict) -> dict:
        if not action:
            return state
        control_items = ["volume_up", "volume_down", "temp_up", "temp_down"]
        current = state.get("current_page", "volume_up")
        if current in ("home", "media", "climate", "phone", "standby"):
            current = "volume_up"
        if action == "wake":
            state["is_awake"] = 1
            if state.get("current_page") in ("home", "standby"):
                state["current_page"] = "volume_up"
            return state
        elif action == "confirm":
            state["is_awake"] = 1
            selected = state.get("current_page", "volume_up")
            if selected == "volume_up":
                state["volume"] = min(100, max(0, state.get("volume", 50) + 5))
            elif selected == "volume_down":
                state["volume"] = min(100, max(0, state.get("volume", 50) - 5))
            elif selected == "temp_up":
                state["temperature"] = min(32, max(16, state.get("temperature", 24) + 1))
            elif selected == "temp_down":
                state["temperature"] = min(32, max(16, state.get("temperature", 24) - 1))
            return state
        elif action == "volume_adjust":
            selected = state.get("current_page", "volume_up")
            if selected in ("temp_up", "temp_down"):
                delta = 1 if selected == "temp_up" else -1
                state["temperature"] = min(32, max(16, state.get("temperature", 24) + delta))
            else:
                delta = 5 if selected != "volume_down" else -5
                state["volume"] = min(100, max(0, state.get("volume", 50) + delta))
            state["is_awake"] = 1
            return state
        elif action == "prev_page":
            try:
                idx = control_items.index(current)
            except ValueError:
                idx = 0
            state["current_page"] = control_items[(idx - 1) % len(control_items)]
            state["is_awake"] = 1
            return state
        elif action == "next_page":
            try:
                idx = control_items.index(current)
            except ValueError:
                idx = -1
            state["current_page"] = control_items[(idx + 1) % len(control_items)]
            state["is_awake"] = 1
            return state
        elif action == "answer_call":
            state["phone_status"] = "in_call"
        elif action == "hang_up":
            state["phone_status"] = "idle"
        elif action == "go_home":
            state["current_page"] = "standby"
            state["is_awake"] = 0
            return state
        return state


owner_gesture_service = OwnerGestureService()
