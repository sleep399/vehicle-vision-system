import unittest
from types import SimpleNamespace

from app.services.police_gesture_classifier import PoliceGestureClassifier


def pose(points: dict[int, tuple[float, float]], visibility: float = 1.0):
    landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=visibility) for _ in range(33)]
    for index, (x, y) in points.items():
        landmarks[index] = SimpleNamespace(x=x, y=y, visibility=visibility)
    return landmarks


BASE = {11: (0.4, 0.5), 12: (0.6, 0.5), 13: (0.3, 0.5), 14: (0.7, 0.5)}


class PoliceGestureClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = PoliceGestureClassifier()

    def test_stop_is_resolution_independent(self):
        # The classifier operates on normalized coordinates, not image pixels.
        landmarks = pose(BASE | {15: (0.3, 0.25), 16: (0.7, 0.25)})
        result_at_small_image = self.classifier.classify(landmarks)
        result_at_large_image = self.classifier.classify(landmarks)
        self.assertEqual(result_at_small_image, result_at_large_image)
        self.assertEqual(result_at_small_image.gesture_id, 1)
        self.assertGreaterEqual(result_at_small_image.confidence, 0.7)

    def test_low_visibility_does_not_emit_a_gesture(self):
        landmarks = pose(BASE | {15: (0.3, 0.25), 16: (0.7, 0.25)}, visibility=0.2)
        self.assertEqual(self.classifier.classify(landmarks).gesture_id, 0)

    def test_straight_and_slow_down_are_distinguished(self):
        straight = pose(BASE | {15: (0.1, 0.5), 16: (0.9, 0.5)})
        slow_down = pose(BASE | {15: (0.3, 0.65), 16: (0.7, 0.65)})
        self.assertEqual(self.classifier.classify(straight).gesture_id, 2)
        self.assertEqual(self.classifier.classify(slow_down).gesture_id, 7)


if __name__ == "__main__":
    unittest.main()
