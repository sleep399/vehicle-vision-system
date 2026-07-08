from __future__ import annotations
import json
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models.records import PoliceGestureRecord
from app.schemas import GestureResponse
from app.services.alert_agent import alert_agent
from app.services.police_gesture_service import POLICE_GESTURES, police_gesture_service
from app.utils.auth import get_current_user
from app.utils.logger import write_log
from app.utils.video import process_video_file

router = APIRouter(prefix="/api/police-gesture", tags=["police gesture"])


@router.post("/recognize", response_model=GestureResponse)
async def recognize(file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    content = await file.read()
    try:
        result = police_gesture_service.recognize(content)
    except Exception as exc:
        write_log(db, "police_gesture", f"recognition failed: {exc}", level="ERROR", user_id=user.id if user else None)
        raise HTTPException(500, str(exc))

    alert_agent.record_gesture_confidence("police", result["confidence"])
    await alert_agent.check_and_alert(db, "police")

    save_path = settings.upload_dir / "police" / f"{uuid.uuid4().hex}{Path(file.filename or '.jpg').suffix or '.jpg'}"
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
    write_log(db, "police_gesture", f"recognized: {result['gesture']} ({result['confidence']:.0%})", user_id=user.id if user else None)
    return GestureResponse(**{k: v for k, v in result.items() if k != "gesture_id"}, record_id=record.id)


@router.post("/recognize-video")
async def recognize_video(
    file: UploadFile = File(...),
    interval: int = Query(1, ge=1, le=600),
    max_results: int = Query(300, ge=1, le=300),
    max_sampled_frames: int = Query(900, ge=1, le=5000),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    suffix = Path(file.filename or "").suffix or ".mp4"
    save_path = settings.upload_dir / "police" / f"{uuid.uuid4().hex}{suffix}"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(await file.read())
    try:
        result = process_video_file(police_gesture_service, save_path, interval, max_results, max_sampled_frames)
    except Exception as exc:
        write_log(db, "police_gesture", f"video recognition failed: {exc}", level="ERROR", user_id=user.id if user else None)
        raise HTTPException(500, str(exc))
    write_log(db, "police_gesture", f"video recognized: sampled {result['sampled_frames']}, hits {result['result_count']}", user_id=user.id if user else None)
    return result


@router.get("/gestures")
def gesture_list():
    return [{"id": k, "en": v[0], "cn": v[1]} for k, v in POLICE_GESTURES.items() if k > 0]


@router.get("/pose-backend")
def get_pose_backend():
    return police_gesture_service.pose_backend_info()


@router.put("/pose-backend")
async def set_pose_backend(payload: dict):
    try:
        return police_gesture_service.set_pose_backend(str(payload.get("backend", "")))
    except Exception as exc:
        raise HTTPException(400, str(exc))


@router.get("/history")
def history(skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    records = db.query(PoliceGestureRecord).order_by(PoliceGestureRecord.created_at.desc()).offset(skip).limit(limit).all()
    return [
        {"id": r.id, "gesture": r.gesture, "gesture_cn": r.gesture_cn, "confidence": r.confidence, "annotated_image": r.annotated_image, "created_at": r.created_at.isoformat()}
        for r in records
    ]
