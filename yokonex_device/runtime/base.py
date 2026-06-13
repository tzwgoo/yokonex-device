from __future__ import annotations

from typing import Protocol

from yokonex_device.models import BluetoothConnectionStatus
from yokonex_device.models import BluetoothDevice
from yokonex_device.models import EmsWaveform
from yokonex_device.models import ToyWaveform


class BluetoothRuntime(Protocol):
    backend_name: str

    async def scan(self) -> list[BluetoothDevice]:
        ...

    async def connect(self, device_id: str) -> BluetoothConnectionStatus:
        ...

    async def disconnect(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        ...

    def get_status(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        ...

    def get_devices(self) -> list[BluetoothDevice]:
        ...

    def get_overlay_payload(self, device_id: str | None = None) -> dict:
        ...

    async def play_waveform(self, device_id: str, waveform: EmsWaveform | ToyWaveform) -> None:
        ...
