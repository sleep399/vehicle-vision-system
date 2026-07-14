import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train.train_yolo_pose_gesture_model import (
    augment_arm_occlusion,
    calculate_class_weights,
    checkpoint_uses_pose_repair,
    delayed_labels,
    evaluate_model,
    fit_prior_from_raw_cache,
    load_cache,
    materialize_repaired_cache,
    WindowDataset,
)
from app.services.ctpgr_pose_adapter import ArmPosePrior, coco_to_ctpgr, repair_coco_pose


class YoloLstmTrainingUtilsTests(unittest.TestCase):
    def test_zero_delay_keeps_labels_without_aliasing(self):
        labels = np.array([0, 1, 2], dtype=np.int64)
        shifted = delayed_labels(labels, 0)
        np.testing.assert_array_equal(shifted, labels)
        self.assertIsNot(shifted, labels)

    def test_positive_delay_pads_with_no_gesture(self):
        labels = np.array([1, 2, 3, 4], dtype=np.int64)
        np.testing.assert_array_equal(delayed_labels(labels, 2), [0, 0, 1, 2])

    def test_negative_delay_is_rejected(self):
        with self.assertRaises(ValueError):
            delayed_labels(np.array([1]), -1)

    def test_zero_power_produces_uniform_weights(self):
        counts = np.arange(1, 10)
        np.testing.assert_allclose(calculate_class_weights(counts, power=0), np.ones(9))

    def test_no_gesture_multiplier_increases_relative_weight(self):
        counts = np.full(9, 100)
        weights = calculate_class_weights(counts, power=0.5, no_gesture_multiplier=2)
        self.assertGreater(weights[0], weights[1])
        self.assertAlmostEqual(float(weights.mean()), 1.0, places=6)

    def test_legacy_checkpoint_metadata_does_not_select_repaired_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "legacy.pt"
            checkpoint.touch()
            checkpoint.with_suffix(".pt.meta.json").write_text(
                '{"pose_repair_version": null}',
                encoding="utf-8",
            )
            self.assertFalse(checkpoint_uses_pose_repair(checkpoint))
            checkpoint.with_suffix(".pt.meta.json").write_text(
                '{"pose_repair_version": "arm-confidence-v1"}',
                encoding="utf-8",
            )
            self.assertTrue(checkpoint_uses_pose_repair(checkpoint))

    def test_arm_occlusion_augmentation_holds_distal_joint_relative_to_shoulder(self):
        coordinates = np.zeros((20, 2, 14), dtype=np.float32)
        coordinates[:, 0, 0] = np.arange(20) / 100
        coordinates[:, 0, 1] = coordinates[:, 0, 0] + 0.1
        coordinates[:, 0, 2] = coordinates[:, 0, 0] + np.arange(20) / 50 + 0.2
        coordinates[:, 0, 3] = np.arange(20) / 120
        coordinates[:, 0, 4] = coordinates[:, 0, 3] + 0.1
        coordinates[:, 0, 5] = coordinates[:, 0, 3] + np.arange(20) / 60 + 0.2
        augmented = augment_arm_occlusion(coordinates, np.random.default_rng(7), probability=1.0)
        self.assertFalse(np.array_equal(augmented, coordinates))
        changed_right = not np.array_equal(augmented[:, :, 0:3], coordinates[:, :, 0:3])
        changed_left = not np.array_equal(augmented[:, :, 3:6], coordinates[:, :, 3:6])
        self.assertNotEqual(changed_right, changed_left)

    def test_repaired_cache_uses_the_same_shared_pose_repair(self):
        pose = np.full((17, 2), 256.0, dtype=np.float32)
        pose[5], pose[6] = (306, 200), (206, 200)
        pose[7], pose[9] = (266, 250), (226, 300)
        pose[8], pose[10] = (246, 250), (286, 300)
        confidence = np.full(17, 0.9, dtype=np.float32)
        corrupted = pose.copy()
        corrupted[9] = (0, 0)
        corrupted_confidence = confidence.copy()
        corrupted_confidence[9] = 0.0
        prior = ArmPosePrior(
            shoulder_width_median=100 / 512,
            upper_median=(0.64, 0.64),
            upper_low=(0.4, 0.4),
            upper_high=(1.0, 1.0),
            lower_median=(0.64, 0.64),
            lower_low=(0.4, 0.4),
            lower_high=(1.0, 1.0),
            source="training consistency test",
        )
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.npz"
            output_path = Path(directory) / "repaired.npz"
            np.savez_compressed(
                raw_path,
                coco_xy=np.stack([pose, corrupted]),
                keypoint_confidence=np.stack([confidence, corrupted_confidence]),
                labels=np.array([0, 4]),
                valid=np.array([True, True]),
            )
            materialize_repaired_cache(raw_path, output_path, prior, hold_frames=5)
            with np.load(output_path) as data:
                cached = data["coord_norm"].copy()

        state = {}
        first = repair_coco_pose(pose, confidence, state=state, prior=prior)
        second = repair_coco_pose(corrupted, corrupted_confidence, state=state, prior=prior)
        expected = np.concatenate(
            [coco_to_ctpgr(first.coordinates), coco_to_ctpgr(second.coordinates)],
            axis=0,
        )
        np.testing.assert_array_equal(cached, expected)

    def test_repaired_cache_resets_label_delay_at_sequence_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_root = Path(directory)
            split_root = cache_root / "train"
            split_root.mkdir()
            coordinates = np.zeros((40, 2, 14), dtype=np.float32)
            labels = np.concatenate([np.ones(20, dtype=np.int64), np.full(20, 2, dtype=np.int64)])
            sequence_reset = np.zeros(40, dtype=np.bool_)
            sequence_reset[20] = True
            np.savez_compressed(
                split_root / "sample.npz",
                coord_norm=coordinates,
                labels=labels,
                valid=np.ones(40, dtype=np.bool_),
                sequence_reset=sequence_reset,
                repair_version=np.asarray("arm-confidence-v1"),
                repair_cache_revision=np.asarray("2"),
                profile_fingerprint=np.asarray("profile"),
                pose_model_sha256=np.asarray("pose"),
                hold_frames=np.asarray(5),
            )
            items = load_cache("train", label_delay=3, cache_root=cache_root)
        self.assertEqual(len(items), 2)
        np.testing.assert_array_equal(items[0]["labels"][:4], [0, 0, 0, 1])
        np.testing.assert_array_equal(items[1]["labels"][:4], [0, 0, 0, 2])
        self.assertEqual({item["source_video"] for item in items}, {"sample"})

    def test_repaired_cache_applies_label_delay_before_removing_invalid_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_root = Path(directory)
            split_root = cache_root / "train"
            split_root.mkdir()
            np.savez_compressed(
                split_root / "sample.npz",
                coord_norm=np.zeros((8, 2, 14), dtype=np.float32),
                labels=np.ones(8, dtype=np.int64),
                valid=np.array([True, False, False, False, True, True, True, True]),
                sequence_reset=np.zeros(8, dtype=np.bool_),
                repair_version=np.asarray("arm-confidence-v1"),
                repair_cache_revision=np.asarray("3"),
                profile_fingerprint=np.asarray("profile"),
                pose_model_sha256=np.asarray("pose"),
                hold_frames=np.asarray(5),
            )
            item = load_cache("train", label_delay=2, cache_root=cache_root)[0]
        np.testing.assert_array_equal(item["timeline_labels"], [0, 0, 1, 1, 1, 1, 1, 1])
        np.testing.assert_array_equal(item["timeline_step_indices"], [0, 4, 5, 6, 7])
        np.testing.assert_array_equal(item["labels"], [0, 1, 1, 1, 1])

    def test_repaired_cache_evaluation_counts_invalid_frames_as_no_gesture(self):
        class AlwaysGestureOne:
            num_hidden = 4

            def eval(self):
                return self

            def __call__(self, features, h, c):
                logits = torch.full((features.shape[0], 9), -10.0, device=features.device)
                logits[:, 1] = 10.0
                return None, h, c, logits

        video = {
            "name": "timeline-test",
            "features": np.zeros((2, 25), dtype=np.float32),
            "labels": np.ones(2, dtype=np.int64),
            "valid": np.ones(2, dtype=np.bool_),
            "timeline_labels": np.ones(3, dtype=np.int64),
            "timeline_step_indices": np.array([0, 2], dtype=np.int64),
            "evaluation_mask": np.ones(3, dtype=np.bool_),
        }
        report = evaluate_model(AlwaysGestureOne(), [video], torch.device("cpu"))
        self.assertEqual(report["per_class"][1]["support"], 3)
        self.assertAlmostEqual(report["per_class"][1]["recall"], 2 / 3)
        self.assertEqual(report["confusion_matrix"][1][0], 1)

    def test_window_dataset_skips_short_segments_and_keeps_fixed_shape(self):
        short = {
            "labels": np.zeros(4, dtype=np.int64),
            "features": np.zeros((4, 25), dtype=np.float32),
            "coords": np.zeros((4, 2, 14), dtype=np.float32),
            "valid": np.ones(4, dtype=np.bool_),
        }
        long = {
            "labels": np.zeros(10, dtype=np.int64),
            "features": np.zeros((10, 25), dtype=np.float32),
            "coords": np.zeros((10, 2, 14), dtype=np.float32),
            "valid": np.ones(10, dtype=np.bool_),
        }
        dataset = WindowDataset([short, long], clip_len=8, stride=4)
        features, labels, valid = dataset[0]
        self.assertEqual(tuple(features.shape), (8, 25))
        self.assertEqual(tuple(labels.shape), (8,))
        self.assertEqual(tuple(valid.shape), (8,))

    def test_pose_prior_deltas_do_not_cross_missing_frame_or_track_reset(self):
        frame_count = 120
        pose = np.full((17, 2), 256.0, dtype=np.float32)
        pose[5], pose[6] = (306, 200), (206, 200)
        pose[7], pose[9] = (266, 250), (226, 300)
        pose[8] = (246, 250)
        coordinates = np.repeat(pose[None], frame_count, axis=0)
        radius = 64.0
        for index in range(frame_count):
            angle = 0.01 * index if index < 60 else 2.0 + 0.01 * (index - 61)
            coordinates[index, 10] = coordinates[index, 8] + radius * np.array(
                [np.cos(angle), np.sin(angle)], dtype=np.float32
            )
        confidence = np.full((frame_count, 17), 0.9, dtype=np.float32)
        confidence[60] = 0.0
        sequence_reset = np.zeros(frame_count, dtype=np.bool_)
        sequence_reset[61] = True
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory) / "raw"
            train_root = raw_root / "train"
            train_root.mkdir(parents=True)
            np.savez_compressed(
                train_root / "sample.npz",
                coco_xy=coordinates,
                keypoint_confidence=confidence,
                valid=np.ones(frame_count, dtype=np.bool_),
                sequence_reset=sequence_reset,
            )
            prior = fit_prior_from_raw_cache(raw_root, Path(directory) / "stats.json", "pose-hash")
        # The fitter keeps a 25-degree floor so legitimate fast arm motion is
        # not rejected; this still proves the roughly 2-radian gap/reset jump
        # was not included in the temporal distribution.
        self.assertLess(prior.direction_delta_high[0], np.deg2rad(30))


if __name__ == "__main__":
    unittest.main()
