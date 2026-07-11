#!/usr/bin/env python3
"""启动车载视觉感知与人机交互系统"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn
from app.config import settings

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  {settings.app_name}")
    print(f"  Web 界面: http://localhost:{settings.port}")
    print(f"  API 文档: http://localhost:{settings.port}/api/docs")
    print(f"  默认账号: admin / admin123")
    print(f"{'='*50}\n")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
