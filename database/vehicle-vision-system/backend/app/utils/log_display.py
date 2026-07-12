"""日志与回放展示层 —— 将类别/级别/消息转为用户可读的中文。"""

from __future__ import annotations

import json
import re
from typing import Any

from app.utils.logger import level_to_cn

LOG_CATEGORY_CN: dict[str, str] = {
    "lpr": "车牌识别",
    "police_gesture": "交警手势",
    "owner_gesture": "车主手势",
    "alert": "告警",
    "user": "用户操作",
    "system": "系统运行",
    "agent": "智能体决策",
}

RECORD_TYPE_CN: dict[str, str] = {
    "lpr": "车牌识别",
    "police_gesture": "交警手势",
    "owner_gesture": "车主手势",
    "record": "识别记录",
}

SOURCE_TYPE_CN: dict[str, str] = {
    "image": "图片",
    "video": "视频",
    "camera": "摄像头",
    "rtsp": "RTSP流",
    "websocket": "实时流",
    "ccpd": "CCPD样本",
}

EXCEPTION_TYPE_CN: dict[str, str] = {
    "RuntimeError": "运行错误",
    "ValueError": "参数错误",
    "TypeError": "类型错误",
    "FileNotFoundError": "文件未找到",
    "PermissionError": "权限不足",
    "ConnectionError": "连接错误",
    "TimeoutError": "请求超时",
    "OSError": "系统错误",
    "ImportError": "依赖缺失",
    "ModuleNotFoundError": "模块未找到",
    "HTTPException": "请求异常",
    "KeyError": "数据缺失",
    "IndexError": "索引越界",
    "AttributeError": "属性错误",
    "MemoryError": "内存不足",
}

# (正则, 中文替换) —— 按顺序匹配，优先具体短语
ERROR_PHRASE_REPLACEMENTS: list[tuple[str, str]] = [
    (
        r"The following operation failed in the TorchScript interpreter\.?",
        "AI 模型推理失败，可能是模型文件损坏或与当前运行环境不兼容",
    ),
    (
        r"Expected all tensors to be on the same device.*",
        "模型计算设备不一致（CPU/GPU 混用），请重启服务或检查模型加载配置",
    ),
    (
        r"CUDA out of memory.*",
        "显卡显存不足，请减小图片尺寸或关闭其他占用显存的程序",
    ),
    (
        r"CUDA error:?.*",
        "显卡运行异常，请检查 CUDA 驱动或改用 CPU 模式",
    ),
    (
        r"No module named ['\"]([^'\"]+)['\"]",
        r"缺少依赖模块「\1」，请联系管理员安装",
    ),
    (
        r"Connection refused",
        "连接被拒绝，目标服务可能未启动",
    ),
    (
        r"Connection reset by peer",
        "连接被远端重置，请检查网络或服务状态",
    ),
    (
        r"timed out|TimeoutError|timeout",
        "请求超时，请稍后重试",
    ),
    (
        r"Unable to open RTSP stream|无法打开 RTSP 流",
        "无法打开 RTSP 视频流，请检查地址与网络",
    ),
    (
        r"Failed to load model|Error loading model",
        "模型加载失败，请检查模型文件是否完整",
    ),
    (
        r"YOLO pose backend requires: pip install ultralytics",
        "缺少 ultralytics 依赖，请安装后重启服务",
    ),
    (
        r"LLM API Key 未配置|LLM API key not configured",
        "智能分析服务密钥未配置",
    ),
    (
        r"401 Unauthorized|403 Forbidden|404 Not Found|500 Internal Server Error",
        "服务请求失败，请稍后重试",
    ),
    (
        r"SSL: CERTIFICATE_VERIFY_FAILED|certificate verify failed",
        "SSL 证书校验失败，请检查网络或证书配置",
    ),
    (
        r"Network is unreachable",
        "网络不可达，请检查网络连接",
    ),
    (
        r"Permission denied",
        "权限不足，无法访问目标资源",
    ),
    (
        r"Address already in use",
        "端口已被占用，请更换端口或关闭冲突进程",
    ),
]


def _latin_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    return letters / max(len(text), 1)


def _looks_like_technical_english(text: str) -> bool:
    """判断是否为面向开发者的英文技术报错。"""
    if not text:
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    lowered = text.lower()
    markers = (
        "error", "exception", "traceback", "failed", "interpreter",
        "torchscript", "cuda", "runtime", "module", "tensor",
        "site-packages", "stack trace", "undefined",
    )
    if any(m in lowered for m in markers):
        return True
    return _latin_char_ratio(text) > 0.55 and len(text) > 12


