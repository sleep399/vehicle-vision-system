from pathlib import Path
from unittest.mock import patch

from app.routers.owner_gesture import router as owner_router
from app.services.owner_gesture_service import (
    OWNER_GESTURES,
    STANDBY_ALLOWED_ACTIONS,
    OwnerGestureService,
    owner_gesture_service,
)


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _service_without_models() -> OwnerGestureService:
    service = OwnerGestureService.__new__(OwnerGestureService)
    service._pending_confirm = None
    service._last_emitted = "no_gesture"
    service._last_action_time = 0.0
    return service


def test_owner_gesture_mapping_covers_eight_actions():
    actions = {key: value[2] for key, value in OWNER_GESTURES.items() if key != "no_gesture"}
    assert actions == {
        "palm_open": "wake",
        "fist": "confirm",
        "circle": "volume_adjust",
        "point_left": "prev_page",
        "point_right": "next_page",
        "thumb_up": "answer_call",
        "thumb_down": "hang_up",
        "wave": "go_home",
    }


def test_only_one_realtime_owner_session_can_mutate_shared_gesture_state():
    first = "owner-test-first"
    second = "owner-test-second"
    owner_gesture_service.release_realtime_session(first)
    owner_gesture_service.release_realtime_session(second)
    assert owner_gesture_service.acquire_realtime_session(first)
    try:
        assert not owner_gesture_service.acquire_realtime_session(second)
    finally:
        owner_gesture_service.release_realtime_session(first)
    assert owner_gesture_service.acquire_realtime_session(second)
    owner_gesture_service.release_realtime_session(second)


def test_point_direction_matches_unmirrored_camera_frame():
    service = _service_without_models()
    assert service.MIRROR_POINT_DIRECTIONS is False
    assert service._normalize_point_direction("point_left") == "point_left"
    assert service._normalize_point_direction("point_right") == "point_right"


def test_vehicle_state_machine_wake_select_confirm_and_standby():
    service = _service_without_models()
    state = {"volume": 50, "temperature": 24, "phone_status": "idle", "current_page": "standby", "is_awake": 0}

    assert service.apply_action_to_state("wake", state)["current_page"] == "volume_up"
    assert state["is_awake"] == 1
    service.apply_action_to_state("next_page", state)
    assert state["current_page"] == "volume_down"
    service.apply_action_to_state("confirm", state)
    assert state["volume"] == 45
    service.apply_action_to_state("answer_call", state)
    assert state["phone_status"] == "in_call"
    service.apply_action_to_state("go_home", state)
    assert state["current_page"] == "standby"
    assert state["is_awake"] == 0


def test_vehicle_controls_match_the_documented_owner_actions():
    service = _service_without_models()
    state = {"volume": 50, "temperature": 24, "phone_status": "idle", "current_page": "volume_up", "is_awake": 1}

    service.apply_action_to_state("volume_adjust", state)
    assert state["volume"] == 55

    service.apply_action_to_state("prev_page", state)
    assert state["current_page"] == "temp_down"
    service.apply_action_to_state("confirm", state)
    assert state["temperature"] == 23

    service.apply_action_to_state("next_page", state)
    assert state["current_page"] == "volume_up"
    service.apply_action_to_state("answer_call", state)
    assert state["phone_status"] == "in_call"
    service.apply_action_to_state("hang_up", state)
    assert state["phone_status"] == "idle"


def test_left_and_right_actions_cycle_all_four_control_items():
    service = _service_without_models()
    state = {
        "volume": 50,
        "temperature": 24,
        "phone_status": "idle",
        "current_page": "volume_up",
        "is_awake": 1,
    }

    next_pages = []
    for _ in range(4):
        service.apply_action_to_state("next_page", state)
        next_pages.append(state["current_page"])
    assert next_pages == ["volume_down", "temp_up", "temp_down", "volume_up"]

    previous_pages = []
    for _ in range(4):
        service.apply_action_to_state("prev_page", state)
        previous_pages.append(state["current_page"])
    assert previous_pages == ["temp_down", "temp_up", "volume_down", "volume_up"]


