# Vehicle Vision System

车载视觉感知与人机交互系统的主项目位于：

```text
database/vehicle-vision-system/
```

该项目包含四个业务模块：

- 车辆车牌识别
- 交警手势识别
- 车主手势控车
- 日志监控与告警智能体

## 启动

```powershell
cd database/vehicle-vision-system
pip install -r requirements.txt
python setup_security.py
python run.py
```

Windows 用户也可以进入该目录后运行 `start.bat`。

每台电脑首次运行一次 `python setup_security.py` 完成本机密钥和 HTTPS 证书初始化。
默认访问地址为 <https://localhost:8001>，API 文档位于
<https://localhost:8001/api/docs>。

详细功能、配置和目录说明请参阅
[`database/vehicle-vision-system/README.md`](database/vehicle-vision-system/README.md)。

仓库中的 `database/CCPD-master`、`database/ctpgr-pytorch-master` 和
`database/hagrid-master` 是各视觉模块使用或参考的数据集与模型项目。
