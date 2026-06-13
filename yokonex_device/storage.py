from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yokonex_device.ems_builtin_waveforms import is_preset_waveform_id
from yokonex_device.gcq_toy_builtin_waveforms import is_gcq_toy_preset_waveform_id
from yokonex_device.models import BluetoothConfigPayload
from yokonex_device.models import BluetoothSettings
from yokonex_device.models import EmsWaveform
from yokonex_device.models import EmsWaveformStep
from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep
from yokonex_device.models import build_default_payload
from yokonex_device.models import payload_to_dict
from yokonex_device.toy_builtin_waveforms import is_toy_preset_waveform_id


class BluetoothSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> BluetoothConfigPayload:
        if not self.path.exists():
            payload = build_default_payload()
            # 首次运行时直接落盘默认设备配置，确保后续波形编辑可持久化。
            self.save(payload)
            return payload

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        defaults = build_default_payload()

        settings_data = payload.get("bluetooth_settings", {})
        settings = BluetoothSettings(
            enabled=bool(settings_data.get("enabled", defaults.bluetooth_settings.enabled)),
            scan_timeout_seconds=max(1, int(settings_data.get("scan_timeout_seconds", defaults.bluetooth_settings.scan_timeout_seconds))),
            connect_timeout_seconds=max(1, int(settings_data.get("connect_timeout_seconds", defaults.bluetooth_settings.connect_timeout_seconds))),
            auto_reconnect=bool(settings_data.get("auto_reconnect", defaults.bluetooth_settings.auto_reconnect)),
            last_connected_device_id=str(settings_data.get("last_connected_device_id", "")),
            last_connected_device_name=str(settings_data.get("last_connected_device_name", "")),
            default_target_device_id=str(settings_data.get("default_target_device_id", "")),
        )

        normalized_input_waveforms = [
            _normalize_waveform(item)
            for item in payload.get("ems_waveforms", [])
            if isinstance(item, dict)
        ]
        if normalized_input_waveforms:
            custom_waveforms = [
                waveform
                for waveform in normalized_input_waveforms
                if waveform.id.lower() != "ems-default-pulse" and not is_preset_waveform_id(waveform.id)
            ]
            waveforms = [*custom_waveforms, *defaults.ems_waveforms]
        else:
            waveforms = defaults.ems_waveforms

        normalized_input_toy_waveforms = [
            _normalize_toy_waveform(item)
            for item in payload.get("toy_waveforms", [])
            if isinstance(item, dict)
        ]
        if normalized_input_toy_waveforms:
            custom_toy_waveforms = [
                waveform
                for waveform in normalized_input_toy_waveforms
                if not is_toy_preset_waveform_id(waveform.id) and not is_gcq_toy_preset_waveform_id(waveform.id)
            ]
            toy_waveforms = [*custom_toy_waveforms, *defaults.toy_waveforms]
        else:
            toy_waveforms = defaults.toy_waveforms

        return BluetoothConfigPayload(
            bluetooth_settings=settings,
            ems_waveforms=waveforms,
            toy_waveforms=toy_waveforms,
        )

    def save(self, payload: BluetoothConfigPayload) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        merged_payload = self._load_existing_raw_payload()
        merged_payload.update(
            {
                "bluetooth_settings": payload_to_dict(payload)["bluetooth_settings"],
                "ems_waveforms": payload_to_dict(payload)["ems_waveforms"],
                "toy_waveforms": payload_to_dict(payload)["toy_waveforms"],
            }
        )
        self.path.write_text(
            json.dumps(merged_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_existing_raw_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}


def _normalize_waveform(item: dict[str, Any]) -> EmsWaveform:
    steps = item.get("steps", [])
    normalized_steps = [
        EmsWaveformStep(
            duration_ms=max(1, int(step.get("duration_ms", 200))),
            channel_a=_normalize_channel_strength(step.get("channel_a", 40)),
            channel_a_mode=max(1, int(step.get("channel_a_mode", step.get("a_mode", 1)))),
            channel_a_frequency=max(1, int(step.get("channel_a_frequency", step.get("a_frequency", 10)))),
            channel_a_pulse_width=max(1, int(step.get("channel_a_pulse_width", step.get("a_pulse_width", 5)))),
            channel_b=_normalize_channel_strength(step.get("channel_b", 40)),
            channel_b_mode=max(1, int(step.get("channel_b_mode", step.get("b_mode", 1)))),
            channel_b_frequency=max(1, int(step.get("channel_b_frequency", step.get("b_frequency", 10)))),
            channel_b_pulse_width=max(1, int(step.get("channel_b_pulse_width", step.get("b_pulse_width", 5)))),
        )
        for step in steps
        if isinstance(step, dict)
    ]
    if not normalized_steps:
        normalized_steps = [EmsWaveformStep()]
    return EmsWaveform(
        id=str(item.get("id", "custom-wave")),
        name=str(item.get("name", "自定义波形")),
        builtin=bool(item.get("builtin", False)),
        editable=bool(item.get("editable", True)),
        execution_mode=str(item.get("execution_mode", "fixed") or "fixed").lower(),
        loop_count=max(1, int(item.get("loop_count", 1))),
        steps=normalized_steps,
    )


def _normalize_toy_waveform(item: dict[str, Any]) -> ToyWaveform:
    device_family = _normalize_toy_device_family(item.get("device_family", "toy"))
    steps = item.get("steps", [])
    normalized_steps = [
        ToyWaveformStep(
            duration_ms=max(1, int(step.get("duration_ms", 200))),
            # 设备层在加载历史配置时就把数值归一，避免旧配置继续带着错误档位运行。
            motor_a=_normalize_toy_speed(step.get("motor_a", 0), device_family=device_family, field="motor_a"),
            motor_b=_normalize_toy_speed(step.get("motor_b", 0), device_family=device_family, field="motor_b"),
            motor_c=_normalize_toy_speed(step.get("motor_c", 0), device_family=device_family, field="motor_c"),
        )
        for step in steps
        if isinstance(step, dict)
    ]
    if not normalized_steps:
        normalized_steps = [ToyWaveformStep()]
    return ToyWaveform(
        id=str(item.get("id", "custom-toy-wave")),
        name=str(item.get("name", "自定义波形")),
        builtin=bool(item.get("builtin", False)),
        editable=bool(item.get("editable", True)),
        device_family=device_family,
        loop_count=max(1, int(item.get("loop_count", 1))),
        steps=normalized_steps,
    )


def _normalize_toy_speed(value: Any, *, device_family: str = "toy", field: str = "") -> int:
    normalized_family = str(device_family or "toy").lower()
    if normalized_family == "gcq":
        if field == "motor_a":
            return 1 if int(value) > 0 else 0
        return max(0, min(int(value), 5))
    return max(0, min(int(value), 20))


def _normalize_toy_device_family(value: Any) -> str:
    family = str(value or "toy").strip().lower()
    if family == "gcq":
        return "gcq"
    return "toy"


def _normalize_channel_strength(value: Any) -> int:
    return max(0, min(int(value), 180))
