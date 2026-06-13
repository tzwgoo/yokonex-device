from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class BluetoothSettings:
    enabled: bool = False
    scan_timeout_seconds: int = 15
    connect_timeout_seconds: int = 20
    auto_reconnect: bool = True
    last_connected_device_id: str = ""
    last_connected_device_name: str = ""
    default_target_device_id: str = ""


@dataclass
class BluetoothDevice:
    device_id: str
    name: str
    device_type: str = "ems"
    protocol: str = "ems_v1"
    rssi: int = -48
    connected: bool = False


@dataclass
class EmsWaveformStep:
    duration_ms: int = 200
    channel_a: int = 40
    channel_a_mode: int = 1
    channel_a_frequency: int = 10
    channel_a_pulse_width: int = 5
    channel_b: int = 40
    channel_b_mode: int = 1
    channel_b_frequency: int = 10
    channel_b_pulse_width: int = 5


@dataclass
class EmsWaveform:
    id: str
    name: str
    builtin: bool = False
    editable: bool = True
    execution_mode: str = "fixed"
    loop_count: int = 1
    steps: list[EmsWaveformStep] = field(default_factory=list)


@dataclass
class ToyWaveformStep:
    """Toy 波形步骤。

    普通 Toy 使用 0-20 速度值；GCQ 灌肠机复用同一结构，
    但 motor_a 表示阀门开关，motor_b / motor_c 为 0-5 档。
    """

    duration_ms: int = 200
    motor_a: int = 0
    motor_b: int = 0
    motor_c: int = 0


@dataclass
class ToyWaveform:
    """Toy 类波形，通过 device_family 区分普通 Toy 与 GCQ。"""

    id: str
    name: str
    builtin: bool = False
    editable: bool = True
    device_family: str = "toy"
    loop_count: int = 1
    steps: list[ToyWaveformStep] = field(default_factory=list)


@dataclass
class BluetoothConfigPayload:
    """设备层配置。

    这里只保存设备连接参数与波形，不承载礼物档位、弹幕规则等应用业务。
    """

    bluetooth_settings: BluetoothSettings = field(default_factory=BluetoothSettings)
    ems_waveforms: list[EmsWaveform] = field(default_factory=list)
    toy_waveforms: list[ToyWaveform] = field(default_factory=list)


@dataclass
class BluetoothConnectionStatus:
    connected: bool = False
    device: BluetoothDevice | None = None
    battery_level: int | None = None
    message: str = "未连接"


def build_default_payload() -> BluetoothConfigPayload:
    from yokonex_device.ems_builtin_waveforms import create_defaults
    from yokonex_device.gcq_toy_builtin_waveforms import create_gcq_toy_defaults
    from yokonex_device.toy_builtin_waveforms import create_toy_defaults

    # 设备依赖层只关心设备预设波形，不关心上层事件规则。
    return BluetoothConfigPayload(
        bluetooth_settings=BluetoothSettings(),
        ems_waveforms=create_defaults(),
        toy_waveforms=[*create_toy_defaults(), *create_gcq_toy_defaults()],
    )


def payload_to_dict(payload: BluetoothConfigPayload) -> dict[str, Any]:
    return asdict(payload)
