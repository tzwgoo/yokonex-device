from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import time
import uuid
from typing import Any

from yokonex_device.models import BluetoothConfigPayload
from yokonex_device.models import BluetoothConnectionStatus
from yokonex_device.models import BluetoothDevice
from yokonex_device.models import EmsWaveform
from yokonex_device.models import EmsWaveformStep
from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep
from yokonex_device.models import payload_to_dict
from yokonex_device.runtime.base import BluetoothRuntime
from yokonex_device.runtime.memory_runtime import MemoryBluetoothRuntime
from yokonex_device.storage import BluetoothSettingsStore


logger = logging.getLogger(__name__)


@dataclass
class _ActiveWaveformState:
    task: asyncio.Task[None] | None = None
    request_id: str = ""
    waveform_id: str = ""
    strength: int = -1
    deadline: float = 0.0


class BluetoothService:
    def __init__(
        self,
        *,
        store: Any,
        runtime: BluetoothRuntime,
        payload: BluetoothConfigPayload | None = None,
        event_hub: Any | None = None,
    ) -> None:
        # 设备层只负责连接参数与波形持久化，不处理上层直播事件规则。
        self.store = store
        self.runtime = runtime
        self.payload = payload or self.store.load()
        self.event_hub = event_hub
        self._waveform_lock = asyncio.Lock()
        self._active_waveforms: dict[str, _ActiveWaveformState] = {}

    @classmethod
    def create_default(cls, *, config_path: Path, event_hub: Any | None = None) -> "BluetoothService":
        store = BluetoothSettingsStore(config_path)
        payload = store.load()
        try:
            runtime = create_real_bluetooth_runtime(
                scan_timeout_seconds=payload.bluetooth_settings.scan_timeout_seconds,
                connect_timeout_seconds=payload.bluetooth_settings.connect_timeout_seconds,
                auto_reconnect=payload.bluetooth_settings.auto_reconnect,
            )
        except Exception as exc:  # pragma: no cover - fallback covered by tests
            logger.warning("真实蓝牙运行时初始化失败，已降级到内存运行时: %s", exc)
            runtime = MemoryBluetoothRuntime()
        return cls(
            store=store,
            runtime=runtime,
            payload=payload,
            event_hub=event_hub,
        )

    async def scan(self) -> list[BluetoothDevice]:
        try:
            devices = await self.runtime.scan()
        except TimeoutError:
            raise RuntimeError("蓝牙扫描超时，请重试") from None
        except Exception as exc:
            raise RuntimeError(_resolve_scan_error_message(exc)) from exc
        return devices

    async def connect(self, device_id: str) -> BluetoothConnectionStatus:
        try:
            status = await self.runtime.connect(device_id)
        except Exception as exc:
            self._publish_bluetooth_connection_control(
                success=False,
                device_id=device_id,
                device_name="",
                message=str(exc) or "蓝牙连接失败",
            )
            raise
        if status.device is not None:
            self.payload.bluetooth_settings.last_connected_device_id = status.device.device_id
            self.payload.bluetooth_settings.last_connected_device_name = status.device.name
            self.payload.bluetooth_settings.default_target_device_id = status.device.device_id
            self.store.save(self.payload)
        self._publish_bluetooth_connection_control(
            success=True,
            device_id="" if status.device is None else status.device.device_id,
            device_name="" if status.device is None else status.device.name,
            message=status.message,
        )
        return status

    async def disconnect(self, device_id: str | None = None) -> BluetoothConnectionStatus:
        return await self.runtime.disconnect(device_id)

    def get_connected_devices(self) -> list[BluetoothDevice]:
        return [
            item
            for item in self.runtime.get_devices()
            if getattr(item, "connected", False)
        ]

    async def trigger_waveform(
        self,
        *,
        event_type: str,
        waveform_id: str,
        device_id: str | None = None,
        publish: bool = True,
    ) -> dict:
        resolved_device_id = self._resolve_runtime_waveform_device_id(device_id)
        waveform = self._find_waveform_any(waveform_id)
        waveform_strength = _resolve_waveform_max_strength(waveform)
        waveform_duration_seconds = _resolve_waveform_duration_seconds(waveform)
        request_id = uuid.uuid4().hex
        task_to_await: asyncio.Task[None] | None = None
        async with self._waveform_lock:
            state = self._get_active_waveform_state(resolved_device_id)
            self._cleanup_finished_waveform_task(resolved_device_id)
            now = time.monotonic()
            if state.task is not None:
                # 同一设备同一时刻只保留最强波形，弱波形直接忽略；相同波形则延长持续时间。
                if waveform_strength <= state.strength:
                    if waveform_id == state.waveform_id:
                        state.deadline = max(state.deadline, now) + waveform_duration_seconds
                        result = self._build_trigger_result(
                            event_type=event_type,
                            waveform=waveform,
                            waveform_id=waveform_id,
                            waveform_strength=waveform_strength,
                            success=True,
                            message=f"{event_type} 已为当前波形追加 {waveform_duration_seconds:.2f} 秒时长",
                            device_id=resolved_device_id,
                        )
                        if publish:
                            self._publish_bluetooth_control(result)
                        return result
                    result = self._build_trigger_result(
                        event_type=event_type,
                        waveform=waveform,
                        waveform_id=waveform_id,
                        waveform_strength=waveform_strength,
                        success=True,
                        message=f"当前设备已有更高强度波形执行中，已忽略 {event_type} 触发",
                        device_id=resolved_device_id,
                    )
                    if publish:
                        self._publish_bluetooth_control(result)
                    return result
                previous_task = state.task
                state.request_id = request_id
                state.waveform_id = waveform_id
                state.strength = waveform_strength
                state.deadline = now + waveform_duration_seconds
                task_to_await = asyncio.create_task(
                    self._run_waveform_until_deadline(
                        waveform,
                        device_id=resolved_device_id,
                        request_id=request_id,
                    )
                )
                state.task = task_to_await
                previous_task.cancel()
            else:
                state.request_id = request_id
                state.waveform_id = waveform_id
                state.strength = waveform_strength
                state.deadline = now + waveform_duration_seconds
                task_to_await = asyncio.create_task(
                    self._run_waveform_until_deadline(
                        waveform,
                        device_id=resolved_device_id,
                        request_id=request_id,
                    )
                )
                state.task = task_to_await
        try:
            await task_to_await
        except asyncio.CancelledError:
            async with self._waveform_lock:
                state = self._get_active_waveform_state(resolved_device_id)
                if state.request_id != request_id:
                    result = self._build_trigger_result(
                        event_type=event_type,
                        waveform=waveform,
                        waveform_id=waveform_id,
                        waveform_strength=waveform_strength,
                        success=True,
                        message=f"{event_type} 波形已被更高强度事件抢占",
                        device_id=resolved_device_id,
                    )
                    if publish:
                        self._publish_bluetooth_control(result)
                    return result
            raise
        except Exception as exc:
            result = self._build_trigger_result(
                event_type=event_type,
                waveform=waveform,
                waveform_id=waveform_id,
                waveform_strength=waveform_strength,
                success=False,
                message=f"波形执行失败: {exc}",
                device_id=resolved_device_id,
            )
            if publish:
                self._publish_bluetooth_control(result)
            return result
        finally:
            async with self._waveform_lock:
                state = self._get_active_waveform_state(resolved_device_id)
                if state.request_id == request_id:
                    self._reset_active_waveform_state(resolved_device_id)
        result = self._build_trigger_result(
            event_type=event_type,
            waveform=waveform,
            waveform_id=waveform_id,
            waveform_strength=waveform_strength,
            success=True,
            message=f"{event_type} 已触发波形 {waveform.name}",
            device_id=resolved_device_id,
        )
        if publish:
            self._publish_bluetooth_control(result)
        return result

    async def trigger_waveforms(
        self,
        *,
        event_type: str,
        targets: list[dict[str, str]],
    ) -> dict:
        if not targets:
            return {
                "matched": False,
                "success": False,
                "message": "当前没有可触发的已连接设备",
                "targets": [],
            }
        results = await asyncio.gather(
            *[
                self.trigger_waveform(
                    event_type=event_type,
                    waveform_id=target["waveform_id"],
                    device_id=target["device_id"],
                    publish=False,
                )
                for target in targets
            ]
        )
        first_result = results[0]
        aggregate = {
            "matched": True,
            "success": any(item.get("success", False) for item in results),
            "event_type": event_type,
            "waveform_id": first_result.get("waveform_id", ""),
            "waveform_name": first_result.get("waveform_name", ""),
            "max_strength": max((int(item.get("max_strength", 0) or 0) for item in results), default=0),
            "device_id": first_result.get("device_id", ""),
            "device_ids": [item.get("device_id", "") for item in results],
            "targets": results,
            "message": f"{event_type} 已向 {len(results)} 台设备分发波形",
        }
        self._publish_bluetooth_control(aggregate)
        return aggregate

    async def preview_waveform(self, waveform_id: str, device_id: str | None = None) -> dict:
        waveform = self._find_waveform_any(waveform_id)
        target_device_id = self._resolve_runtime_waveform_device_id(device_id)
        try:
            await self._play_waveform_with_runtime_compat(target_device_id, waveform)
        except Exception as exc:
            result = self._build_trigger_result(
                event_type="waveform_preview",
                waveform=waveform,
                waveform_id=waveform_id,
                waveform_strength=_resolve_waveform_max_strength(waveform),
                success=False,
                message=f"测试播放失败: {exc}",
                device_id=target_device_id,
            )
            self._publish_bluetooth_control(result)
            return result
        result = self._build_trigger_result(
            event_type="waveform_preview",
            waveform=waveform,
            waveform_id=waveform_id,
            waveform_strength=_resolve_waveform_max_strength(waveform),
            success=True,
            message=f"已测试播放波形 {waveform.name}",
            device_id=target_device_id,
        )
        self._publish_bluetooth_control(result)
        return result

    def get_status_payload(self) -> dict:
        devices = self.runtime.get_devices()
        connected_devices = [item for item in devices if getattr(item, "connected", False)]
        primary_device_id = self._resolve_target_device_id()
        primary_status = self.runtime.get_status(primary_device_id)
        payload_dict = payload_to_dict(self.payload)
        return {
            "runtime_backend": getattr(self.runtime, "backend_name", "unknown"),
            "enabled": self.payload.bluetooth_settings.enabled,
            "connected": bool(connected_devices),
            "connected_count": len(connected_devices),
            "connected_device_ids": [item.device_id for item in connected_devices],
            "battery_level": primary_status.battery_level,
            "message": self._resolve_status_message(primary_status=primary_status, connected_devices=connected_devices),
            "device": None if primary_status.device is None else {
                "device_id": primary_status.device.device_id,
                "name": primary_status.device.name,
                "device_type": primary_status.device.device_type,
                "protocol": primary_status.device.protocol,
                "rssi": primary_status.device.rssi,
                "product_id": primary_status.device.product_id,
                "product_version": primary_status.device.product_version,
                "motor_a_mode_count": primary_status.device.motor_a_mode_count,
                "motor_b_mode_count": primary_status.device.motor_b_mode_count,
                "motor_c_mode_count": primary_status.device.motor_c_mode_count,
            },
            "devices": [
                self._build_device_status_payload(item)
                for item in devices
            ],
            "ems_waveforms": payload_dict["ems_waveforms"],
            "toy_waveforms": payload_dict["toy_waveforms"],
        }

    def get_overlay_payload(self, device_id: str | None = None) -> dict:
        connected_devices = self.get_connected_devices()
        filtered_devices = connected_devices
        if device_id:
            filtered_devices = [
                item
                for item in connected_devices
                if item.device_id == device_id
            ]
        overlay_devices = [
            self._build_overlay_device_payload(item)
            for item in filtered_devices
        ]
        target_device_id = "" if not overlay_devices else overlay_devices[0]["device_id"]
        payload = self.runtime.get_overlay_payload(target_device_id)
        status = self.runtime.get_status(target_device_id) if target_device_id else self.runtime.get_status()
        active_state = self._active_waveforms.get(target_device_id or "")
        active_strength = -1 if active_state is None else active_state.strength
        overlay_revision = sum(max(0, int(item.get("revision", 0) or 0)) for item in overlay_devices)
        if overlay_revision <= 0:
            overlay_revision = max(0, int(payload.get("revision", 0) or 0))
        return {
            "device_id": str(target_device_id or ""),
            "connected": bool(overlay_devices),
            "connected_count": len(overlay_devices),
            "device_name": str(payload.get("device_name", "") or ""),
            "device_type": str(payload.get("device_type", "") or ""),
            "protocol": str(
                payload.get("protocol", "")
                or ("" if status.device is None else status.device.protocol)
                or ""
            ),
            "product_id": payload.get("product_id", None if status.device is None else status.device.product_id),
            "product_version": payload.get("product_version", None if status.device is None else status.device.product_version),
            "motor_a_mode_count": int(
                payload.get("motor_a_mode_count", 0 if status.device is None else status.device.motor_a_mode_count) or 0
            ),
            "motor_b_mode_count": int(
                payload.get("motor_b_mode_count", 0 if status.device is None else status.device.motor_b_mode_count) or 0
            ),
            "motor_c_mode_count": int(
                payload.get("motor_c_mode_count", 0 if status.device is None else status.device.motor_c_mode_count) or 0
            ),
            "waveform_name": str(payload.get("waveform_name", "") or ""),
            "battery_level": _normalize_battery_level(
                payload.get("battery_level", status.battery_level),
            ),
            "display_max_strength": _resolve_overlay_display_max_strength(
                payload=payload,
                active_waveform_strength=active_strength,
            ),
            "control_mode": str(payload.get("control_mode", "") or ""),
            "fixed_mode": max(0, int(payload.get("fixed_mode", 0) or 0)),
            "motor_mask": max(0, int(payload.get("motor_mask", 0) or 0)),
            "devices": overlay_devices,
            "channel_a": max(0, int(payload.get("channel_a", 0) or 0)),
            "channel_b": max(0, int(payload.get("channel_b", 0) or 0)),
            "motor_a": max(0, int(payload.get("motor_a", 0) or 0)),
            "motor_b": max(0, int(payload.get("motor_b", 0) or 0)),
            "motor_c": max(0, int(payload.get("motor_c", 0) or 0)),
            "pressure_a": max(0, int(payload.get("pressure_a", 0) or 0)),
            "pressure_b": max(0, int(payload.get("pressure_b", 0) or 0)),
            "step_index": max(0, int(payload.get("step_index", 0) or 0)),
            "step_count": max(0, int(payload.get("step_count", 0) or 0)),
            "updated_at": float(payload.get("updated_at", 0) or 0),
            "history": [
                {
                    **item,
                    "channel_a": max(0, int(item.get("channel_a", 0) or 0)),
                    "channel_b": max(0, int(item.get("channel_b", 0) or 0)),
                    "motor_a": max(0, int(item.get("motor_a", 0) or 0)),
                    "motor_b": max(0, int(item.get("motor_b", 0) or 0)),
                    "motor_c": max(0, int(item.get("motor_c", 0) or 0)),
                    "pressure_a": max(0, int(item.get("pressure_a", 0) or 0)),
                    "pressure_b": max(0, int(item.get("pressure_b", 0) or 0)),
                    "control_mode": str(item.get("control_mode", "") or ""),
                    "fixed_mode": max(0, int(item.get("fixed_mode", 0) or 0)),
                    "motor_mask": max(0, int(item.get("motor_mask", 0) or 0)),
                }
                for item in payload.get("history", [])
                if isinstance(item, dict)
            ][-90:],
            "revision": overlay_revision,
        }

    def get_studio_payload(self) -> dict:
        payload_dict = payload_to_dict(self.payload)
        return {
            "ems_waveforms": payload_dict["ems_waveforms"],
            "toy_waveforms": payload_dict["toy_waveforms"],
        }

    def create_waveform(self, *, name: str, device_type: str = "ems") -> dict:
        if device_type in {"toy", "gcq", "gcq_aes"}:
            waveform = ToyWaveform(
                id=_generate_custom_waveform_id(self.payload),
                name=str(name or "").strip() or "自定义波形",
                builtin=False,
                editable=True,
                device_family="gcq_aes" if device_type == "gcq_aes" else ("gcq" if device_type == "gcq" else "toy"),
                loop_count=1,
                steps=[ToyWaveformStep(duration_ms=200, motor_a=0, motor_b=0, motor_c=0, control_mode="speed", fixed_mode=0, motor_mask=0)],
            )
            self.payload.toy_waveforms.insert(0, waveform)
        else:
            waveform = EmsWaveform(
                id=_generate_custom_waveform_id(self.payload),
                name=str(name or "").strip() or "自定义波形",
                builtin=False,
                editable=True,
                execution_mode="fixed",
                loop_count=1,
                steps=[EmsWaveformStep(duration_ms=200, channel_a=0, channel_b=0)],
            )
            self.payload.ems_waveforms.insert(0, waveform)
        self.store.save(self.payload)
        return _build_waveform_mutation_response(self.payload, waveform)

    def duplicate_waveform(self, *, source_waveform_id: str, name: str) -> dict:
        source = self._find_waveform_any(source_waveform_id)
        if isinstance(source, ToyWaveform):
            duplicated = ToyWaveform(
                id=_generate_custom_waveform_id(self.payload),
                name=str(name or "").strip() or f"{source.name} - 副本",
                builtin=False,
                editable=True,
                device_family=source.device_family,
                loop_count=source.loop_count,
                steps=[_clone_toy_waveform_step(step) for step in source.steps],
            )
            self.payload.toy_waveforms.insert(0, duplicated)
        else:
            duplicated = EmsWaveform(
                id=_generate_custom_waveform_id(self.payload),
                name=str(name or "").strip() or f"{source.name} - 副本",
                builtin=False,
                editable=True,
                execution_mode=source.execution_mode,
                loop_count=source.loop_count,
                steps=[_clone_waveform_step(step) for step in source.steps],
            )
            self.payload.ems_waveforms.insert(0, duplicated)
        self.store.save(self.payload)
        return _build_waveform_mutation_response(self.payload, duplicated)

    def update_waveform(self, *, waveform_id: str, name: str, steps: list[dict]) -> dict:
        waveform = self._find_waveform_any(waveform_id)
        if waveform.builtin:
            raise ValueError("内置波形不支持直接编辑")
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("波形名称不能为空")
        if isinstance(waveform, ToyWaveform):
            normalized_steps = _merge_toy_editable_steps(
                existing_steps=waveform.steps,
                incoming_steps=steps,
                device_family=waveform.device_family,
            )
        else:
            normalized_steps = _merge_editable_steps(
                existing_steps=waveform.steps,
                incoming_steps=steps,
            )
        waveform.name = normalized_name
        waveform.steps = normalized_steps
        self.store.save(self.payload)
        return _build_waveform_mutation_response(self.payload, waveform)

    def delete_waveform(self, waveform_id: str) -> dict:
        waveform = self._find_waveform_any(waveform_id)
        if waveform.builtin:
            raise ValueError("内置波形不支持删除")
        if isinstance(waveform, ToyWaveform):
            self.payload.toy_waveforms[:] = [
                item
                for item in self.payload.toy_waveforms
                if item.id != waveform_id
            ]
        else:
            self.payload.ems_waveforms[:] = [
                item
                for item in self.payload.ems_waveforms
                if item.id != waveform_id
            ]
        self.store.save(self.payload)
        payload_dict = payload_to_dict(self.payload)
        return {
            "success": True,
            "deleted_waveform_id": waveform_id,
            "ems_waveforms": payload_dict["ems_waveforms"],
            "toy_waveforms": payload_dict["toy_waveforms"],
        }

    def _find_waveform_any(self, waveform_id: str) -> EmsWaveform | ToyWaveform:
        waveform = next((item for item in self.payload.ems_waveforms if item.id == waveform_id), None)
        if waveform is not None:
            return waveform
        waveform = next((item for item in self.payload.toy_waveforms if item.id == waveform_id), None)
        if waveform is not None:
            return waveform
        raise ValueError(f"未找到波形 {waveform_id}")

    def _publish_bluetooth_control(self, payload: dict[str, Any]) -> None:
        if self.event_hub is None or not hasattr(self.event_hub, "publish_control"):
            return
        self.event_hub.publish_control(
            {
                "type": "bluetooth_trigger",
                "timestamp": int(time.time()),
                "payload": payload,
            }
        )

    def _publish_bluetooth_connection_control(
        self,
        *,
        success: bool,
        device_id: str,
        device_name: str,
        message: str,
    ) -> None:
        if self.event_hub is None or not hasattr(self.event_hub, "publish_control"):
            return
        self.event_hub.publish_control(
            {
                "type": "bluetooth_connect",
                "timestamp": int(time.time()),
                "payload": {
                    "success": bool(success),
                    "device_id": str(device_id or ""),
                    "device_name": str(device_name or ""),
                    "message": str(message or ("蓝牙连接成功" if success else "蓝牙连接失败")),
                },
            }
        )

    def _cleanup_finished_waveform_task(self, device_id: str) -> None:
        state = self._active_waveforms.get(device_id)
        if state is None or state.task is None or not state.task.done():
            return
        self._reset_active_waveform_state(device_id)

    async def _run_waveform_until_deadline(
        self,
        waveform: EmsWaveform | ToyWaveform,
        *,
        device_id: str,
        request_id: str,
    ) -> None:
        # 同一请求可能因为续时而重复执行多轮，这里统一复用相同的截止时间控制。
        while True:
            await self._play_waveform_with_runtime_compat(device_id, waveform)
            async with self._waveform_lock:
                state = self._get_active_waveform_state(device_id)
                if state.request_id != request_id:
                    return
                remaining_seconds = state.deadline - time.monotonic()
            if remaining_seconds <= 0:
                return

    def _get_active_waveform_state(self, device_id: str) -> _ActiveWaveformState:
        return self._active_waveforms.setdefault(device_id, _ActiveWaveformState())

    def _reset_active_waveform_state(self, device_id: str) -> None:
        self._active_waveforms[device_id] = _ActiveWaveformState()

    def _resolve_target_device_id(self, device_id: str | None = None) -> str:
        connected_devices = self.get_connected_devices()
        connected_device_ids = {item.device_id for item in connected_devices}
        if device_id and device_id in connected_device_ids:
            return device_id
        default_device_id = str(self.payload.bluetooth_settings.default_target_device_id or "")
        if default_device_id in connected_device_ids:
            return default_device_id
        return "" if not connected_devices else connected_devices[0].device_id

    def _resolve_runtime_waveform_device_id(self, device_id: str | None = None) -> str:
        resolved_device_id = self._resolve_target_device_id(device_id)
        if resolved_device_id:
            return resolved_device_id
        return str(device_id or "__default__")

    def _resolve_status_message(
        self,
        *,
        primary_status: BluetoothConnectionStatus,
        connected_devices: list[BluetoothDevice],
    ) -> str:
        if not connected_devices:
            return primary_status.message
        if len(connected_devices) == 1:
            return primary_status.message
        return f"已连接 {len(connected_devices)} 台设备"

    def _build_device_status_payload(self, device: BluetoothDevice) -> dict:
        status = self.runtime.get_status(device.device_id)
        active_state = self._active_waveforms.get(device.device_id)
        return {
            "device_id": device.device_id,
            "name": device.name,
            "device_type": device.device_type,
            "protocol": device.protocol,
            "rssi": device.rssi,
            "connected": device.connected,
            "product_id": device.product_id,
            "product_version": device.product_version,
            "motor_a_mode_count": device.motor_a_mode_count,
            "motor_b_mode_count": device.motor_b_mode_count,
            "motor_c_mode_count": device.motor_c_mode_count,
            "battery_level": status.battery_level,
            "active_waveform_id": "" if active_state is None else active_state.waveform_id,
            "active_waveform_strength": -1 if active_state is None else active_state.strength,
        }

    def _build_overlay_device_payload(self, device: BluetoothDevice) -> dict:
        payload = self.runtime.get_overlay_payload(device.device_id)
        status = self.runtime.get_status(device.device_id)
        active_state = self._active_waveforms.get(device.device_id)
        active_strength = -1 if active_state is None else active_state.strength
        return {
            "device_id": device.device_id,
            "device_name": str(payload.get("device_name", device.name) or device.name),
            "device_type": str(payload.get("device_type", device.device_type) or device.device_type),
            "protocol": str(payload.get("protocol", device.protocol) or device.protocol),
            "product_id": payload.get("product_id", device.product_id),
            "product_version": payload.get("product_version", device.product_version),
            "motor_a_mode_count": int(payload.get("motor_a_mode_count", device.motor_a_mode_count) or 0),
            "motor_b_mode_count": int(payload.get("motor_b_mode_count", device.motor_b_mode_count) or 0),
            "motor_c_mode_count": int(payload.get("motor_c_mode_count", device.motor_c_mode_count) or 0),
            "waveform_name": str(payload.get("waveform_name", "") or ""),
            "battery_level": _normalize_battery_level(
                payload.get("battery_level", status.battery_level),
            ),
            "display_max_strength": _resolve_overlay_display_max_strength(
                payload=payload,
                active_waveform_strength=active_strength,
            ),
            "control_mode": str(payload.get("control_mode", "") or ""),
            "fixed_mode": max(0, int(payload.get("fixed_mode", 0) or 0)),
            "motor_mask": max(0, int(payload.get("motor_mask", 0) or 0)),
            "channel_a": max(0, int(payload.get("channel_a", 0) or 0)),
            "channel_b": max(0, int(payload.get("channel_b", 0) or 0)),
            "motor_a": max(0, int(payload.get("motor_a", 0) or 0)),
            "motor_b": max(0, int(payload.get("motor_b", 0) or 0)),
            "motor_c": max(0, int(payload.get("motor_c", 0) or 0)),
            "pressure_a": max(0, int(payload.get("pressure_a", 0) or 0)),
            "pressure_b": max(0, int(payload.get("pressure_b", 0) or 0)),
            "step_index": max(0, int(payload.get("step_index", 0) or 0)),
            "step_count": max(0, int(payload.get("step_count", 0) or 0)),
            "updated_at": float(payload.get("updated_at", 0) or 0),
            "connected": bool(device.connected),
            "revision": max(0, int(payload.get("revision", 0) or 0)),
            "history": [
                {
                    **item,
                    "channel_a": max(0, int(item.get("channel_a", 0) or 0)),
                    "channel_b": max(0, int(item.get("channel_b", 0) or 0)),
                    "motor_a": max(0, int(item.get("motor_a", 0) or 0)),
                    "motor_b": max(0, int(item.get("motor_b", 0) or 0)),
                    "motor_c": max(0, int(item.get("motor_c", 0) or 0)),
                    "pressure_a": max(0, int(item.get("pressure_a", 0) or 0)),
                    "pressure_b": max(0, int(item.get("pressure_b", 0) or 0)),
                    "control_mode": str(item.get("control_mode", "") or ""),
                    "fixed_mode": max(0, int(item.get("fixed_mode", 0) or 0)),
                    "motor_mask": max(0, int(item.get("motor_mask", 0) or 0)),
                }
                for item in payload.get("history", [])
                if isinstance(item, dict)
            ][-90:],
        }

    def _build_trigger_result(
        self,
        *,
        event_type: str,
        waveform: EmsWaveform | ToyWaveform,
        waveform_id: str,
        waveform_strength: int,
        success: bool,
        message: str,
        device_id: str,
    ) -> dict:
        return {
            "matched": True,
            "event_type": event_type,
            "waveform_id": waveform_id,
            "waveform_name": waveform.name,
            "max_strength": waveform_strength,
            "success": success,
            "message": message,
            "device_id": device_id,
        }

    async def _play_waveform_with_runtime_compat(
        self,
        device_id: str,
        waveform: EmsWaveform | ToyWaveform,
    ) -> None:
        try:
            await self.runtime.play_waveform(device_id, waveform)
        except RuntimeError:
            if getattr(self.runtime, "backend_name", "") != "memory":
                raise
            devices = self.runtime.get_devices()
            if not devices:
                devices = await self.runtime.scan()
            if not devices:
                raise
            await self.runtime.connect(devices[0].device_id)
            await self.runtime.play_waveform(devices[0].device_id, waveform)
        except TypeError:
            await self.runtime.play_waveform(waveform)


