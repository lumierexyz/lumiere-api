import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.database import registry  # noqa: F401  (registers models)
from app.database.session import get_engine

DESCRIPTION = """
Lumiere financial intelligence API.

All figures are computed deterministically from your imported transactions and official statistics
(FRED, Eurostat). Forecasts, coverage estimates and portfolio results are conditional estimates and
simulations, not guarantees or investment advice. Lumiere does not hold funds or execute trades.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Lumiere API",
        version="0.1.0",
        description=DESCRIPTION,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        client = request.client.host if request.client else "unknown"
        try:
            limiter.check(f"ip:{client}", settings.rate_limit_per_minute * 2)
        except Exception:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": "Too many requests.", "details": None}},
            )
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        logger.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "user_id": getattr(request.state, "user_id", None),
            },
        )
        return response

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready() -> JSONResponse:
        checks = {
            "database": False,
            "auth_configured": bool(settings.jwt_issuer),
            "fred_configured": bool(settings.fred_api_key),
            "openai_configured": bool(settings.openai_api_key),
        }
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            logger.warning("readiness_db_failed")
        ok = checks["database"] and checks["auth_configured"]
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not_ready", "checks": checks},
        )

    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
