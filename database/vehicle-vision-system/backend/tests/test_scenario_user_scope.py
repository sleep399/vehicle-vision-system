"""Account isolation regression tests for scenario fusion state and conflicts."""

import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.logs import SystemLog
from app.models.scenario import ScenarioConflict
from app.services import scenario_fusion_service as scenario_module
from app.services.scenario_fusion_service import ScenarioFusionService


def _scenario_session():
    engine = create_engine("sqlite:///:memory:")
    ScenarioConflict.__table__.create(engine)
    SystemLog.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _conflict(user_id: int | None, scenario_id: str) -> ScenarioConflict:
    return ScenarioConflict(
        user_id=user_id,
        scenario_id=scenario_id,
        conflict_type="test_conflict",
        severity="warning",
        status="open",
        fusion_recommendation="test recommendation",
    )


def test_snapshots_keep_accounts_isolated_and_guests_shared():
    service = ScenarioFusionService()

    async def collect_signals():
        await service.ingest_lpr(
            None,
            success=True,
            plate_count=1,
            plates=["GUEST"],
            evaluate_conflicts=False,
        )
        await service.ingest_lpr(
            None,
            success=True,
            plate_count=1,
            plates=["USER1"],
            evaluate_conflicts=False,
            user_id=1,
        )
        await service.ingest_lpr(
            None,
            success=True,
            plate_count=1,
            plates=["USER2"],
            evaluate_conflicts=False,
            user_id=2,
        )

    asyncio.run(collect_signals())

    assert service.get_snapshot()["lpr"]["plates"] == ["GUEST"]
    assert service.get_snapshot(user_id=1)["lpr"]["plates"] == ["USER1"]
    assert service.get_snapshot(user_id=2)["lpr"]["plates"] == ["USER2"]
    assert service.for_user(None) is service
    assert service.for_user(1) is service.for_user(1)
    assert service.for_user(1) is not service.for_user(2)


def test_driving_advice_cache_is_scoped_per_account(monkeypatch):
    service = ScenarioFusionService()
    calls: list[tuple[int | None, str | None]] = []

    async def fake_generate(correlated, snapshot, *, force_template=False, user_id=None):
        calls.append((user_id, snapshot["lpr"]["source"]))
        return {
            "advice": f"advice-{user_id}",
            "signals_summary": "test",
            "priority": "normal",
            "mode": "template",
            "sources": {},
        }

    monkeypatch.setattr(scenario_module.llm_service, "generate_driving_advice", fake_generate)

    async def exercise_cache():
        for user_id, source in ((1, "camera-a"), (2, "camera-b")):
            await service.ingest_lpr(
                None,
                success=True,
                plate_count=1,
                plates=["SAME"],
                source=source,
                evaluate_conflicts=False,
                user_id=user_id,
            )
        first_user = await service.get_driving_advice(user_id=1)
        second_user = await service.get_driving_advice(user_id=2)
        first_cached = await service.get_driving_advice(user_id=1)
        second_cached = await service.get_driving_advice(user_id=2)
        return first_user, second_user, first_cached, second_cached

    first_user, second_user, first_cached, second_cached = asyncio.run(exercise_cache())

    assert calls == [(1, "camera-a"), (2, "camera-b")]
    assert first_user["advice"] == "advice-1"
    assert second_user["advice"] == "advice-2"
    assert first_cached["cached"] is True
    assert second_cached["cached"] is True


def test_conflict_queries_and_resolution_use_exact_account_scope():
    db = _scenario_session()
    service = ScenarioFusionService()
    try:
        guest = _conflict(None, "guest")
        user_one = _conflict(1, "user-one")
        user_two = _conflict(2, "user-two")
        db.add_all([guest, user_one, user_two])
        db.commit()

        assert [item["scenario_id"] for item in service.list_conflicts(db)] == ["guest"]
        assert [
            item["scenario_id"]
            for item in service.list_conflicts(db, user_id=1)
        ] == ["user-one"]
        assert [
            item["scenario_id"]
            for item in service.list_conflicts(db, user_id=2)
        ] == ["user-two"]

        assert service.resolve_conflict(db, user_two.id, user_id=1) is None
        db.refresh(user_two)
        assert user_two.status == "open"

        resolved = service.resolve_conflict(db, user_two.id, user_id=2)
        assert resolved is not None
        assert resolved["status"] == "resolved"
        assert resolved["user_id"] == 2
    finally:
        db.close()


def test_conflict_cooldown_alerts_and_logs_are_scoped_per_account(monkeypatch):
    db = _scenario_session()
    service = ScenarioFusionService()
    alert_user_ids: list[int | None] = []
    log_user_ids: list[int | None] = []

    async def fake_monitor(*args, user_id=None, **kwargs):
        alert_user_ids.append(user_id)
        return SimpleNamespace(id=77)

    def fake_log(*args, user_id=None, **kwargs):
        log_user_ids.append(user_id)
        return None

    monkeypatch.setattr(scenario_module.alert_agent, "monitor", fake_monitor)
    monkeypatch.setattr(scenario_module, "write_log", fake_log)
    monkeypatch.setattr(scenario_module, "write_agent_log", fake_log)

    async def create_conflict(user_id: int):
        await service.ingest_police(
            db,
            gesture="stop",
            confidence=0.99,
            user_id=user_id,
        )
        return await service.ingest_owner(
            db,
            action="wake",
            confidence=0.99,
            user_id=user_id,
        )

    async def create_both():
        first = await create_conflict(42)
        second = await create_conflict(43)
        return first, second

    try:
        first, second = asyncio.run(create_both())
        assert first is not None
        assert second is not None
        assert {first.user_id, second.user_id} == {42, 43}
        assert alert_user_ids and set(alert_user_ids) == {42, 43}
        assert log_user_ids.count(42) == 2
        assert log_user_ids.count(43) == 2
        assert service.get_snapshot()["open_conflicts"] == 0
        assert service.get_snapshot(user_id=42)["open_conflicts"] == 1
        assert service.get_snapshot(user_id=43)["open_conflicts"] == 1
    finally:
        db.close()