def create_real_bluetooth_runtime(
    *,
    scan_timeout_seconds: int,
    connect_timeout_seconds: int,
    auto_reconnect: bool,
) -> BluetoothRuntime:
    from yokonex_device.runtime.bleak_runtime import BleakBluetoothRuntime

    return BleakBluetoothRuntime(
        scan_timeout_seconds=scan_timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        auto_reconnect=auto_reconnect,
    )


def _normalize_battery_level(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return None


def _generate_custom_waveform_id(payload: BluetoothConfigPayload) -> str:
    existing_ids = {item.id for item in payload.ems_waveforms} | {item.id for item in payload.toy_waveforms}
    while True:
        waveform_id = f"custom-wave-{uuid.uuid4().hex[:8]}"
        if waveform_id not in existing_ids:
            return waveform_id


def _clone_waveform_step(step: EmsWaveformStep) -> EmsWaveformStep:
    return EmsWaveformStep(
        duration_ms=step.duration_ms,
        channel_a=step.channel_a,
        channel_a_mode=step.channel_a_mode,
        channel_a_frequency=step.channel_a_frequency,
        channel_a_pulse_width=step.channel_a_pulse_width,
        channel_b=step.channel_b,
        channel_b_mode=step.channel_b_mode,
        channel_b_frequency=step.channel_b_frequency,
        channel_b_pulse_width=step.channel_b_pulse_width,
    )


def _clone_toy_waveform_step(step: ToyWaveformStep) -> ToyWaveformStep:
    return ToyWaveformStep(
        duration_ms=step.duration_ms,
        motor_a=step.motor_a,
        motor_b=step.motor_b,
        motor_c=step.motor_c,
        control_mode=step.control_mode,
        fixed_mode=step.fixed_mode,
        motor_mask=step.motor_mask,
    )


def _merge_editable_steps(*, existing_steps: list[EmsWaveformStep], incoming_steps: list[dict]) -> list[EmsWaveformStep]:
    if not incoming_steps:
        raise ValueError("波形至少需要一个分段")
    normalized_steps: list[EmsWaveformStep] = []
    for index, step in enumerate(incoming_steps):
        base_step = existing_steps[index] if index < len(existing_steps) else EmsWaveformStep(channel_a=0, channel_b=0)
        normalized_steps.append(
            EmsWaveformStep(
                duration_ms=max(1, int(step.get("duration_ms", base_step.duration_ms) or base_step.duration_ms)),
                channel_a=_normalize_waveform_strength(step.get("channel_a", base_step.channel_a)),
                channel_a_mode=base_step.channel_a_mode,
                channel_a_frequency=base_step.channel_a_frequency,
                channel_a_pulse_width=base_step.channel_a_pulse_width,
                channel_b=_normalize_waveform_strength(step.get("channel_b", base_step.channel_b)),
                channel_b_mode=base_step.channel_b_mode,
                channel_b_frequency=base_step.channel_b_frequency,
                channel_b_pulse_width=base_step.channel_b_pulse_width,
            )
        )
    return normalized_steps


def _merge_toy_editable_steps(
    *,
    existing_steps: list[ToyWaveformStep],
    incoming_steps: list[dict],
    device_family: str = "toy",
) -> list[ToyWaveformStep]:
    if not incoming_steps:
        raise ValueError("波形至少需要一个分段")
    normalized_steps: list[ToyWaveformStep] = []
    for index, step in enumerate(incoming_steps):
        base_step = existing_steps[index] if index < len(existing_steps) else ToyWaveformStep()
        normalized_steps.append(
            ToyWaveformStep(
                duration_ms=max(1, int(step.get("duration_ms", base_step.duration_ms) or base_step.duration_ms)),
                # GCQ 与普通 Toy 共用结构，但不同设备的有效档位范围不同。
                motor_a=_normalize_toy_speed(step.get("motor_a", base_step.motor_a), device_family=device_family, field="motor_a"),
                motor_b=_normalize_toy_speed(step.get("motor_b", base_step.motor_b), device_family=device_family, field="motor_b"),
                motor_c=_normalize_toy_speed(step.get("motor_c", base_step.motor_c), device_family=device_family, field="motor_c"),
                control_mode=_normalize_toy_control_mode(
                    step.get("control_mode", base_step.control_mode),
                    fixed_mode=step.get("fixed_mode", base_step.fixed_mode),
                ),
                fixed_mode=_normalize_toy_fixed_mode(step.get("fixed_mode", base_step.fixed_mode)),
                motor_mask=_normalize_toy_motor_mask(step.get("motor_mask", base_step.motor_mask)),
            )
        )
    return normalized_steps


def _normalize_toy_speed(value: Any, *, device_family: str = "toy", field: str = "") -> int:
    normalized_family = str(device_family or "toy").lower()
    if normalized_family == "gcq_aes":
        if field == "motor_a":
            return max(0, min(int(value), 2))
        if field == "motor_b":
            return max(0, min(int(value), 1))
        return 0
    if normalized_family == "gcq":
        if field == "motor_a":
            return 1 if int(value) > 0 else 0
        return max(0, min(int(value), 5))
    return max(0, min(int(value), 20))


def _normalize_toy_control_mode(value: Any, *, fixed_mode: Any = 0) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"fixed", "fixed_mode", "mode", "pattern"}:
        return "fixed_mode"
    if _normalize_toy_fixed_mode(fixed_mode) > 0:
        return "fixed_mode"
    return "speed"