def humanize_error_text(text: str | None) -> str:
    """将常见英文/技术异常翻译为用户可读的中文。"""
    raw = (text or "").strip()
    if not raw:
        return raw

    result = raw
    for pattern, replacement in ERROR_PHRASE_REPLACEMENTS:
        if re.search(pattern, result, flags=re.IGNORECASE):
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    exc_match = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception)):\s*(.+)$", result, flags=re.DOTALL)
    if exc_match:
        exc_name, exc_msg = exc_match.groups()
        exc_cn = EXCEPTION_TYPE_CN.get(exc_name, "系统异常")
        inner = humanize_error_text(exc_msg.strip())
        if inner and inner != exc_msg.strip():
            return f"{exc_cn}：{inner}"
        if _looks_like_technical_english(exc_msg):
            return f"{exc_cn}：运行出现异常，请稍后重试"

    if _looks_like_technical_english(result):
        return "系统运行异常，请稍后重试或联系管理员"

    return result


def category_cn(category: str | None) -> str:
    if not category:
        return "系统"
    return LOG_CATEGORY_CN.get(category, category)


def record_type_cn(record_type: str | None) -> str:
    if not record_type:
        return "识别记录"
    return RECORD_TYPE_CN.get(record_type, category_cn(record_type))


def source_type_cn(source: str | None) -> str:
    if not source:
        return ""
    return SOURCE_TYPE_CN.get(source, source)


def _strip_traceback(text: str) -> str:
    """去掉堆栈跟踪，只保留对人有用的首行说明。"""
    if not text:
        return ""
    if "Traceback (most recent call last)" in text:
        head = text.split("Traceback (most recent call last)")[0].strip()
        if head:
            return head.rstrip("：:").strip()
        return "系统运行出现异常"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned: list[str] = []
    for ln in lines:
        if ln.startswith("File \"") or re.match(r"^\s*File ", ln):
            continue
        if re.match(r"^\s*\^+", ln):
            continue
        if "site-packages" in ln and ("\\" in ln or "/" in ln):
            continue
        if re.match(r"^[A-Za-z_][\w.]*Error:", ln):
            cleaned.append(ln)
            continue
        if len(ln) > 180 and ("\\" in ln or "site-packages" in ln):
            continue
        cleaned.append(ln)
    if not cleaned:
        return lines[0][:120] if lines else text[:120]
    return cleaned[0][:200]


def sanitize_log_message(message: str | None, detail: Any = None) -> str:
    """将日志消息转为普通人能看懂的一句话（隐藏乱码堆栈）。"""
    msg = (message or "").strip()
    detail_obj = detail
    if isinstance(detail_obj, str):
        try:
            detail_obj = json.loads(detail_obj)
        except Exception:
            detail_obj = None

    if isinstance(detail_obj, dict):
        err = detail_obj.get("error_message") or detail_obj.get("error")
        err_type = detail_obj.get("error_type")
        if err:
            err = humanize_error_text(str(err))
        if err and ("Traceback" in msg or len(msg) > 160 or "File \"" in msg):
            prefix = humanize_error_text(_strip_traceback(msg) or category_cn(None))
            if err_type and str(err_type) not in prefix:
                type_cn = EXCEPTION_TYPE_CN.get(str(err_type), str(err_type))
                return f"{prefix}：{type_cn} — {err}"
            return f"{prefix}：{err}"
        if err and err not in msg:
            base = humanize_error_text(msg)
            return f"{base}（{err}）" if base else err

    if "Traceback (most recent call last)" in msg:
        return humanize_error_text(_strip_traceback(msg) or "系统运行出现异常，请稍后重试")

    if len(msg) > 220 and ("\\" in msg or "site-packages" in msg):
        return humanize_error_text(_strip_traceback(msg))

    return humanize_error_text(msg) or "（无详细说明）"


def format_log_entry(
    *,
    category: str | None,
    level: str | None,
    message: str | None,
    detail: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """统一格式化单条日志供 API / 回放 / SSE 使用。"""
    level_cn = level_to_cn(level)
    cat_cn = category_cn(category)
    display_message = sanitize_log_message(message, detail)
    if category in ("agent", "alert"):
        from app.utils.alert_analysis import humanize_replay_log_message
        display_message = humanize_replay_log_message(display_message, category)
    return {
        **extra,
        "category": category,
        "category_cn": cat_cn,
        "level": level_cn,
        "level_cn": level_cn,
        "message": message or "",
        "display_message": display_message,
        "detail_json": detail if isinstance(detail, (dict, list)) else detail,
    }


def format_record_entry(record: dict[str, Any]) -> dict[str, Any]:
    """识别记录条目中文化。"""
    r = dict(record)
    r["type_cn"] = record_type_cn(r.get("type"))
    if r.get("source_type"):
        r["source_type_cn"] = source_type_cn(r.get("source_type"))
    return r
