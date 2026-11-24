"""Apply database migrations.

Usage:
  python scripts/migrate.py            apply everything pending
  python scripts/migrate.py --status   list applied and pending, change nothing
  python scripts/migrate.py --seed     apply, then load the synthetic seed events

The target database comes from DATABASE_URL (or .env), default SQLite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_dotenv, load_settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.migrate import MigrationError, applied_versions, apply_migrations, discover  # noqa: E402
from app.seed import seed  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply database migrations.")
    parser.add_argument("--status", action="store_true", help="show state and exit")
    parser.add_argument("--seed", action="store_true", help="load seed events after migrating")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url)
    print(f"database: {engine.url.render_as_string(hide_password=True)}")

    try:
        if args.status:
            applied = applied_versions(engine)
            for migration in discover():
                state = "applied" if migration.version in applied else "pending"
                print(f"  {migration.version}_{migration.name}: {state}")
            return 0

        newly_applied = apply_migrations(engine)
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1

    if newly_applied:
        for migration in newly_applied:
            print(f"  applied {migration.version}_{migration.name}")
    else:
        print("  nothing to apply, schema is current")

    if args.seed:
        counts = seed(engine, max_attempts=settings.job_max_attempts)
        print(f"  seed: {counts['created']} created, {counts['duplicate']} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
