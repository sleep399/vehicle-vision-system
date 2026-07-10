import json
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.logs import SystemLog


def write_log(
    db: Session,
    category: str,
    message: str,
    level: str = "INFO",
    detail: dict | None = None,
    user_id: int | None = None,
):
    log = SystemLog(
        category=category,
        level=level,
        message=message,
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
        user_id=user_id,
        created_at=datetime.utcnow(),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
