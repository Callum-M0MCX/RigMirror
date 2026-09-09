"""Per-band SUB VFO alignment rules, independent of radio CAT dialects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


ALIGNMENT_STEP_HZ = 10
MAX_ALIGNMENT_HZ = 1_000


@dataclass(frozen=True, slots=True)
class AmateurBand:
    key: str
    label: str
    lower_hz: int
    upper_hz: int

    def contains(self, frequency_hz: int) -> bool:
        return self.lower_hz <= frequency_hz <= self.upper_hz


AMATEUR_BANDS = (
    AmateurBand("160m", "160 m / 1.8 MHz", 1_800_000, 2_000_000),
    AmateurBand("80m", "80 m / 3.5 MHz", 3_500_000, 4_000_000),
    AmateurBand("60m", "60 m / 5 MHz", 5_000_000, 5_500_000),
    AmateurBand("40m", "40 m / 7 MHz", 7_000_000, 7_300_000),
    AmateurBand("30m", "30 m / 10 MHz", 10_100_000, 10_150_000),
    AmateurBand("20m", "20 m / 14 MHz", 14_000_000, 14_350_000),
    AmateurBand("17m", "17 m / 18 MHz", 18_068_000, 18_168_000),
    AmateurBand("15m", "15 m / 21 MHz", 21_000_000, 21_450_000),
    AmateurBand("12m", "12 m / 24 MHz", 24_890_000, 24_990_000),
    AmateurBand("10m", "10 m / 28 MHz", 28_000_000, 29_700_000),
    AmateurBand("6m", "6 m / 50 MHz", 50_000_000, 54_000_000),
)
VALID_BAND_KEYS = frozenset(band.key for band in AMATEUR_BANDS)


def band_for_frequency(frequency_hz: int) -> Optional[AmateurBand]:
    return next((band for band in AMATEUR_BANDS if band.contains(frequency_hz)), None)


def normalise_offsets(offsets: object) -> dict[str, int]:
    """Accept persisted data defensively and retain only useful band entries."""
    if not isinstance(offsets, Mapping):
        return {}
    clean: dict[str, int] = {}
    for key, value in offsets.items():
        if key not in VALID_BAND_KEYS:
            continue
        try:
            offset = int(value)
        except (TypeError, ValueError):
            continue
        offset = max(-MAX_ALIGNMENT_HZ, min(MAX_ALIGNMENT_HZ, offset))
        # The first UI deliberately works in universally useful 10 Hz steps.
        offset = round(offset / ALIGNMENT_STEP_HZ) * ALIGNMENT_STEP_HZ
        if offset:
            clean[str(key)] = offset
    return clean


def alignment_offset(frequency_hz: int, offsets: Mapping[str, int]) -> int:
    band = band_for_frequency(frequency_hz)
    return int(offsets.get(band.key, 0)) if band is not None else 0


def aligned_sub_frequency(master_frequency_hz: int, offsets: Mapping[str, int]) -> int:
    return master_frequency_hz + alignment_offset(master_frequency_hz, offsets)


def alignment_profile_signature(
    layout: str,
    master_model: str,
    master_port: str,
    sub_model: str,
    sub_port: str,
) -> str:
    """Bind corrections to an ordered physical/configured radio pairing."""
    parts = (
        layout.strip().casefold(), master_model.strip().casefold(),
        master_port.strip().casefold(), sub_model.strip().casefold(),
        sub_port.strip().casefold(),
    )
    return "|".join(parts)
