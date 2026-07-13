#!/usr/bin/env python3
"""启动车载视觉感知与人机交互系统"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app.utils.quiet_logs import UVICORN_LOG_CONFIG, configure_quiet_logs

configure_quiet_logs()

import uvicorn
from app.config import settings
from app.utils.crypto import validate_aes_key


def _resolve_security_file(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path

if __name__ == "__main__":
    if settings.secret_key in {
        "dev-secret-key-change-in-production",
        "change-this-to-a-random-secret-key-in-production",
    } or len(settings.secret_key) < 32:
        raise SystemExit("JWT 签名密钥不安全，请先运行 python setup_security.py")
    try:
        validate_aes_key()
    except RuntimeError as exc:
        raise SystemExit(f"安全配置未完成：{exc}") from exc
    certfile = _resolve_security_file(settings.https_certfile)
    keyfile = _resolve_security_file(settings.https_keyfile)
    if not certfile.is_file() or not keyfile.is_file():
        raise SystemExit("HTTPS 证书不存在，请先运行 python setup_security.py")

    print(f"\n{'='*50}")
    print(f"  {settings.app_name}")
    print(f"  Web 界面: https://localhost:{settings.port}")
    print(f"  API 文档: https://localhost:{settings.port}/api/docs")
    print(f"  默认账号: admin / admin123")
    print(f"{'='*50}\n")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=UVICORN_LOG_CONFIG,
        ssl_certfile=str(certfile),
        ssl_keyfile=str(keyfile),
    )
