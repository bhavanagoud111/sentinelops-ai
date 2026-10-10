"""Export JSON Schemas for versioned contracts.

Usage: ``uv run python scripts/export_schemas.py [--check]``
``--check`` exits non-zero if the committed schemas are stale (used in CI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel
from sentinel_contracts import AlertEvent, Incident, IncidentEvent, IncidentTransition

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "schemas"
MODELS: dict[str, type[BaseModel]] = {
    "alert_event.v1.json": AlertEvent,
    "incident.v1.json": Incident,
    "incident_transition.v1.json": IncidentTransition,
    "incident_event.v1.json": IncidentEvent,
}


def render(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale: list[str] = []
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, model in MODELS.items():
        path = SCHEMA_DIR / filename
        expected = render(model)
        if check:
            if not path.exists() or path.read_text() != expected:
                stale.append(filename)
        else:
            path.write_text(expected)
    if stale:
        print(f"stale schemas (run `make schemas`): {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
