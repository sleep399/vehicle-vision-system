"""多路感知场景冲突 API —— 快照、冲突列表、评估与处置闭环。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.scenario_fusion_service import scenario_fusion_service

router = APIRouter(prefix="/api/scenario", tags=["多路感知融合"])


class EvaluateRequest(BaseModel):
    police_gesture: str = Field(..., description="交警手势，如 stop / go_straight")
    owner_action: str = Field(..., description="车主控车动作，如 wake / confirm")
    police_gesture_cn: str | None = None
    owner_gesture_cn: str | None = None
    plates: list[str] = Field(default_factory=list)
    confidence: float = 0.9


class ResolveConflictRequest(BaseModel):
    resolution_note: str | None = None


@router.get("/snapshot", summary="多路感知实时快照")
def get_snapshot() -> dict[str, Any]:
    """返回 LPR / 交警 / 车主三路最近信号与冲突概况。"""
    return scenario_fusion_service.get_snapshot()


@router.get("/advice", summary="跨模块融合驾驶建议（LLM）")
async def get_driving_advice(
    force_refresh: bool = False,
    force_template: bool = False,
) -> dict[str, Any]:
    """综合车牌 + 交警手势 + 车主控车，由 LLM 生成综合驾驶建议。"""
    return await scenario_fusion_service.get_driving_advice(
        force_refresh=force_refresh,
        force_template=force_template,
    )


@router.get("/conflicts", summary="场景冲突列表")
def list_conflicts(
    status: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """查询近期场景冲突及融合建议。"""
    return {
        "items": scenario_fusion_service.list_conflicts(db, status=status, limit=limit),
        "total": len(scenario_fusion_service.list_conflicts(db, status=status, limit=limit)),
    }


@router.post("/evaluate", summary="手动触发场景冲突评估（演示/测试）")
async def evaluate_scenario(
    body: EvaluateRequest,
    db: Session = Depends(get_db),
):
    """注入模拟信号并执行冲突判定，用于联调与演示。"""
    await scenario_fusion_service.ingest_lpr(
        db, success=True, plate_count=len(body.plates), plates=body.plates, source="manual_evaluate",
    )
    await scenario_fusion_service.ingest_police(
        db,
        gesture=body.police_gesture,
        gesture_cn=body.police_gesture_cn,
        confidence=body.confidence,
        source="manual_evaluate",
    )
    conflict = await scenario_fusion_service.ingest_owner(
        db,
        gesture_cn=body.owner_gesture_cn,
        action=body.owner_action,
        confidence=body.confidence,
        source="manual_evaluate",
    )
    return {
        "conflict_detected": conflict is not None,
        "conflict": scenario_fusion_service._conflict_to_dict(conflict) if conflict else None,
        "snapshot": scenario_fusion_service.get_snapshot(),
    }


@router.post("/conflicts/{conflict_id}/resolve", summary="处置场景冲突并关闭告警联动")
def resolve_conflict(
    conflict_id: int,
    body: ResolveConflictRequest | None = None,
    db: Session = Depends(get_db),
):
    """操作员确认按融合建议处置，解除车主动作抑制并联动关闭关联告警。"""
    note = body.resolution_note if body else None
    result = scenario_fusion_service.resolve_conflict(db, conflict_id, resolution_note=note)
    if not result:
        raise HTTPException(404, "场景冲突记录不存在")
    return {"message": "场景冲突已处置", "conflict": result}
