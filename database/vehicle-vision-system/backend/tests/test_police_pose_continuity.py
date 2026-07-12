import unittest

import numpy as np

from app.config import settings
from app.services.police_gesture_service import PoliceGestureService


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


if __name__ == "__main__":
    unittest.main()
