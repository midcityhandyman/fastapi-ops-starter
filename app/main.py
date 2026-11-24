"""Application factory.

Run with ``python -m app serve`` or
``uvicorn app.main:create_app --factory``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from app import SERVICE_NAME, __version__
from app.api import public, secured
from app.config import Settings, load_settings
from app.db import make_engine
from app.jobs import Worker
from app.logging_setup import request_id_var
from app.notify import Notifier, build_notifier

log = logging.getLogger("ops.app")
access_log = logging.getLogger("ops.access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    notifier: Notifier | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    owns_engine = engine is None
    engine = engine or make_engine(settings.database_url)
    worker = Worker(engine, settings, notifier or build_notifier(settings))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.using_dev_key:
            log.warning("no OPS_API_KEYS set; accepting the built in development key only")
        if settings.worker_enabled:
            worker.start()
        log.info("startup complete", extra={"env": settings.env, "dialect": engine.dialect.name})
        try:
            yield
        finally:
            worker.stop()
            if owns_engine:
                engine.dispose()
            log.info("shutdown complete")

    app = FastAPI(
        title=SERVICE_NAME,
        version=__version__,
        description="Slim operations API: keyed ingestion, one record table, background jobs.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.worker = worker

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - last line of defence
                log.exception("unhandled error")
                response = JSONResponse(
                    {"detail": "internal server error", "request_id": request_id}, status_code=500
                )
            response.headers["X-Request-ID"] = request_id
            access_log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "api_key": getattr(request.state, "api_key_name", None),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    app.include_router(public)
    app.include_router(secured)
    return app
