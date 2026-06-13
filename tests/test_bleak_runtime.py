from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yokonex_device.models import EmsWaveform
from yokonex_device.models import EmsWaveformStep
from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep
from yokonex_device.runtime.bleak_runtime import BleakBluetoothRuntime
from yokonex_device.runtime.bleak_runtime import _decrypt_gcq_aes_packet
from yokonex_device.runtime.bleak_runtime import _encrypt_gcq_aes_packet


EMS_SERVICE_UUID = "0000ff30-0000-1000-8000-00805f9b34fb"
EMS_WRITE_CHAR_UUID = "0000ff31-0000-1000-8000-00805f9b34fb"
TOY_SERVICE_UUID = "0000ff40-0000-1000-8000-00805f9b34fb"
TOY_WRITE_CHAR_UUID = "0000ff41-0000-1000-8000-00805f9b34fb"
TOY_NOTIFY_CHAR_UUID = "0000ff42-0000-1000-8000-00805f9b34fb"
GCQ_TOY_SERVICE_UUID = "0000ff70-0000-1000-8000-00805f9b34fb"
GCQ_TOY_WRITE_CHAR_UUID = "0000ff71-0000-1000-8000-00805f9b34fb"
GCQ_TOY_NOTIFY_CHAR_UUID = "0000ff72-0000-1000-8000-00805f9b34fb"
GCQ_AES_SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
GCQ_AES_WRITE_CHAR_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"
GCQ_AES_NOTIFY_CHAR_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"


class FakeBleakClient:
    def __init__(self, ble_device, disconnected_callback=None, **kwargs) -> None:
        self.ble_device = ble_device
        self.disconnected_callback = disconnected_callback
        self.connected = False
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_callbacks: dict[str, object] = {}
        self.services = kwargs.get("services")

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def write_gatt_char(self, char_specifier, data, response: bool | None = None) -> None:
        self.writes.append((char_specifier, bytes(data), bool(response)))

    async def start_notify(self, char_specifier, callback) -> None:
        self.notify_callbacks[str(char_specifier)] = callback

    async def stop_notify(self, char_specifier) -> None:
        self.notify_callbacks.pop(str(char_specifier), None)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def get_services(self):
        return self.services


def _decrypt_write_payload(write_entry: tuple[str, bytes, bool]) -> bytes:
    decrypted = _decrypt_gcq_aes_packet(write_entry[1])
    if decrypted is None:
        raise AssertionError("GCQ AES 密文解密失败")
    return decrypted


@pytest.mark.anyio
async def test_bleak_runtime_scan_filters_and_classifies_supported_ems_devices() -> None:
    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
            "AA:BB:CC:DD:EE:02": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:02", name="YYC-DJ-001", rssi=-53),
                SimpleNamespace(local_name="YYC-DJ-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
            "AA:BB:CC:DD:EE:03": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:03", name="Heart Rate Sensor", rssi=-60),
                SimpleNamespace(local_name="Heart Rate Sensor", service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"]),
            ),
        }

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=FakeBleakClient,
    )

    devices = await runtime.scan()

    assert [item.device_id for item in devices] == [
        "AA:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:02",
    ]
    assert devices[0].protocol == "ems_v2"
    assert devices[1].protocol == "ems_v1"
    assert all(item.device_type == "ems" for item in devices)


@pytest.mark.anyio
async def test_bleak_runtime_scan_classifies_gcq_toy_device_by_service_uuid() -> None:
    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:11": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:11", name="YISKJ Device", rssi=-47),
                SimpleNamespace(local_name="YISKJ Device", service_uuids=[GCQ_TOY_SERVICE_UUID]),
            ),
        }

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=FakeBleakClient,
    )

    devices = await runtime.scan()

    assert len(devices) == 1
    assert devices[0].device_type == "toy"
    assert devices[0].protocol == "yiskj_gcq_toy_013"


@pytest.mark.anyio
async def test_bleak_runtime_scan_classifies_gcq_aes_device_by_service_uuid() -> None:
    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:31": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:31", name="TDL-YISKJ-003", rssi=-44),
                SimpleNamespace(local_name="TDL-YISKJ-003", service_uuids=[GCQ_AES_SERVICE_UUID]),
            ),
        }

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=FakeBleakClient,
    )

    devices = await runtime.scan()

    assert len(devices) == 1
    assert devices[0].device_type == "toy"
    assert devices[0].protocol == "yiskj_gcq_v1_aes"