def _normalize_toy_fixed_mode(value: Any) -> int:
    return max(0, min(int(value), 255))


def _normalize_toy_motor_mask(value: Any) -> int:
    return max(0, min(int(value), 0x07))


def _normalize_waveform_strength(value: Any) -> int:
    return max(0, min(int(value), 180))


def _resolve_waveform_max_strength(waveform: EmsWaveform | ToyWaveform) -> int:
    if not waveform.steps:
        return 0
    if isinstance(waveform, ToyWaveform):
        return max(
            max(step.motor_a, step.motor_b, step.motor_c, 20 if _normalize_toy_control_mode(step.control_mode, fixed_mode=step.fixed_mode) == "fixed_mode" and step.fixed_mode > 0 else 0)
            for step in waveform.steps
        )
    return max(max(step.channel_a, step.channel_b) for step in waveform.steps)


def _resolve_overlay_display_max_strength(*, payload: dict[str, Any], active_waveform_strength: int) -> int:
    device_type = str(payload.get("device_type", "") or "").lower()
    protocol = str(payload.get("protocol", "") or "").lower()
    if protocol == "yiskj_gcq_v1_aes":
        return 2
    if protocol == "yiskj_gcq_toy_013":
        return 5
    if device_type in {"toy", "gcq"}:
        return 20

    if active_waveform_strength > 0:
        return max(1, min(180, int(active_waveform_strength)))

    peak_strength = max(
        [
            max(0, int(payload.get("channel_a", 0) or 0)),
            max(0, int(payload.get("channel_b", 0) or 0)),
            *[
                max(
                    max(0, int(item.get("channel_a", 0) or 0)),
                    max(0, int(item.get("channel_b", 0) or 0)),
                )
                for item in payload.get("history", [])
                if isinstance(item, dict)
            ],
        ],
        default=0,
    )
    if peak_strength > 0:
        return max(50, min(180, peak_strength))
    return 50


