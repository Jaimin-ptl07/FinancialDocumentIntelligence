"""Regenerate the normalized taxonomy + v4 field-extraction schema from the
committed raw grid. Deterministic; run after `dump_taxonomy_grid.py`.

Runnable with the backend venv (pure `fdi.core`, no openpyxl):
    backend/.venv/Scripts/python.exe scripts/build_taxonomy_schema.py

Reads:  docs/architecture/taxonomy/taxonomy.raw.json
Writes: docs/architecture/taxonomy/taxonomy.normalized.json
        docs/architecture/field-extraction-schema.v4.json
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from fdi.core.schema import validate_extraction_schema  # noqa: E402
from fdi.core.schema_builder import build_schema  # noqa: E402
from fdi.core.taxonomy import parse_taxonomy  # noqa: E402

RAW = ROOT / "docs" / "architecture" / "taxonomy" / "taxonomy.raw.json"
NORM = ROOT / "docs" / "architecture" / "taxonomy" / "taxonomy.normalized.json"
SCHEMA = ROOT / "docs" / "architecture" / "field-extraction-schema.v4.json"


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    normalized = parse_taxonomy(raw)
    NORM.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    schema = build_schema(normalized)
    errors = validate_extraction_schema(schema)
    if errors:
        print("SCHEMA VALIDATION FAILED:")
        for e in errors[:40]:
            print("  -", e)
        return 1
    SCHEMA.write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")

    n_tags = sum(
        1
        for st in normalized["statements"]
        for r in st["rows"]
        if r["tag"] and not r["excluded"]
    )
    print(f"OK  statements={len(normalized['statements'])} "
          f"tags={n_tags} warnings={len(normalized['warnings'])}")
    print(f"    wrote {NORM.name} and {SCHEMA.name}")
    for w in normalized["warnings"]:
        print("    warn:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
