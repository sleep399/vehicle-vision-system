import json
import logging
import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.records import LicensePlateRecord
from app.schemas import LPRResponse
from app.services.lpr_service import lpr_service
from app.services.lpr_video_service import lpr_video_service
from app.utils.auth import get_current_user
from app.utils.crypto import encrypt_json, decrypt_json
from app.utils.recognition_monitor import record_lpr_recognition
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lpr", tags=["车牌识别"])

PLATE_RE = re.compile(r"^[\u4e00-\u9fa5A-Z]{1}[A-Z][A-Z0-9]{5}$")


@router.post("/recognize", response_model=LPRResponse, summary="上传图片识别车牌")
async def recognize_image(
    file: UploadFile = File(...),
    mode: str = Query("ccpd", pattern="^(ccpd|lprnet)$"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    content = await file.read()
    filename = file.filename or ""
    force_model = mode == "lprnet"
    try:
        result = lpr_service.recognize(content, filename, force_model=force_model)
    except Exception as e:
        await record_lpr_recognition(
            db, success=False, source="图片上传", error=str(e),
            user_id=user.id if user else None,
        )
        raise HTTPException(500, str(e))

    await record_lpr_recognition(
        db, success=result["success"], source="图片上传",
        plate_count=result["plate_count"], plates=result["plates"],
        model_available=result.get("model_available"),
        user_id=user.id if user else None,
    )

    save_name = f"{uuid.uuid4().hex}.jpg"
    save_path = settings.upload_dir / "lpr" / save_name
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)

    encrypted_plates = encrypt_json({"plates": result["plates"]})
    record = LicensePlateRecord(
        user_id=user.id if user else None,
        source_type="image",
        image_path=str(save_path),
        annotated_image=result["annotated_image"],
        plates_json=encrypted_plates,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return LPRResponse(**result, record_id=record.id)


@router.post("/recognize-video", summary="上传视频识别车牌")
async def recognize_video(
    file: UploadFile = File(...),
    interval: int = Query(1, ge=1, le=60),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    content = await file.read()
    save_path = settings.upload_dir / "lpr" / f"{uuid.uuid4().hex}.mp4"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)
    logger.info("[LPR-API] recognize-video saved=%s bytes=%s interval=%s", save_path, len(content), interval)

    try:
        summary = lpr_video_service.process_video(save_path, sample_interval=interval)
        logger.info("[LPR-API] recognize-video summary frame_count=%s total_frames=%s annotated=%s", summary.get('frame_count'), summary.get('total_frames'), summary.get('annotated_video_path'))
    except Exception as e:
        logger.exception("[LPR-API] recognize-video failed")
        await record_lpr_recognition(
            db, success=False, source="视频上传", error=str(e),
            user_id=user.id if user else None,
        )
        raise HTTPException(500, str(e))

    best = summary.get("best")
    record_id = None
    fused_map: dict[tuple[str, str], dict] = {}
    total_candidates = 0
    for item in summary.get("results", []):
        frame_index = item.get("frame_index")
        for p in item.get("plates", []):
            total_candidates += 1
            plate_number = (p.get("plate_number") or "").strip()
            confidence = float(p.get("confidence", 0.0))
            valid_format = bool(plate_number and PLATE_RE.match(plate_number))
            valid_conf = confidence >= 0.35
            logger.info(
                "[LPR-API] plate candidate frame=%s plate=%s conf=%.3f format=%s conf_ok=%s",
                frame_index, plate_number or "<empty>", confidence, valid_format, valid_conf,
            )
            if not valid_format or not valid_conf:
                continue
            key = (plate_number, p.get("plate_color", "蓝牌"))
            agg = fused_map.setdefault(key, {
                "plate_number": plate_number,
                "plate_color": p.get("plate_color", "蓝牌"),
                "confidence_sum": 0.0,
                "hit_count": 0,
                "max_confidence": 0.0,
                "frames": [],
                "source": "yolo_lprnet",
            })
            agg["confidence_sum"] += confidence
            agg["hit_count"] += 1
            agg["max_confidence"] = max(agg["max_confidence"], confidence)
            agg["frames"].append(frame_index)
    video_records = []
    for agg in fused_map.values():
        video_records.append({
            "plate_number": agg["plate_number"],
            "plate_color": agg["plate_color"],
            "confidence": round((agg["confidence_sum"] / max(agg["hit_count"], 1)), 3),
            "max_confidence": round(agg["max_confidence"], 3),
            "hit_count": agg["hit_count"],
            "frames": sorted(set(agg["frames"])),
            "frame_index": agg["frames"][0] if agg["frames"] else None,
            "source": "yolo_lprnet",
        })
    logger.info("[LPR-API] video candidates=%s valid_records=%s", total_candidates, len(video_records))
    video_records.sort(key=lambda x: (x.get("hit_count", 0), x.get("max_confidence", 0)), reverse=True)
    if not video_records:
        video_records = [{"plate_number": "未识别", "plate_color": "无", "confidence": 0.0, "max_confidence": 0.0, "hit_count": 0, "frames": [], "frame_index": None, "source": "yolo_lprnet"}]
    encrypted_plates = encrypt_json({"plates": video_records})
    record = LicensePlateRecord(
        user_id=user.id if user else None,
        source_type="video",
        image_path=str(save_path),
        annotated_image=(summary.get("best", {}) or {}).get("annotated_image"),
        plates_json=encrypted_plates,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    record_id = record.id
    logger.info("[LPR-API] video record saved id=%s plates=%s", record_id, len(video_records))
    logger.info("[LPR-API] video plates=%s", ", ".join(
        f"{x['plate_number']}@F{x['frame_index']} hit={x['hit_count']} avg={x['confidence']:.2f} max={x['max_confidence']:.2f}"
        for x in video_records[:10]
    ))

    valid_video_records = [p for p in video_records if (p.get("plate_number") or "") not in ("", "未识别")]
    await record_lpr_recognition(
        db, success=bool(valid_video_records), source="视频上传",
        plate_count=len(valid_video_records), plates=video_records,
        model_available=lpr_video_service.model_available(),
        user_id=user.id if user else None,
        extra={"record_id": record_id, "sampled_frames": summary["frame_count"], "total_frames": summary["total_frames"]},
    )
    annotated_video_path = summary.get("annotated_video_path")
    annotated_video_url = None
    if annotated_video_path:
        annotated_video_url = "/uploads/lpr/" + Path(annotated_video_path).name
    return {
        "frame_count": summary["frame_count"],
        "total_frames": summary["total_frames"],
        "results": summary["results"],
        "best": best,
        "record_id": record_id,
        "model_available": lpr_video_service.model_available(),
        "source": "yolo_lprnet",
        "annotated_video_path": annotated_video_path,
        "annotated_video_url": annotated_video_url,
    }


@router.get("/preview.mjpg", summary="获取实时预览流")
def preview_mjpg() -> Response:
    return Response(content=b"", media_type="multipart/x-mixed-replace; boundary=frame")


@router.post("/rtsp/start", summary="启动沙盘 RTSP 车牌识别")
async def recognize_rtsp_stream(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rtsp_url = (payload.get("rtsp_url") or "").strip()
    if not rtsp_url:
        raise HTTPException(400, "rtsp_url 不能为空")
    source_name = (payload.get("source_name") or payload.get("source") or "live1").strip()
    label = (payload.get("label") or "").strip()
    status = lpr_video_service.model_status()
    if not status.get("model_available"):
        await record_lpr_recognition(
            db, success=False, source="RTSP/视频流", model_available=False,
            error=status.get("message") or "YOLO+LPRNet 模型未加载",
            user_id=user.id if user else None,
        )
        raise HTTPException(500, status.get("message") or "YOLO+LPRNet 模型未加载")

    try:
        parsed = urlparse(rtsp_url)
        local_path = None
        if parsed.scheme.lower() == 'file':
            local_path = Path(unquote(parsed.path.lstrip('/')))
        elif rtsp_url.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.m4v')):
            local_path = Path(rtsp_url)
        if local_path is not None:
            summary = lpr_video_service.start_video_file_stream(local_path, source_name=source_name)
        else:
            summary = lpr_video_service.start_rtsp_stream(rtsp_url=rtsp_url, source_name=source_name, label=label)
    except Exception as e:
        logger.exception("[LPR-API] rtsp start failed")
        await record_lpr_recognition(
            db, success=False, source="RTSP/视频流", error=str(e),
            user_id=user.id if user else None,
            extra={"rtsp_url": rtsp_url, "source_name": source_name, "action": "start"},
        )
        raise HTTPException(500, str(e))

    await record_lpr_recognition(
        db, success=True, source="RTSP/视频流", model_available=True,
        user_id=user.id if user else None,
        extra={"rtsp_url": rtsp_url, "source_name": source_name, "label": label, "action": "start"},
    )
    return summary


@router.get("/preview/{source_name}.mjpg", summary="车牌识别预览流")
def preview_stream(source_name: str):
    return StreamingResponse(
        lpr_video_service.preview_frame_generator(source_name),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/preview/{source_name}/status", summary="车牌识别预览状态")
def preview_status(source_name: str):
    return lpr_video_service.preview_status(source_name)


@router.post("/rtsp/stop", summary="停止沙盘 RTSP 车牌识别")
async def stop_rtsp_stream(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rtsp_url = (payload.get("rtsp_url") or "").strip()
    source_name = (payload.get("source_name") or payload.get("source") or "live1").strip()
    if not rtsp_url and not source_name:
        raise HTTPException(400, "rtsp_url 或 source_name 不能为空")
    result = lpr_video_service.stop_rtsp_stream(rtsp_url=rtsp_url, source_name=source_name)
    history = result.get("history") or {"plates": [{"plate_number": "未识别", "plate_color": "无", "confidence": 0.0, "max_confidence": 0.0, "hit_count": 0, "frames": [], "frame_index": None, "source": "yolo_lprnet"}]}
    encrypted_plates = encrypt_json({"plates": history.get("plates", [])})
    record = LicensePlateRecord(
        user_id=user.id if user else None,
        source_type="rtsp",
        image_path=None,
        annotated_image=None,
        plates_json=encrypted_plates,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    plates = history.get("plates", [])
    valid_plates = [p for p in plates if (p.get("plate_number") or "") not in ("", "未识别")]
    await record_lpr_recognition(
        db, success=bool(valid_plates), source="RTSP/视频流",
        plate_count=len(valid_plates), plates=plates,
        user_id=user.id if user else None,
        extra={"record_id": record.id, "source_name": source_name, "action": "stop"},
    )
    return {**result, "record_id": record.id}


@router.get("/model-status", summary="图片识别模型状态（RPNet/CCPD GT）")
def model_status():
    return {
        "model_available": lpr_service.model_available(),
        "engine": "image",
        "message": "RPNet 已就绪" if lpr_service.model_available() else "请将 fh02.pth 放到 backend/app/models/ 目录",
    }


@router.get("/video-model-status", summary="视频识别模型状态（YOLO+LPRNet）")
def video_model_status():
    return lpr_video_service.model_status()


@router.post("/video-history", summary="独立保存视频识别历史")
async def save_video_history(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    plates = payload.get("plates") or []
    source_path = payload.get("source_path") or ""
    annotated_image = payload.get("annotated_image")
    valid = []
    seen = set()
    for p in plates:
        plate_number = (p.get("plate_number") or "").strip()
        confidence = float(p.get("confidence", 0.0))
        if not plate_number or not PLATE_RE.match(plate_number) or confidence < 0.65:
            continue
        key = (plate_number, p.get("plate_color", "蓝牌"))
        if key in seen:
            continue
        seen.add(key)
        valid.append({
            "plate_number": plate_number,
            "plate_color": p.get("plate_color", "蓝牌"),
            "confidence": confidence,
            "frame_index": p.get("frame_index"),
            "source": p.get("source", "yolo_lprnet"),
        })
    logger.info("[LPR-API] video-history request plates=%s valid=%s source=%s", len(plates), len(valid), source_path)
    if not valid:
        return {"saved": False, "record_id": None, "message": "没有符合条件的车牌"}
    encrypted_plates = encrypt_json({"plates": valid})
    record = LicensePlateRecord(
        user_id=user.id if user else None,
        source_type="video",
        image_path=source_path or str(settings.upload_dir / "lpr" / f"{uuid.uuid4().hex}.mp4"),
        annotated_image=annotated_image,
        plates_json=encrypted_plates,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info("[LPR-API] video-history saved id=%s plates=%s", record.id, len(valid))
    return {"saved": True, "record_id": record.id, "plate_count": len(valid)}


@router.get("/history", summary="历史识别记录")
def history(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    q = db.query(LicensePlateRecord).order_by(LicensePlateRecord.created_at.desc())
    if user:
        q = q.filter((LicensePlateRecord.user_id == user.id) | (LicensePlateRecord.user_id.is_(None)))
    records = q.offset(skip).limit(limit).all()
    items = []
    for r in records:
        plates = decrypt_json(r.plates_json).get("plates", []) if r.plates_json else []
        items.append({
            "id": r.id,
            "source_type": r.source_type,
            "plate_count": len(plates),
            "plates": plates,
            "annotated_image": r.annotated_image,
            "created_at": r.created_at.isoformat(),
        })
    return items


@router.get("/stats", summary="车牌识别统计")
def lpr_stats(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    q = db.query(LicensePlateRecord)
    if user:
        q = q.filter(
            (LicensePlateRecord.user_id == user.id) | (LicensePlateRecord.user_id.is_(None))
        )
    total = q.count()
    recent = (
        q.order_by(LicensePlateRecord.created_at.desc())
        .limit(5)
        .all()
    )
    items = []
    for r in recent:
        plates = decrypt_json(r.plates_json).get("plates", []) if r.plates_json else []
        items.append({
            "id": r.id,
            "plate_count": len(plates),
            "plates": plates,
            "source_type": r.source_type,
            "created_at": r.created_at.isoformat(),
        })
    return {"total": total, "recent": items}


@router.get("/ccpd-sample", summary="从 CCPD 数据集获取样本图片路径")
def ccpd_sample(db: Session = Depends(get_db)):
    ccpd_path = (settings.base_dir / settings.ccpd_data_path).resolve()
    split_file = ccpd_path / "split" / "test.txt"
    if not split_file.exists():
        return {"samples": [], "message": "CCPD split 文件存在，请将图片数据放置于 CCPD 目录下对应子文件夹"}
    lines = split_file.read_text(encoding="utf-8").strip().split("\n")[:20]
    samples = []
    for line in lines:
        rel = line.strip()
        if not rel:
            continue
        img_path = ccpd_path / rel
        samples.append({"relative": rel, "exists": img_path.exists(), "full_path": str(img_path)})
    return {"samples": samples, "ccpd_root": str(ccpd_path)}


@router.post("/recognize-ccpd", response_model=LPRResponse, summary="识别 CCPD 数据集样本")
async def recognize_ccpd_sample(
    relative: str = Query(..., description="CCPD 图片相对路径"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    ccpd_path = (settings.base_dir / settings.ccpd_data_path).resolve()
    img_path = (ccpd_path / relative).resolve()
    if not str(img_path).startswith(str(ccpd_path)):
        raise HTTPException(400, "非法路径")
    if not img_path.exists():
        raise HTTPException(404, f"图片不存在: {relative}")

    content = img_path.read_bytes()
    try:
        result = lpr_service.recognize(content, filename=relative, img_path=str(img_path))
    except Exception as e:
        await record_lpr_recognition(
            db, success=False, source="CCPD样本", error=str(e),
            user_id=user.id if user else None, extra={"relative": relative},
        )
        raise HTTPException(500, str(e))

    await record_lpr_recognition(
        db, success=result["success"], source="CCPD样本",
        plate_count=result["plate_count"], plates=result["plates"],
        model_available=result.get("model_available"),
        user_id=user.id if user else None, extra={"relative": relative},
    )

    encrypted_plates = encrypt_json({"plates": result["plates"]})
    record = LicensePlateRecord(
        user_id=user.id if user else None,
        source_type="ccpd",
        image_path=str(img_path),
        annotated_image=result["annotated_image"],
        plates_json=encrypted_plates,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return LPRResponse(**result, record_id=record.id)
