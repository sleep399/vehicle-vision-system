"""识别结果监控 —— 日志与告警联动测试"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.database import Base, SessionLocal, engine
from app.models.logs import SystemLog
from app.services.alert_agent import alert_agent
from app.utils.recognition_monitor import (
    record_lpr_recognition,
    record_police_recognition,
    record_owner_recognition,
    record_owner_confirm,
    record_owner_vehicle_state,
)


class RecognitionMonitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        alert_agent._last_alert_time.clear()
        alert_agent._failure_counts.clear()
        alert_agent._confidence_history.clear()

    async def asyncTearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        # 该测试使用项目共享的内存引擎；恢复表结构，避免污染随后执行的认证测试。
        Base.metadata.create_all(bind=engine)

    async def test_lpr_success_writes_info_log(self):
        await record_lpr_recognition(
            self.db, success=True, source="图片上传", plate_count=1,
            plates=[{"plate_number": "京A12345", "confidence": 0.9}],
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "lpr").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.level, "信息")
        self.assertIn("识别成功", log.message)

    async def test_lpr_no_plate_writes_warn_log(self):
        await record_lpr_recognition(
            self.db, success=False, source="图片上传", plate_count=0, plates=[],
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "lpr").first()
        self.assertEqual(log.level, "警告")
        self.assertIn("未识别到有效车牌", log.message)

    async def test_lpr_error_writes_error_log(self):
        await record_lpr_recognition(
            self.db, success=False, source="图片上传", error="decode failed",
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "lpr").first()
        self.assertEqual(log.level, "错误")
        self.assertIn("识别失败", log.message)

    async def test_police_low_confidence_writes_warn_log(self):
        await record_police_recognition(
            self.db, source="图片上传", gesture_cn="停止", confidence=0.2, gesture="stop",
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "police_gesture").first()
        self.assertEqual(log.level, "警告")
        self.assertIn("停止", log.message)

    async def test_police_error_records_failure(self):
        await record_police_recognition(
            self.db, source="WebSocket流", error="model missing",
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "police_gesture").first()
        self.assertEqual(log.level, "错误")
        self.assertGreater(len(list(alert_agent._confidence_history.get("police", []))), 0)

    async def test_owner_action_writes_info_log(self):
        await record_owner_recognition(
            self.db,
            source="图片上传",
            gesture_cn="握拳",
            confidence=0.92,
            gesture="fist",
            action="confirm",
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "owner_gesture").first()
        self.assertEqual(log.level, "信息")
        self.assertIn("手势触发", log.message)

    async def test_owner_no_gesture_writes_warn_log(self):
        await record_owner_recognition(
            self.db, source="WebSocket流", gesture="no_gesture", confidence=0.1,
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "owner_gesture").first()
        self.assertEqual(log.level, "警告")
        self.assertIn("未识别到有效手势", log.message)

    async def test_owner_needs_confirmation_writes_warn_log(self):
        await record_owner_recognition(
            self.db,
            source="WebSocket流",
            gesture_cn="握拳",
            confidence=0.55,
            gesture="fist",
            needs_confirmation=True,
            confirm_prompt="是否确认执行握拳？",
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "owner_gesture").first()
        self.assertEqual(log.level, "警告")
        self.assertIn("待确认", log.message)

    def test_owner_vehicle_state_log(self):
        record_owner_vehicle_state(
            self.db,
            source="手动更新",
            vehicle_state={
                "volume": 60, "temperature": 22, "phone_status": "idle",
                "current_page": "volume_up", "is_awake": 1,
            },
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "owner_gesture").first()
        self.assertIn("车辆状态更新", log.message)
        self.assertIn("已唤醒", log.message)

    def test_owner_confirm_cancel_log(self):
        record_owner_confirm(
            self.db,
            source="二次确认",
            accepted=False,
            pending={"gesture": "fist", "gesture_cn": "握拳", "action": "confirm"},
        )
        log = self.db.query(SystemLog).filter(SystemLog.category == "owner_gesture").first()
        self.assertEqual(log.level, "警告")
        self.assertIn("已取消", log.message)


if __name__ == "__main__":
    unittest.main()
