import unittest
from types import SimpleNamespace

import numpy as np

from app.config import settings
from app.services.police_gesture_service import PoliceGestureService
from app.services.ctpgr_pose_adapter import select_person_index_with_match


class PolicePoseContinuityTests(unittest.TestCase):
    def test_initial_selection_uses_largest_person(self):
        boxes = np.array([[0, 0, 100, 100], [0, 0, 200, 200]], dtype=np.float32)
        self.assertEqual(PoliceGestureService._select_person_index(boxes), 1)

    def test_tracking_keeps_previous_person_when_larger_bystander_appears(self):
        previous = np.array([10, 10, 110, 210], dtype=np.float32)
        boxes = np.array(
            [
                [14, 12, 114, 212],
                [180, 0, 500, 500],
            ],
            dtype=np.float32,
        )
        self.assertEqual(PoliceGestureService._select_person_index(boxes, previous), 0)

    def test_tracking_rejects_only_five_percent_overlap_as_a_person_switch(self):
        previous = np.array([0, 0, 100, 100], dtype=np.float32)
        boxes = np.array([[90, 0, 190, 100]], dtype=np.float32)
        index, matched = select_person_index_with_match(boxes, previous)
        self.assertEqual(index, 0)
        self.assertFalse(matched)

    def test_missing_pose_reuses_recent_coordinates_for_configured_limit(self):
        original_limit = settings.police_pose_hold_frames
        settings.police_pose_hold_frames = 2
        try:
            coord = np.ones((1, 2, 14), dtype=np.float32)
            state = {"last_coord": coord, "last_box": np.ones(4), "missed_pose_frames": 0}
            np.testing.assert_array_equal(PoliceGestureService._reuse_recent_pose(state), coord)
            np.testing.assert_array_equal(PoliceGestureService._reuse_recent_pose(state), coord)
            with self.assertRaises(ValueError):
                PoliceGestureService._reuse_recent_pose(state)
            self.assertIsNone(state["last_box"])
        finally:
            settings.police_pose_hold_frames = original_limit

    def test_missing_pose_without_reusable_coordinates_still_resets_track(self):
        original_limit = settings.police_pose_hold_frames
        settings.police_pose_hold_frames = 2
        try:
            state = {
                "h": np.ones(1),
                "c": np.ones(1),
                "last_coord": None,
                "last_box": np.ones(4),
                "missed_pose_frames": 0,
                "pose_repair_state": {"arm_occluded_frames": [6, 0]},
            }
            for _ in range(3):
                with self.assertRaises(ValueError):
                    PoliceGestureService._reuse_recent_pose(state)
            self.assertIsNone(state["last_box"])
            self.assertIsNone(state["h"])
            self.assertEqual(state["pose_repair_state"], {})
        finally:
            settings.police_pose_hold_frames = original_limit

    def test_person_switch_reset_clears_lstm_and_pose_history(self):
        state = {
            "h": np.ones(1),
            "c": np.ones(1),
            "last_coord": np.ones((1, 2, 14)),
            "last_box": np.ones(4),
            "missed_pose_frames": 1,
            "pose_repair_state": {"previous_xy": np.ones((17, 2))},
        }
        PoliceGestureService._reset_pose_track_state(state)
        self.assertIsNone(state["h"])
        self.assertIsNone(state["c"])
        self.assertIsNone(state["last_coord"])
        self.assertIsNone(state["last_box"])
        self.assertEqual(state["pose_repair_state"], {})

    def test_unusable_pose_is_not_reported_as_confident_no_gesture(self):
        service = PoliceGestureService()
        service._plain_payload = lambda image, gesture_id, confidence, coord, reason: {
            "gesture_id": gesture_id,
            "confidence": confidence,
            "reason": reason,
        }
        payload = service._no_gesture_payload(np.zeros((8, 8, 3), dtype=np.uint8), "pose missing")
        self.assertEqual(payload["gesture_id"], 0)
        self.assertEqual(payload["confidence"], 0.0)
        self.assertEqual(payload["reason"], "pose missing")

    def test_pose_backend_switch_uses_separate_classifiers(self):
        service = PoliceGestureService()
        service._predictor = SimpleNamespace(bla="ctpgr-bla", g_model="ctpgr-model")
        service._bla = "yolo-bla"
        service._g_model = "yolo-model"
        service._pose_backend_override = "yolo"
        self.assertEqual(service.bla, "yolo-bla")
        self.assertEqual(service.g_model, "yolo-model")
        service._pose_backend_override = "ctpgr"
        self.assertEqual(service.bla, "ctpgr-bla")
        self.assertEqual(service.g_model, "ctpgr-model")
        service._pose_backend_override = "yolo"
        self.assertEqual(service.g_model, "yolo-model")


if __name__ == "__main__":
    unittest.main()
