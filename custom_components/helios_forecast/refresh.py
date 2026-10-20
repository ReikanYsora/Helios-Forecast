"""Stable refresh phases spread hourly requests across installations."""

import hashlib


def refresh_phase(entry_id: str) -> tuple[int, int]:
    """Stay near the hour boundary without a fleet-wide request instant."""
    phase = int.from_bytes(hashlib.sha256(entry_id.encode()).digest()[:8], "big")
    return 5 + phase % 5, (phase // 5) % 60