def test_circle_and_fist_adjust_each_selected_control():
    service = _service_without_models()
    cases = {
        "volume_up": ("volume", 50, 55),
        "volume_down": ("volume", 50, 45),
        "temp_up": ("temperature", 24, 25),
        "temp_down": ("temperature", 24, 23),
    }

    for action in ("volume_adjust", "confirm"):
        for current_page, (field, initial, expected) in cases.items():
            state = {
                "volume": 50,
                "temperature": 24,
                "phone_status": "idle",
                "current_page": current_page,
                "is_awake": 1,
            }
            assert state[field] == initial
            service.apply_action_to_state(action, state)
            assert state[field] == expected


def test_phone_shortcuts_remain_available_while_vehicle_ui_is_asleep():
    service = _service_without_models()
    assert STANDBY_ALLOWED_ACTIONS == {"wake", "answer_call", "hang_up"}

    for action in ("answer_call", "hang_up"):
        allowed_action, needs_confirmation, blocked = service._gate_action_for_standby(
            action,
            False,
            {"is_awake": 0, "current_page": "standby"},
            respect_standby=True,
            confirm_mode=False,
        )
        assert allowed_action == action
        assert needs_confirmation is False
        assert blocked is None

    state = {
        "volume": 50,
        "temperature": 24,
        "phone_status": "idle",
        "current_page": "standby",
        "is_awake": 0,
    }
    service.apply_action_to_state("answer_call", state)
    assert state["phone_status"] == "in_call"
    assert state["is_awake"] == 0
    service.apply_action_to_state("hang_up", state)
    assert state["phone_status"] == "idle"

    blocked_action, needs_confirmation, blocked = service._gate_action_for_standby(
        "next_page",
        False,
        {"is_awake": 0, "current_page": "standby"},
        respect_standby=True,
        confirm_mode=False,
    )
    assert blocked_action is None
    assert needs_confirmation is False
    assert blocked == "next_page"

    service._pending_confirm = {
        "gesture": "fist",
        "gesture_cn": "握拳",
        "confidence": 0.8,
        "action": "confirm",
    }
    blocked_action, needs_confirmation, blocked = service._gate_action_for_standby(
        None,
        True,
        {"is_awake": 0, "current_page": "standby"},
        respect_standby=True,
        confirm_mode=False,
    )
    assert blocked_action is None
    assert needs_confirmation is False
    assert blocked == "confirm"
    assert service.has_pending_confirm() is False


def test_uploaded_video_keeps_phone_shortcut_action_while_vehicle_ui_is_asleep():
    class OneFrameCapture:
        def __init__(self):
            self.read_count = 0

        def isOpened(self):
            return True

        def read(self):
            self.read_count += 1
            return (True, object()) if self.read_count == 1 else (False, None)

        def get(self, _property):
            return 0.0

        def release(self):
            return None

    service = _service_without_models()
    service._reset_runtime_state = lambda *args, **kwargs: None
    service.recognize_frame = lambda *args, **kwargs: {
        "gesture": "thumb_up",
        "gesture_cn": "拇指向上",
        "confidence": 0.9,
        "action": "answer_call",
    }
    service._select_video_best_result = lambda results: dict(results[-1])
    initial_state = {
        "volume": 50,
        "temperature": 24,
        "phone_status": "idle",
        "current_page": "standby",
        "is_awake": 0,
    }

    with patch(
        "app.services.owner_gesture_service.cv2.VideoCapture",
        return_value=OneFrameCapture(),
    ):
        payload = service.process_video(
            Path("standby-thumb-up.mp4"),
            vehicle_state=initial_state,
            respect_standby=True,
        )

    assert payload["best_result"]["action"] == "answer_call"
    assert payload["best_result"]["vehicle_state"]["phone_status"] == "in_call"
    assert payload["final_vehicle_state"]["phone_status"] == "in_call"
    assert payload["final_vehicle_state"]["is_awake"] == 0


def test_low_confidence_confirm_is_deferred_and_can_be_accepted():
    service = _service_without_models()
    action, pending = service._maybe_defer_for_confirmation("fist", 0.8, "confirm")
    assert action is None
    assert pending is True
    assert service.has_pending_confirm()
    accepted = service.confirm_pending(True)
    assert accepted["gesture"] == "fist"
    assert accepted["action"] == "confirm"
    assert not service.has_pending_confirm()


