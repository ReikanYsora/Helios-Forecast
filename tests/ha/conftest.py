"""Shared fixtures for the Home-Assistant-coupled test group.

Separate from the flat `tests/` suite on purpose: those tests exercise pure,
stdlib-only computation and CI runs them with a bare `pip install pytest`, by
design (see .github/workflows/tests.yml). This group needs the real Home
Assistant test harness (`pytest-homeassistant-custom-component`), which is
heavy and not part of that fast job.

`importorskip` makes the whole group skip cleanly, not fail, when the harness
isn't installed, so the bare CI job keeps passing untouched. Run this group
locally with the harness installed and `-o asyncio_mode=auto` (the harness's
own fixtures assume it): `pip install pytest-homeassistant-custom-component`,
then `pytest -o asyncio_mode=auto tests/ha`.
"""

import pytest

pytest.importorskip("homeassistant")

pytest_plugins = "pytest_homeassistant_custom_component"
