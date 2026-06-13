"""三通道飞机杯内置预设波形。"""
from __future__ import annotations

from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep


TOY_SPEED_MAX = 20

PRESET_NAMES = [
    "轻抚",
    "律动",
    "渐强",
    "波浪",
    "连击",
    "脉冲",
    "旋转",
    "交替",
    "颤动",
    "弹跳",
]

# (duration_ms, motor_a, motor_b, motor_c)
PRESET_PATTERNS: list[list[tuple[int, int, int, int]]] = [
    [(200, 3, 0, 0), (200, 5, 0, 0), (200, 3, 0, 0), (300, 0, 0, 0)],
    [(150, 5, 3, 0), (150, 8, 5, 0), (150, 5, 3, 0), (200, 0, 0, 0)],
    [(120, 2, 0, 0), (120, 5, 2, 0), (120, 8, 5, 0), (120, 12, 8, 0), (120, 16, 12, 0), (200, 0, 0, 0)],
    [(100, 3, 0, 0), (100, 6, 3, 0), (100, 10, 6, 0), (100, 6, 10, 0), (100, 3, 6, 0), (100, 0, 3, 0), (200, 0, 0, 0)],
    [(80, 15, 10, 0), (60, 0, 0, 0), (80, 15, 10, 0), (60, 0, 0, 0), (80, 15, 10, 0), (200, 0, 0, 0)],
    [(100, 12, 8, 5), (150, 0, 0, 0), (100, 12, 8, 5), (250, 0, 0, 0)],
    [(120, 8, 0, 0), (120, 0, 8, 0), (120, 0, 0, 8), (120, 0, 8, 0), (200, 0, 0, 0)],
    [(100, 10, 0, 0), (100, 0, 10, 0), (100, 10, 0, 0), (100, 0, 10, 0), (200, 0, 0, 0)],
    [(60, 20, 15, 10), (60, 0, 0, 0), (60, 20, 15, 10), (60, 0, 0, 0), (60, 20, 15, 10), (60, 0, 0, 0), (200, 0, 0, 0)],
    [(80, 5, 0, 0), (80, 10, 5, 0), (80, 15, 10, 5), (120, 20, 15, 10), (200, 0, 0, 0)],
]

PRESET_WAVEFORM_IDS = {f"toy-preset-{index:02}" for index in range(1, len(PRESET_PATTERNS) + 1)}


def create_toy_defaults() -> list[ToyWaveform]:
    waveforms: list[ToyWaveform] = []
    for mode in range(1, len(PRESET_PATTERNS) + 1):
        waveforms.append(_create_preset(mode))
    return waveforms


def _create_preset(mode: int) -> ToyWaveform:
    mode_index = max(1, min(mode, len(PRESET_PATTERNS)))
    steps = [
        ToyWaveformStep(
            duration_ms=duration_ms,
            motor_a=_clamp_speed(motor_a),
            motor_b=_clamp_speed(motor_b),
            motor_c=_clamp_speed(motor_c),
        )
        for duration_ms, motor_a, motor_b, motor_c in PRESET_PATTERNS[mode_index - 1]
    ]
    return ToyWaveform(
        id=f"toy-preset-{mode_index:02}",
        name=f"Toy 预设 {mode_index:02} - {PRESET_NAMES[mode_index - 1]}",
        builtin=True,
        editable=False,
        device_family="toy",
        loop_count=1,
        steps=steps,
    )


def is_toy_preset_waveform_id(waveform_id: str | None) -> bool:
    return str(waveform_id or "").lower() in PRESET_WAVEFORM_IDS


def _clamp_speed(value: int) -> int:
    return max(0, min(int(value), TOY_SPEED_MAX))
