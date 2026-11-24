"""Load the synthetic seed events through the normal ingestion path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from app.ingest import ingest_event

SEED_FILE = Path(__file__).resolve().parent.parent / "seeds" / "events.json"


def load_seed_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Return a list of {"source": ..., "payload": {...}} entries."""
    data = json.loads((path or SEED_FILE).read_text(encoding="utf-8"))
    return data["events"]


def seed(engine: Engine, path: Path | None = None, max_attempts: int = 4) -> dict[str, int]:
    """Ingest every seed event. Running it twice creates nothing new."""
    counts = {"created": 0, "duplicate": 0}
    for entry in load_seed_events(path):
        result = ingest_event(engine, entry["source"], entry["payload"], max_attempts=max_attempts)
        counts["created" if result.created else "duplicate"] += 1
    return counts
