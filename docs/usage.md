# yokonex-device 使用说明

## 1. 依赖职责

`yokonex-device` 只处理设备层能力：

- 扫描设备
- 连接设备
- 保存设备配置
- 管理波形
- 执行波形
- 输出设备状态和 overlay 数据

不处理业务层能力：

- 事件规则
- 礼物档位
- 直播消息匹配
- 业务日志

## 2. 初始化

```python
from pathlib import Path

from yokonex_device import BluetoothService


service = BluetoothService.create_default(
    config_path=Path("config/bluetooth_settings.json"),
)
```

也可以手动注入运行时：

```python
from pathlib import Path

from yokonex_device.runtime.memory_runtime import MemoryBluetoothRuntime
from yokonex_device.service import BluetoothService
from yokonex_device.storage import BluetoothSettingsStore


service = BluetoothService(
    store=BluetoothSettingsStore(Path("config/bluetooth_settings.json")),
    runtime=MemoryBluetoothRuntime(),
)
```

## 3. 常用接口

### 扫描设备

```python
devices = await service.scan()
```

### 连接设备

```python
status = await service.connect(device_id)
```

### 断开设备

```python
await service.disconnect()
```

### 读取状态

```python
service.get_status_payload()
service.get_overlay_payload()
```

### 创建波形

```python
service.create_waveform(name="我的波形")
service.create_waveform(name="Toy 波形", device_type="toy")
service.create_waveform(name="GCQ 波形", device_type="gcq")
```

### 编辑波形

```python
service.update_waveform(
    waveform_id="custom-wave-xxxx",
    name="新的波形",
    steps=[
        {"duration_ms": 180, "channel_a": 100, "channel_b": 80},
        {"duration_ms": 220, "channel_a": 0, "channel_b": 0},
    ],
)
```

### 预览波形

```python
await service.preview_waveform("ems-preset-01")
```

### 触发波形

```python
await service.trigger_waveform(
    event_type="manual_test",
    waveform_id="ems-preset-01",
)
```

### 多设备分发

```python
await service.trigger_waveforms(
    event_type="manual_batch",
    targets=[
        {"device_id": "ems-demo-001", "waveform_id": "ems-preset-01"},
        {"device_id": "toy-demo-001", "waveform_id": "toy-preset-01"},
    ],
)
```

## 4. 推荐集成方式

推荐由上层业务仓库包装一个应用服务：

1. 上层根据直播事件命中业务规则。
2. 上层得到目标波形 ID。
3. 上层调用 `yokonex-device` 执行波形。

这样可以保持设备依赖边界干净，后续更容易单独发版。
