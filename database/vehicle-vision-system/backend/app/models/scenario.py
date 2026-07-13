from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class ScenarioConflict(Base):
    """多路感知场景冲突记录，与告警事件联动形成处置闭环。"""

    __tablename__ = "scenario_conflicts"

    id = Column(Integer, primary_key=True, index=True)
    scenario_id = Column(String(64), index=True, nullable=False)
    conflict_type = Column(String(64), index=True, nullable=False)
    severity = Column(String(16), index=True, nullable=False)
    status = Column(String(16), default="open", index=True)
    sources_json = Column(Text, nullable=True)
    fusion_recommendation = Column(Text, nullable=False)
    suppress_owner_actions = Column(String(8), default="0")
    alert_id = Column(Integer, nullable=True, index=True)
    resolution_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
