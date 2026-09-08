"""The check-up's problems as Home Assistant repair issues.

One issue per problem, keyed by the entry so two entries never share one, created or refreshed
after every check and deleted the moment the problem is gone: a corrected configuration clears
its own issues without anyone dismissing anything. Text and placeholders come from the
`issues` block of the translations, under the problem's key.
"""

from __future__ import annotations

from typing import Iterable, Set

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .checkup import ERROR, Problem
from .const import DOMAIN


def _issue_id(entry: ConfigEntry, problem: Problem) -> str:
    return f"{entry.entry_id}_{problem.issue_id}"


def sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    problems: Iterable[Problem],
    previous: Set[str],
    *,
    retire: bool = True,
) -> Set[str]:
    """Publish `problems`, retire the issues of `previous` that are no longer among them, and return
    the ids now published so the next sync knows what to retire.

    ``retire=False`` only adds. A caller publishing part of the list, as the configuration checks do
    before any data is fetched, would otherwise delete every issue the data checks raised and create
    it again seconds later. Deleting an issue throws away the registry entry, and with it the user's
    decision to ignore that one, forty-eight times a day on a thirty-minute refresh.
    """
    current = {_issue_id(entry, p): p for p in problems}
    for issue_id in (previous - set(current)) if retire else ():
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, problem in current.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR if problem.severity == ERROR else ir.IssueSeverity.WARNING,
            translation_key=problem.key,
            translation_placeholders={"entry": entry.title, **problem.placeholders},
        )
    return set(current) if retire else previous | set(current)


def clear(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Retire every issue of this entry (the entry is being removed)."""
    registry = ir.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
