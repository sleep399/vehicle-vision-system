"""多路感知场景冲突规则与融合建议模板。"""

from __future__ import annotations

from typing import Any

# 交警手势优先级高于车主控车；停止/靠边属于道路安全最高优先级
POLICE_PRIORITY_GESTURES = frozenset({
    "stop", "pull_over", "slow_down", "turn_left", "turn_right", "lane_change",
})

OWNER_DRIVING_ACTIONS = frozenset({
    "wake", "confirm", "volume_adjust", "prev_page", "next_page",
    "answer_call", "hang_up", "go_home",
})

CONFLICT_RULES: list[dict[str, Any]] = [
    {
        "id": "police_stop_vs_owner_wake",
        "title": "交警停止 vs 车主唤醒",
        "when": {"police": "stop", "owner_action": "wake"},
        "severity": "critical",
        "suppress_owner": True,
        "recommendation": "立即服从交警停止手势，暂停唤醒车机；待交警放行后再操作车内功能。",
    },
    {
        "id": "police_stop_vs_owner_confirm",
        "title": "交警停止 vs 车主确认控车",
        "when": {"police": "stop", "owner_action": "confirm"},
        "severity": "critical",
        "suppress_owner": True,
        "recommendation": "交警示意停车，禁止执行车内确认操作；先停车并保持双手可见。",
    },
    {
        "id": "police_stop_vs_owner_control",
        "title": "交警停止 vs 车主控车动作",
        "when": {"police": "stop", "owner_action_any": OWNER_DRIVING_ACTIONS - {"wake", "confirm"}},
        "severity": "critical",
        "suppress_owner": True,
        "recommendation": "道路安全优先：服从交警停止指令，暂停音量/翻页/通话等车内操作。",
    },
    {
        "id": "police_pull_over_vs_owner_go_home",
        "title": "交警靠边 vs 车主返回待机",
        "when": {"police": "pull_over", "owner_action": "go_home"},
        "severity": "warning",
        "suppress_owner": False,
        "recommendation": "交警要求靠边停车，与「返回待机」意图冲突；建议先靠边停稳，再操作车机休眠。",
    },
    {
        "id": "police_turn_vs_owner_page",
        "title": "交警转向 vs 车主翻页",
        "when": {"police_any": {"turn_left", "turn_right", "lane_change"}, "owner_action_any": {"prev_page", "next_page"}},
        "severity": "warning",
        "suppress_owner": True,
        "recommendation": "交警正在指挥转向/变道，请暂停翻页操作，双手保持方向盘控制。",
    },
    {
        "id": "police_slow_down_vs_owner_volume",
        "title": "交警减速 vs 车主调节音量",
        "when": {"police": "slow_down", "owner_action": "volume_adjust"},
        "severity": "warning",
        "suppress_owner": True,
        "recommendation": "交警示意减速慢行，建议先减速并暂停音量调节，避免分心。",
    },
    {
        "id": "police_go_straight_vs_owner_hang_up",
        "title": "交警直行 vs 车主挂断",
        "when": {"police": "go_straight", "owner_action": "hang_up"},
        "severity": "info",
        "suppress_owner": False,
        "recommendation": "交警示意直行，挂断电话与通行不冲突，但建议单手操作、保持注意力。",
    },
]

POLICE_GESTURE_CN: dict[str, str] = {
    "stop": "停止",
    "go_straight": "直行",
    "turn_left": "左转弯",
    "left_turn_wait": "左转弯待转",
    "turn_right": "右转弯",
    "lane_change": "变道",
    "slow_down": "减速慢行",
    "pull_over": "靠边停车",
    "no_gesture": "无手势",
}

def normalize_plate_labels(plates: list | None) -> list[str]:
    """将 LPR 返回的 dict/字符串统一为可展示的车牌号列表。"""
    labels: list[str] = []
    for item in plates or []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                labels.append(text)
            continue
        if isinstance(item, dict):
            text = (
                item.get("plate_number")
                or item.get("plate")
                or item.get("text")
                or item.get("number")
            )
            if text:
                labels.append(str(text).strip())
    return labels


OWNER_ACTION_CN: dict[str, str] = {
    "wake": "唤醒系统",
    "confirm": "确认执行",
    "volume_adjust": "调节音量/温度",
    "prev_page": "上一功能",
    "next_page": "下一功能",
    "answer_call": "接听电话",
    "hang_up": "挂断电话",
    "go_home": "返回待机",
}


def _match_value(actual: str | None, expected: str | set[str] | frozenset[str] | None) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    if isinstance(expected, (set, frozenset)):
        return actual in expected
    return actual == expected


def rule_matches(rule: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """判断当前多路感知快照是否命中规则。"""
    when = rule.get("when") or {}
    police = snapshot.get("police_gesture")
    owner_action = snapshot.get("owner_action")

    if "police" in when and not _match_value(police, when["police"]):
        return False
    if "police_any" in when and not _match_value(police, when["police_any"]):
        return False
    if "owner_action" in when and not _match_value(owner_action, when["owner_action"]):
        return False
    if "owner_action_any" in when and not _match_value(owner_action, when["owner_action_any"]):
        return False
    return True


def build_fusion_summary(rule: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """生成融合建议摘要。"""
    police_cn = snapshot.get("police_gesture_cn") or POLICE_GESTURE_CN.get(
        snapshot.get("police_gesture") or "", "未知交警手势"
    )
    owner_cn = snapshot.get("owner_action_cn") or OWNER_ACTION_CN.get(
        snapshot.get("owner_action") or "", "未知车主动作"
    )
    plates = normalize_plate_labels(snapshot.get("plates"))
    plate_hint = f"，关联车牌 {', '.join(plates[:3])}" if plates else ""
    return (
        f"检测到场景冲突：交警「{police_cn}」与车主「{owner_cn}」同时出现{plate_hint}。"
        f"融合建议：{rule['recommendation']}"
    )
