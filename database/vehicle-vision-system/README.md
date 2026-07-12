# 车载视觉感知与人机交互系统

前后端分离的车载摄像头视觉感知 Web 系统，涵盖车牌识别、交警手势识别、车主手势控车、日志监控与 LLM 告警智能体。

## 功能清单

| 模块 | 功能 | 技术方案 |
|------|------|----------|
| 车牌识别 | 单图/批量图片、视频、摄像头与 RTSP 输入，结果标注与历史查询 | RPNet + YOLO + LPRNet |
| 交警手势 | 8 种标准手势，骨骼关键点，连续视频识别 | YOLO Pose + LSTM/CTPGR |
| 车主控车 | 8 种手势，图片/视频/WebSocket，状态机、持续帧与二次确认 | MediaPipe Hands |
| 告警智能体 | 分类日志、异常感知、巡检、回放、LLM 摘要、WebSocket/SSE/邮件/Webhook | FastAPI + LLM API |
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
LLM_PROVIDER=openai       # openai/qwen/deepseek/zhipu/custom
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
| `POST /api/police-gesture/recognize-video` | 长视频交警手势识别 |
| `POST /api/owner-gesture/recognize` | 车主手势控车 |
| `POST /api/owner-gesture/recognize-video` | 车主手势视频识别 |
| `POST /api/owner-gesture/confirm` | 确认或取消待执行手势 |
| `WS /api/owner-gesture/ws-stream` | 车主实时手势识别与控车 |
| `GET /api/monitor/alerts` | 告警历史 |
| `GET /api/monitor/alerts/analytics` | 告警分析统计 |
| `GET /api/monitor/alerts/{id}/replay` | 告警事件回放 |
| `GET /api/monitor/logs` | 系统日志 |
| `GET /api/monitor/logs/stream` | 实时日志 SSE |
| `POST /api/monitor/assistant` | 告警智能助手 |
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

- 模型权重与 MediaPipe task 文件需放在项目约定的模型目录；缺失时接口会返回模型状态或降级结果
- 实时摄像头功能需 HTTPS 或 localhost 环境
- 推荐使用项目启动脚本指定的 Python 3.11 环境
