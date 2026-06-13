from __future__ import annotations

import json

from yokonex_device.gcq_toy_builtin_waveforms import create_gcq_toy_defaults
from yokonex_device.storage import BluetoothSettingsStore


def test_store_returns_default_payload_when_file_missing(tmp_path) -> None:
    store = BluetoothSettingsStore(tmp_path / "bluetooth.json")

    payload = store.load()

    assert (tmp_path / "bluetooth.json").exists()
    assert payload.bluetooth_settings.enabled is False
    assert payload.ems_waveforms
    assert payload.toy_waveforms
    assert payload.ems_waveforms[0].id == "ems-default-pulse"


def test_store_save_preserves_unknown_business_fields(tmp_path) -> None:
    path = tmp_path / "bluetooth.json"
    path.write_text(
        json.dumps(
            {
                "bluetooth_settings": {"enabled": True},
                "ems_waveforms": [],
                "toy_waveforms": [],
                "bluetooth_event_rules": [{"id": "gift-tier-01"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = BluetoothSettingsStore(path)

    payload = store.load()
    payload.bluetooth_settings.default_target_device_id = "demo-device"
    store.save(payload)

    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["bluetooth_settings"]["default_target_device_id"] == "demo-device"
    assert saved["bluetooth_event_rules"] == [{"id": "gift-tier-01"}]


def test_store_loads_gcq_custom_waveforms_and_clamps_ranges(tmp_path) -> None:
    path = tmp_path / "bluetooth.json"
    path.write_text(
        json.dumps(
            {
                "toy_waveforms": [
                    {
                        "id": "custom-gcq-wave",
                        "name": "GCQ 波形",
                        "builtin": False,
                        "editable": True,
                        "device_family": "gcq",
                        "steps": [
                            {
                                "duration_ms": 200,
                                "motor_a": 12,
                                "motor_b": 8,
                                "motor_c": 6,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = BluetoothSettingsStore(path)

    payload = store.load()

    assert payload.toy_waveforms[0].id == "custom-gcq-wave"
    assert payload.toy_waveforms[0].steps[0].motor_a == 1
    assert payload.toy_waveforms[0].steps[0].motor_b == 5
    assert payload.toy_waveforms[0].steps[0].motor_c == 5


def test_gcq_builtin_waveforms_open_valve_only_in_last_step() -> None:
    waveforms = create_gcq_toy_defaults()

    for waveform in waveforms:
        assert waveform.steps[-1].motor_a == 1
        assert waveform.steps[-1].motor_b == 0
        assert waveform.steps[-1].motor_c == 0


def test_store_loads_toy_fixed_mode_steps_and_clamps_fields(tmp_path) -> None:
    path = tmp_path / "bluetooth.json"
    path.write_text(
        json.dumps(
            {
                "toy_waveforms": [
                    {
                        "id": "custom-toy-fixed-wave",
                        "name": "固定模式波形",
                        "builtin": False,
                        "editable": True,
                        "device_family": "toy",
                        "steps": [
                            {
                                "duration_ms": 180,
                                "control_mode": "fixed",
                                "fixed_mode": 999,
                                "motor_mask": 12,
                                "motor_a": 30,
                                "motor_b": 25,
                                "motor_c": 5,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = BluetoothSettingsStore(path)

    payload = store.load()
    step = payload.toy_waveforms[0].steps[0]

    assert payload.toy_waveforms[0].id == "custom-toy-fixed-wave"
    assert step.control_mode == "fixed_mode"
    assert step.fixed_mode == 255
    assert step.motor_mask == 0x07
    assert step.motor_a == 20
    assert step.motor_b == 20
    assert step.motor_c == 5


def test_store_loads_gcq_aes_waveforms_and_clamps_ranges(tmp_path) -> None:
    path = tmp_path / "bluetooth.json"
    path.write_text(
        json.dumps(
            {
                "toy_waveforms": [
                    {
                        "id": "custom-gcq-aes-wave",
                        "name": "GCQ AES 波形",
                        "builtin": False,
                        "editable": True,
                        "device_family": "gcq_aes",
                        "steps": [
                            {
                                "duration_ms": 1200,
                                "motor_a": 9,
                                "motor_b": 5,
                                "motor_c": 7,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = BluetoothSettingsStore(path)

    payload = store.load()
    step = payload.toy_waveforms[0].steps[0]

    assert payload.toy_waveforms[0].device_family == "gcq_aes"
    assert step.motor_a == 2
    assert step.motor_b == 1
    assert step.motor_c == 0
