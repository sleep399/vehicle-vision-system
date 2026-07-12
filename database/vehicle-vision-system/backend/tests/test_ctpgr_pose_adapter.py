import unittest

import numpy as np

from app.services.ctpgr_pose_adapter import CTPGR_HEATMAP_SIZE, coco_to_ctpgr


class CtpgrPoseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.coco = np.array([(20 + i * 21, 30 + i * 17) for i in range(17)], dtype=np.float32)

    def test_maps_coco_joints_to_aichallenger_order(self):
        result = coco_to_ctpgr(self.coco)[0].T
        expected_right_shoulder = np.floor(self.coco[6] / 512 * 64) / 64
        expected_left_wrist = np.floor(self.coco[9] / 512 * 64) / 64
        np.testing.assert_array_equal(result[0], expected_right_shoulder)
        np.testing.assert_array_equal(result[5], expected_left_wrist)

    def test_coordinates_match_ctpgr_heatmap_grid(self):
        result = coco_to_ctpgr(self.coco)
        scaled = result * CTPGR_HEATMAP_SIZE
        np.testing.assert_allclose(scaled, np.round(scaled))
        self.assertLessEqual(float(result.max()), 63 / 64)

    def test_zero_coordinates_are_kept_like_ctpgr_argmax(self):
        self.coco[10] = (0, 0)
        result = coco_to_ctpgr(self.coco)[0].T
        np.testing.assert_array_equal(result[2], (0.0, 0.0))

    def test_rejects_invalid_pose_shape(self):
        with self.assertRaises(ValueError):
            coco_to_ctpgr(np.zeros((14, 2), dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
