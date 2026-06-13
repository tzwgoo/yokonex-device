from __future__ import annotations

import time

from yokonex_device.models import BluetoothConnectionStatus
from yokonex_device.models import BluetoothDevice
from yokonex_device.models import EmsWaveform
from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep


class MemoryBluetoothRuntime:
    backend_name = "memory"

    def __init__(self) -> None:
        self._devices: list[BluetoothDevice] = []
        self._connected_device_ids: set[str] = set()
        self._battery_levels: dict[str, int | None] = {}
        self._overlay_payloads: dict[str, dict] = {}

    async def scan(self) -> list[BluetoothDevice]:
        self._devices = [
            BluetoothDevice(
                device_id="ems-demo-001",
                name="YYC-DJ-DEMO",
                device_type="ems",
                protocol="ems_v1",
                rssi=-42,
                connected="ems-demo-001" in self._connected_device_ids,
            ),
            BluetoothDevice(
                device_id="ems-demo-002",
                name="YYC-DJ-V2-DEMO",
                device_type="ems",
                protocol="ems_v2",
                rssi=-51,
                connected="ems-demo-002" in self._connected_device_ids,
            ),
            BluetoothDevice(
                device_id="toy-demo-001",
                name="YCY-FJB-DEMO",
                device_type="toy",
                protocol="toy",
                rssi=-38,
                connected="toy-demo-001" in self._connected_device_ids,
                product_id=1,
                product_version=1,
                motor_a_mode_count=5,
                motor_b_mode_count=5,
                motor_c_mode_count=0,
            ),
            BluetoothDevice(
                device_id="gcq-toy-demo-001",
                name="YISKJ-GCQ-TOY-013-DEMO",
                device_type="toy",
                protocol="yiskj_gcq_toy_013",
                rssi=-36,
                connected="gcq-toy-demo-001" in self._connected_device_ids,
            ),
            BluetoothDevice(
                device_id="gcq-aes-demo-001",
                name="YISKJ-GCQ-AES-DEMO",
                device_type="toy",
                protocol="yiskj_gcq_v1_aes",
                rssi=-37,
                connected="gcq-aes-demo-001" in self._connected_device_ids,
            ),
        ]
        return list(self._devices)

    async def connect(self, device_id: str) -> BluetoothConnectionStatus:
        device = next((item for item in self._devices if item.device_id == device_id), None)
        if device is None:
            raise ValueError("未找到指定蓝牙设备")
        self._connected_device_ids.add(device_id)
        self._battery_levels[device_id] = 100
        self._sync_connected_flags()
        self._set_overlay_payload(
            device_id,
            connected=True,
            device_name=device.name,
            device_type=device.device_type,
            protocol=device.protocol,
            battery_level=self._battery_levels[device_id],
            product_id=device.product_id,
            product_version=device.product_version,
            motor_a_mode_count=device.motor_a_mode_count,
            motor_b_mode_count=device.motor_b_mode_count,
            motor_c_mode_count=device.motor_c_mode_count,
        )
        return BluetoothConnectionStatus(
            connected=True,
            device=device,
            battery_level=self._battery_levels[device_id],
            message=f"已连接 {device.name}",
        )

    async def disconnect(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        disconnected_device = None
        target_ids = [device_id] if device_id else list(self._connected_device_ids)
        if device_id:
            disconnected_device = next((item for item in self._devices if item.device_id == device_id), None)
        for current_device_id in target_ids:
            self._connected_device_ids.discard(current_device_id)
            self._battery_levels.pop(current_device_id, None)
            self._overlay_payloads.pop(current_device_id, None)
        self._sync_connected_flags()
        return BluetoothConnectionStatus(
            connected=bool(self._connected_device_ids),
            device=disconnected_device,
            battery_level=None,
            message="已断开蓝牙设备" if device_id is None else f"已断开蓝牙设备 {device_id}",
        )

    def get_status(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        device = self._resolve_connected_device(device_id)
        return BluetoothConnectionStatus(
            connected=device is not None,
            device=device,
            battery_level=self._battery_levels.get(device.device_id) if device is not None else None,
            message=f"已连接 {device.name}" if device is not None else "未连接",
        )

    def get_devices(self) -> list[BluetoothDevice]:
        return list(self._devices)

    def get_overlay_payload(self, device_id: str | None = None) -> dict:
        target_device_id = self._resolve_overlay_device_id(device_id)
        if not target_device_id:
            return self._build_default_overlay_payload()
        payload = self._overlay_payloads.get(target_device_id, self._build_default_overlay_payload())
        return {
            **payload,
            "history": list(payload["history"]),
        }

    async def play_waveform(
        self,
        device_id: str | EmsWaveform | ToyWaveform,
        waveform: EmsWaveform | ToyWaveform | None = None,
    ) -> None:
        if waveform is None:
            waveform = device_id
            resolved_device_id = self._resolve_overlay_device_id(None)
        else:
            resolved_device_id = str(device_id)
        if waveform is None or not waveform.steps:
            return None
        device = self._resolve_connected_device(resolved_device_id)
        if device is None:
            raise RuntimeError("当前没有已连接的蓝牙设备")

        history = list(self._overlay_payloads.get(resolved_device_id, self._build_default_overlay_payload())["history"])
        is_toy_device = device.device_type == "toy"
        for index, step in enumerate(waveform.steps, start=1):
            if is_toy_device:
                if isinstance(step, ToyWaveformStep):
                    toy_step = step
                else:
                    from yokonex_device.runtime.bleak_runtime import _ems_step_to_toy

                    toy_step = _ems_step_to_toy(step)
                history.append(
                    {
                        "motor_a": toy_step.motor_a,
                        "motor_b": toy_step.motor_b,
                        "motor_c": toy_step.motor_c,
                        "control_mode": toy_step.control_mode,
                        "fixed_mode": toy_step.fixed_mode,
                        "motor_mask": toy_step.motor_mask,
                    }
                )
                self._set_overlay_payload(
                    resolved_device_id,
                    connected=True,
                    device_name=device.name,
                    device_type=device.device_type,
                    protocol=device.protocol,
                    waveform_name=waveform.name,
                    battery_level=self._battery_levels.get(resolved_device_id),
                    motor_a=toy_step.motor_a,
                    motor_b=toy_step.motor_b,
                    motor_c=toy_step.motor_c,
                    control_mode=toy_step.control_mode,
                    fixed_mode=toy_step.fixed_mode,
                    motor_mask=toy_step.motor_mask,
                    step_index=index,
                    step_count=len(waveform.steps),
                    history=history[-90:],
                    product_id=device.product_id,
                    product_version=device.product_version,
                    motor_a_mode_count=device.motor_a_mode_count,
                    motor_b_mode_count=device.motor_b_mode_count,
                    motor_c_mode_count=device.motor_c_mode_count,
                )
                continue

            history.append(
                {
                    "channel_a": getattr(step, "channel_a", 0),
                    "channel_b": getattr(step, "channel_b", 0),
                }
            )
            self._set_overlay_payload(
                resolved_device_id,
                connected=True,
                device_name=device.name,
                device_type=device.device_type,
                protocol=device.protocol,
                waveform_name=waveform.name,
                battery_level=self._battery_levels.get(resolved_device_id),
                channel_a=getattr(step, "channel_a", 0),
                channel_b=getattr(step, "channel_b", 0),
                step_index=index,
                step_count=len(waveform.steps),
                history=history[-90:],
                product_id=device.product_id,
                product_version=device.product_version,
                motor_a_mode_count=device.motor_a_mode_count,
                motor_b_mode_count=device.motor_b_mode_count,
                motor_c_mode_count=device.motor_c_mode_count,
            )

        if is_toy_device:
            self._set_overlay_payload(
                resolved_device_id,
                connected=True,
                device_name=device.name,
                device_type=device.device_type,
                protocol=device.protocol,
                waveform_name="",
                battery_level=self._battery_levels.get(resolved_device_id),
                motor_a=0,
                motor_b=0,
                motor_c=0,
                control_mode="",
                fixed_mode=0,
                motor_mask=0,
                step_index=0,
                step_count=0,
                history=[*history[-90:], {"motor_a": 0, "motor_b": 0, "motor_c": 0}][-90:],
                product_id=device.product_id,
                product_version=device.product_version,
                motor_a_mode_count=device.motor_a_mode_count,
                motor_b_mode_count=device.motor_b_mode_count,
                motor_c_mode_count=device.motor_c_mode_count,
            )
        else:
            self._set_overlay_payload(
                resolved_device_id,
                connected=True,
                device_name=device.name,
                device_type=device.device_type,
                protocol=device.protocol,
                waveform_name="",
                battery_level=self._battery_levels.get(resolved_device_id),
                channel_a=0,
                channel_b=0,
                step_index=0,
                step_count=0,
                history=[*history[-90:], {"channel_a": 0, "channel_b": 0}][-90:],
                product_id=device.product_id,
                product_version=device.product_version,
                motor_a_mode_count=device.motor_a_mode_count,
                motor_b_mode_count=device.motor_b_mode_count,
                motor_c_mode_count=device.motor_c_mode_count,
            )
        return None

    def _resolve_connected_device(self, device_id: str | None = None) -> BluetoothDevice | None:
        if device_id:
            return next(
                (item for item in self._devices if item.device_id == device_id and item.connected),
                None,
            )
        return next((item for item in self._devices if item.connected), None)

    def _resolve_overlay_device_id(self, device_id: str | None) -> str:
        if device_id and device_id in self._connected_device_ids:
            return device_id
        for item in self._devices:
            if item.connected:
                return item.device_id
        return ""

    def _sync_connected_flags(self) -> None:
        for item in self._devices:
            item.connected = item.device_id in self._connected_device_ids

    def _set_overlay_payload(self, device_id: str, **updates) -> None:
        current_payload = self._overlay_payloads.get(device_id, self._build_default_overlay_payload())
        self._overlay_payloads[device_id] = {
            **current_payload,
            **updates,
            "updated_at": time.time(),
            "revision": int(current_payload.get("revision", 0)) + 1,
        }

    def _build_default_overlay_payload(self) -> dict:
        return {
            "connected": False,
            "device_name": "",
            "device_type": "",
            "protocol": "",
            "product_id": None,
            "product_version": None,
            "motor_a_mode_count": 0,
            "motor_b_mode_count": 0,
            "motor_c_mode_count": 0,
            "waveform_name": "",
            "battery_level": None,
            "control_mode": "",
            "fixed_mode": 0,
            "motor_mask": 0,
            "pressure_a": 0,
            "pressure_b": 0,
            "channel_a": 0,
            "channel_b": 0,
            "motor_a": 0,
            "motor_b": 0,
            "motor_c": 0,
            "step_index": 0,
            "step_count": 0,
            "updated_at": 0.0,
            "history": [],
            "revision": 0,
        }