@pytest.mark.anyio
async def test_bleak_runtime_can_connect_and_disconnect_scanned_device() -> None:
    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=FakeBleakClient,
    )

    await runtime.scan()
    connected = await runtime.connect("AA:BB:CC:DD:EE:01")
    disconnected = await runtime.disconnect()

    assert connected.connected is True
    assert connected.device is not None
    assert connected.device.device_id == "AA:BB:CC:DD:EE:01"
    assert runtime.get_status().connected is False
    assert disconnected.connected is False


@pytest.mark.anyio
async def test_bleak_runtime_connect_queries_battery_for_ems_v2_device() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")

    client = created_clients[-1]
    assert "0000ff32-0000-1000-8000-00805f9b34fb" in client.notify_callbacks
    assert client.writes[0][0] == EMS_WRITE_CHAR_UUID
    assert client.writes[0][1] == bytes([0x35, 0x71, 0x04, 0xAA])


@pytest.mark.anyio
async def test_bleak_runtime_connect_queries_battery_for_ems_v1_device() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:02": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:02", name="YYC-DJ-001", rssi=-53),
                SimpleNamespace(local_name="YYC-DJ-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:02")

    client = created_clients[-1]
    assert "0000ff32-0000-1000-8000-00805f9b34fb" in client.notify_callbacks
    assert client.writes[0][0] == EMS_WRITE_CHAR_UUID
    assert client.writes[0][1] == bytes([0x35, 0x71, 0x04, 0xAA])


