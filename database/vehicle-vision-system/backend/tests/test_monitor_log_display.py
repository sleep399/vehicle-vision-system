"""日志展示层单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.utils.log_display import (
    format_log_entry,
    format_record_entry,
    humanize_error_text,
    sanitize_log_message,
)


class LogDisplayTest(unittest.TestCase):
    def test_sanitize_strips_traceback(self):
        msg = "[lpr] 识别失败: x\nTraceback (most recent call last):\n  File \"C:\\path\\site-packages\\foo.py\""
        detail = {"error_type": "ValueError", "error_message": "无法解析图像"}
        text = sanitize_log_message(msg, detail)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("site-packages", text)
        self.assertIn("无法解析图像", text)

    def test_torchscript_error_translated(self):
        msg = "[图片上传] 识别失败: The following operation failed in the TorchScript interpreter."
        text = sanitize_log_message(msg)
        self.assertNotIn("TorchScript interpreter", text)
        self.assertIn("模型推理失败", text)
        self.assertIn("图片上传", text)

    def test_humanize_error_text_pure_english(self):
        text = humanize_error_text("CUDA out of memory. Tried to allocate 2.00 GiB")
        self.assertIn("显存", text)

    def test_format_log_entry_chinese_fields(self):
        row = format_log_entry(
            category="lpr",
            level="ERROR",
            message="识别失败",
            detail={"error_message": "模型未加载"},
            id=1,
        )
        self.assertEqual(row["category_cn"], "车牌识别")
        self.assertEqual(row["level_cn"], "错误")
        self.assertIn("模型未加载", row["display_message"])

    def test_format_record_entry_type_cn(self):
        rec = format_record_entry({"type": "lpr", "id": 23})
        self.assertEqual(rec["type_cn"], "车牌识别")


if __name__ == "__main__":
    unittest.main()
