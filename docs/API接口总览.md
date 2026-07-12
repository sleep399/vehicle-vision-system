# API 接口总览

本表按当前 [openapi.yaml](openapi.yaml) 整理，列格式与需求文档中的接口清单一致。通用错误响应与状态码见[全局错误码表](全局错误码表.md)。

| 接口名称 | 所属模块 | 请求方式 | 接口路径 | 鉴权方式 | 入参说明 | 出参结构 | 内部调用模块 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 用户注册 | 用户认证模块 | POST | `/api/auth/register` | 无需鉴权 | JSON：`username`、`password`、`email?`、`phone?` | `access_token`、`token_type` | 用户表、密码加密、系统日志 |
| 用户登录 | 用户认证模块 | POST | `/api/auth/login` | 无需鉴权 | JSON：`username`、`password` | `access_token`、`token_type` | 用户表、密码校验、系统日志 |
| 发送验证码 | 用户认证模块 | POST | `/api/auth/send-code` | 无需鉴权 | JSON：`target`、`target_type(email/phone)` | `message`、`code`（仅演示）、`expires_in` | 验证码表、系统日志 |
| 验证码登录 | 用户认证模块 | POST | `/api/auth/login-code` | 无需鉴权 | JSON：`target`、`code`、`target_type` | `access_token`、`token_type` | 验证码表、用户表、系统日志 |
| 创建微信扫码会话 | 用户认证模块 | POST | `/api/auth/wechat/qrcode` | 无需鉴权 | 无 | `session_id`、`qrcode_url`、`poll_url` | 微信会话表、系统日志 |
| 获取扫码二维码 | 用户认证模块 | GET | `/api/auth/wechat/qrcode/{session_id}` | 无需鉴权 | 路径：`session_id` | PNG 二维码二进制流 | 微信会话表、二维码生成器 |
| 确认扫码登录 | 用户认证模块 | POST | `/api/auth/wechat/confirm/{session_id}` | 无需鉴权（演示） | 路径：`session_id` | `status` | 微信会话表、用户表、系统日志 |
| 轮询扫码状态 | 用户认证模块 | GET | `/api/auth/wechat/poll/{session_id}` | 无需鉴权 | 路径：`session_id` | `status`；确认后附 `access_token` | 微信会话表、用户表 |
| 获取当前用户 | 用户认证模块 | GET | `/api/auth/me` | Bearer JWT（必需） | 请求头：`Authorization` | `id`、`username`、`email`、`phone` | JWT 校验、用户表 |
| 图片车牌识别 | 车辆车牌识别模块 | POST | `/api/lpr/recognize` | Bearer JWT（可选） | form-data：`file`（图片） | `plates`、`plate_count`、`annotated_image`、`success`、`record_id` | LPR 服务、告警智能体、文件存储、车牌记录、系统日志 |
| 视频车牌识别 | 车辆车牌识别模块 | POST | `/api/lpr/recognize-video` | Bearer JWT（可选） | form-data：`file`（视频）；query：`interval=1..60` | `frame_count`、`results` | LPR 视频服务、文件存储、系统日志 |
| 车牌识别历史 | 车辆车牌识别模块 | GET | `/api/lpr/history` | Bearer JWT（可选） | query：`skip`、`limit` | 记录数组：`id`、`plate_count`、`annotated_image`、`created_at` | 车牌记录、数据解密 |
| CCPD 样本查询 | 车辆车牌识别模块 | GET | `/api/lpr/ccpd-sample` | 无需鉴权 | 无 | `samples`、`ccpd_root`、`message?` | CCPD 数据集文件 |
| 交警手势视频识别 | 交警手势识别模块 | POST | `/api/police-gesture/recognize-video` | Bearer JWT（可选） | form-data：`file`（视频），query：`interval`、`max_results`、`max_sampled_frames` | 采样帧结果、手势变化、命中数量 | CTPGR/YOLO 姿态检测、LSTM 时序分类、文件存储、系统日志 |
| 交警手势字典 | 交警手势识别模块 | GET | `/api/police-gesture/gestures` | 无需鉴权 | 无 | 数组：`id`、`en`、`cn` | 交警手势字典 |
| 交警手势历史 | 交警手势识别模块 | GET | `/api/police-gesture/history` | 无需鉴权 | query：`skip`、`limit` | 数组：`id`、`gesture`、`gesture_cn`、`confidence`、`annotated_image`、`created_at` | 交警手势记录 |
| 车主手势识别与控车 | 车主手势控车模块 | POST | `/api/owner-gesture/recognize` | Bearer JWT（可选） | form-data：`file`（图片或 GIF） | `gesture`、`gesture_cn`、`confidence`、`action?`、`keypoints`、`annotated_image`、`record_id` | 手势识别服务、车辆状态、告警智能体、文件存储、手势记录、系统日志 |
| 获取模拟车辆状态 | 车主手势控车模块 | GET | `/api/owner-gesture/vehicle-state` | Bearer JWT（可选） | 无 | `volume`、`temperature`、`phone_status`、`current_page`、`is_awake` | 车辆状态表 |
| 手动更新车辆状态 | 车主手势控车模块 | PUT | `/api/owner-gesture/vehicle-state` | Bearer JWT（可选） | JSON：完整 `VehicleState` | 更新后的 `VehicleState` | 车辆状态表、系统日志 |
| 车主手势字典 | 车主手势控车模块 | GET | `/api/owner-gesture/gestures` | 无需鉴权 | 无 | 数组：`key`、`en`、`cn`、`action` | 车主手势字典 |
| 车主手势历史 | 车主手势控车模块 | GET | `/api/owner-gesture/history` | 无需鉴权 | query：`skip`、`limit` | 数组：`id`、`gesture`、`gesture_cn`、`confidence`、`action`、`annotated_image`、`created_at` | 车主手势记录 |
| 查询系统日志 | 日志监控与告警智能体 | GET | `/api/monitor/logs` | 无需鉴权 | query：`category`、`level`、`user_id`、`start`、`end`、`skip`、`limit` | 日志数组：`id`、`category`、`level`、`message`、`detail_json`、`created_at` | 系统日志表 |
| 查询告警历史 | 日志监控与告警智能体 | GET | `/api/monitor/alerts` | 无需鉴权 | query：`level`、`skip`、`limit` | 告警数组：`id`、`level`、`event_type`、`title`、`summary`、`status` | 告警事件表 |
| 获取告警统计 | 日志监控与告警智能体 | GET | `/api/monitor/alerts/stats` | 无需鉴权 | 无 | `total`、`by_level`、`by_type`、`recent` | 告警智能体、告警事件表 |
| 处理告警 | 日志监控与告警智能体 | POST | `/api/monitor/alerts/{alert_id}/resolve` | 无需鉴权 | 路径：`alert_id` | `message`、`id?` | 告警事件表 |
| 触发测试告警 | 日志监控与告警智能体 | POST | `/api/monitor/alerts/test` | 无需鉴权 | 无 | `id`、`title`、`summary` | 告警智能体、LLM 服务、告警事件表、WebSocket 推送 |
| 告警智能体问答 | 日志监控与告警智能体 | POST | `/api/monitor/assistant` | 无需鉴权 | JSON：`question`、`event_type?`、`path?`、`ip?` | `answer`、`context` | LLM 服务 |

## WebSocket 接口（补充）

| 接口名称 | 所属模块 | 请求方式 | 接口路径 | 鉴权方式 | 入参说明 | 出参结构 | 内部调用模块 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 告警实时推送 | 日志监控与告警智能体 | WebSocket | `/ws/alerts` | 当前未校验 | 客户端可发送任意文本作保活 | `type=alert` 的告警事件；异常时 `type=error` | 告警智能体、WebSocket 客户端集合 |
| 实时识别流 | 车辆车牌/交警手势/车主手势 | WebSocket | `/ws/stream/{module}` | 当前未校验 | `{module}`：`lpr/police/owner`；文本 JSON：`type=frame`、Base64 `data` | `type=result`、`module`、`data`；心跳返回 `pong` | 对应识别服务 |

> 鉴权“可选”表示路由接受匿名请求；传入有效 JWT 时会把记录关联至当前用户。WebSocket 鉴权尚未在当前代码中实现。
