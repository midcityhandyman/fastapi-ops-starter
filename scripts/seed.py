"""Load the synthetic seed events into an already migrated database.

Usage: python scripts/seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_dotenv, load_settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.migrate import pending_migrations  # noqa: E402
from app.seed import seed  # noqa: E402


def main() -> int:
    load_dotenv()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    if pending_migrations(engine):
        print("database has pending migrations; run python scripts/migrate.py first", file=sys.stderr)
        return 1
    counts = seed(engine, max_attempts=settings.job_max_attempts)
    print(f"seed: {counts['created']} created, {counts['duplicate']} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
