# YOLO11s-Pose + LSTM 训练报告

## 训练配置

- 姿态模型：`yolo11s-pose.pt`
- 数据集：`C:\Users\baiwu\PoliceGestureLong`
- 训练视频：8 个
- 验证视频：2 个
- 独立测试视频：11 个
- 特征：CTPGR 原版 13 个骨长、6 个夹角余弦、6 个夹角正弦
- 序列长度：450 帧
- 标签延迟：15 帧
- 损失：按类别频次平方根加权的交叉熵
- 优化器：AdamW
- 最佳轮次：31
- 早停轮次：39

## 模型文件

- 原模型：`database/ctpgr-pytorch-master/checkpoints/lstm.pt`
- 原模型备份：`database/ctpgr-pytorch-master/checkpoints/backups/lstm_before_yolo11s_20260711.pt`
- 新模型：`database/ctpgr-pytorch-master/checkpoints/lstm_yolo11s.pt`
- 原模型及备份 SHA-256：`CBC6B8A4323D269065EC585EC2C3B1BC01C934E9016BA5E7346771FAF97CA2CB`
- 新模型 SHA-256：`2046E96B1E6EF986F5BAD4A823CEBA3D890131CA2BE83EC903C088042C7D30C8`

## 评估结果

在相同的 YOLO11s 测试关键点缓存上：

| 模型 | Macro-F1 |
|---|---:|
| 原 `lstm.pt` | 0.5402 |
| 新 `lstm_yolo11s.pt` | 0.8886 |

新模型逐类指标：

| 类别 | Precision | Recall | F1 |
|---|---:|---:|---:|
| 无手势 | 0.9548 | 0.8622 | 0.9062 |
| 停止 | 0.8480 | 0.9050 | 0.8756 |
| 直行 | 0.8663 | 0.9533 | 0.9077 |
| 左转 | 0.7904 | 0.9498 | 0.8628 |
| 左转待转 | 0.7421 | 0.9234 | 0.8229 |
| 右转 | 0.8551 | 0.8989 | 0.8765 |
| 变道 | 0.8995 | 0.9549 | 0.9264 |
| 减速 | 0.8310 | 0.9762 | 0.8978 |
| 靠边停车 | 0.8904 | 0.9556 | 0.9218 |

缓存和完整 JSON 报告位于 `database/ctpgr-pytorch-master/generated/coords_yolo11s` 与 `database/ctpgr-pytorch-master/generated/yolo11s_lstm_report.json`。这些生成文件不纳入 Git。
