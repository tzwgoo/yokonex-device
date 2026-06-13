from __future__ import annotations

# 依赖层只暴露设备连接与波形执行能力，事件分发规则留在应用层。
from yokonex_device.service import BluetoothService

__all__ = ["BluetoothService"]
