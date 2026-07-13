"""Test configuration for the canonical backend package."""

import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 测试使用固定的非生产密钥，避免依赖每位开发者自己的 .env。
os.environ.setdefault("AES_KEY", "11" * 32)