def _resolve_waveform_duration_seconds(waveform: EmsWaveform | ToyWaveform) -> float:
    total_duration_ms = sum(max(1, int(getattr(step, "duration_ms", 0) or 0)) for step in waveform.steps)
    return max(total_duration_ms / 1000, 0.001)


def _build_waveform_mutation_response(payload: BluetoothConfigPayload, waveform: EmsWaveform | ToyWaveform) -> dict:
    payload_dict = payload_to_dict(payload)
    if isinstance(waveform, ToyWaveform):
        waveform_data = next(item for item in payload_dict["toy_waveforms"] if item["id"] == waveform.id)
    else:
        waveform_data = next(item for item in payload_dict["ems_waveforms"] if item["id"] == waveform.id)
    return {
        "success": True,
        "waveform": waveform_data,
        "ems_waveforms": payload_dict["ems_waveforms"],
        "toy_waveforms": payload_dict["toy_waveforms"],
    }


def _resolve_scan_error_message(error: Exception) -> str:
    raw_message = str(error or "").strip()
    error_name = error.__class__.__name__
    normalized_message = raw_message.lower()
    if error_name == "BleakBluetoothNotAvailableError" or "no bluetooth adapter found" in normalized_message:
        return "当前主机未检测到蓝牙适配器"
    if raw_message:
        return f"蓝牙扫描失败: {raw_message}"
    return "蓝牙扫描失败，请检查蓝牙权限或适配器状态"
