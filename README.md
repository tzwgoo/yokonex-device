# yokonex-device

`yokonex-device` 是一个面向 YOKONEX 设备的独立 Python 仓库，负责：

- 蓝牙设备扫描
- 蓝牙连接与断开
- 设备状态读取
- EMS / Toy / GCQ 波形模型
- 自定义波形与内置波形管理
- 单设备 / 多设备波形执行
- overlay 遥测数据输出

这个仓库刻意只保留设备层能力，不包含直播业务规则，例如：

- 礼物档位
- 价格区间映射
- 弹幕关键词匹配
- 直播事件分发
- OBS 最近事件列表聚合

这些能力应放在业务项目中，由上层项目计算出目标波形后，再调用本仓库提供的接口。

## 仓库结构

```text
yokonex-device/
  README.md
  pyproject.toml
  .gitignore
  docs/
    usage.md
  tests/
    test_bleak_runtime.py
    test_service.py
    test_storage.py
  yokonex_device/
    __init__.py
    models.py
    service.py
    storage.py
    ems_builtin_waveforms.py
    toy_builtin_waveforms.py
    gcq_toy_builtin_waveforms.py
    runtime/
      base.py
      bleak_runtime.py
      memory_runtime.py
```

## 安装

开发安装：

```bash
pip install -e .[dev]
```

普通安装：

```bash
pip install .
```

## 快速开始

```python
from pathlib import Path

from yokonex_device import BluetoothService


service = BluetoothService.create_default(
    config_path=Path("config/bluetooth_settings.json"),
)
```

扫描与连接：

```python
devices = await service.scan()
status = await service.connect(devices[0].device_id)
print(status.connected, status.device)
```

触发波形：

```python
result = await service.trigger_waveform(
    event_type="manual_test",
    waveform_id="ems-preset-01",
)
print(result)
```

更多接入方式见 [docs/usage.md](./docs/usage.md)。

## 发布

构建：

```bash
python -m build
```

测试：

```bash
python -m pytest
```

上传到 PyPI：

```bash
python -m twine upload dist/*
```

## 与主仓库的关系

这个目录是从 `Bililive-YOKONEX` 中拆出的独立仓库骨架，适合作为新仓库初始化起点：

1. 进入 `standalone/yokonex-device`
2. 执行 `git init`
3. 关联新的远端仓库
4. 提交并推送

如果后续还需要从主仓库继续同步设备层能力，建议把同步边界控制在：

- `yokonex_device/`
- `docs/usage.md`
- `tests/test_bleak_runtime.py`
- 设备层专属测试
