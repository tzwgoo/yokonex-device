# yokonex-device

`yokonex-device` 是一个面向 YOKONEX 设备的独立 Python SDK，提供蓝牙扫描、连接、波形管理、波形执行和实时遥测能力。

GitHub 仓库：

- [tzwgoo/yokonex-device](https://github.com/tzwgoo/yokonex-device)

## 适用场景

这个仓库适合在以下场景中复用：

- 桌面控制台需要连接 YOKONEX 设备
- 直播互动项目需要把事件映射成设备波形
- 本地工具需要直接管理 EMS / Toy / GCQ 波形
- 需要独立测试蓝牙运行时、设备状态和波形执行逻辑

## 能力范围

SDK 当前负责这些设备层能力：

- 蓝牙设备扫描
- 蓝牙连接与断开
- 设备状态读取
- EMS / Toy / GCQ 波形模型
- 内置波形与自定义波形管理
- 单设备 / 多设备波形执行
- overlay 遥测数据输出
- 设备配置与波形配置持久化

## 不负责的内容

这个 SDK 刻意不处理业务层规则，以下能力应放在上层应用：

- 礼物档位
- 价格区间映射
- 弹幕关键词匹配
- 直播事件分发
- OBS 最近事件聚合展示

推荐做法是：

1. 上层项目先完成业务事件匹配。
2. 上层项目得到目标波形 ID。
3. 上层项目调用 `yokonex-device` 执行目标波形。

## 仓库结构

```text
yokonex-device/
  README.md
  LICENSE
  pyproject.toml
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

### 从源码安装

普通安装：

```bash
pip install .
```

开发安装：

```bash
pip install -e .[dev]
```

### 从 PyPI 安装

如果该版本已经发布到 PyPI，可以直接安装：

```bash
pip install yokonex-device
```

## 快速开始

### 1. 创建服务

```python
from pathlib import Path

from yokonex_device import BluetoothService


service = BluetoothService.create_default(
    config_path=Path("config/bluetooth_settings.json"),
)
```

### 2. 扫描并连接设备

```python
devices = await service.scan()
status = await service.connect(devices[0].device_id)

print(status.connected)
print(status.device)
```

### 3. 触发波形

```python
result = await service.trigger_waveform(
    event_type="manual_test",
    waveform_id="ems-preset-01",
)
print(result)
```

### 4. 读取状态

```python
status_payload = service.get_status_payload()
overlay_payload = service.get_overlay_payload()
```

更完整的接入方式见 [docs/usage.md](./docs/usage.md)。

## 常用接口

`BluetoothService` 当前提供这些核心方法：

- `create_default(config_path, event_hub=None)`
- `scan()`
- `connect(device_id)`
- `disconnect(device_id=None)`
- `get_connected_devices()`
- `get_status_payload()`
- `get_overlay_payload(device_id=None)`
- `get_studio_payload()`
- `create_waveform(name, device_type="ems")`
- `duplicate_waveform(source_waveform_id, name)`
- `update_waveform(waveform_id, name, steps)`
- `delete_waveform(waveform_id)`
- `preview_waveform(waveform_id, device_id=None)`
- `trigger_waveform(event_type, waveform_id, device_id=None, publish=True)`
- `trigger_waveforms(event_type, targets)`

## 开发与验证

运行测试：

```bash
python -m pytest
```

构建产物：

```bash
python -m build
```

当前仓库已经验证过独立测试和构建流程。

## 发布

### 手动发布

```bash
python -m build
python -m twine upload dist/*
```

### GitHub Actions 自动发布

仓库内置了 PyPI 发布工作流：

- [publish.yml](./.github/workflows/publish.yml)

发布方式：

1. 更新 `pyproject.toml` 里的版本号
2. 提交并推送代码
3. 打 tag，例如：

```bash
git tag v0.1.1
git push origin v0.1.1
```

工作流会先测试、再构建、最后通过 PyPI Trusted Publisher 发布。

## 许可证

本项目使用 [MIT License](./LICENSE)。
