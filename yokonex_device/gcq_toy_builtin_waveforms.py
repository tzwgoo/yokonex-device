"""灌肠机内置波形预设。"""
from __future__ import annotations

from yokonex_device.models import ToyWaveform
from yokonex_device.models import ToyWaveformStep


GCQ_TOY_VALVE_MAX = 1
GCQ_TOY_PUMP_LEVEL_MAX = 5

PRESET_NAMES = [
    "预充气",
    "缓慢灌注",
    "递进充盈",
    "波浪推送",
    "脉冲点射",
    "深推循环",
    "呼吸放松",
    "交替增压",
    "冲刷节奏",
    "峰值爆发",
]

# (duration_ms, valve, air_pump, water_pump)
# 灌肠机内置波形默认采用“先充压、最后放阀”的节奏：
# 前面的分段主要控制气泵和水泵，最后一个分段再打开气阀做释放。
PRESET_PATTERNS: list[list[tuple[int, int, int, int]]] = [
    [(240, 0, 1, 0), (240, 0, 2, 0), (220, 0, 1, 0), (260, 1, 0, 0)],
    [(260, 0, 2, 1), (260, 0, 2, 1), (260, 0, 2, 1), (280, 1, 0, 0)],
    [(180, 0, 1, 1), (180, 0, 2, 1), (180, 0, 2, 2), (180, 0, 3, 2), (260, 1, 0, 0)],
    [(150, 0, 2, 1), (150, 0, 1, 2), (150, 0, 3, 1), (150, 0, 1, 3), (240, 1, 0, 0)],
    [(100, 0, 3, 2), (80, 0, 0, 0), (100, 0, 3, 2), (80, 0, 0, 0), (120, 0, 3, 2), (220, 1, 0, 0)],
    [(180, 0, 3, 2), (180, 0, 3, 3), (180, 0, 4, 3), (220, 1, 0, 0)],
    [(220, 0, 1, 1), (220, 0, 2, 1), (220, 0, 1, 1), (260, 1, 0, 0)],
    [(140, 0, 3, 1), (140, 0, 1, 3), (140, 0, 3, 1), (140, 0, 1, 3), (240, 1, 0, 0)],
    [(120, 0, 2, 4), (120, 0, 2, 3), (120, 0, 3, 5), (120, 0, 2, 3), (220, 1, 0, 0)],
    [(90, 0, 4, 3), (90, 0, 5, 4), (90, 0, 5, 5), (120, 0, 4, 5), (240, 1, 0, 0)],
]

PRESET_WAVEFORM_IDS = {f"gcq-toy-preset-{index:02}" for index in range(1, len(PRESET_PATTERNS) + 1)}


def create_gcq_toy_defaults() -> list[ToyWaveform]:
    return [_create_preset(index) for index in range(1, len(PRESET_PATTERNS) + 1)]


def is_gcq_toy_preset_waveform_id(waveform_id: str | None) -> bool:
    return str(waveform_id or "").lower() in PRESET_WAVEFORM_IDS


def _create_preset(mode: int) -> ToyWaveform:
    mode_index = max(1, min(mode, len(PRESET_PATTERNS)))
    steps = [
        ToyWaveformStep(
            duration_ms=duration_ms,
            motor_a=_clamp_valve_state(motor_a),
            motor_b=_clamp_pump_level(motor_b),
            motor_c=_clamp_pump_level(motor_c),
        )
        for duration_ms, motor_a, motor_b, motor_c in PRESET_PATTERNS[mode_index - 1]
    ]
    return ToyWaveform(
        id=f"gcq-toy-preset-{mode_index:02}",
        name=f"灌肠机预设 {mode_index:02} - {PRESET_NAMES[mode_index - 1]}",
        builtin=True,
        editable=False,
        device_family="gcq",
        loop_count=1,
        steps=steps,
    )


def _clamp_valve_state(value: int) -> int:
    return max(0, min(int(value), GCQ_TOY_VALVE_MAX))


def _clamp_pump_level(value: int) -> int:
    return max(0, min(int(value), GCQ_TOY_PUMP_LEVEL_MAX))
