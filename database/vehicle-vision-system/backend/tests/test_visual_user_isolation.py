from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.records import (
    LicensePlateRecord,
    OwnerGestureRecord,
    PoliceGestureRecord,
)
from app.routers.lpr import history as lpr_history
from app.routers.lpr import lpr_stats
from app.routers.owner_gesture import history as owner_history
from app.routers.police_gesture import history as police_history
from app.utils.crypto import encrypt_json


@pytest.fixture
def visual_db():
    engine = create_engine("sqlite:///:memory:")
    for model in (LicensePlateRecord, PoliceGestureRecord, OwnerGestureRecord):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_visual_records(db) -> None:
    for user_id, plate in ((101, "京A10101"), (202, "京A20202"), (None, "京A00000")):
        db.add(LicensePlateRecord(
            user_id=user_id,
            source_type="image",
            plates_json=encrypt_json({"plates": [{"plate_number": plate}]}),
        ))
        db.add(PoliceGestureRecord(
            user_id=user_id,
            source_type="stream",
            gesture="stop",
            gesture_cn=f"交警-{user_id}",
            confidence=0.9,
        ))
        db.add(OwnerGestureRecord(
            user_id=user_id,
            source_type="stream",
            gesture="fist",
            gesture_cn=f"车主-{user_id}",
            confidence=0.9,
        ))
    db.commit()


def test_visual_histories_and_lpr_stats_are_scoped_to_exact_user(visual_db):
    _seed_visual_records(visual_db)
    user = SimpleNamespace(id=101)

    lpr_items = lpr_history(db=visual_db, user=user)
    police_items = police_history(db=visual_db, user=user)
    owner_items = owner_history(db=visual_db, user=user)
    stats = lpr_stats(db=visual_db, user=user)

    assert [item["plates"][0]["plate_number"] for item in lpr_items] == ["京A10101"]
    assert [item["gesture_cn"] for item in police_items] == ["交警-101"]
    assert [item["gesture_cn"] for item in owner_items] == ["车主-101"]
    assert stats["total"] == 1
    assert stats["recent"][0]["plates"][0]["plate_number"] == "京A10101"


def test_visual_guest_histories_share_only_null_user_records(visual_db):
    _seed_visual_records(visual_db)

    lpr_items = lpr_history(db=visual_db, user=None)
    police_items = police_history(db=visual_db, user=None)
    owner_items = owner_history(db=visual_db, user=None)
    stats = lpr_stats(db=visual_db, user=None)

    assert [item["plates"][0]["plate_number"] for item in lpr_items] == ["京A00000"]
    assert [item["gesture_cn"] for item in police_items] == ["交警-None"]
    assert [item["gesture_cn"] for item in owner_items] == ["车主-None"]
    assert stats["total"] == 1
