import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.alerts import AlertEvent
from app.models.logs import SystemLog
from app.models.user import User
from app.routers.monitor import router as monitor_router
from app.services.alert_agent import AlertAgent
from app.services.log_stream import broadcast_log, register, unregister
from app.utils.auth import create_access_token


@pytest.fixture()
def isolated_monitor(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.routers.monitor.SessionLocal", session_factory)

    db = session_factory()
    alice = User(username="monitor_alice", hashed_password="unused", is_active=True)
    bob = User(username="monitor_bob", hashed_password="unused", is_active=True)
    db.add_all([alice, bob])
    db.commit()
    db.refresh(alice)
    db.refresh(bob)

    app = FastAPI()
    app.include_router(monitor_router)

    def override_get_db():
        test_db = session_factory()
        try:
            yield test_db
        finally:
            test_db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield {
        "client": client,
        "db": db,
        "alice": alice,
        "bob": bob,
        "alice_token": create_access_token({"sub": alice.username}),
        "bob_token": create_access_token({"sub": bob.username}),
    }
    client.close()
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add_log(db, message: str, user_id: int | None):
    db.add(SystemLog(category="system", level="信息", message=message, user_id=user_id))


def _add_alert(db, title: str, user_id: int | None, event_type: str = "test_event") -> AlertEvent:
    alert = AlertEvent(
        user_id=user_id,
        level="info",
        event_type=event_type,
        title=title,
        summary=title,
        status="open",
    )
    db.add(alert)
    return alert


def test_log_list_and_stats_are_scoped_to_account_or_shared_guests(isolated_monitor):
    ctx = isolated_monitor
    db = ctx["db"]
    _add_log(db, "alice-only", ctx["alice"].id)
    _add_log(db, "bob-only", ctx["bob"].id)
    _add_log(db, "guest-shared", None)
    db.commit()

    alice_logs = ctx["client"].get(
        f"/api/monitor/logs?user_id={ctx['bob'].id}",
        headers=_auth(ctx["alice_token"]),
    )
    assert alice_logs.status_code == 200
    assert [row["message"] for row in alice_logs.json()] == ["alice-only"]

    alice_stats = ctx["client"].get(
        "/api/monitor/logs/stats?hours=24",
        headers=_auth(ctx["alice_token"]),
    )
    assert alice_stats.status_code == 200
    assert alice_stats.json()["total"] == 1

    guest_logs = ctx["client"].get("/api/monitor/logs")
    assert guest_logs.status_code == 200
    assert [row["message"] for row in guest_logs.json()] == ["guest-shared"]


def test_query_token_authenticates_eventsource_style_requests(isolated_monitor):
    ctx = isolated_monitor
    _add_log(ctx["db"], "alice-stream-scope", ctx["alice"].id)
    _add_log(ctx["db"], "guest-stream-scope", None)
    ctx["db"].commit()

    response = ctx["client"].get(
        f"/api/monitor/logs?token={ctx['alice_token']}",
    )
    assert response.status_code == 200
    assert [row["message"] for row in response.json()] == ["alice-stream-scope"]


def test_alert_list_detail_resolve_and_cleanup_cannot_cross_accounts(isolated_monitor):
    ctx = isolated_monitor
    db = ctx["db"]
    alice_alert = _add_alert(db, "alice-alert", ctx["alice"].id)
    bob_alert = _add_alert(db, "bob-alert", ctx["bob"].id)
    guest_alert = _add_alert(db, "guest-alert", None)
    db.commit()
    for alert in (alice_alert, bob_alert, guest_alert):
        db.refresh(alert)

    alice_list = ctx["client"].get(
        "/api/monitor/alerts",
        headers=_auth(ctx["alice_token"]),
    )
    assert alice_list.status_code == 200
    assert [row["id"] for row in alice_list.json()] == [alice_alert.id]

    cross_detail = ctx["client"].get(
        f"/api/monitor/alerts/{alice_alert.id}",
        headers=_auth(ctx["bob_token"]),
    )
    assert cross_detail.status_code == 404

    cross_resolve = ctx["client"].post(
        f"/api/monitor/alerts/{alice_alert.id}/resolve",
        json={"resolution_note": "must-not-change"},
        headers=_auth(ctx["bob_token"]),
    )
    assert cross_resolve.status_code == 404

    cleanup = ctx["client"].post(
        "/api/monitor/alerts/cleanup-noise",
        headers=_auth(ctx["alice_token"]),
    )
    assert cleanup.status_code == 200
    assert cleanup.json()["resolved"] == 1
    db.expire_all()
    assert db.get(AlertEvent, alice_alert.id).status == "resolved"
    assert db.get(AlertEvent, bob_alert.id).status == "open"
    assert db.get(AlertEvent, guest_alert.id).status == "open"


def test_log_stream_broadcasts_only_to_matching_scope():
    alice_queue = asyncio.Queue()
    bob_queue = asyncio.Queue()
    guest_queue = asyncio.Queue()
    register(alice_queue, user_id=11)
    register(bob_queue, user_id=22)
    register(guest_queue, user_id=None)
    try:
        broadcast_log({"message": "alice-live", "user_id": 11})
        assert alice_queue.get_nowait()["message"] == "alice-live"
        assert bob_queue.empty()
        assert guest_queue.empty()

        broadcast_log({"message": "guest-live", "user_id": None})
        assert guest_queue.get_nowait()["message"] == "guest-live"
        assert bob_queue.empty()
    finally:
        unregister(alice_queue)
        unregister(bob_queue)
        unregister(guest_queue)


def test_alert_agent_memory_and_realtime_pushes_are_scoped_per_account():
    agent = AlertAgent()
    agent.record_lpr_result(False, user_id=11)
    agent.record_lpr_result(True, user_id=22)
    agent.record_llm_call(True, tokens_used=100, user_id=11)
    agent.record_llm_call(True, tokens_used=250, user_id=22)

    assert agent.get_perception_snapshot(user_id=11)["lpr"]["recent_failures"] == 1
    assert agent.get_perception_snapshot(user_id=22)["lpr"]["recent_failures"] == 0
    assert agent.get_token_usage(user_id=11)["used"] == 100
    assert agent.get_token_usage(user_id=22)["used"] == 250
    assert agent.get_token_usage(user_id=None)["used"] == 0

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    alice_ws = FakeWebSocket()
    bob_ws = FakeWebSocket()
    guest_ws = FakeWebSocket()
    alice_sse = asyncio.Queue()
    bob_sse = asyncio.Queue()
    guest_sse = asyncio.Queue()
    agent.register_ws(alice_ws, user_id=11)
    agent.register_ws(bob_ws, user_id=22)
    agent.register_ws(guest_ws, user_id=None)
    agent.register_sse(alice_sse, user_id=11)
    agent.register_sse(bob_sse, user_id=22)
    agent.register_sse(guest_sse, user_id=None)

    async def broadcast_alice_alert():
        payload = {"type": "alert", "user_id": 11, "title": "alice-only"}
        await agent.broadcast(payload, user_id=11)
        await agent.broadcast_sse(payload, user_id=11)

    asyncio.run(broadcast_alice_alert())
    assert [item["title"] for item in alice_ws.sent] == ["alice-only"]
    assert bob_ws.sent == []
    assert guest_ws.sent == []
    assert alice_sse.get_nowait()["title"] == "alice-only"
    assert bob_sse.empty()
    assert guest_sse.empty()


def test_monitor_frontend_uses_tokens_and_has_no_user_id_filter():
    backend_dir = Path(__file__).resolve().parents[1]
    html = (backend_dir / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (backend_dir / "static" / "js" / "app.js").read_text(encoding="utf-8")
    workbench = (
        backend_dir / "static" / "js" / "monitoring-workbench.js"
    ).read_text(encoding="utf-8")

    assert 'id="log-user"' not in html
    assert 'app.js?v=20260713-user-scope1' in html
    assert "monitorStreamUrl('/api/monitor/stream')" in workbench
    assert workbench.count("monitorStreamUrl('/api/monitor/logs/stream')") == 2
    assert "/ws/alerts${tokenQuery}" in workbench
    assert "eventStreamUrl('/api/monitor/stream')" in app_js
    assert "eventStreamUrl('/api/monitor/logs/stream')" in app_js
    assert "new EventSource(this.apiUrl" not in app_js
    assert "&user_id=" not in workbench
