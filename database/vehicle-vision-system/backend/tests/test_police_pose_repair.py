import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.services.ctpgr_pose_adapter import (
    ArmPosePrior,
    fit_arm_pose_prior,
    load_arm_pose_prior,
    repair_coco_pose,
    save_arm_pose_prior,
)


def sample_pose(scale: float = 1.0) -> np.ndarray:
    pose = np.array([(256.0, 256.0) for _ in range(17)], dtype=np.float32)
    pose[0] = (256, 100)
    pose[5] = (306, 200)
    pose[6] = (206, 200)
    pose[7] = (266, 250)
    pose[8] = (246, 250)
    pose[9] = (226, 300)
    pose[10] = (286, 300)
    pose[11] = (306, 330)
    pose[12] = (206, 330)
    pose[13] = (306, 410)
    pose[14] = (206, 410)
    pose[15] = (306, 490)
    pose[16] = (206, 490)
    center = np.array([256.0, 256.0], dtype=np.float32)
    return (pose - center) * scale + center


def synthetic_prior() -> ArmPosePrior:
    arm_ratio = float(np.hypot(40, 50) / 100)
    return ArmPosePrior(
        shoulder_width_median=100 / 512,
        upper_median=(arm_ratio, arm_ratio),
        upper_low=(0.45, 0.45),
        upper_high=(0.90, 0.90),
        lower_median=(arm_ratio, arm_ratio),
        lower_low=(0.45, 0.45),
        lower_high=(0.90, 0.90),
        angle_low=(0.10, 0.10),
        angle_high=(np.pi, np.pi),
        upper_delta_high=(0.20, 0.20),
        lower_delta_high=(0.20, 0.20),
        angle_delta_high=(np.deg2rad(70), np.deg2rad(70)),
        sample_count=100,
        source="synthetic test",
    )


