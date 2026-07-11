# 车载视觉感知与人机交互系统

前后端分离的车载摄像头视觉感知 Web 系统，涵盖车牌识别、交警手势识别、车主手势控车、日志监控与 LLM 告警智能体。

## 功能清单

| 模块 | 功能 | 技术方案 |
|------|------|----------|
| 车牌识别 | 图片/视频/实时摄像头输入，检测+OCR，结果标注与历史查询 | OpenCV + EasyOCR |
| 交警手势 | 8 种标准手势，骨骼关键点，实时视频流 | MediaPipe Pose + 规则分类 |
| 车主控车 | 6+ 种手势，手部关键点，模拟车辆控制面板，误触发抑制 | MediaPipe Hands |
| 告警智能体 | 日志监控、异常感知、LLM 摘要、WebSocket/邮件/Webhook 推送 | FastAPI + LLM API |
| 扩展 | 多种登录、Swagger 文档、AES 加密存储 | JWT + OpenAPI + AES-GCM |

## 快速启动

```bash
cd vehicle-vision-system
pip install -r requirements.txt
python run.py
```

或双击 `start.bat`（Windows）。

访问 http://localhost:8001

- 默认账号：`admin` / `admin123`
- API 文档：http://localhost:8001/api/docs

## 项目结构

```
vehicle-vision-system/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置
│   │   ├── database.py          # SQLite 数据库
│   │   ├── models/              # 数据模型
│   │   ├── routers/             # API 路由
│   │   ├── services/            # 识别服务 & 告警智能体
│   │   └── utils/               # 工具（加密、认证、日志）
│   └── static/                  # Web 前端
├── data/                        # 数据库文件
├── uploads/                     # 上传文件
├── requirements.txt
├── run.py
└── .env.example
```

## 数据集关联

本项目引用同级目录下的三个数据集：

- **CCPD** (`../CCPD-master`) — 车牌识别，支持从文件名解析 Ground Truth
- **CTPGR** (`../ctpgr-pytorch-master`) — 交警手势参考（8 种标准手势映射）
- **HaGRID** (`../hagrid-master`) — 手势识别参考

将 CCPD 图片放入对应子目录后，可通过 `/api/lpr/ccpd-sample` 查看样本。

## 配置

复制 `.env.example` 为 `.env` 并按需修改：

```env
LLM_API_KEY=sk-xxx          # OpenAI 兼容 API Key（留空使用模板告警）
WEBHOOK_URL=https://...       # 企业微信/钉钉机器人 Webhook
SMTP_HOST=smtp.example.com    # 邮件通知
```

## 登录方式

1. **账号密码** — admin / admin123
2. **验证码登录** — 邮箱/手机号 + 验证码（演示模式返回验证码）
3. **微信扫码** — 模拟扫码（3 秒后自动确认）
4. **跳过登录** — 直接体验（部分功能可用）

## API 概览

| 端点 | 说明 |
|------|------|
| `POST /api/lpr/recognize` | 上传图片识别车牌 |
| `POST /api/police-gesture/recognize` | 交警手势识别 |
| `POST /api/owner-gesture/recognize` | 车主手势控车 |
| `GET /api/monitor/alerts` | 告警历史 |
| `GET /api/monitor/logs` | 系统日志 |
| `WS /ws/alerts` | 实时告警推送 |
| `WS /ws/stream/{module}` | 实时视频流识别 |

## 手势映射（车主控车）

| 手势 | 动作 |
|------|------|
| 手掌张开 | 唤醒系统 |
| 握拳 | 确认执行 |
| 单指画圈 | 调节音量 |
| 左/右滑 | 切换功能页 |
| 拇指向上/下 | 接听/挂断电话 |
| 挥手 | 返回主页 |

## 注意事项

- 首次运行 EasyOCR 会自动下载模型（约 100MB），请保持网络畅通
- 实时摄像头功能需 HTTPS 或 localhost 环境
- Python 3.10+ 推荐，已在 Python 3.14 测试通过
