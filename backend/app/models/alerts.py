from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(16), index=True)
    event_type = Column(String(64), index=True)
    title = Column(String(256), nullable=False)
    summary = Column(Text, nullable=False)
    detail_json = Column(Text, nullable=True)
    root_cause = Column(Text, nullable=True)
    suggestion = Column(Text, nullable=True)
    channels_sent = Column(String(128), default="web")
    status = Column(String(16), default="open")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
