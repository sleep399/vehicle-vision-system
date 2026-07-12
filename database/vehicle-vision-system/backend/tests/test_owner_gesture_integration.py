from pathlib import Path

from app.routers.owner_gesture import router as owner_router
from app.services.owner_gesture_service import (
    OWNER_GESTURES,
    OwnerGestureService,
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