def test_pending_confirmation_is_isolated_between_users_and_shared_for_guests():
    service = _service_without_models()
    service._maybe_defer_for_confirmation(
        "fist", 0.8, "confirm", context_id=101,
    )
    service._maybe_defer_for_confirmation(
        "thumb_up", 0.8, "confirm", context_id=202,
    )
    service._maybe_defer_for_confirmation(
        "thumb_down", 0.8, "confirm", context_id=None,
    )

    assert service.has_pending_confirm(context_id=101)
    assert service.has_pending_confirm(context_id=202)
    assert service.has_pending_confirm(context_id=None)
    assert service.confirm_pending(True, context_id=303) is None

    assert service.confirm_pending(True, context_id=202)["gesture"] == "thumb_up"
    assert service.has_pending_confirm(context_id=101)
    assert service.confirm_pending(True, context_id=None)["gesture"] == "thumb_down"
    assert service.confirm_pending(True, context_id=101)["gesture"] == "fist"


def test_realtime_gesture_requires_stable_candidate_and_bridges_short_gap():
    service = _service_without_models()
    service._realtime_candidate_gesture = "no_gesture"
    service._realtime_candidate_confidence = 0.0
    service._realtime_candidate_since = 0.0
    service._realtime_confirmed_gesture = "no_gesture"
    service._realtime_confirmed_confidence = 0.0
    service._realtime_confirmed_at = 0.0

    gesture, _, debug = service._apply_realtime_confirmation("fist", 0.9, 10.0)
    assert gesture == "no_gesture"
    assert debug["hold"] == "wait_candidate"

    gesture, confidence, debug = service._apply_realtime_confirmation("fist", 0.92, 10.2)
    assert gesture == "fist"
    assert confidence == 0.92
    assert debug["hold"] == "candidate_confirmed"

    gesture, confidence, debug = service._apply_realtime_confirmation("no_gesture", 0.0, 10.3)
    assert gesture == "fist"
    assert confidence == 0.92
    assert debug["hold"] == "keep_confirmed"


def test_owner_public_routes_and_frontend_contract_are_present():
    paths = {route.path for route in owner_router.routes if hasattr(route, "path")}
    assert "/api/owner-gesture/recognize-video" in paths
    assert "/api/owner-gesture/confirm" in paths
    assert "/api/owner-gesture/ws-stream" in paths

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
    assert 'id="owner-function-selector"' in html
    assert 'id="owner-camera-device"' in html
    assert 'id="owner-camera-status"' in html
    assert "const shouldApplyVehicleState = !opts.realtime" in js
    assert "{ realtime: module === 'owner' }" in js
    assert "const saved = await this.api('/api/owner-gesture/vehicle-state'" in js
    assert 'id="standby-page"' in html
    assert "/api/owner-gesture/ws-stream" in js
    assert "openCameraStream(module)" in js
    assert "refreshCameraDevices(module)" in js
    assert "cameraDevicePriority(device)" in js
    assert "自动选择（优先笔记本前置摄像头）" in html
    assert "showGestureConfirm(prompt)" in js
    assert "renderOwnerVideoPayload(payload)" in js
    assert "/api/owner-gesture/recognize-video" in js
    assert "ownerLastGestureUntil" in js
    assert "ownerStandbyDismissed = true" in js
    assert "module === 'police' ? 80 : 200" in js
    assert "msg.type === 'frame_error' || msg.type === 'error'" in js
    assert "ownerActionLabel(action)" in js
    assert "点赞 → 接听电话" in html
    assert "倒赞 → 挂断电话" in html
    assert '.function-card.active' in css
    assert '.phone-status.in-call' in css
    assert "this.updatePhoneState(s.phone_status)" in js
    assert "card.setAttribute('aria-current'" in js
    assert "this.updateOwnerFunctionHighlight(this.ownerCurrentControl)" in js
    assert "this.ownerCurrentControl = controlItems.includes(s.current_page) ? s.current_page : 'volume_up'" in js
    assert "names[this.ownerCurrentControl] || this.ownerCurrentControl" in js
    assert "const isStandbyPhoneShortcut = ['answer_call', 'hang_up'].includes(data.action)" in js
