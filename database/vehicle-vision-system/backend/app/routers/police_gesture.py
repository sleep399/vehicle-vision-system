import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.records import PoliceGestureRecord
from app.services.police_gesture_service import POLICE_GESTURES, police_gesture_service
from app.utils.auth import get_current_user
from app.utils.recognition_monitor import record_police_recognition
from app.utils.video import process_video_file


def _gesture_from_payload(payload: dict) -> tuple[str, str]:
    gesture_id = payload.get("gesture_id")
    if gesture_id is not None:
        try:
            gesture_id = int(gesture_id)
        except (TypeError, ValueError):
            gesture_id = None
        if gesture_id in POLICE_GESTURES:
            return POLICE_GESTURES[gesture_id]

    gesture = str(payload.get("gesture") or "").strip()
    gesture_cn = str(payload.get("gesture_cn") or "").strip()
    known_by_en = {en: cn for en, cn in POLICE_GESTURES.values()}
    known_by_cn = {cn: en for en, cn in POLICE_GESTURES.values()}
    if gesture in known_by_en:
        return gesture, known_by_en[gesture]
    if gesture_cn in known_by_cn:
        return known_by_cn[gesture_cn], gesture_cn
    return POLICE_GESTURES[0]


def _clamp_confidence(value) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(confidence, 1.0))


router = APIRouter(prefix="/api/police-gesture", tags=["交警手势识别"])


@router.post("/recognize-video", summary="识别交警手势视频")
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
    except Exception as e:
        await record_police_recognition(
            db, source="视频上传", error=str(e),
            user_id=user.id if user else None,
        )
        raise HTTPException(500, str(e))

    best = max(result.get("results", []), key=lambda row: float(row.get("confidence", 0.0)), default=None)
    await record_police_recognition(
        db, source="视频上传",
        gesture_cn=(best or {}).get("gesture_cn", "无手势"),
        confidence=float((best or {}).get("confidence", 0.0)),
        gesture=(best or {}).get("gesture"),
        user_id=user.id if user else None,
        extra={"sampled_frames": result["sampled_frames"], "result_count": result["result_count"]},
    )
    return result


@router.get("/gestures", summary="支持的手势列表")
def gesture_list():
    return [{"id": k, "en": v[0], "cn": v[1]} for k, v in POLICE_GESTURES.items() if k > 0]


@router.get("/pose-backend", summary="获取交警姿态识别后端")
def get_pose_backend():
    return police_gesture_service.pose_backend_info()


@router.put("/pose-backend", summary="切换交警姿态识别后端")
async def set_pose_backend(payload: dict):
    try:
        return police_gesture_service.set_pose_backend(str(payload.get("backend", "")))
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/history", summary="历史记录")
def history(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    q = db.query(PoliceGestureRecord).order_by(PoliceGestureRecord.created_at.desc())
    if user:
        q = q.filter((PoliceGestureRecord.user_id == user.id) | (PoliceGestureRecord.user_id.is_(None)))
    records = q.offset(skip).limit(limit).all()
    return [
        {
            "id": r.id,
            "source_type": r.source_type,
            "gesture": r.gesture,
            "gesture_cn": r.gesture_cn,
            "confidence": r.confidence,
            "keypoints": json.loads(r.keypoints_json) if r.keypoints_json else [],
            "annotated_image": r.annotated_image,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]


@router.post("/history", summary="保存交警手势历史记录")
async def save_history(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    gesture, gesture_cn = _gesture_from_payload(payload)
    source_type = str(payload.get("source_type") or "stream").strip().lower()
    if source_type not in {"image", "video", "camera", "stream"}:
        source_type = "stream"
    keypoints = payload.get("keypoints") or []
    record = PoliceGestureRecord(
        user_id=user.id if user else None,
        source_type=source_type,
        image_path=str(payload.get("source_path") or "") or None,
        gesture=gesture,
        gesture_cn=gesture_cn,
        confidence=_clamp_confidence(payload.get("confidence")),
        keypoints_json=json.dumps(keypoints, ensure_ascii=False),
        annotated_image=payload.get("annotated_image"),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    await record_police_recognition(
        db, source=source_type, gesture_cn=gesture_cn,
        confidence=record.confidence, gesture=gesture,
        user_id=user.id if user else None,
        extra={"record_id": record.id},
    )
    return {
        "saved": True,
        "record_id": record.id,
        "source_type": record.source_type,
        "gesture": record.gesture,
        "gesture_cn": record.gesture_cn,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat(),
    }
