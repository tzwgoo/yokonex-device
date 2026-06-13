from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.ciphers import modes
from yokonex_device.models import BluetoothConnectionStatus
from yokonex_device.models import BluetoothDevice
from yokonex_device.models import EmsWaveform
from yokonex_device.models import EmsWaveformStep
from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep

try:
    from bleak import BleakClient
    from bleak import BleakScanner
except ImportError:  # pragma: no cover - exercised through runtime fallback
    BleakClient = None
    BleakScanner = None


EMS_SERVICE_UUID = "0000ff30-0000-1000-8000-00805f9b34fb"
EMS_WRITE_CHAR_UUID = "0000ff31-0000-1000-8000-00805f9b34fb"
EMS_NOTIFY_CHAR_UUID = "0000ff32-0000-1000-8000-00805f9b34fb"

TOY_SERVICE_UUID = "0000ff40-0000-1000-8000-00805f9b34fb"
TOY_WRITE_CHAR_UUID = "0000ff41-0000-1000-8000-00805f9b34fb"
TOY_NOTIFY_CHAR_UUID = "0000ff42-0000-1000-8000-00805f9b34fb"

GCQ_TOY_SERVICE_UUID = "0000ff70-0000-1000-8000-00805f9b34fb"
GCQ_TOY_WRITE_CHAR_UUID = "0000ff71-0000-1000-8000-00805f9b34fb"
GCQ_TOY_NOTIFY_CHAR_UUID = "0000ff72-0000-1000-8000-00805f9b34fb"
GCQ_AES_SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
GCQ_AES_WRITE_CHAR_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"
GCQ_AES_NOTIFY_CHAR_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"

TOY_NAME_PREFIXES = ("YCY-FJB", "YCY-TDD")
GCQ_TOY_PROTOCOL = "yiskj_gcq_toy_013"
GCQ_AES_PROTOCOL = "yiskj_gcq_v1_aes"
TOY_MOTOR_ALL = 0x07
GCQ_AES_KEY = bytes.fromhex("F638BC9CFA477480AB3242F6B04557A1")

LOGGER = logging.getLogger("bili_live.bluetooth.runtime")


@dataclass
class _RuntimeDeviceState:
    device: BluetoothDevice
    client: Any
    battery_level: int | None
    overlay_payload: dict[str, Any]
    manual_disconnect_requested: bool = False
    reconnect_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None


