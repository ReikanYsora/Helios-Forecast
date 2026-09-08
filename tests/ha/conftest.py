"""Shared fixtures for the Home-Assistant-coupled test group.

Separate from the flat `tests/` suite on purpose: those exercise pure,
stdlib-only computation, while this group needs the real Home Assistant test
harness (`pytest-homeassistant-custom-component`), which is heavy. CI runs both
groups as two jobs and requires both (see .github/workflows/tests.yml).

`importorskip` makes the group skip cleanly rather than fail on a machine
without the harness. That is a convenience for the pure job and for a quick
local run, never a licence to ship on the pure suite alone: a skip here hides
every test of the recorder, the config entry and the archive migration. Run it
locally with `-o asyncio_mode=auto`, which the harness's own fixtures assume:
`pytest -o asyncio_mode=auto tests/ha`.
"""

import pytest

pytest.importorskip("homeassistant")

pytest_plugins = "pytest_homeassistant_custom_component"
