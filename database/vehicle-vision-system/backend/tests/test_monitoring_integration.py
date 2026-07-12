import asyncio
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models.alerts import AlertEvent
from app.models.logs import SystemLog
from app.models.records import OwnerGestureRecord  # noqa: F401
from app.models.user import User  # noqa: F401
from app.routers.monitor import router as monitor_router
from app.services.alert_agent import EVENT_TYPES, alert_agent


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _memory_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_monitoring_defaults_do_not_send_external_notifications():
    defaults = Settings(_env_file=None)
    assert defaults.alert_sse_enabled is True
    assert defaults.alert_email_enabled is False
    assert defaults.alert_webhook_enabled is False


def test_monitoring_public_api_surface_is_registered():
    paths = {route.path for route in monitor_router.routes if hasattr(route, "path")}
    required = {
        "/api/monitor/logs/stats", "/api/monitor/logs/stream",
        "/api/monitor/alerts/analytics", "/api/monitor/alerts/timeline",
        "/api/monitor/alerts/{alert_id}/replay", "/api/monitor/agent/briefing",
        "/api/monitor/assistant", "/api/monitor/notifications/test",
    }
    assert required <= paths


def test_alert_agent_persists_stats_and_replay_without_llm_network():
    db = _memory_session()
    try:
        alert = asyncio.run(alert_agent.trigger_alert(
            db,
            "lpr_consecutive_failure",
            "critical",
            {"count": 5, "module": "lpr"},
            force_template=True,
        ))
        assert alert.id is not None
        assert db.query(AlertEvent).count() == 1
        assert db.query(SystemLog).count() >= 1

        stats = alert_agent.get_stats(db)
        assert stats["total"] == 1
        assert stats["open"] == 1
        assert stats["by_level"]["critical"] == 1

        replay = alert_agent.get_event_replay(db, alert.id)
        assert replay["alert"]["id"] == alert.id
        assert replay["cause_analysis"]["primary_cause"]
        assert "timeline_events" in replay
    finally:
        db.close()


def test_required_anomaly_types_are_available():
    required = {
        "lpr_consecutive_failure", "gesture_low_confidence", "llm_api_timeout",
        "llm_token_exhausted", "unauthorized_access", "database_connection_error",
        "model_load_failure",
    }
    assert required <= EVENT_TYPES.keys()


def test_recognition_routes_use_unified_monitoring_without_replacing_workflows():
    router_dir = BACKEND_DIR / "app" / "routers"
    expected = {
        "lpr.py": "record_lpr_recognition",
        "police_gesture.py": "record_police_recognition",
        "owner_gesture.py": "record_owner_recognition",
        "websocket.py": "_stream_state_signature",
    }
    for filename, marker in expected.items():
        assert marker in (router_dir / filename).read_text(encoding="utf-8")


def test_monitoring_frontend_exposes_structured_alerts_and_chinese_logs():
    html = (BACKEND_DIR / "static" / "index.html").read_text(encoding="utf-8")
    js = (BACKEND_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    js += (BACKEND_DIR / "static" / "js" / "monitoring-workbench.js").read_text(encoding="utf-8")
    assert 'id="assistant-context-bar"' in html
    assert 'id="test-alert-type"' in html
    assert '<option value="警告">警告</option>' in html
    assert "severity_assessment" in js
    assert "focusedAlertId" in js
    assert "display_message || l.message" in js


def test_complete_monitoring_workbench_assets_and_controls_are_present():
    html = (BACKEND_DIR / "static" / "index.html").read_text(encoding="utf-8")
    workbench = (BACKEND_DIR / "static" / "js" / "monitoring-workbench.js").read_text(encoding="utf-8")
    css = (BACKEND_DIR / "static" / "css" / "monitoring-workbench.css").read_text(encoding="utf-8")
    for element_id in (
        "assistant-bot", "agent-activity", "alert-analytics-panel",
        "replay-player", "assistant-context-bar",
    ):
        assert f'id="{element_id}"' in html
    for marker in (
        "initAssistant", "runAgentPatrol", "askAssistant", "startVoiceInput",
        "speakAssistant", "exportLogs", "renderLogStats", "viewReplay",
        "loadAlertAnalytics",
    ):
        assert marker in workbench
    assert ".assistant-panel" in css
    assert ".timeline" in css
    assert ".log-stats" in css


def test_assistant_api_reports_real_llm_or_template_fallback_mode():
    source = (BACKEND_DIR / "app" / "routers" / "monitor.py").read_text(encoding="utf-8")
    service = (BACKEND_DIR / "app" / "services" / "llm_service.py").read_text(encoding="utf-8")
    assert '"ai": {' in source
    assert '"mode": getattr(llm_service, "last_assistant_mode", "template")' in source
    assert 'self.last_assistant_mode = "llm"' in service
    assert 'self.last_assistant_mode = "template"' in service
    assert "await alert_agent.handle_llm_failure(" in service
    assert 'await alert_agent.check_and_alert(db, "llm")' not in service
