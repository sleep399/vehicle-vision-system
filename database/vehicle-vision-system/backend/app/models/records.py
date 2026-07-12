from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Float, Text
from app.database import Base


class LicensePlateRecord(Base):
    __tablename__ = "license_plate_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    source_type = Column(String(16), default="image")
    image_path = Column(String(512))
    annotated_image = Column(Text, nullable=True)
    plates_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PoliceGestureRecord(Base):
    __tablename__ = "police_gesture_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    source_type = Column(String(16), default="image")
    image_path = Column(String(512), nullable=True)
    gesture = Column(String(32), nullable=False)
    gesture_cn = Column(String(32), nullable=False)
    confidence = Column(Float, default=0.0)
    keypoints_json = Column(Text, nullable=True)
    annotated_image = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class OwnerGestureRecord(Base):
    __tablename__ = "owner_gesture_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    source_type = Column(String(16), default="image")
    image_path = Column(String(512), nullable=True)
    gesture = Column(String(32), nullable=False)
    gesture_cn = Column(String(32), nullable=False)
    confidence = Column(Float, default=0.0)
    action = Column(String(64), nullable=True)
    keypoints_json = Column(Text, nullable=True)
    annotated_image = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class VehicleState(Base):
    __tablename__ = "vehicle_state"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    volume = Column(Integer, default=50)
    temperature = Column(Integer, default=24)
    phone_status = Column(String(16), default="idle")
    current_page = Column(String(32), default="standby")
    is_awake = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)
