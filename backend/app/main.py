"""FastAPI application factory."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import create_router
from .config import Settings, config_problems, load_settings
from .errors import AppError
from .security import RedactingFilter
from .state import AppState, build_state

log = logging.getLogger("talkdox")


def _configure_logging(env: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    f = RedactingFilter()
    for h in root.handlers:
        h.addFilter(f)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def create_app(settings: Settings | None = None, state: AppState | None = None, **overrides) -> FastAPI:
    st = state or build_state(settings or load_settings(), **overrides)
    s = st.settings
    _configure_logging(s.env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orphaned = st.db.fail_orphaned_jobs()
        if orphaned:
            log.warning("marked %d interrupted ingest job(s) as failed", orphaned)
        for p in config_problems(s):
            log.warning(p)
        stop = threading.Event()

        def purge_loop():
            while not stop.wait(3600):
                try:
                    n = st.db.purge_expired(s.retention_days)
                    if n:
                        log.info("retention purge removed %d workspace(s)", n)
                except Exception:
                    log.exception("retention purge failed")

        if s.retention_days > 0:
            threading.Thread(target=purge_loop, daemon=True, name="purge").start()
        log.info("TalkDox ready: llm=%s embeddings=%s ocr=%s", st.llm.provider + ":" + st.llm.model if st.llm else None,
                 st.embedder.fingerprint if st.embedder else None, st.ocr.name if st.ocr else None)
        yield
        stop.set()
        st.ingest.shutdown()
        st.db.close()

    app = FastAPI(title="TalkDox API", version="2.0.0", lifespan=lifespan,
                  docs_url="/api/docs" if not s.is_prod else None, openapi_url="/api/openapi.json" if not s.is_prod else None,
                  redoc_url=None)
    app.state.talkdox = st

    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type"], expose_headers=["X-Request-ID"], max_age=600)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        cl = request.headers.get("content-length")
        limit = (s.max_upload_mb * 10 + 1) * 1024 * 1024
        if cl and cl.isdigit() and int(cl) > limit:
            return JSONResponse({"error": "Request body too large.", "code": "payload_too_large"}, status_code=413)
        t0 = time.time()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            resp = JSONResponse({"error": "Something went wrong on our side.", "code": "internal_error",
                                 "request_id": rid}, status_code=500)
        resp.headers["X-Request-ID"] = rid
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        if s.is_prod:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        log.info("%s %s %s %dms rid=%s", request.method, request.url.path, resp.status_code,
                 int((time.time() - t0) * 1000), rid)
        return resp

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        headers = {}
        if exc.code == "rate_limited" and exc.detail.get("retry_after"):
            headers["Retry-After"] = str(exc.detail["retry_after"])
        return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"error": f"Invalid request: {loc or 'body'} - {first.get('msg', 'invalid value')}",
                             "code": "invalid_request"}, status_code=422)

    app.include_router(create_router(st))

    @app.get("/")
    def root():
        return {"service": "TalkDox API", "docs": "/api/docs" if not s.is_prod else None, "health": "/api/health"}

    return app
