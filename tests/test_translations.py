"""The 27 translation files against strings.json.

Home Assistant has no per-key fallback here: a key a locale is missing simply does not render,
and a placeholder a locale names but the code does not pass raises at runtime, in that language
only. Nothing checked this, on a release that hand-edited every one of the files.
Runnable with ``python3 tests/test_translations.py`` or under pytest.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_COMPONENT = _REPO_ROOT / "custom_components" / "helios_forecast"
_STRINGS = _COMPONENT / "strings.json"
_TRANSLATIONS = sorted((_COMPONENT / "translations").glob("*.json"))

# The one string that is the same everywhere because it is the product's name.
_MAY_MATCH_ENGLISH = {"options.step.init.title"}
# And the languages where a word happens to be spelled as in English.
_MAY_MATCH_ENGLISH_BY_LANG = {
    "fr": {
        "services.get_forecast.fields.config_entry_id.name",
        "services.get_battery_soc_forecast.fields.config_entry_id.name",
    },
}


def _flat(obj: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in obj.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _placeholders(text: object) -> set:
    return set(re.findall(r"\{(\w+)\}", text)) if isinstance(text, str) else set()


def test_there_is_a_translation_for_every_language_the_repository_claims() -> None:
    assert len(_TRANSLATIONS) == 27
    assert (_COMPONENT / "translations" / "en.json").exists()


def test_every_locale_carries_exactly_the_keys_of_strings_json() -> None:
    base = _flat(json.loads(_STRINGS.read_text(encoding="utf-8")))
    assert base, "strings.json is empty"
    for path in _TRANSLATIONS:
        local = _flat(json.loads(path.read_text(encoding="utf-8")))
        assert set(local) == set(base), (
            f"{path.name}: missing {sorted(set(base) - set(local))}, extra {sorted(set(local) - set(base))}"
        )


def test_no_locale_names_a_placeholder_the_code_does_not_pass() -> None:
    base = _flat(json.loads(_STRINGS.read_text(encoding="utf-8")))
    for path in _TRANSLATIONS:
        local = _flat(json.loads(path.read_text(encoding="utf-8")))
        for key, text in local.items():
            assert _placeholders(text) == _placeholders(base[key]), f"{path.name}: {key}"


def test_nothing_ships_as_untranslated_english() -> None:
    base = _flat(json.loads(_STRINGS.read_text(encoding="utf-8")))
    for path in _TRANSLATIONS:
        lang = path.stem
        if lang == "en":
            continue
        allowed = _MAY_MATCH_ENGLISH | _MAY_MATCH_ENGLISH_BY_LANG.get(lang, set())
        local = _flat(json.loads(path.read_text(encoding="utf-8")))
        untranslated = [k for k, v in local.items() if v == base[k] and k not in allowed]
        assert not untranslated, f"{path.name}: still English -> {untranslated}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all translation tests passed")
