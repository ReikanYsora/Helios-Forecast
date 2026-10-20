"""Refresh phases are stable and spread requests without HA or a network."""

from custom_components.helios_forecast.refresh import refresh_phase


def test_refresh_phase_is_stable_bounded_and_uses_the_whole_window():
    phases = [refresh_phase(str(i)) for i in range(1000)]
    assert phases == [refresh_phase(str(i)) for i in range(1000)]
    assert all(5 <= minute <= 9 and 0 <= second < 60 for minute, second in phases)
    assert len(set(phases)) > 250
