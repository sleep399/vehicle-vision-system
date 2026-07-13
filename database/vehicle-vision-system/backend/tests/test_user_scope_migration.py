from sqlalchemy import create_engine, inspect, text

from app import database as database_module
from app.models.alerts import AlertEvent
from app.models.scenario import ScenarioConflict


def _create_legacy_table_without_user_id(engine, model) -> None:
    definitions = []
    for column in model.__table__.columns:
        if column.name == "user_id":
            continue
        type_sql = column.type.compile(dialect=engine.dialect)
        primary_key = " PRIMARY KEY" if column.primary_key else ""
        definitions.append(f'"{column.name}" {type_sql}{primary_key}')
    statement = f'CREATE TABLE "{model.__tablename__}" ({", ".join(definitions)})'
    with engine.begin() as connection:
        connection.execute(text(statement))


def test_init_db_upgrades_legacy_user_scope_columns_and_indexes(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    _create_legacy_table_without_user_id(engine, AlertEvent)
    _create_legacy_table_without_user_id(engine, ScenarioConflict)
    monkeypatch.setattr(database_module, "engine", engine)

    database_module.init_db()

    inspector = inspect(engine)
    assert "user_id" in {column["name"] for column in inspector.get_columns("alert_events")}
    assert "user_id" in {column["name"] for column in inspector.get_columns("scenario_conflicts")}
    alert_indexes = {index["name"] for index in inspector.get_indexes("alert_events")}
    scenario_indexes = {index["name"] for index in inspector.get_indexes("scenario_conflicts")}
    assert "ix_alert_events_user_created" in alert_indexes
    assert "ix_alert_events_user_status_created" in alert_indexes
    assert "ix_scenario_conflicts_user_created" in scenario_indexes
    engine.dispose()