@pytest.mark.anyio
async def test_bleak_runtime_connect_initializes_gcq_toy_device_and_starts_heartbeat() -> None:
    created_clients: list[FakeBleakClient] = []
    heartbeat_started = asyncio.Event()

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:11": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:11", name="YISKJ Device", rssi=-47),
                SimpleNamespace(local_name="YISKJ Device", service_uuids=[GCQ_TOY_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        if seconds == 1.0:
            heartbeat_started.set()
            await asyncio.Event().wait()

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:11")
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    client = created_clients[-1]
    assert GCQ_TOY_NOTIFY_CHAR_UUID in client.notify_callbacks
    assert client.writes[0] == (GCQ_TOY_WRITE_CHAR_UUID, bytes([0x35, 0x13, 0x00, 0x00, 0x00, 0x48]), False)
    assert client.writes[1] == (GCQ_TOY_WRITE_CHAR_UUID, bytes([0x35, 0x14, 0x00, 0x00, 0x00, 0x49]), False)
    assert any(item[1] == bytes([0x35, 0x17, 0x00, 0x00, 0x00, 0x4C]) for item in client.writes)

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_connect_initializes_gcq_aes_device_and_queries_status_and_battery() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:31": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:31", name="TDL-YISKJ-003", rssi=-44),
                SimpleNamespace(local_name="TDL-YISKJ-003", service_uuids=[GCQ_AES_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:31")

    client = created_clients[-1]
    assert GCQ_AES_NOTIFY_CHAR_UUID in client.notify_callbacks
    assert client.writes[0][0] == GCQ_AES_WRITE_CHAR_UUID
    assert client.writes[1][0] == GCQ_AES_WRITE_CHAR_UUID
    assert _decrypt_write_payload(client.writes[0])[:4] == bytes([0xBF, 0x0F, 0xA0, 0x04])
    assert _decrypt_write_payload(client.writes[1])[:4] == bytes([0xBF, 0x0F, 0xA0, 0x05])

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_connect_queries_toy_device_info_and_updates_capabilities() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:21": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:21", name="YCY-FJB-001", rssi=-45),
                SimpleNamespace(local_name="YCY-FJB-001", service_uuids=[TOY_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:21")

    client = created_clients[-1]
    assert TOY_NOTIFY_CHAR_UUID in client.notify_callbacks
    assert client.writes[0] == (TOY_WRITE_CHAR_UUID, bytes([0x35, 0x10, 0x45]), False)

    notify_callback = client.notify_callbacks[TOY_NOTIFY_CHAR_UUID]
    notify_callback(TOY_NOTIFY_CHAR_UUID, bytearray([0x35, 0x10, 0x09, 0x02, 0x07, 0x04, 0x01, 0x00, 0x00, 0x5C]))
    await asyncio.sleep(0)

    status = runtime.get_status("AA:BB:CC:DD:EE:21")
    overlay = runtime.get_overlay_payload("AA:BB:CC:DD:EE:21")

    assert status.device is not None
    assert status.device.product_id == 9
    assert status.device.product_version == 2
    assert status.device.motor_a_mode_count == 7
    assert status.device.motor_b_mode_count == 4
    assert status.device.motor_c_mode_count == 1
    assert overlay["product_id"] == 9
    assert overlay["product_version"] == 2
    assert overlay["motor_a_mode_count"] == 7
    assert overlay["motor_b_mode_count"] == 4
    assert overlay["motor_c_mode_count"] == 1

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_connect_reclassifies_device_from_gatt_services_before_subscribing() -> None:
    created_clients: list[FakeBleakClient] = []
    heartbeat_started = asyncio.Event()

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:12": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:12", name="YYC-DJ-MISCLASSIFIED", rssi=-47),
                # 模拟广播阶段没有 GCQ service UUID，旧逻辑会先按设备名落到 EMS 分支。
                SimpleNamespace(local_name="YYC-DJ-MISCLASSIFIED", service_uuids=[]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        if seconds == 1.0:
            heartbeat_started.set()
            await asyncio.Event().wait()

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(
            *args,
            **kwargs,
            services=[SimpleNamespace(uuid=GCQ_TOY_SERVICE_UUID)],
        )
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    devices = await runtime.scan()
    assert devices[0].device_type == "ems"

    await runtime.connect("AA:BB:CC:DD:EE:12")
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    client = created_clients[-1]
    assert GCQ_TOY_NOTIFY_CHAR_UUID in client.notify_callbacks
    assert EMS_WRITE_CHAR_UUID not in [item[0] for item in client.writes]
    assert runtime.get_status().device is not None
    assert runtime.get_status().device.protocol == "yiskj_gcq_toy_013"

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_updates_battery_level_from_notify_packet() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")
    notify_callback = created_clients[-1].notify_callbacks["0000ff32-0000-1000-8000-00805f9b34fb"]
    notify_callback("0000ff32-0000-1000-8000-00805f9b34fb", bytearray([0x35, 0x71, 0x04, 88, 0x00]))
    await asyncio.sleep(0)

    status = runtime.get_status()
    overlay = runtime.get_overlay_payload()

    assert status.battery_level == 88
    assert overlay["battery_level"] == 88


@pytest.mark.anyio
async def test_bleak_runtime_updates_battery_level_from_notify_packet_for_ems_v1() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:02": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:02", name="YYC-DJ-001", rssi=-53),
                SimpleNamespace(local_name="YYC-DJ-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:02")
    notify_callback = created_clients[-1].notify_callbacks["0000ff32-0000-1000-8000-00805f9b34fb"]
    notify_callback("0000ff32-0000-1000-8000-00805f9b34fb", bytearray([0x35, 0x71, 0x04, 76, 0x00]))
    await asyncio.sleep(0)

    status = runtime.get_status()
    overlay = runtime.get_overlay_payload()

    assert status.battery_level == 76
    assert overlay["battery_level"] == 76


@pytest.mark.anyio
async def test_bleak_runtime_updates_battery_and_status_from_gcq_notify_packets_during_active_waveform() -> None:
    created_clients: list[FakeBleakClient] = []
    heartbeat_started = asyncio.Event()

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:11": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:11", name="YISKJ Device", rssi=-47),
                SimpleNamespace(local_name="YISKJ Device", service_uuids=[GCQ_TOY_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        if seconds == 1.0:
            heartbeat_started.set()
            await asyncio.Event().wait()

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:11")
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)
    notify_callback = created_clients[-1].notify_callbacks[GCQ_TOY_NOTIFY_CHAR_UUID]
    runtime._set_overlay_payload("AA:BB:CC:DD:EE:11", waveform_name="gcq-waveform")

    notify_callback(GCQ_TOY_NOTIFY_CHAR_UUID, bytearray([0x35, 0x14, 88, 0x00, 0x00, 0x00]))
    notify_callback(GCQ_TOY_NOTIFY_CHAR_UUID, bytearray([0x35, 0x13, 0xFF, 0x03, 0x05, 0x00]))
    await asyncio.sleep(0)

    status = runtime.get_status()
    overlay = runtime.get_overlay_payload()

    assert status.battery_level == 88
    assert overlay["battery_level"] == 88
    assert overlay["motor_a"] == 1
    assert overlay["motor_b"] == 3
    assert overlay["motor_c"] == 5

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_gcq_status_notify_does_not_override_idle_overlay_strength() -> None:
    created_clients: list[FakeBleakClient] = []
    heartbeat_started = asyncio.Event()

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:11": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:11", name="YISKJ Device", rssi=-47),
                SimpleNamespace(local_name="YISKJ Device", service_uuids=[GCQ_TOY_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        if seconds == 1.0:
            heartbeat_started.set()
            await asyncio.Event().wait()

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:11")
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)
    notify_callback = created_clients[-1].notify_callbacks[GCQ_TOY_NOTIFY_CHAR_UUID]

    notify_callback(GCQ_TOY_NOTIFY_CHAR_UUID, bytearray([0x35, 0x14, 88, 0x00, 0x00, 0x00]))
    notify_callback(GCQ_TOY_NOTIFY_CHAR_UUID, bytearray([0x35, 0x13, 0xFF, 0x03, 0x05, 0x00]))
    await asyncio.sleep(0)

    status = runtime.get_status()
    overlay = runtime.get_overlay_payload()

    assert status.battery_level == 88
    assert overlay["battery_level"] == 88
    assert overlay["waveform_name"] == ""
    assert overlay["motor_a"] == 0
    assert overlay["motor_b"] == 0
    assert overlay["motor_c"] == 0

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_updates_gcq_aes_status_pressure_and_battery_from_notify_packets() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:31": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:31", name="TDL-YISKJ-003", rssi=-44),
                SimpleNamespace(local_name="TDL-YISKJ-003", service_uuids=[GCQ_AES_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:31")

    notify_callback = created_clients[-1].notify_callbacks[GCQ_AES_NOTIFY_CHAR_UUID]
    notify_callback(
        GCQ_AES_NOTIFY_CHAR_UUID,
        bytearray(_encrypt_gcq_aes_packet(bytes.fromhex("BF0FB0010100140ACE0153C01574464D"))),
    )
    notify_callback(
        GCQ_AES_NOTIFY_CHAR_UUID,
        bytearray(_encrypt_gcq_aes_packet(bytes.fromhex("BF0FB00201030210CE0153C01574464D"))),
    )
    notify_callback(
        GCQ_AES_NOTIFY_CHAR_UUID,
        bytearray(_encrypt_gcq_aes_packet(bytes.fromhex("BF0FB00310745DE1CE0174464D53C015"))),
    )
    await asyncio.sleep(0)

    status = runtime.get_status("AA:BB:CC:DD:EE:31")
    overlay = runtime.get_overlay_payload("AA:BB:CC:DD:EE:31")

    assert status.battery_level == 16
    assert overlay["battery_level"] == 16
    assert overlay["motor_a"] == 1
    assert overlay["motor_b"] == 0
    assert overlay["pressure_a"] == 0x0103
    assert overlay["pressure_b"] == 0x0210

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_notify_callback_schedules_async_battery_update() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")
    notify_callback = created_clients[-1].notify_callbacks["0000ff32-0000-1000-8000-00805f9b34fb"]

    # 模拟真实 Bleak 的同步回调调用方式，确保电量处理协程会被正确调度。
    notify_callback("0000ff32-0000-1000-8000-00805f9b34fb", bytearray([0x35, 0x71, 0x04, 66, 0x00]))
    await asyncio.sleep(0)

    status = runtime.get_status()
    overlay = runtime.get_overlay_payload()

    assert status.battery_level == 66
    assert overlay["battery_level"] == 66


@pytest.mark.anyio
async def test_bleak_runtime_writes_waveform_packets_to_ems_characteristic() -> None:
    sleep_calls: list[float] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=FakeBleakClient,
        sleep_func=fake_sleep,
    )
    waveform = EmsWaveform(
        id="wf-1",
        name="娴嬭瘯娉㈠舰",
        execution_mode="fixed",
        steps=[
            EmsWaveformStep(duration_ms=180, channel_a=48, channel_b=24, channel_a_mode=6, channel_b_mode=6),
            EmsWaveformStep(duration_ms=120, channel_a=0, channel_b=0),
        ],
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")
    await runtime.play_waveform(waveform)

    client = runtime._client
    assert client is not None
    assert [item[0] for item in client.writes[-3:]] == [
        EMS_WRITE_CHAR_UUID,
        EMS_WRITE_CHAR_UUID,
        EMS_WRITE_CHAR_UUID,
    ]
    assert len(client.writes[-3][1]) == 10
    assert client.writes[-3][1][5] == 0x06
    assert client.writes[-3][1][8] == 0x06
    assert sleep_calls == [0.18, 0.12]


@pytest.mark.anyio
async def test_bleak_runtime_writes_gcq_packets_for_gcq_toy_device() -> None:
    created_clients: list[FakeBleakClient] = []
    sleep_calls: list[float] = []
    heartbeat_started = asyncio.Event()

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:11": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:11", name="YISKJ Device", rssi=-47),
                SimpleNamespace(local_name="YISKJ Device", service_uuids=[GCQ_TOY_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(seconds: float) -> None:
        if seconds == 1.0:
            heartbeat_started.set()
            await asyncio.Event().wait()
        sleep_calls.append(seconds)

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )
    waveform = ToyWaveform(
        id="gcq-wf-1",
        name="gcq-waveform",
        steps=[
            ToyWaveformStep(duration_ms=200, motor_a=1, motor_b=5, motor_c=2),
            ToyWaveformStep(duration_ms=150, motor_a=0, motor_b=1, motor_c=5),
        ],
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:11")
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    client = created_clients[-1]
    client.writes.clear()
    await runtime.play_waveform("AA:BB:CC:DD:EE:11", waveform)

    assert client.writes == [
        (GCQ_TOY_WRITE_CHAR_UUID, bytes([0x35, 0x12, 0xFF, 0x05, 0x02, 0x4D]), False),
        (GCQ_TOY_WRITE_CHAR_UUID, bytes([0x35, 0x12, 0x00, 0x01, 0x05, 0x4D]), False),
        (GCQ_TOY_WRITE_CHAR_UUID, bytes([0x35, 0x12, 0x00, 0x00, 0x00, 0x47]), False),
    ]
    assert sleep_calls[-2:] == [0.2, 0.15]

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_writes_gcq_aes_packets_for_gcq_aes_waveform() -> None:
    created_clients: list[FakeBleakClient] = []
    sleep_calls: list[float] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:31": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:31", name="TDL-YISKJ-003", rssi=-44),
                SimpleNamespace(local_name="TDL-YISKJ-003", service_uuids=[GCQ_AES_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:31")

    client = created_clients[-1]
    client.writes.clear()
    waveform = ToyWaveform(
        id="gcq-aes-wf-1",
        name="gcq-aes-waveform",
        device_family="gcq_aes",
        steps=[
            ToyWaveformStep(duration_ms=2000, motor_a=1, motor_b=1, motor_c=0),
            ToyWaveformStep(duration_ms=1000, motor_a=2, motor_b=0, motor_c=0),
        ],
    )

    await runtime.play_waveform("AA:BB:CC:DD:EE:31", waveform)

    decrypted_packets = [_decrypt_write_payload(item) for item in client.writes]
    assert len(decrypted_packets) == 5
    assert decrypted_packets[0][:7] == bytes([0xBF, 0x0F, 0xA0, 0x01, 0x01, 0x00, 0x02])
    assert decrypted_packets[1][:7] == bytes([0xBF, 0x0F, 0xA0, 0x02, 0x01, 0x00, 0x02])
    assert decrypted_packets[2][:7] == bytes([0xBF, 0x0F, 0xA0, 0x01, 0x02, 0x00, 0x01])
    assert decrypted_packets[3][:7] == bytes([0xBF, 0x0F, 0xA0, 0x02, 0x00, 0x00, 0x01])
    assert decrypted_packets[4][:4] == bytes([0xBF, 0x0F, 0xA0, 0x03])
    assert sleep_calls[-2:] == [2.0, 1.0]

    overlay = runtime.get_overlay_payload("AA:BB:CC:DD:EE:31")
    assert overlay["motor_a"] == 0
    assert overlay["motor_b"] == 0
    assert overlay["history"][-2]["motor_a"] == 2
    assert overlay["history"][-2]["motor_b"] == 0

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_writes_fixed_mode_packets_for_toy_device() -> None:
    created_clients: list[FakeBleakClient] = []
    sleep_calls: list[float] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        assert timeout == 6
        assert return_adv is True
        return {
            "AA:BB:CC:DD:EE:21": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:21", name="YCY-FJB-001", rssi=-45),
                SimpleNamespace(local_name="YCY-FJB-001", service_uuids=[TOY_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=6,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:21")
    client = created_clients[-1]
    client.writes.clear()

    waveform = ToyWaveform(
        id="toy-fixed-01",
        name="toy-fixed-waveform",
        steps=[
            ToyWaveformStep(duration_ms=200, control_mode="fixed_mode", fixed_mode=3, motor_mask=0x03),
            ToyWaveformStep(duration_ms=150, control_mode="fixed_mode", fixed_mode=1, motor_mask=0x04),
        ],
    )

    await runtime.play_waveform("AA:BB:CC:DD:EE:21", waveform)

    assert client.writes == [
        (TOY_WRITE_CHAR_UUID, bytes([0x35, 0x11, 0x03, 0x03, 0x4C]), False),
        (TOY_WRITE_CHAR_UUID, bytes([0x35, 0x11, 0x04, 0x01, 0x4B]), False),
        (TOY_WRITE_CHAR_UUID, bytes([0x35, 0x11, 0x07, 0x00, 0x4D]), False),
    ]
    assert sleep_calls == [0.2, 0.15]

    overlay = runtime.get_overlay_payload("AA:BB:CC:DD:EE:21")
    assert overlay["control_mode"] == ""
    assert overlay["fixed_mode"] == 0
    assert overlay["motor_mask"] == 0
    assert overlay["history"][-2]["control_mode"] == "fixed_mode"
    assert overlay["history"][-2]["fixed_mode"] == 1
    assert overlay["history"][-2]["motor_mask"] == 0x04

    await runtime.disconnect()


@pytest.mark.anyio
async def test_bleak_runtime_connect_respects_connect_timeout() -> None:
    class SlowBleakClient(FakeBleakClient):
        async def connect(self) -> None:
            await asyncio.sleep(0.05)
            self.connected = True

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        connect_timeout_seconds=0.01,
        scanner_discover=fake_discover,
        client_factory=SlowBleakClient,
    )

    await runtime.scan()

    with pytest.raises(TimeoutError):
        await runtime.connect("AA:BB:CC:DD:EE:01")


@pytest.mark.anyio
async def test_bleak_runtime_marks_disconnect_reason_when_device_drops() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")
    created_clients[-1].connected = False
    created_clients[-1].disconnected_callback(created_clients[-1])

    status = runtime.get_status()

    assert status.connected is False
    assert "断开" in status.message


@pytest.mark.anyio
async def test_bleak_runtime_auto_reconnects_after_unexpected_disconnect() -> None:
    created_clients: list[FakeBleakClient] = []

    async def fake_discover(*, timeout: float, return_adv: bool):
        return {
            "AA:BB:CC:DD:EE:01": (
                SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="YYC-DJ-V2-001", rssi=-41),
                SimpleNamespace(local_name="YYC-DJ-V2-001", service_uuids=[EMS_SERVICE_UUID]),
            ),
        }

    async def fake_sleep(_seconds: float) -> None:
        return None

    def client_factory(*args, **kwargs):
        client = FakeBleakClient(*args, **kwargs)
        created_clients.append(client)
        return client

    runtime = BleakBluetoothRuntime(
        scan_timeout_seconds=5,
        scanner_discover=fake_discover,
        client_factory=client_factory,
        sleep_func=fake_sleep,
        auto_reconnect=True,
    )

    await runtime.scan()
    await runtime.connect("AA:BB:CC:DD:EE:01")
    created_clients[-1].connected = False
    created_clients[-1].disconnected_callback(created_clients[-1])
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    status = runtime.get_status()

    assert len(created_clients) >= 2
    assert status.connected is True
    assert "重连" in status.message

