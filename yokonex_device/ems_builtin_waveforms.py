from __future__ import annotations

from yokonex_device.models import EmsWaveform
from yokonex_device.models import EmsWaveformStep


CURRENT_BUILTIN_PRESET_VERSION = 2
RECOMMENDED_MIN_STRENGTH = 40
RECOMMENDED_MAX_STRENGTH = 50
SOURCE_MIN_POSITIVE_STRENGTH = 6
SOURCE_MAX_POSITIVE_STRENGTH = 30

PRESET_NAMES = [
    "呼吸",
    "潮汐",
    "连击",
    "快速按捏",
    "按捏渐强",
    "心跳节奏",
    "压缩",
    "节奏步伐",
    "颗粒摩擦",
    "渐变弹跳",
    "波浪涟漪",
    "雨水冲刷",
    "变速敲击",
    "信号灯",
    "挑逗1",
    "挑逗2",
]

PRESET_PATTERNS = [
    [(140, 6), (140, 10), (140, 14), (140, 18), (140, 24), (140, 30), (220, 0)],
    [(120, 6), (120, 10), (120, 14), (120, 18), (120, 22), (120, 26), (120, 22), (120, 18), (120, 14)],
    [(70, 28), (60, 0), (70, 28), (90, 18), (180, 0)],
    [(120, 30)],
    [(90, 8), (90, 0), (90, 14), (90, 0), (90, 20), (90, 0), (90, 26), (90, 0), (90, 30)],
    [(90, 30), (60, 0), (60, 18), (220, 0), (90, 26), (60, 0), (60, 14), (280, 0)],
    [(45, 30), (20, 0), (45, 30), (28, 0), (45, 30), (36, 0), (45, 30), (44, 0), (45, 30), (52, 0), (45, 30), (60, 0), (45, 30), (68, 0), (45, 30)],
    [(50, 6), (80, 0), (50, 18), (70, 0), (50, 8), (60, 0), (50, 24), (50, 0), (50, 12), (40, 0), (50, 28), (30, 0), (50, 16)],
    [(180, 28), (180, 28), (180, 28), (180, 28), (160, 0)],
    [(100, 4), (110, 0), (120, 12), (90, 0), (160, 30)],
    [(80, 6), (90, 0), (130, 30), (120, 22)],
    [(120, 10), (120, 14), (120, 18), (120, 24), (120, 30), (220, 0)],
    [(120, 24), (90, 0), (120, 24), (90, 0), (120, 24), (180, 0), (70, 24), (50, 0), (70, 24), (50, 0), (70, 24), (50, 0), (70, 24)],
    [(80, 28), (45, 0), (80, 28), (45, 0), (80, 28), (45, 0), (80, 28), (120, 0), (60, 8), (70, 0), (80, 18), (120, 0), (90, 30)],
    [(90, 6), (90, 10), (90, 14), (90, 18), (90, 24), (260, 0), (110, 30)],
    [(90, 4), (90, 7), (90, 10), (90, 13), (90, 16), (90, 19), (90, 22), (90, 25), (90, 28), (120, 30)],
]

PRESET_WAVEFORM_IDS = {f"ems-preset-{index:02}" for index in range(1, 17)}


def create_defaults() -> list[EmsWaveform]:
    waveforms = [
        EmsWaveform(
            id="ems-default-pulse",
            name="EMS 默认波形",
            builtin=True,
            editable=False,
            execution_mode="fixed",
            loop_count=1,
            steps=[EmsWaveformStep()],
        )
    ]
    for mode in range(1, 17):
        waveforms.append(create_preset(mode))
    return waveforms


def create_preset(mode: int) -> EmsWaveform:
    mode_index = max(1, min(mode, len(PRESET_PATTERNS)))
    steps = [
        EmsWaveformStep(
            duration_ms=duration_ms,
            channel_a=map_preset_strength(strength),
            channel_a_mode=mode_index,
            channel_b=map_preset_strength(strength),
            channel_b_mode=mode_index,
        )
        for duration_ms, strength in PRESET_PATTERNS[mode_index - 1]
    ]
    return EmsWaveform(
        id=f"ems-preset-{mode_index:02}",
        name=f"EMS 预设 {mode_index:02} - {PRESET_NAMES[mode_index - 1]}",
        builtin=True,
        editable=False,
        execution_mode="fixed",
        loop_count=1,
        steps=steps,
    )


def is_preset_waveform_id(waveform_id: str | None) -> bool:
    return str(waveform_id or "").lower() in PRESET_WAVEFORM_IDS


def map_preset_strength(strength: int) -> int:
    if strength <= 0:
        return 0
    clamped = max(SOURCE_MIN_POSITIVE_STRENGTH, min(strength, SOURCE_MAX_POSITIVE_STRENGTH))
    ratio = (clamped - SOURCE_MIN_POSITIVE_STRENGTH) / (SOURCE_MAX_POSITIVE_STRENGTH - SOURCE_MIN_POSITIVE_STRENGTH)
    return RECOMMENDED_MIN_STRENGTH + round(ratio * (RECOMMENDED_MAX_STRENGTH - RECOMMENDED_MIN_STRENGTH))
