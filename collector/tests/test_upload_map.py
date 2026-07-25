# Drift guard for app/upload_map.py against the vendored catalog
# (shared/can_messages.toml). Runs with or without pytest:
#
#   cd collector && ../.venv/bin/python tests/test_upload_map.py     # standalone
#   cd collector && ../.venv/bin/python -m pytest tests/             # if pytest installed
#
# Fails if the map references a message or field the catalog doesn't define, if a
# decoded message type is missing from the map, or if the map's field set doesn't
# exactly cover a dataclass's fields. Re-run after `shared/sync.sh`.

import os
import sys
import tomllib
from dataclasses import fields as dc_fields

# Allow `python tests/test_upload_map.py` from the collector/ dir to import `app`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import parsed_tables, upload_map

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CATALOG = os.path.join(_REPO_ROOT, "shared", "can_messages.toml")


def _load_catalog() -> dict[str, set]:
    """message/radio_packet name -> set of its field names (bytes + bits)."""
    assert os.path.exists(_CATALOG), f"vendored catalog missing at {_CATALOG}"
    with open(_CATALOG, "rb") as f:
        doc = tomllib.load(f)
    catalog: dict[str, set] = {}
    for section in ("message", "radio_packet"):
        for entry in doc.get(section, []):
            names = set()
            for part in ("bytes", "bits"):
                for field in entry.get(part, []):
                    names.add(field["name"])
            catalog[entry["name"]] = names
    return catalog


CATALOG = _load_catalog()


def test_every_decoded_message_is_mapped():
    """Every dataclass the collector stores/uploads has an entry in UPLOAD_MAP."""
    missing = [c.__name__ for c in parsed_tables.PARSED_MESSAGES
               if c not in upload_map.UPLOAD_MAP]
    assert not missing, f"unmapped message types: {missing}"


def test_map_covers_exactly_the_dataclass_fields():
    """The map lists every field of each dataclass, and nothing extra."""
    for cls, m in upload_map.UPLOAD_MAP.items():
        mapped = set(m.fields)
        actual = {f.name for f in dc_fields(cls)}
        assert mapped == actual, (
            f"{cls.__name__}: map fields {mapped} != dataclass fields {actual}"
        )


def test_mapped_names_exist_in_catalog():
    """Message name and every non-local output field exist in the catalog."""
    for cls, m in upload_map.UPLOAD_MAP.items():
        assert m.toml_name in CATALOG, (
            f"{cls.__name__}: message '{m.toml_name}' not in catalog"
        )
        catalog_fields = CATALOG[m.toml_name]
        for out in m.fields.values():
            if out in m.local:
                continue
            assert out in catalog_fields, (
                f"{cls.__name__}: field '{out}' not in catalog message "
                f"'{m.toml_name}' (available: {sorted(catalog_fields)})"
            )


def test_local_fields_are_declared_in_fields():
    """Every 'local' output name is actually one of the map's outputs."""
    for cls, m in upload_map.UPLOAD_MAP.items():
        outputs = set(m.fields.values())
        assert m.local <= outputs, (
            f"{cls.__name__}: local names {m.local - outputs} not among outputs"
        )


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as err:
            failed += 1
            print(f"FAIL {t.__name__}: {err}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
