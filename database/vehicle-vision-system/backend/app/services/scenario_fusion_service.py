"""多路感知场景冲突判定、融合建议与告警联动服务。"""

from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.scenario import ScenarioConflict
from app.services.alert_agent import alert_agent
from app.services.llm_service import llm_service
from app.services.owner_gesture_service import owner_gesture_service
from app.utils.logger import write_agent_log, write_log
from app.utils.scenario_rules import (
    CONFLICT_RULES,
    OWNER_ACTION_CN,
    POLICE_GESTURE_CN,
    build_fusion_summary,
    normalize_plate_labels,
    rule_matches,
)


class ScenarioFusionService:
    """汇聚 LPR / 交警 / 车主三路感知，判定冲突并联动告警。"""

    def __init__(self) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._last_signals: dict[str, Any] = {
            "lpr": None,
            "police": None,
            "owner": None,
        }
        self._recent_conflicts: deque[dict[str, Any]] = deque(maxlen=50)
        self._cooldown_until: dict[str, datetime] = {}
        self._last_driving_advice: dict[str, Any] | None = None
        self._last_driving_advice_at: datetime | None = None
        self._last_driving_advice_key: str | None = None

    def _now(self) -> datetime:
        return datetime.utcnow()

    def _window_cutoff(self) -> datetime:
        return self._now() - timedelta(seconds=settings.scenario_window_seconds)

    def _prune_events(self) -> None:
        cutoff = self._window_cutoff()
        while self._events and self._events[0]["timestamp"] < cutoff:
            self._events.popleft()

    def _record_event(self, module: str, payload: dict[str, Any]) -> None:
        event = {"module": module, "timestamp": self._now(), **payload}
        self._events.append(event)
        self._last_signals[module] = payload
        self._prune_events()

    def _fusion_status_hint(self, correlated: dict[str, Any] | None = None) -> str:
        snap = correlated or self._build_correlation_snapshot()
        has_police = bool(snap.get("police_gesture"))
        has_owner_action = bool(snap.get("owner_action"))
        has_owner_gesture = bool(
            snap.get("owner_gesture") and snap.get("owner_gesture") != "no_gesture"
        )
        if has_police and has_owner_action:
            return "交警与车主控车信号已对齐，当前无规则冲突"
        if has_police and has_owner_gesture:
            return "已识别车主手势，需触发控车动作（唤醒/翻页/确认等）才会判定冲突"
        if has_police:
            return ""
        if has_owner_action:
            return "已捕获车主控车动作，等待交警手势信号（30 秒窗口内）"
        return ""

    def get_snapshot(self) -> dict[str, Any]:
        """返回多路感知实时快照（供 API / 助手使用）。"""
        police = self._last_signals.get("police") or {}
        owner = self._last_signals.get("owner") or {}
        lpr = self._last_signals.get("lpr") or {}
        correlated = self._build_correlation_snapshot()
        plate_labels = normalize_plate_labels(lpr.get("plates"))
        return {
            "window_seconds": settings.scenario_window_seconds,
            "fusion_status_hint": self._fusion_status_hint(correlated),
            "lpr": {
                "plate_count": lpr.get("plate_count", 0),
                "plates": plate_labels,
                "success": lpr.get("success"),
                "updated_at": lpr.get("updated_at"),
            },
            "police": {
                "gesture": police.get("gesture"),
                "gesture_cn": police.get("gesture_cn"),
                "confidence": police.get("confidence"),
                "updated_at": police.get("updated_at"),
            },
            "owner": {
                "gesture": owner.get("gesture"),
                "gesture_cn": owner.get("gesture_cn"),
                "action": owner.get("action"),
                "action_cn": owner.get("action_cn"),
                "confidence": owner.get("confidence"),
                "updated_at": owner.get("updated_at"),
            },
            "open_conflicts": sum(1 for c in self._recent_conflicts if c.get("status") == "open"),
            "owner_suppressed": owner_gesture_service.is_action_suppressed()[0],
            "suppress_reason": owner_gesture_service.is_action_suppressed()[1],
        }

    def _advice_cache_key(self, correlated: dict[str, Any]) -> str:
        return json.dumps({
            "police": correlated.get("police_gesture"),
            "plates": correlated.get("plates"),
            "owner_action": correlated.get("owner_action"),
            "owner_gesture": correlated.get("owner_gesture"),
        }, ensure_ascii=False, sort_keys=True)

    async def get_driving_advice(
        self,
        *,
        force_refresh: bool = False,
        force_template: bool = False,
    ) -> dict[str, Any]:
        """跨模块融合推理：综合三路感知生成 LLM 驾驶建议。"""
        snapshot = self.get_snapshot()
        correlated = self._build_correlation_snapshot()
        cache_key = self._advice_cache_key(correlated)
        cache_ttl = timedelta(seconds=settings.scenario_advice_cache_seconds)

        if (
            not force_refresh
            and self._last_driving_advice
            and self._last_driving_advice_key == cache_key
            and self._last_driving_advice_at
            and self._now() - self._last_driving_advice_at < cache_ttl
        ):
            cached = dict(self._last_driving_advice)
            cached["cached"] = True
            return cached

        advice = await llm_service.generate_driving_advice(
            correlated, snapshot, force_template=force_template,
        )
        result = {
            **advice,
            "snapshot": snapshot,
            "correlated": correlated,
            "cached": False,
            "llm_configured": settings.llm_configured,
        }
        self._last_driving_advice = result
        self._last_driving_advice_at = self._now()
        self._last_driving_advice_key = cache_key
        return result

    def _build_correlation_snapshot(self) -> dict[str, Any]:
        cutoff = self._window_cutoff()
        police = owner = lpr = None
        for event in reversed(self._events):
            if event["timestamp"] < cutoff:
                break
            mod = event["module"]
            if mod == "police" and police is None:
                police = event
            elif mod == "owner" and owner is None:
                owner = event
            elif mod == "lpr" and lpr is None:
                lpr = event
            if police and owner:
                break

        snapshot: dict[str, Any] = {
            "police_gesture": (police or {}).get("gesture"),
            "police_gesture_cn": (police or {}).get("gesture_cn"),
            "police_confidence": (police or {}).get("confidence"),
            "owner_gesture": (owner or {}).get("gesture"),
            "owner_gesture_cn": (owner or {}).get("gesture_cn"),
            "owner_action": (owner or {}).get("action"),
            "owner_action_cn": (owner or {}).get("action_cn"),
            "owner_confidence": (owner or {}).get("confidence"),
            "plates": normalize_plate_labels((lpr or {}).get("plates")),
            "plate_count": (lpr or {}).get("plate_count", 0),
        }
        return snapshot

    def _in_cooldown(self, conflict_type: str) -> bool:
        until = self._cooldown_until.get(conflict_type)
        return until is not None and self._now() < until

    def _set_cooldown(self, conflict_type: str) -> None:
        self._cooldown_until[conflict_type] = self._now() + timedelta(
            seconds=settings.scenario_conflict_cooldown_seconds
        )

    async def ingest_lpr(
        self,
        db: Session,
        *,
        success: bool,
        plate_count: int = 0,
        plates: list | None = None,
        source: str = "",
    ) -> ScenarioConflict | None:
        payload = {
            "success": success,
            "plate_count": plate_count,
            "plates": normalize_plate_labels(plates),
            "source": source,
            "updated_at": self._now().isoformat(),
        }
        self._record_event("lpr", payload)
        return await self.evaluate(db, trigger_module="lpr")

    async def ingest_police(
        self,
        db: Session,
        *,
        gesture: str | None,
        gesture_cn: str | None = None,
        confidence: float = 0.0,
        source: str = "",
    ) -> ScenarioConflict | None:
        if not gesture or gesture == "no_gesture":
            return None
        payload = {
            "gesture": gesture,
            "gesture_cn": gesture_cn or POLICE_GESTURE_CN.get(gesture, gesture),
            "confidence": confidence,
            "source": source,
            "updated_at": self._now().isoformat(),
        }
        self._record_event("police", payload)
        return await self.evaluate(db, trigger_module="police")

    async def ingest_owner(
        self,
        db: Session,
        *,
        gesture: str | None = None,
        gesture_cn: str | None = None,
        action: str | None = None,
        confidence: float = 0.0,
        source: str = "",
    ) -> ScenarioConflict | None:
        has_gesture = bool(gesture and gesture != "no_gesture")
        if not action and not has_gesture:
            return None
        payload = {
            "gesture": gesture,
            "gesture_cn": gesture_cn,
            "action": action,
            "action_cn": OWNER_ACTION_CN.get(action, action) if action else None,
            "confidence": confidence,
            "source": source,
            "updated_at": self._now().isoformat(),
        }
        self._record_event("owner", payload)
        if not action:
            return None
        return await self.evaluate(db, trigger_module="owner")

    async def evaluate(
        self,
        db: Session,
        *,
        trigger_module: str | None = None,
        force: bool = False,
    ) -> ScenarioConflict | None:
        """在时间窗口内关联三路信号并判定冲突。"""
        snapshot = self._build_correlation_snapshot()
        if not snapshot.get("police_gesture") or not snapshot.get("owner_action"):
            return None

        for rule in CONFLICT_RULES:
            if not rule_matches(rule, snapshot):
                continue
            conflict_type = rule["id"]
            if not force and self._in_cooldown(conflict_type):
                continue
            return await self._raise_conflict(db, rule, snapshot, trigger_module=trigger_module)

        return None

    async def _raise_conflict(
        self,
        db: Session,
        rule: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        trigger_module: str | None = None,
    ) -> ScenarioConflict:
        conflict_type = rule["id"]
        severity = rule.get("severity", "warning")
        scenario_id = str(uuid.uuid4())[:12]
        fusion_text = build_fusion_summary(rule, snapshot)
        suppress = bool(rule.get("suppress_owner"))

        if suppress:
            owner_gesture_service.set_action_suppression(
                settings.scenario_suppress_seconds,
                fusion_text,
            )

        context = {
            "scenario_id": scenario_id,
            "conflict_rule_id": conflict_type,
            "conflict_title": rule.get("title", conflict_type),
            "trigger_module": trigger_module,
            "police_gesture": snapshot.get("police_gesture"),
            "police_gesture_cn": snapshot.get("police_gesture_cn"),
            "owner_gesture": snapshot.get("owner_gesture"),
            "owner_gesture_cn": snapshot.get("owner_gesture_cn"),
            "owner_action": snapshot.get("owner_action"),
            "owner_action_cn": snapshot.get("owner_action_cn"),
            "plates": snapshot.get("plates") or [],
            "fusion_recommendation": rule["recommendation"],
            "fusion_summary": fusion_text,
            "suppress_owner_actions": suppress,
        }

        alert = await alert_agent.monitor(
            db,
            "scenario_conflict_detected",
            severity,
            context,
            force_template=True,
        )

        if suppress and alert:
            await alert_agent.monitor(
                db,
                "owner_action_suppressed",
                "warning",
                {
                    **context,
                    "parent_alert_id": alert.id,
                    "message": "因场景冲突，车主控车动作已被临时抑制。",
                },
                force_template=True,
            )

        if alert:
            await alert_agent.monitor(
                db,
                "fusion_recommendation_issued",
                "info",
                {**context, "parent_alert_id": alert.id},
                force_template=True,
            )

        row = ScenarioConflict(
            scenario_id=scenario_id,
            conflict_type=conflict_type,
            severity=severity,
            status="open",
            sources_json=json.dumps(snapshot, ensure_ascii=False),
            fusion_recommendation=rule["recommendation"],
            suppress_owner_actions="1" if suppress else "0",
            alert_id=alert.id if alert else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        self._set_cooldown(conflict_type)
        conflict_item = self._conflict_to_dict(row)
        self._recent_conflicts.appendleft(conflict_item)

        write_log(
            db,
            "agent",
            f"多路感知冲突: {rule.get('title', conflict_type)}",
            level="WARN" if severity == "warning" else ("CRITICAL" if severity == "critical" else "INFO"),
            detail={
                "scenario_id": scenario_id,
                "conflict_type": conflict_type,
                "fusion_recommendation": rule["recommendation"],
                "alert_id": alert.id if alert else None,
            },
        )
        write_agent_log(
            db,
            f"场景融合建议: {rule['recommendation']}",
            level="INFO",
            detail={"scenario_id": scenario_id, "conflict_type": conflict_type},
        )
        return row

    def _conflict_to_dict(self, row: ScenarioConflict) -> dict[str, Any]:
        sources = None
        if row.sources_json:
            try:
                sources = json.loads(row.sources_json)
            except Exception:
                sources = row.sources_json
        return {
            "id": row.id,
            "scenario_id": row.scenario_id,
            "conflict_type": row.conflict_type,
            "severity": row.severity,
            "status": row.status,
            "sources": sources,
            "fusion_recommendation": row.fusion_recommendation,
            "suppress_owner_actions": row.suppress_owner_actions == "1",
            "alert_id": row.alert_id,
            "resolution_note": row.resolution_note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }

    def list_conflicts(
        self,
        db: Session,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        q = db.query(ScenarioConflict).order_by(ScenarioConflict.created_at.desc())
        if status:
            q = q.filter(ScenarioConflict.status == status)
        rows = q.limit(limit).all()
        return [self._conflict_to_dict(r) for r in rows]

    def resolve_conflict(
        self,
        db: Session,
        conflict_id: int,
        *,
        resolution_note: str | None = None,
    ) -> dict[str, Any] | None:
        row = db.query(ScenarioConflict).filter(ScenarioConflict.id == conflict_id).first()
        if not row:
            return None
        row.status = "resolved"
        row.resolution_note = resolution_note or "操作员已确认并按融合建议处置"
        row.resolved_at = self._now()
        db.commit()
        db.refresh(row)

        owner_gesture_service.clear_action_suppression()

        if row.alert_id:
            from app.models.alerts import AlertEvent

            alert = db.query(AlertEvent).filter(AlertEvent.id == row.alert_id).first()
            if alert and alert.status != "resolved":
                alert.status = "resolved"
                alert.resolved_at = self._now()
                alert.resolution_note = row.resolution_note
                db.commit()

        for item in self._recent_conflicts:
            if item.get("id") == conflict_id:
                item["status"] = "resolved"
                item["resolution_note"] = row.resolution_note
                item["resolved_at"] = row.resolved_at.isoformat() if row.resolved_at else None

        write_log(
            db,
            "agent",
            f"场景冲突已处置: #{conflict_id}",
            level="INFO",
            detail={"conflict_id": conflict_id, "scenario_id": row.scenario_id},
        )
        return self._conflict_to_dict(row)


scenario_fusion_service = ScenarioFusionService()
