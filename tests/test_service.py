from __future__ import annotations

import asyncio

import pytest

from yokonex_device.runtime.memory_runtime import MemoryBluetoothRuntime
from yokonex_device.service import BluetoothService


class FakeEventHub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish_control(self, event: dict) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_service_can_scan_connect_and_disconnect(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yokonex_device.service.create_real_bluetooth_runtime",
        lambda **kwargs: MemoryBluetoothRuntime(),
    )
    service = BluetoothService.create_default(config_path=tmp_path / "bluetooth.json")

    scanned = await service.scan()
    connected = await service.connect(scanned[0].device_id)
    disconnected = await service.disconnect()

    assert scanned
    assert connected.connected is True
    assert disconnected.connected is False


@pytest.mark.anyio
async def test_service_publishes_connect_and_trigger_events(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yokonex_device.service.create_real_bluetooth_runtime",
        lambda **kwargs: MemoryBluetoothRuntime(),
    )
    event_hub = FakeEventHub()
    service = BluetoothService.create_default(
        config_path=tmp_path / "bluetooth.json",
        event_hub=event_hub,
    )

    scanned = await service.scan()
    await service.connect(scanned[0].device_id)
    result = await service.trigger_waveform(
        event_type="manual_test",
        waveform_id="ems-preset-01",
    )

    assert result["success"] is True
    assert event_hub.events[0]["type"] == "bluetooth_connect"
    assert event_hub.events[-1]["type"] == "bluetooth_trigger"


def test_service_status_payload_contains_device_models(tmp_path) -> None:
    service = BluetoothService(
        store=type("Store", (), {"load": lambda self: __import__("yokonex_device.models", fromlist=["build_default_payload"]).build_default_payload(), "save": lambda self, payload: None})(),
        runtime=MemoryBluetoothRuntime(),
    )

    payload = service.get_status_payload()

    assert payload["runtime_backend"] == "memory"
    assert isinstance(payload["ems_waveforms"], list)
    assert isinstance(payload["toy_waveforms"], list)
    assert "rules" not in payload


def test_service_can_create_update_and_delete_custom_waveform(tmp_path) -> None:
    service = BluetoothService(
        store=type("Store", (), {"load": lambda self: __import__("yokonex_device.models", fromlist=["build_default_payload"]).build_default_payload(), "save": lambda self, payload: None})(),
        runtime=MemoryBluetoothRuntime(),
    )

    created = service.create_waveform(name="测试波形")
    waveform_id = created["waveform"]["id"]

    updated = service.update_waveform(
        waveform_id=waveform_id,
        name="已编辑波形",
        steps=[{"duration_ms": 180, "channel_a": 120, "channel_b": 80}],
    )
    deleted = service.delete_waveform(waveform_id)

    assert created["success"] is True
    assert updated["waveform"]["name"] == "已编辑波形"
    assert deleted["deleted_waveform_id"] == waveform_id


class PreemptibleRuntime:
    backend_name = "preemptible"

    def __init__(self) -> None:
        self.started_waveforms: list[str] = []
        self.cancelled_waveforms: list[str] = []
        self.completed_waveforms: list[str] = []
        self._devices = []
        self._release_events: dict[str, list[asyncio.Event]] = {}

    async def scan(self):
        return []

    async def connect(self, device_id: str):
        raise NotImplementedError

    async def disconnect(self, device_id: str | None = None):
        raise NotImplementedError

    def get_status(self, device_id: str | None = None):
        from yokonex_device.models import BluetoothConnectionStatus

        return BluetoothConnectionStatus()

    def get_devices(self):
        return list(self._devices)

    def get_overlay_payload(self, device_id: str | None = None):
        return {
            "connected": False,
            "device_name": "",
            "device_type": "",
            "waveform_name": "",
            "battery_level": None,
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

    async def play_waveform(self, waveform) -> None:
        self.started_waveforms.append(waveform.id)
        release_event = asyncio.Event()
        self._release_events.setdefault(waveform.id, []).append(release_event)
        try:
            await release_event.wait()
            await asyncio.sleep(sum(max(1, int(getattr(step, "duration_ms", 0) or 0)) for step in waveform.steps) / 1000)
        except asyncio.CancelledError:
            self.cancelled_waveforms.append(waveform.id)
            raise
        else:
            self.completed_waveforms.append(waveform.id)

    def release_next(self, waveform_id: str) -> None:
        events = self._release_events.get(waveform_id, [])
        if not events:
            raise AssertionError(f"未找到待释放波形: {waveform_id}")
        events.pop(0).set()


async def _wait_for_started_waveforms(runtime: PreemptibleRuntime, expected_count: int) -> None:
    for _ in range(50):
        if len(runtime.started_waveforms) >= expected_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等待波形启动超时: expected={expected_count} actual={len(runtime.started_waveforms)}")


@pytest.mark.anyio
async def test_service_preempts_running_waveform_when_new_one_has_higher_strength() -> None:
    from yokonex_device.models import build_default_payload

    runtime = PreemptibleRuntime()
    service = BluetoothService(
        store=type("Store", (), {"load": lambda self: build_default_payload(), "save": lambda self, payload: None})(),
        runtime=runtime,
    )
    stronger_waveform = service.create_waveform(name="更强波形")
    service.update_waveform(
        waveform_id=stronger_waveform["waveform"]["id"],
        name="更强波形",
        steps=[{"duration_ms": 200, "channel_a": 180, "channel_b": 120}],
    )

    weaker_task = asyncio.create_task(service.trigger_waveform(event_type="low", waveform_id="ems-preset-01"))
    await _wait_for_started_waveforms(runtime, 1)

    stronger_task = asyncio.create_task(
        service.trigger_waveform(event_type="high", waveform_id=stronger_waveform["waveform"]["id"])
    )
    await _wait_for_started_waveforms(runtime, 2)

    weaker_result = await weaker_task
    runtime.release_next(stronger_waveform["waveform"]["id"])
    stronger_result = await stronger_task

    assert weaker_result["success"] is True
    assert "抢占" in weaker_result["message"]
    assert stronger_result["success"] is True
