import ast
from pathlib import Path
import unittest


class PoliceGestureVideoOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_dir = Path(__file__).resolve().parents[1] / "app"
        cls.router_tree = ast.parse((app_dir / "routers" / "police_gesture.py").read_text(encoding="utf-8"))
        cls.service_tree = ast.parse((app_dir / "services" / "police_gesture_service.py").read_text(encoding="utf-8"))

    def test_only_video_recognition_route_is_exposed(self):
        post_paths = set()
        for node in ast.walk(self.router_tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "post"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    post_paths.add(decorator.args[0].value)
        self.assertIn("/recognize-video", post_paths)
        self.assertNotIn("/recognize", post_paths)

    def test_service_has_no_single_image_entrypoint(self):
        methods = {
            node.name
            for node in ast.walk(self.service_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("recognize", methods)
        self.assertNotIn("recognize_image", methods)
        self.assertIn("recognize_frame_continuous", methods)


if __name__ == "__main__":
    unittest.main()