class PolicePoseRepairTests(unittest.TestCase):
    def setUp(self):
        self.pose = sample_pose()
        self.confidence = np.full(17, 0.9, dtype=np.float32)
        self.prior = synthetic_prior()

    def test_high_confidence_anatomical_arm_passes_through(self):
        result = repair_coco_pose(self.pose, self.confidence, prior=self.prior)
        np.testing.assert_array_equal(result.coordinates, self.pose)
        self.assertFalse(result.repaired[[5, 6, 7, 8, 9, 10]].any())

    def test_threshold_values_are_inclusive(self):
        confidence = self.confidence.copy()
        confidence[[5, 6]] = 0.35
        confidence[[7, 8]] = 0.40
        confidence[[9, 10]] = 0.45
        result = repair_coco_pose(self.pose, confidence, prior=self.prior)
        np.testing.assert_array_equal(result.coordinates, self.pose)

    def test_low_confidence_wrist_uses_previous_direction_and_bone_length(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        corrupted = self.pose.copy()
        corrupted[9] = (20, 20)
        confidence = self.confidence.copy()
        confidence[9] = 0.10
        result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior)
        expected_length = self.prior.lower_median[1] * 100
        actual_length = np.linalg.norm(result.coordinates[9] - result.coordinates[7])
        self.assertAlmostEqual(float(actual_length), expected_length, places=4)
        self.assertTrue(result.repaired[9])
        self.assertLess(np.linalg.norm(result.coordinates[9] - self.pose[9]), 1.0)

    def test_high_confidence_geometric_outlier_is_repaired(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        corrupted = self.pose.copy()
        corrupted[10] = (500, 20)
        result = repair_coco_pose(corrupted, self.confidence, state=state, prior=self.prior)
        self.assertTrue(result.repaired[10])
        self.assertLess(np.linalg.norm(result.coordinates[10] - self.pose[10]), 1.0)

    def test_high_confidence_mirrored_wrist_jump_is_repaired(self):
        state = {}
        bent = self.pose.copy()
        bent[10] = bent[8] + np.array([64.0, 0.0], dtype=np.float32)
        repair_coco_pose(bent, self.confidence, state=state, prior=self.prior)
        mirrored = bent.copy()
        mirrored[10] = mirrored[8] + np.array([-14.2, 62.4], dtype=np.float32)
        result = repair_coco_pose(mirrored, self.confidence, state=state, prior=self.prior)
        self.assertTrue(result.repaired[10])
        self.assertLess(np.linalg.norm(result.coordinates[10] - bent[10]), 10.0)
        repeated = repair_coco_pose(mirrored, self.confidence, state=state, prior=self.prior)
        self.assertTrue(repeated.repaired[10])
        self.assertLess(np.linalg.norm(repeated.coordinates[10] - bent[10]), 10.0)

    def test_anatomically_valid_fast_motion_is_not_smoothed(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        moved = self.pose.copy()
        radius = np.linalg.norm(moved[10] - moved[8])
        moved[10] = moved[8] + np.array([radius, 0], dtype=np.float32)
        result = repair_coco_pose(moved, self.confidence, state=state, prior=self.prior)
        np.testing.assert_array_equal(result.coordinates[10], moved[10])
        self.assertFalse(result.repaired[10])

    def test_two_bad_distal_joints_hold_for_five_frames_then_invalidate_arm(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior, hold_frames=5)
        confidence = self.confidence.copy()
        confidence[[8, 10]] = 0.0
        corrupted = self.pose.copy()
        corrupted[[8, 10]] = (0, 0)
        for _ in range(5):
            result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior, hold_frames=5)
            self.assertTrue(result.arm_valid[0])
            np.testing.assert_allclose(result.coordinates[[6, 8, 10]], self.pose[[6, 8, 10]])
        result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior, hold_frames=5)
        self.assertFalse(result.arm_valid[0])
        self.assertTrue(result.arm_valid[1])
        first_recovery = repair_coco_pose(
            self.pose,
            self.confidence,
            state=state,
            prior=self.prior,
            hold_frames=5,
        )
        self.assertFalse(first_recovery.arm_valid[0])
        recovered = repair_coco_pose(
            self.pose,
            self.confidence,
            state=state,
            prior=self.prior,
            hold_frames=5,
        )
        self.assertTrue(recovered.arm_valid[0])
        self.assertEqual(state["arm_occluded_frames"][0], 0)

    def test_single_bad_wrist_is_not_extrapolated_beyond_five_frames(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior, hold_frames=5)
        confidence = self.confidence.copy()
        confidence[10] = 0.0
        for _ in range(5):
            result = repair_coco_pose(self.pose, confidence, state=state, prior=self.prior, hold_frames=5)
            self.assertTrue(result.arm_valid[0])
        result = repair_coco_pose(self.pose, confidence, state=state, prior=self.prior, hold_frames=5)
        self.assertFalse(result.arm_valid[0])
        self.assertFalse(result.usable)

    def test_arm_can_reinitialize_in_a_new_valid_pose_after_long_occlusion(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior, hold_frames=5)
        confidence = self.confidence.copy()
        confidence[10] = 0.0
        for _ in range(6):
            repair_coco_pose(self.pose, confidence, state=state, prior=self.prior, hold_frames=5)
        recovered_pose = self.pose.copy()
        recovered_pose[10] = recovered_pose[8] + np.array([64.0, 0.0], dtype=np.float32)
        first_recovery = repair_coco_pose(
            recovered_pose,
            self.confidence,
            state=state,
            prior=self.prior,
            hold_frames=5,
        )
        self.assertFalse(first_recovery.usable)
        recovered = repair_coco_pose(
            recovered_pose,
            self.confidence,
            state=state,
            prior=self.prior,
            hold_frames=5,
        )
        self.assertTrue(recovered.usable)
        self.assertTrue(recovered.arm_valid[0])
        self.assertEqual(state["arm_occluded_frames"][0], 0)
        np.testing.assert_array_equal(recovered.coordinates[10], recovered_pose[10])

    def test_cold_start_with_unreliable_distal_joints_is_rejected(self):
        confidence = self.confidence.copy()
        confidence[[7, 8, 9, 10]] = 0.0
        result = repair_coco_pose(self.pose, confidence, state={}, prior=self.prior)
        self.assertFalse(result.usable)
        self.assertEqual(result.arm_valid, (False, False))

    def test_repaired_shoulder_is_usable_briefly_then_times_out(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior, hold_frames=5)
        confidence = self.confidence.copy()
        confidence[6] = 0.0
        corrupted = self.pose.copy()
        corrupted[6] = (0, 0)
        for _ in range(5):
            result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior, hold_frames=5)
            self.assertTrue(result.arm_valid[0])
        result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior, hold_frames=5)
        self.assertFalse(result.arm_valid[0])

    def test_low_confidence_elbow_uses_reliable_wrist_circle_solution(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        corrupted = self.pose.copy()
        corrupted[8] = (0, 0)
        confidence = self.confidence.copy()
        confidence[8] = 0.0
        result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior)
        self.assertTrue(result.repaired[8])
        np.testing.assert_array_equal(result.coordinates[10], self.pose[10])
        self.assertAlmostEqual(
            float(np.linalg.norm(result.coordinates[8] - result.coordinates[6])),
            self.prior.upper_median[0] * 100,
            places=3,
        )

    def test_unreachable_wrist_is_not_kept_when_elbow_is_bad(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        corrupted = self.pose.copy()
        corrupted[8] = (0, 0)
        corrupted[10] = corrupted[6]
        confidence = self.confidence.copy()
        confidence[8] = 0.0
        result = repair_coco_pose(corrupted, confidence, state=state, prior=self.prior)
        self.assertTrue(result.repaired[8])
        self.assertTrue(result.repaired[10])
        self.assertGreater(np.linalg.norm(result.coordinates[10] - result.coordinates[8]), 40)

    def test_left_and_right_occlusion_state_are_independent(self):
        state = {}
        repair_coco_pose(self.pose, self.confidence, state=state, prior=self.prior)
        confidence = self.confidence.copy()
        confidence[[7, 9]] = 0.0
        result = repair_coco_pose(self.pose, confidence, state=state, prior=self.prior)
        self.assertEqual(state["arm_occluded_frames"], [0, 1])
        self.assertTrue(result.arm_valid[0])

    def test_invalid_shapes_and_non_finite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            repair_coco_pose(np.zeros((14, 2)), self.confidence)
        with self.assertRaises(ValueError):
            repair_coco_pose(self.pose, np.ones(14))
        broken = self.pose.copy()
        broken[10, 0] = np.nan
        with self.assertRaises(ValueError):
            repair_coco_pose(broken, self.confidence)


class ArmPosePriorTests(unittest.TestCase):
    def test_profile_fit_is_scale_normalized_and_robust_to_outlier(self):
        first = np.repeat(sample_pose()[None], 40, axis=0)
        second = np.repeat(sample_pose(1.5)[None], 40, axis=0)
        first[-1, 10] = (0, 0)
        confidence = np.full((40, 17), 0.9, dtype=np.float32)
        prior = fit_arm_pose_prior([first, second], [confidence, confidence], source="unit test")
        expected = float(np.hypot(40, 50) / 100)
        self.assertAlmostEqual(prior.upper_median[0], expected, places=3)
        self.assertAlmostEqual(prior.lower_median[1], expected, places=3)
        self.assertEqual(prior.sample_count, 80)

    def test_profile_round_trip_checks_fingerprint(self):
        prior = synthetic_prior()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            save_arm_pose_prior(prior, path)
            loaded = load_arm_pose_prior(path)
        self.assertEqual(loaded, prior)
        self.assertEqual(loaded.fingerprint, prior.fingerprint)


if __name__ == "__main__":
    unittest.main()