class BleakBluetoothRuntime:
    backend_name = "bleak"

    def __init__(
        self,
        *,
        scan_timeout_seconds: int,
        connect_timeout_seconds: float = 20,
        auto_reconnect: bool = False,
        scanner_discover: Callable[..., Awaitable[Any]] | None = None,
        client_factory: Callable[..., Any] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if scanner_discover is None:
            if BleakScanner is None:
                raise RuntimeError("未安装 bleak，无法启用真实蓝牙运行时")
            scanner_discover = BleakScanner.discover
        if client_factory is None:
            if BleakClient is None:
                raise RuntimeError("未安装 bleak，无法启用真实蓝牙运行时")
            client_factory = BleakClient
        self._scan_timeout_seconds = scan_timeout_seconds
        self._connect_timeout_seconds = max(0.01, float(connect_timeout_seconds))
        self._auto_reconnect = bool(auto_reconnect)
        self._scanner_discover = scanner_discover
        self._client_factory = client_factory
        self._sleep = sleep_func or asyncio.sleep
        self._devices: list[BluetoothDevice] = []
        self._ble_devices: dict[str, Any] = {}
        self._device_states: dict[str, _RuntimeDeviceState] = {}
        # 向后兼容旧测试和诊断逻辑，保留最近一次连接设备的 client 句柄。
        self._client: Any | None = None
        self._status_message = "未连接"

    async def scan(self) -> list[BluetoothDevice]:
        discovered = await self._scanner_discover(
            timeout=self._scan_timeout_seconds,
            return_adv=True,
        )
        devices: list[BluetoothDevice] = []
        ble_devices: dict[str, Any] = {}
        if isinstance(discovered, dict):
            values = discovered.values()
        else:
            values = ((item, SimpleNamespace(service_uuids=[])) for item in discovered)
        for ble_device, advertisement in values:
            mapped = classify_device(ble_device=ble_device, advertisement=advertisement)
            if mapped is None:
                continue
            devices.append(mapped)
            ble_devices[mapped.device_id] = ble_device
        self._devices = devices
        self._ble_devices = ble_devices
        self._sync_connected_flags()
        return list(self._devices)

    async def connect(self, device_id: str) -> BluetoothConnectionStatus:
        ble_device = self._ble_devices.get(device_id)
        device = next((item for item in self._devices if item.device_id == device_id), None)
        if ble_device is None or device is None:
            raise ValueError("未找到指定蓝牙设备")

        existing_state = self._device_states.get(device_id)
        if existing_state is not None:
            if self._is_state_connected(existing_state):
                return BluetoothConnectionStatus(
                    connected=True,
                    device=device,
                    battery_level=existing_state.battery_level,
                    message=f"已连接 {device.name}",
                )
            if existing_state.reconnect_task is not None and not existing_state.reconnect_task.done():
                existing_state.reconnect_task.cancel()
                existing_state.reconnect_task = None

        client = self._client_factory(
            ble_device,
            disconnected_callback=lambda disconnected_client, target_device_id=device_id: self._handle_disconnect(
                target_device_id,
                disconnected_client,
            ),
        )
        await asyncio.wait_for(client.connect(), timeout=self._connect_timeout_seconds)
        state = _RuntimeDeviceState(
            device=device,
            client=client,
            battery_level=None,
            overlay_payload=self._build_default_overlay_payload(),
        )
        self._device_states[device_id] = state
        self._client = client
        await self._refresh_connected_device_profile(state)
        self._sync_connected_flags()
        if not self._is_state_connected(state):
            raise RuntimeError("蓝牙设备连接失败")
        await self._initialize_device_telemetry(device_id, device)
        self._status_message = f"已连接 {device.name}"
        self._set_overlay_payload(
            device_id,
            connected=True,
            device_name=device.name,
            device_type=device.device_type,
            protocol=device.protocol,
            battery_level=state.battery_level,
            product_id=device.product_id,
            product_version=device.product_version,
            motor_a_mode_count=device.motor_a_mode_count,
            motor_b_mode_count=device.motor_b_mode_count,
            motor_c_mode_count=device.motor_c_mode_count,
        )
        return BluetoothConnectionStatus(
            connected=True,
            device=device,
            battery_level=state.battery_level,
            message=self._status_message,
        )

    async def disconnect(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        target_device_ids = [device_id] if device_id is not None else list(self._device_states)
        disconnected_device = None
        for current_device_id in target_device_ids:
            state = self._device_states.get(current_device_id)
            if state is None:
                continue
            disconnected_device = state.device
            state.manual_disconnect_requested = True
            if state.reconnect_task is not None and not state.reconnect_task.done():
                state.reconnect_task.cancel()
                state.reconnect_task = None
            if state.heartbeat_task is not None and not state.heartbeat_task.done():
                state.heartbeat_task.cancel()
                state.heartbeat_task = None
            if state.client is not None and getattr(state.client, "is_connected", False):
                stop_notify = getattr(state.client, "stop_notify", None)
                if callable(stop_notify):
                    notify_uuid = _resolve_notify_uuid(state.device)
                    try:
                        await stop_notify(notify_uuid)
                    except Exception:
                        LOGGER.debug("停止蓝牙通知失败", exc_info=True)
                await state.client.disconnect()
            self._device_states.pop(current_device_id, None)
        self._client = None if not self._device_states else next(iter(self._device_states.values())).client
        self._sync_connected_flags()
        self._status_message = "已断开蓝牙设备"
        return BluetoothConnectionStatus(
            connected=bool(self._get_connected_device_ids()),
            device=disconnected_device,
            battery_level=None,
            message=self._status_message,
        )

    def get_status(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        state = self._resolve_state(device_id)
        device = None if state is None else state.device
        return BluetoothConnectionStatus(
            connected=device is not None,
            device=device,
            battery_level=None if state is None else state.battery_level,
            message=self._status_message if self._status_message else (f"已连接 {device.name}" if device is not None else "未连接"),
        )

    def get_devices(self) -> list[BluetoothDevice]:
        return list(self._devices)

    def get_overlay_payload(self, device_id: str | None = None) -> dict:
        state = self._resolve_state(device_id)
        if state is None:
            return self._build_default_overlay_payload()
        return {
            **state.overlay_payload,
            "history": list(state.overlay_payload["history"]),
        }

    async def play_waveform(
        self,
        device_id: str | EmsWaveform | ToyWaveform,
        waveform: EmsWaveform | ToyWaveform | None = None,
    ) -> None:
        if waveform is None:
            waveform = device_id
            resolved_device_id = self._resolve_default_connected_device_id()
        else:
            resolved_device_id = str(device_id)
        if waveform is None:
            raise RuntimeError("当前没有可播放波形的设备")
        state = self._device_states.get(resolved_device_id)
        if state is None or not self._is_state_connected(state):
            raise RuntimeError("当前没有已连接的蓝牙设备")
        device = state.device
        is_toy_device = device.device_type == "toy"
        write_uuid = _resolve_write_uuid(device)
        history = list(state.overlay_payload["history"])
        toy_uses_fixed_mode = False
        try:
            if is_toy_device:
                for index, step in enumerate(waveform.steps, start=1):
                    if isinstance(step, ToyWaveformStep):
                        toy_step = step
                    else:
                        toy_step = _ems_step_to_toy(step)
                    # 灌肠机协议复用 Toy 三通道波形编辑器：
                    # motor_a 只表示气阀开关，motor_b/motor_c 直接表示气泵和水泵的 0-5 档位。
                    if device.protocol == GCQ_TOY_PROTOCOL:
                        packet = create_gcq_toy_packet(toy_step)
                        overlay_motor_a = toy_step.motor_a
                        overlay_motor_b = toy_step.motor_b
                        overlay_motor_c = toy_step.motor_c
                        control_mode = "speed"
                        fixed_mode = 0
                        motor_mask = 0
                        packets = [packet]
                    elif device.protocol == GCQ_AES_PROTOCOL:
                        packets, overlay_motor_a, overlay_motor_b, overlay_motor_c = create_gcq_aes_packets(
                            toy_step,
                            device_family=getattr(waveform, "device_family", "toy"),
                            duration_ms=max(getattr(step, "duration_ms", 200), 0),
                        )
                        control_mode = "pump_state"
                        fixed_mode = 0
                        motor_mask = 0x03
                    else:
                        control_mode = _resolve_toy_control_mode(toy_step)
                        if control_mode == "fixed_mode":
                            # FF40 协议下不同设备通过 0x10 上报马达和固定模式数量，
                            # 这里统一走 0x11 指令，避免把仅支持固定模式的设备当成实时调速设备。
                            packet = create_toy_fixed_mode_packet(toy_step)
                            overlay_motor_a, overlay_motor_b, overlay_motor_c = _resolve_toy_fixed_mode_overlay_values(toy_step)
                            fixed_mode = _clamp_toy_fixed_mode_index(toy_step.fixed_mode)
                            motor_mask = _resolve_toy_motor_mask(toy_step)
                            toy_uses_fixed_mode = True
                            packets = [packet]
                        else:
                            packet = create_toy_speed_packet(toy_step)
                            overlay_motor_a = toy_step.motor_a
                            overlay_motor_b = toy_step.motor_b
                            overlay_motor_c = toy_step.motor_c
                            fixed_mode = 0
                            motor_mask = 0
                            packets = [packet]
                    history.append(
                        {
                            "motor_a": overlay_motor_a,
                            "motor_b": overlay_motor_b,
                            "motor_c": overlay_motor_c,
                            "control_mode": control_mode,
                            "fixed_mode": fixed_mode,
                            "motor_mask": motor_mask,
                        }
                    )
                    history = history[-90:]
                    self._set_overlay_payload(
                        resolved_device_id,
                        connected=True,
                        device_name=device.name,
                        device_type=device.device_type,
                        protocol=device.protocol,
                        waveform_name=waveform.name,
                        battery_level=state.battery_level,
                        motor_a=overlay_motor_a,
                        motor_b=overlay_motor_b,
                        motor_c=overlay_motor_c,
                        control_mode=control_mode,
                        fixed_mode=fixed_mode,
                        motor_mask=motor_mask,
                        step_index=index,
                        step_count=len(waveform.steps),
                        history=history,
                        product_id=device.product_id,
                        product_version=device.product_version,
                        motor_a_mode_count=device.motor_a_mode_count,
                        motor_b_mode_count=device.motor_b_mode_count,
                        motor_c_mode_count=device.motor_c_mode_count,
                    )
                    for packet in packets:
                        await state.client.write_gatt_char(write_uuid, packet, response=False)
                    await self._sleep(max(getattr(step, "duration_ms", 200), 0) / 1000)
            else:
                packets = create_waveform_packets(waveform=waveform, protocol=device.protocol)
                for index, ((packet, duration_seconds), step) in enumerate(zip(packets, waveform.steps, strict=False), start=1):
                    history.append(
                        {
                            "channel_a": getattr(step, "channel_a", 0),
                            "channel_b": getattr(step, "channel_b", 0),
                        }
                    )
                    history = history[-90:]
                    self._set_overlay_payload(
                        resolved_device_id,
                        connected=True,
                        device_name=device.name,
                        device_type=device.device_type,
                        protocol=device.protocol,
                        waveform_name=waveform.name,
                        battery_level=state.battery_level,
                        channel_a=getattr(step, "channel_a", 0),
                        channel_b=getattr(step, "channel_b", 0),
                        step_index=index,
                        step_count=len(waveform.steps),
                        history=history,
                        product_id=device.product_id,
                        product_version=device.product_version,
                        motor_a_mode_count=device.motor_a_mode_count,
                        motor_b_mode_count=device.motor_b_mode_count,
                        motor_c_mode_count=device.motor_c_mode_count,
                    )
                    await state.client.write_gatt_char(write_uuid, packet, response=False)
                    await self._sleep(duration_seconds)
        finally:
            if is_toy_device:
                if device.protocol == GCQ_TOY_PROTOCOL:
                    stop_packet = create_gcq_toy_stop_packet()
                elif device.protocol == GCQ_AES_PROTOCOL:
                    stop_packet = create_gcq_aes_stop_packet()
                elif toy_uses_fixed_mode:
                    stop_packet = create_toy_fixed_mode_stop_packet()
                else:
                    stop_packet = create_toy_stop_packet()
            else:
                stop_packet = create_stop_packet(protocol=device.protocol)
            await state.client.write_gatt_char(write_uuid, stop_packet, response=False)
            if is_toy_device:
                self._set_overlay_payload(
                    resolved_device_id,
                    connected=True,
                    device_name=device.name,
                    device_type=device.device_type,
                    protocol=device.protocol,
                    waveform_name="",
                    battery_level=state.battery_level,
                    motor_a=0,
                    motor_b=0,
                    motor_c=0,
                    control_mode="",
                    fixed_mode=0,
                    motor_mask=0,
                    step_index=0,
                    step_count=0,
                    history=[*history, {"motor_a": 0, "motor_b": 0, "motor_c": 0, "control_mode": "", "fixed_mode": 0, "motor_mask": 0}][-90:],
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
                    battery_level=state.battery_level,
                    channel_a=0,
                    channel_b=0,
                    step_index=0,
                    step_count=0,
                    history=[*history, {"channel_a": 0, "channel_b": 0}][-90:],
                    product_id=device.product_id,
                    product_version=device.product_version,
                    motor_a_mode_count=device.motor_a_mode_count,
                    motor_b_mode_count=device.motor_b_mode_count,
                    motor_c_mode_count=device.motor_c_mode_count,
                )

    def _handle_disconnect(self, device_id: str, _client: Any) -> None:
        state = self._device_states.get(device_id)
        if state is None:
            return
        previous_device_name = state.device.name
        state.client = None
        state.battery_level = None
        if state.heartbeat_task is not None and not state.heartbeat_task.done():
            state.heartbeat_task.cancel()
            state.heartbeat_task = None
        self._sync_connected_flags()
        if state.manual_disconnect_requested:
            LOGGER.info("蓝牙设备已主动断开 device_id=%s name=%s", device_id, previous_device_name)
            return
        self._client = None if not self._device_states else next(iter(self._device_states.values())).client
        self._status_message = f"蓝牙设备已断开: {previous_device_name or device_id or '未知设备'}"
        LOGGER.warning(
            "蓝牙设备连接断开 device_id=%s name=%s auto_reconnect=%s",
            device_id,
            previous_device_name,
            self._auto_reconnect,
        )
        self._set_overlay_payload(
            device_id,
            connected=False,
            device_name=state.device.name,
            device_type=state.device.device_type,
            waveform_name="",
            battery_level=None,
            channel_a=0,
            channel_b=0,
            motor_a=0,
            motor_b=0,
            motor_c=0,
            step_index=0,
            step_count=0,
            history=[],
        )
        if self._auto_reconnect and (state.reconnect_task is None or state.reconnect_task.done()):
            self._status_message = f"蓝牙设备已断开，正在尝试重连 {previous_device_name or device_id}"
            state.reconnect_task = asyncio.create_task(self._attempt_reconnect(device_id))

    def _sync_connected_flags(self) -> None:
        connected_ids = set(self._get_connected_device_ids())
        for item in self._devices:
            item.connected = item.device_id in connected_ids

    async def _attempt_reconnect(self, device_id: str) -> None:
        state = self._device_states.get(device_id)
        if state is None:
            return
        device_name = state.device.name
        try:
            await self._sleep(1.5)
            ble_device = self._ble_devices.get(device_id)
            device = next((item for item in self._devices if item.device_id == device_id), None)
            if ble_device is None or device is None:
                self._status_message = f"蓝牙设备已断开，且无法找到设备进行重连: {device_name or device_id}"
                LOGGER.warning("蓝牙自动重连失败，设备已不存在 device_id=%s name=%s", device_id, device_name)
                return
            client = self._client_factory(
                ble_device,
                disconnected_callback=lambda disconnected_client, target_device_id=device_id: self._handle_disconnect(
                    target_device_id,
                    disconnected_client,
                ),
            )
            await asyncio.wait_for(client.connect(), timeout=self._connect_timeout_seconds)
            state.client = client
            self._client = client
            state.manual_disconnect_requested = False
            await self._refresh_connected_device_profile(state)
            self._sync_connected_flags()
            if not self._is_state_connected(state):
                raise RuntimeError("蓝牙自动重连后状态仍未连接")
            await self._initialize_device_telemetry(device_id, device)
            self._status_message = f"蓝牙已自动重连 {device.name}"
            LOGGER.info("蓝牙自动重连成功 device_id=%s name=%s", device_id, device.name)
            self._set_overlay_payload(
                device_id,
                connected=True,
                device_name=device.name,
                device_type=device.device_type,
                battery_level=state.battery_level,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status_message = f"蓝牙自动重连失败: {exc}"
            LOGGER.warning("蓝牙自动重连失败 device_id=%s name=%s error=%s", device_id, device_name, exc)
        finally:
            state = self._device_states.get(device_id)
            if state is not None:
                state.reconnect_task = None

    def _set_overlay_payload(self, device_id: str, **updates) -> None:
        state = self._device_states.get(device_id)
        if state is None:
            return
        state.overlay_payload = {
            **state.overlay_payload,
            **updates,
            "updated_at": time.time(),
            "revision": int(state.overlay_payload.get("revision", 0)) + 1,
        }

    async def _refresh_connected_device_profile(self, state: _RuntimeDeviceState) -> None:
        """连接成功后按真实 GATT 服务再次识别设备，避免广播缺字段时误判协议。"""
        client = state.client
        if client is None or not getattr(client, "is_connected", False):
            return
        service_uuids = await _load_connected_service_uuids(client)
        if not service_uuids:
            return
        state.device = _apply_connected_service_profile(state.device, service_uuids)

    async def _initialize_device_telemetry(self, device_id: str, device: BluetoothDevice) -> None:
        state = self._device_states.get(device_id)
        if state is None:
            return
        state.battery_level = None
        client = state.client
        if client is None or not getattr(client, "is_connected", False):
            return
        if device.protocol == GCQ_AES_PROTOCOL:
            start_notify = getattr(client, "start_notify", None)
            if callable(start_notify):
                await start_notify(
                    GCQ_AES_NOTIFY_CHAR_UUID,
                    lambda sender, data, target_device_id=device_id: self._dispatch_notify_callback(
                        self._handle_gcq_aes_notify(target_device_id, sender, data),
                    ),
                )
            # 按协议先主动查询当前双泵状态与电量，建立连接后即可同步到状态面板。
            await client.write_gatt_char(
                GCQ_AES_WRITE_CHAR_UUID,
                _build_gcq_aes_status_query_packet(),
                response=False,
            )
            await client.write_gatt_char(
                GCQ_AES_WRITE_CHAR_UUID,
                _build_gcq_aes_battery_query_packet(),
                response=False,
            )
            return
        if device.protocol == GCQ_TOY_PROTOCOL:
            start_notify = getattr(client, "start_notify", None)
            if callable(start_notify):
                await start_notify(
                    GCQ_TOY_NOTIFY_CHAR_UUID,
                    lambda sender, data, target_device_id=device_id: self._dispatch_notify_callback(
                        self._handle_gcq_toy_notify(target_device_id, sender, data),
                    ),
                )
            await client.write_gatt_char(
                GCQ_TOY_WRITE_CHAR_UUID,
                _build_gcq_toy_status_query(),
                response=False,
            )
            await client.write_gatt_char(
                GCQ_TOY_WRITE_CHAR_UUID,
                _build_gcq_toy_battery_query(),
                response=False,
            )
            if state.heartbeat_task is not None and not state.heartbeat_task.done():
                state.heartbeat_task.cancel()
            state.heartbeat_task = asyncio.create_task(self._run_gcq_toy_heartbeat(device_id))
            return
        if device.device_type == "toy":
            start_notify = getattr(client, "start_notify", None)
            if callable(start_notify):
                await start_notify(
                    TOY_NOTIFY_CHAR_UUID,
                    lambda sender, data, target_device_id=device_id: self._dispatch_notify_callback(
                        self._handle_toy_notify(target_device_id, sender, data),
                    ),
                )
            await client.write_gatt_char(
                TOY_WRITE_CHAR_UUID,
                _build_toy_device_info_query(),
                response=False,
            )
            return
        if device.device_type != "ems":
            return
        start_notify = getattr(client, "start_notify", None)
        if not callable(start_notify):
            return
        await start_notify(
            EMS_NOTIFY_CHAR_UUID,
            lambda sender, data, target_device_id=device_id: self._dispatch_notify_callback(
                self._handle_notify(target_device_id, sender, data),
            ),
        )
        await client.write_gatt_char(
            EMS_WRITE_CHAR_UUID,
            _build_ems_query_packet(0x04),
            response=False,
        )

    def _dispatch_notify_callback(self, result: Any) -> None:
        """把 Bleak 的同步通知回调桥接到异步处理逻辑，避免协程对象被直接丢弃。"""
        if not inspect.isawaitable(result):
            return
        task = asyncio.create_task(result)
        task.add_done_callback(self._consume_notify_task_result)

    def _consume_notify_task_result(self, task: asyncio.Task[Any]) -> None:
        """统一兜底通知处理异常，避免后台任务报错后静默丢失电量更新。"""
        try:
            task.result()
        except Exception:
            LOGGER.warning("蓝牙通知处理失败", exc_info=True)

    async def _handle_notify(self, device_id: str, _sender: Any, data: bytearray) -> None:
        battery_level = _try_parse_ems_battery_level(bytes(data))
        if battery_level is None:
            return
        state = self._device_states.get(device_id)
        if state is None:
            return
        state.battery_level = battery_level
        self._set_overlay_payload(
            device_id,
            connected=True,
            device_name=state.device.name,
            device_type=state.device.device_type,
            protocol=state.device.protocol,
            battery_level=battery_level,
            product_id=state.device.product_id,
            product_version=state.device.product_version,
            motor_a_mode_count=state.device.motor_a_mode_count,
            motor_b_mode_count=state.device.motor_b_mode_count,
            motor_c_mode_count=state.device.motor_c_mode_count,
        )

    async def _handle_toy_notify(self, device_id: str, _sender: Any, data: bytearray) -> None:
        parsed = _try_parse_toy_notify(bytes(data))
        if parsed is None:
            return
        state = self._device_states.get(device_id)
        if state is None:
            return
        if parsed.get("type") == "battery":
            state.battery_level = parsed["level"]
        elif parsed.get("type") == "device_info":
            # FF40 协议通过设备信息包上报产品和马达能力，后续上层可按这些能力决定展示与下发方式。
            state.device.product_id = parsed.get("product_id")
            state.device.product_version = parsed.get("product_version")
            state.device.motor_a_mode_count = max(0, int(parsed.get("motor_a_mode_count", 0) or 0))
            state.device.motor_b_mode_count = max(0, int(parsed.get("motor_b_mode_count", 0) or 0))
            state.device.motor_c_mode_count = max(0, int(parsed.get("motor_c_mode_count", 0) or 0))
        self._set_overlay_payload(
            device_id,
            connected=True,
            device_name=state.device.name,
            device_type=state.device.device_type,
            protocol=state.device.protocol,
            battery_level=state.battery_level,
            product_id=state.device.product_id,
            product_version=state.device.product_version,
            motor_a_mode_count=state.device.motor_a_mode_count,
            motor_b_mode_count=state.device.motor_b_mode_count,
            motor_c_mode_count=state.device.motor_c_mode_count,
        )

    async def _handle_gcq_toy_notify(self, device_id: str, _sender: Any, data: bytearray) -> None:
        parsed = _try_parse_gcq_toy_notify(bytes(data))
        if parsed is None:
            return
        state = self._device_states.get(device_id)
        if state is None:
            return
        updates: dict[str, Any] = {
            "connected": True,
            "device_name": state.device.name,
            "device_type": state.device.device_type,
            "protocol": state.device.protocol,
            "battery_level": state.battery_level,
            "product_id": state.device.product_id,
            "product_version": state.device.product_version,
            "motor_a_mode_count": state.device.motor_a_mode_count,
            "motor_b_mode_count": state.device.motor_b_mode_count,
            "motor_c_mode_count": state.device.motor_c_mode_count,
        }
        if parsed.get("type") == "battery":
            state.battery_level = parsed["level"]
            updates["battery_level"] = state.battery_level
        elif parsed.get("type") == "status":
            # 设备待机时也会持续上报当前状态，这里只在正在播放波形时刷新叠加窗强度，
            # 避免停止播放后又被设备状态包把当前强度覆盖成非零。
            if state.overlay_payload.get("waveform_name"):
                # 灌肠机按真实设备语义展示：气阀只有开/关，气泵和水泵只有 0-5 档。
                updates["motor_a"] = 1 if parsed.get("valve_open", False) else 0
                updates["motor_b"] = _clamp_gcq_level(parsed.get("air_pump_level", 0))
                updates["motor_c"] = _clamp_gcq_level(parsed.get("water_pump_level", 0))
        self._set_overlay_payload(device_id, **updates)

    async def _handle_gcq_aes_notify(self, device_id: str, _sender: Any, data: bytearray) -> None:
        parsed = _try_parse_gcq_aes_notify(bytes(data))
        if parsed is None:
            return
        state = self._device_states.get(device_id)
        if state is None:
            return
        updates: dict[str, Any] = {
            "connected": True,
            "device_name": state.device.name,
            "device_type": state.device.device_type,
            "protocol": state.device.protocol,
            "battery_level": state.battery_level,
            "product_id": state.device.product_id,
            "product_version": state.device.product_version,
            "motor_a_mode_count": state.device.motor_a_mode_count,
            "motor_b_mode_count": state.device.motor_b_mode_count,
            "motor_c_mode_count": state.device.motor_c_mode_count,
        }
        if parsed.get("type") == "battery":
            state.battery_level = parsed["level"]
            updates["battery_level"] = state.battery_level
        elif parsed.get("type") == "status":
            updates["motor_a"] = _clamp_gcq_aes_peristaltic_state(parsed.get("peristaltic_state", 0))
            updates["motor_b"] = _clamp_gcq_aes_water_state(parsed.get("water_state", 0))
        elif parsed.get("type") == "pressure":
            updates["pressure_a"] = _clamp_pressure_value(parsed.get("pressure_a", 0))
            updates["pressure_b"] = _clamp_pressure_value(parsed.get("pressure_b", 0))
        self._set_overlay_payload(device_id, **updates)

    async def _run_gcq_toy_heartbeat(self, device_id: str) -> None:
        """灌肠机协议要求主机每秒发送一次心跳，避免设备在长连接空闲时主动掉线。"""
        try:
            while True:
                state = self._device_states.get(device_id)
                if state is None or state.client is None or not getattr(state.client, "is_connected", False):
                    return
                await state.client.write_gatt_char(
                    GCQ_TOY_WRITE_CHAR_UUID,
                    _build_gcq_toy_heartbeat_packet(),
                    response=False,
                )
                await self._sleep(1.0)
        except asyncio.CancelledError:
            raise

    def _resolve_state(self, device_id: str | None = None) -> _RuntimeDeviceState | None:
        if device_id is not None:
            state = self._device_states.get(device_id)
            if state is not None and self._is_state_connected(state):
                return state
            return None
        for current_device_id in self._get_connected_device_ids():
            state = self._device_states.get(current_device_id)
            if state is not None and self._is_state_connected(state):
                return state
        return None

    def _resolve_default_connected_device_id(self) -> str:
        connected_device_ids = self._get_connected_device_ids()
        return "" if not connected_device_ids else connected_device_ids[0]

    def _get_connected_device_ids(self) -> list[str]:
        return [
            device_id
            for device_id, state in self._device_states.items()
            if self._is_state_connected(state)
        ]

    def _is_state_connected(self, state: _RuntimeDeviceState) -> bool:
        return state.client is not None and bool(getattr(state.client, "is_connected", False))

    def _build_default_overlay_payload(self) -> dict[str, Any]:
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


def classify_ems_device(*, ble_device: Any, advertisement: Any) -> BluetoothDevice | None:
    """向后兼容别名，委托给 classify_device。"""
    return classify_device(ble_device=ble_device, advertisement=advertisement)


def classify_device(*, ble_device: Any, advertisement: Any) -> BluetoothDevice | None:
    """分类蓝牙广播设备，返回 BluetoothDevice 或 None。"""
    service_uuids = _normalize_service_uuids(getattr(advertisement, "service_uuids", []))
    name = (
        getattr(advertisement, "local_name", None)
        or getattr(ble_device, "name", None)
        or getattr(ble_device, "address", "")
    )
    name_upper = str(name).upper()

    if GCQ_AES_SERVICE_UUID in service_uuids:
        return BluetoothDevice(
            device_id=str(getattr(ble_device, "address", "")),
            name=str(name),
            device_type="toy",
            protocol=GCQ_AES_PROTOCOL,
            rssi=int(getattr(ble_device, "rssi", getattr(advertisement, "rssi", -60)) or -60),
            connected=False,
        )

    if GCQ_TOY_SERVICE_UUID in service_uuids:
        return BluetoothDevice(
            device_id=str(getattr(ble_device, "address", "")),
            name=str(name),
            device_type="toy",
            protocol=GCQ_TOY_PROTOCOL,
            rssi=int(getattr(ble_device, "rssi", getattr(advertisement, "rssi", -60)) or -60),
            connected=False,
        )

    if TOY_SERVICE_UUID in service_uuids or any(name_upper.startswith(prefix) for prefix in TOY_NAME_PREFIXES):
        return BluetoothDevice(
            device_id=str(getattr(ble_device, "address", "")),
            name=str(name),
            device_type="toy",
            protocol="toy",
            rssi=int(getattr(ble_device, "rssi", getattr(advertisement, "rssi", -60)) or -60),
            connected=False,
        )

    if EMS_SERVICE_UUID not in service_uuids and not name_upper.startswith("YYC-DJ"):
        return None
    protocol = "ems_v2"
    if name_upper.startswith("YYC-DJ-V2"):
        protocol = "ems_v2"
    elif name_upper.startswith("YYC-DJ"):
        protocol = "ems_v1"
    return BluetoothDevice(
        device_id=str(getattr(ble_device, "address", "")),
        name=str(name),
        device_type="ems",
        protocol=protocol,
        rssi=int(getattr(ble_device, "rssi", getattr(advertisement, "rssi", -60)) or -60),
        connected=False,
    )


def create_waveform_packets(*, waveform: EmsWaveform, protocol: str) -> list[tuple[bytes, float]]:
    packets: list[tuple[bytes, float]] = []
    for step in waveform.steps:
        if protocol == "ems_v1":
            packet = _create_v1_packet(step)
        elif str(waveform.execution_mode).lower() == "realtime":
            packet = _create_v2_realtime_packet(step)
        else:
            packet = _create_v2_fixed_packet(step)
        packets.append((packet, max(step.duration_ms, 0) / 1000))
    return packets


def create_stop_packet(*, protocol: str) -> bytes:
    if protocol == "ems_v1":
        return _create_v1_stop_packet()
    return _create_v2_fixed_packet(
        EmsWaveformStep(duration_ms=0, channel_a=0, channel_b=0),
    )


def _create_v1_packet(step: EmsWaveformStep) -> bytes:
    channel = _resolve_v1_channel(step)
    enabled = 0x01 if channel != 0x00 else 0x00
    use_channel_b = channel == 0x02 or step.channel_b > step.channel_a
    strength = step.channel_b if use_channel_b else step.channel_a
    mode = step.channel_b_mode if use_channel_b else step.channel_a_mode
    frequency = step.channel_b_frequency if use_channel_b else step.channel_a_frequency
    pulse_width = step.channel_b_pulse_width if use_channel_b else step.channel_a_pulse_width
    bytes_list = [
        0x35,
        0x11,
        channel,
        enabled,
        _high(strength),
        _low(strength),
        mode,
        frequency if mode == 0x11 else 0x00,
        pulse_width if mode == 0x11 else 0x00,
    ]
    bytes_list.append(_compute_checksum(bytes_list))
    return bytes(bytes_list)


def _create_v2_fixed_packet(step: EmsWaveformStep) -> bytes:
    bytes_list = [
        0x35,
        0x11,
        0x01,
        _high(step.channel_a),
        _low(step.channel_a),
        step.channel_a_mode,
        _high(step.channel_b),
        _low(step.channel_b),
        step.channel_b_mode,
    ]
    bytes_list.append(_compute_checksum(bytes_list))
    return bytes(bytes_list)


def _create_v2_realtime_packet(step: EmsWaveformStep) -> bytes:
    bytes_list = [
        0x35,
        0x11,
        0x02,
        _high(step.channel_a),
        _low(step.channel_a),
        step.channel_a_frequency,
        step.channel_a_pulse_width,
        _high(step.channel_b),
        _low(step.channel_b),
        step.channel_b_frequency,
        step.channel_b_pulse_width,
    ]
    bytes_list.append(_compute_checksum(bytes_list))
    return bytes(bytes_list)


def _create_v1_stop_packet() -> bytes:
    bytes_list = [
        0x35,
        0x11,
        0x03,
        0x00,
        0x00,
        0x01,
        0x01,
        0x00,
        0x00,
    ]
    bytes_list.append(_compute_checksum(bytes_list))
    return bytes(bytes_list)


def _resolve_v1_channel(step: EmsWaveformStep) -> int:
    a_enabled = step.channel_a > 0
    b_enabled = step.channel_b > 0
    if a_enabled and b_enabled:
        return 0x03
    if a_enabled:
        return 0x01
    if b_enabled:
        return 0x02
    return 0x00


def _normalize_service_uuids(service_uuids: Iterable[str] | None) -> set[str]:
    if service_uuids is None:
        return set()
    return {str(item).lower() for item in service_uuids if item}


async def _load_connected_service_uuids(client: Any) -> set[str]:
    direct_services = _extract_service_uuids_from_services(getattr(client, "services", None))
    if direct_services:
        return direct_services

    get_services = getattr(client, "get_services", None)
    if not callable(get_services):
        return set()
    services = await get_services()
    return _extract_service_uuids_from_services(services)


def _extract_service_uuids_from_services(services: Any) -> set[str]:
    if services is None:
        return set()
    if isinstance(services, dict):
        candidates = services.values()
    elif hasattr(services, "values") and callable(getattr(services, "values", None)):
        candidates = services.values()
    else:
        candidates = services

    uuids: set[str] = set()
    try:
        for item in candidates:
            uuid = getattr(item, "uuid", item)
            if uuid:
                uuids.add(str(uuid).lower())
    except TypeError:
        return set()
    return uuids


def _apply_connected_service_profile(device: BluetoothDevice, service_uuids: set[str]) -> BluetoothDevice:
    if GCQ_AES_SERVICE_UUID in service_uuids:
        device.device_type = "toy"
        device.protocol = GCQ_AES_PROTOCOL
        return device
    if GCQ_TOY_SERVICE_UUID in service_uuids:
        device.device_type = "toy"
        device.protocol = GCQ_TOY_PROTOCOL
        return device
    if TOY_SERVICE_UUID in service_uuids:
        device.device_type = "toy"
        device.protocol = "toy"
        return device
    if EMS_SERVICE_UUID in service_uuids:
        device.device_type = "ems"
        device.protocol = _resolve_ems_protocol_by_name(device.name)
    return device


def _resolve_ems_protocol_by_name(name: str) -> str:
    name_upper = str(name or "").upper()
    if name_upper.startswith("YYC-DJ-V2"):
        return "ems_v2"
    if name_upper.startswith("YYC-DJ"):
        return "ems_v1"
    return "ems_v2"


def _high(value: int) -> int:
    clipped = max(0, min(int(value), 0xFFFF))
    return (clipped >> 8) & 0xFF


def _low(value: int) -> int:
    clipped = max(0, min(int(value), 0xFFFF))
    return clipped & 0xFF


def _compute_checksum(values: Iterable[int]) -> int:
    total = 0
    for item in values:
        total = (total + item) & 0xFF
    return total


def _build_ems_query_packet(query_type: int) -> bytes:
    values = [0x35, 0x71, max(0, min(int(query_type), 0xFF))]
    values.append(_compute_checksum(values))
    return bytes(values)


def _try_parse_ems_battery_level(packet: bytes) -> int | None:
    if len(packet) < 4 or packet[0] != 0x35 or packet[1] != 0x71 or packet[2] != 0x04:
        return None
    return max(0, min(int(packet[3]), 100))


def create_toy_speed_packet(step: ToyWaveformStep) -> bytes:
    """构建 Toy 实时速率控制包 35 12 motor_a motor_b motor_c checksum。"""
    values = [0x35, 0x12, _clamp_toy_speed(step.motor_a), _clamp_toy_speed(step.motor_b), _clamp_toy_speed(step.motor_c)]
    values.append(_compute_checksum(values))
    return bytes(values)


def create_toy_fixed_mode_packet(step: ToyWaveformStep) -> bytes:
    """构建 Toy 固定模式控制包 35 11 motor_mask fixed_mode checksum。"""
    values = [0x35, 0x11, _resolve_toy_motor_mask(step), _clamp_toy_fixed_mode_index(step.fixed_mode)]
    values.append(_compute_checksum(values))
    return bytes(values)


def create_gcq_toy_packet(step: ToyWaveformStep) -> bytes:
    """构建灌肠机实时控制包 35 12 valve air_pump water_pump checksum。"""
    values = [
        0x35,
        0x12,
        0xFF if _clamp_gcq_valve_state(step.motor_a) > 0 else 0x00,
        _clamp_gcq_level(step.motor_b),
        _clamp_gcq_level(step.motor_c),
    ]
    values.append(_compute_checksum(values))
    return bytes(values)


def create_toy_stop_packet() -> bytes:
    """构建 Toy 停止包，所有马达速度归零。"""
    return create_toy_speed_packet(ToyWaveformStep())


def create_toy_fixed_mode_stop_packet() -> bytes:
    """构建 Toy 固定模式停止包，关闭所有马达固定模式。"""
    return create_toy_fixed_mode_packet(ToyWaveformStep(control_mode="fixed_mode", fixed_mode=0, motor_mask=TOY_MOTOR_ALL))


def create_gcq_toy_stop_packet() -> bytes:
    """构建灌肠机停止包，关闭气阀、气泵和水泵。"""
    return create_gcq_toy_packet(ToyWaveformStep())


def create_gcq_aes_packets(step: ToyWaveformStep, *, device_family: str, duration_ms: int) -> tuple[list[bytes], int, int, int]:
    """构建 FFB0 灌肠机一代协议的双泵控制包。"""
    seconds = _clamp_gcq_aes_duration_seconds(duration_ms)
    family = str(device_family or "toy").strip().lower()
    if family == "gcq_aes":
        peristaltic_state = _clamp_gcq_aes_peristaltic_state(step.motor_a)
        water_state = _clamp_gcq_aes_water_state(step.motor_b)
    elif family == "gcq":
        peristaltic_state = 1 if int(step.motor_b) > 0 else 0
        water_state = 1 if int(step.motor_c) > 0 else 0
    else:
        peristaltic_state = 1 if int(step.motor_a) > 0 else 0
        water_state = 1 if int(step.motor_b) > 0 else 0
    packets = [
        _build_gcq_aes_pump_command_packet(command=0x01, state=peristaltic_state, seconds=seconds),
        _build_gcq_aes_pump_command_packet(command=0x02, state=water_state, seconds=seconds),
    ]
    return packets, peristaltic_state, water_state, 0


def create_gcq_aes_stop_packet() -> bytes:
    """构建 FFB0 灌肠机一代协议的全停指令。"""
    return _build_gcq_aes_simple_command_packet(0x03)


def _build_toy_device_info_query() -> bytes:
    """构建 Toy 设备信息查询包 35 10 checksum。"""
    values = [0x35, 0x10]
    values.append(_compute_checksum(values))
    return bytes(values)


def _build_gcq_toy_status_query() -> bytes:
    values = [0x35, 0x13, 0x00, 0x00, 0x00]
    values.append(_compute_checksum(values))
    return bytes(values)


def _build_gcq_toy_battery_query() -> bytes:
    values = [0x35, 0x14, 0x00, 0x00, 0x00]
    values.append(_compute_checksum(values))
    return bytes(values)


def _build_gcq_toy_heartbeat_packet() -> bytes:
    values = [0x35, 0x17, 0x00, 0x00, 0x00]
    values.append(_compute_checksum(values))
    return bytes(values)


def _build_gcq_aes_pump_command_packet(*, command: int, state: int, seconds: int, filler: bytes | None = None) -> bytes:
    plaintext = _build_gcq_aes_plaintext(
        0xA0,
        command,
        bytes([max(0, min(int(state), 0xFF)), _high(seconds), _low(seconds)]),
        filler=filler,
    )
    return _encrypt_gcq_aes_packet(plaintext)


def _build_gcq_aes_simple_command_packet(command: int, filler: bytes | None = None) -> bytes:
    plaintext = _build_gcq_aes_plaintext(0xA0, command, b"", filler=filler)
    return _encrypt_gcq_aes_packet(plaintext)


def _build_gcq_aes_status_query_packet() -> bytes:
    return _build_gcq_aes_simple_command_packet(0x04)


def _build_gcq_aes_battery_query_packet() -> bytes:
    return _build_gcq_aes_simple_command_packet(0x05)


def _build_gcq_aes_plaintext(prefix: int, command: int, payload: bytes, *, filler: bytes | None = None) -> bytes:
    header = bytes([0xBF, 0x0F, max(0, min(int(prefix), 0xFF)), max(0, min(int(command), 0xFF))])
    normalized_payload = bytes(payload or b"")
    pad_length = 16 - len(header) - len(normalized_payload)
    if pad_length < 0:
        raise ValueError("GCQ AES 指令长度超出 16 字节限制")
    random_filler = filler if filler is not None else secrets.token_bytes(pad_length)
    random_filler = bytes(random_filler[:pad_length]).ljust(pad_length, b"\x00")
    return header + normalized_payload + random_filler


def _encrypt_gcq_aes_packet(plaintext: bytes) -> bytes:
    if len(plaintext) != 16:
        raise ValueError("GCQ AES 明文长度必须为 16 字节")
    cipher = Cipher(algorithms.AES(GCQ_AES_KEY), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _decrypt_gcq_aes_packet(ciphertext: bytes) -> bytes | None:
    if len(ciphertext) != 16:
        return None
    cipher = Cipher(algorithms.AES(GCQ_AES_KEY), modes.ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _clamp_toy_speed(value: int) -> int:
    return max(0, min(int(value), 20))


def _clamp_toy_fixed_mode_index(value: int) -> int:
    return max(0, min(int(value), 255))


def _clamp_toy_motor_mask(value: int) -> int:
    return max(0, min(int(value), TOY_MOTOR_ALL))


def _resolve_toy_control_mode(step: ToyWaveformStep) -> str:
    normalized = str(getattr(step, "control_mode", "") or "").strip().lower()
    if normalized in {"fixed", "fixed_mode", "mode", "pattern"}:
        return "fixed_mode"
    if _clamp_toy_fixed_mode_index(getattr(step, "fixed_mode", 0)) > 0:
        return "fixed_mode"
    return "speed"


def _resolve_toy_motor_mask(step: ToyWaveformStep) -> int:
    raw_mask = _clamp_toy_motor_mask(getattr(step, "motor_mask", 0))
    if raw_mask > 0:
        return raw_mask
    derived_mask = 0
    if int(getattr(step, "motor_a", 0) or 0) > 0:
        derived_mask |= 0x01
    if int(getattr(step, "motor_b", 0) or 0) > 0:
        derived_mask |= 0x02
    if int(getattr(step, "motor_c", 0) or 0) > 0:
        derived_mask |= 0x04
    return derived_mask or TOY_MOTOR_ALL


def _resolve_toy_fixed_mode_overlay_values(step: ToyWaveformStep) -> tuple[int, int, int]:
    mode_value = max(1, min(_clamp_toy_fixed_mode_index(step.fixed_mode), 20))
    motor_mask = _resolve_toy_motor_mask(step)
    return (
        mode_value if motor_mask & 0x01 else 0,
        mode_value if motor_mask & 0x02 else 0,
        mode_value if motor_mask & 0x04 else 0,
    )


def _clamp_gcq_valve_state(value: int) -> int:
    return 1 if int(value) > 0 else 0


def _clamp_gcq_level(value: int) -> int:
    return max(0, min(int(value), 5))


def _clamp_gcq_aes_peristaltic_state(value: int) -> int:
    return max(0, min(int(value), 2))


def _clamp_gcq_aes_water_state(value: int) -> int:
    return max(0, min(int(value), 1))


def _clamp_gcq_aes_duration_seconds(duration_ms: int) -> int:
    return max(0, min(int(round(max(0, int(duration_ms)) / 1000)), 0xFFFF))


def _clamp_pressure_value(value: Any) -> int:
    return max(0, min(int(value), 0xFFFF))


def _ems_step_to_toy(step: EmsWaveformStep) -> ToyWaveformStep:
    """把 EMS 波形步转换为 Toy 马达步，强度 0-180 映射到速度 0-20。"""
    motor_a = int(step.channel_a / 180 * 20)
    motor_b = int(step.channel_b / 180 * 20)
    return ToyWaveformStep(
        duration_ms=max(1, step.duration_ms),
        motor_a=motor_a,
        motor_b=motor_b,
        motor_c=0,
    )


def _try_parse_toy_notify(data: bytes) -> dict | None:
    """解析 Toy 设备通知包。"""
    if len(data) < 3 or data[0] != 0x35:
        return None
    cmd = data[1]
    if cmd == 0x13 and len(data) >= 5 and data[2] == 0x01:
        return {"type": "battery", "level": max(0, min(int(data[3]), 100))}
    if cmd == 0x10 and len(data) >= 10:
        return {
            "type": "device_info",
            "product_id": int(data[2]),
            "product_version": int(data[3]),
            "motor_a_mode_count": int(data[4]),
            "motor_b_mode_count": int(data[5]),
            "motor_c_mode_count": int(data[6]),
        }
    if cmd == 0x14:
        return {"type": "heartbeat"}
    return None


def _try_parse_gcq_aes_notify(data: bytes) -> dict | None:
    plaintext = _decrypt_gcq_aes_packet(data)
    if plaintext is None or len(plaintext) != 16:
        return None
    if plaintext[0] != 0xBF or plaintext[1] != 0x0F or plaintext[2] != 0xB0:
        return None
    command = plaintext[3]
    if command == 0x01:
        return {
            "type": "status",
            "peristaltic_state": _clamp_gcq_aes_peristaltic_state(plaintext[4]),
            "water_state": _clamp_gcq_aes_water_state(plaintext[5]),
        }
    if command == 0x02:
        return {
            "type": "pressure",
            "pressure_a": (int(plaintext[4]) << 8) | int(plaintext[5]),
            "pressure_b": (int(plaintext[6]) << 8) | int(plaintext[7]),
        }
    if command == 0x03:
        return {
            "type": "battery",
            "level": max(0, min(int(plaintext[4]), 100)),
        }
    return None


def _try_parse_gcq_toy_notify(data: bytes) -> dict | None:
    """解析灌肠机设备通知包。"""
    if len(data) < 3 or data[0] != 0x35:
        return None
    cmd = data[1]
    if cmd == 0x13 and len(data) >= 6:
        return {
            "type": "status",
            "valve_open": data[2] == 0xFF,
            "air_pump_level": max(0, min(int(data[3]), 5)),
            "water_pump_level": max(0, min(int(data[4]), 5)),
        }
    if cmd == 0x14 and len(data) >= 4:
        return {"type": "battery", "level": max(0, min(int(data[2]), 100))}
    if cmd == 0x15 and len(data) >= 8:
        return {
            "type": "sensor",
            "air_pressure": (int(data[2]) << 8) | int(data[3]),
            "water_pressure": (int(data[4]) << 8) | int(data[5]),
            "water_temperature": int(data[6]),
        }
    return None


def _resolve_write_uuid(device: BluetoothDevice) -> str:
    if device.protocol == GCQ_AES_PROTOCOL:
        return GCQ_AES_WRITE_CHAR_UUID
    if device.protocol == GCQ_TOY_PROTOCOL:
        return GCQ_TOY_WRITE_CHAR_UUID
    if device.device_type == "toy":
        return TOY_WRITE_CHAR_UUID
    return EMS_WRITE_CHAR_UUID


def _resolve_notify_uuid(device: BluetoothDevice) -> str:
    if device.protocol == GCQ_AES_PROTOCOL:
        return GCQ_AES_NOTIFY_CHAR_UUID
    if device.protocol == GCQ_TOY_PROTOCOL:
        return GCQ_TOY_NOTIFY_CHAR_UUID
    if device.device_type == "toy":
        return TOY_NOTIFY_CHAR_UUID
    return EMS_NOTIFY_CHAR_UUID
