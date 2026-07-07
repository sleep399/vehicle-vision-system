import uuid
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.records import PoliceGestureRecord
from app.schemas import GestureResponse
from app.services.police_gesture_service import police_gesture_service
from app.services.alert_agent import alert_agent
from app.utils.auth import get_current_user
from app.utils.logger import write_log
from app.config import settings
import json

router = APIRouter(prefix="/api/police-gesture", tags=["交警手势识别"])


@router.post("/recognize", response_model=GestureResponse, summary="识别交警手势")
async def recognize(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    content = await file.read()
    try:
        result = police_gesture_service.recognize(content)
    except Exception as e:
        write_log(db, "police_gesture", f"识别失败: {e}", level="ERROR", user_id=user.id if user else None)
        raise HTTPException(500, str(e))

    alert_agent.record_gesture_confidence("police", result["confidence"])
    await alert_agent.check_and_alert(db, "police")

    save_path = settings.upload_dir / "police" / f"{uuid.uuid4().hex}.jpg"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)

    record = PoliceGestureRecord(
        user_id=user.id if user else None,
        source_type="image",
        image_path=str(save_path),
        gesture=result["gesture"],
        gesture_cn=result["gesture_cn"],
        confidence=result["confidence"],
        keypoints_json=json.dumps(result["keypoints"], ensure_ascii=False),
        annotated_image=result["annotated_image"],
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    write_log(db, "police_gesture", f"识别手势: {result['gesture_cn']} ({result['confidence']:.0%})", user_id=user.id if user else None)
    return GestureResponse(**{k: v for k, v in result.items() if k != "gesture_id"}, record_id=record.id)


@router.get("/gestures", summary="支持的手势列表")
def gesture_list():
    from app.services.police_gesture_service import POLICE_GESTURES
    return [{"id": k, "en": v[0], "cn": v[1]} for k, v in POLICE_GESTURES.items() if k > 0]


@router.get("/history", summary="历史记录")
def history(skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    records = db.query(PoliceGestureRecord).order_by(PoliceGestureRecord.created_at.desc()).offset(skip).limit(limit).all()
    return [
        {
            "id": r.id,
            "gesture": r.gesture,
            "gesture_cn": r.gesture_cn,
            "confidence": r.confidence,
            "annotated_image": r.annotated_image,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]
