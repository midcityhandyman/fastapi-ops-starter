"""Command line entry point: ``python -m app <command>``.

Commands:
  serve    run the API (and the in process worker unless WORKER_ENABLED=false)
  worker   run a standalone worker process
  genkey   print a new API key and the hashed OPS_API_KEYS entry for it
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys

from app.config import ConfigError, hash_key, load_dotenv, load_settings
from app.db import make_engine
from app.jobs import Worker
from app.logging_setup import configure_logging
from app.migrate import pending_migrations
from app.notify import build_notifier

log = logging.getLogger("ops.cli")


def _serve() -> int:
    import uvicorn

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )
    return 0


def _worker() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.database_url)
    pending = pending_migrations(engine)
    if pending:
        log.error("database has pending migrations; run scripts/migrate.py first")
        return 1
    worker = Worker(engine, settings, build_notifier(settings))
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        log.info("worker interrupted")
    return 0


def _genkey(name: str) -> int:
    key = secrets.token_urlsafe(32)
    print(f"API key (give this to the caller, it is not stored): {key}")
    print(f"OPS_API_KEYS entry (put this on the server):        {name}:sha256:{hash_key(key)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the API")
    sub.add_parser("worker", help="run a standalone worker")
    genkey = sub.add_parser("genkey", help="generate an API key")
    genkey.add_argument("--name", default="client", help="label for the key (default: client)")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        if args.command == "serve":
            return _serve()
        if args.command == "worker":
            return _worker()
        return _genkey(args.name)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
