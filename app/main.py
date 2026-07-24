import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

from app.config import settings
from app.database import engine
from app.models.user import UserRoleAssignment
from app.routers import (
    actions,
    audit_logs,
    auth,
    chat,
    chat_autofill,
    chat_offer,
    chat_potential,
    internal,
    kpi_data,
    kpi_settings,
    market_view,
    old_rfqs,
    mcp_router,
    notification_logs,
    owner,
    products,
    rfq,
    routing_config,
    support,
    team_view,
    users,
)

app = FastAPI(
    title="RFQ Management API",
    version="1.0.0",
    description="Avocarbon RFQ chatbot backend - Phase 1",
)

@app.on_event("startup")
async def _create_user_roles_table() -> None:
    """Create user_roles table if it doesn't exist, then migrate existing primary roles."""
    async with engine.begin() as conn:
        await conn.run_sync(UserRoleAssignment.__table__.create, checkfirst=True)
        # Idempotent migration: seed user_roles from each user's primary role column
        await conn.execute(sa_text(
            "INSERT INTO user_roles (user_email, role) "
            "SELECT email, role::text FROM users WHERE role IS NOT NULL "
            "ON CONFLICT (user_email, role) DO NOTHING"
        ))


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = PROJECT_ROOT / "Frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"
SPA_RESERVED_PREFIXES = ("api", "docs", "redoc", "openapi.json")


def _build_allowed_origins() -> list[str]:
    origins = set(settings.frontend_urls)

    for origin in list(origins):
        parsed = urlsplit(origin)
        if parsed.scheme and parsed.hostname in {"localhost", "127.0.0.1"}:
            sibling_host = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
            sibling_netloc = sibling_host if not parsed.port else f"{sibling_host}:{parsed.port}"
            origins.add(urlunsplit((parsed.scheme, sibling_netloc, "", "", "")))

    return sorted(origins)


def _frontend_index_path() -> Path:
    return FRONTEND_DIST_DIR / "index.html"


def _is_reserved_spa_path(full_path: str) -> bool:
    return any(
        full_path == prefix or full_path.startswith(f"{prefix}/")
        for prefix in SPA_RESERVED_PREFIXES
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    # This handler runs inside ServerErrorMiddleware, which is outside CORSMiddleware.
    # The response is sent on the original (unwrapped) ASGI send, so we must add
    # CORS headers manually — otherwise the browser blocks it as a CORS error.
    origin = request.headers.get("origin", "")
    cors_headers: dict[str, str] = {}
    if origin:
        cors_headers["Access-Control-Allow-Origin"] = origin
        cors_headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(
        status_code=500,
        content={"detail": f"[{type(exc).__name__}] {str(exc)[:400]}"},
        headers=cors_headers,
    )

app.include_router(auth.router)
app.include_router(audit_logs.router)
app.include_router(notification_logs.router)
app.include_router(users.router)
app.include_router(rfq.router)
app.include_router(products.router)
app.include_router(actions.router)
app.include_router(owner.router)
app.include_router(routing_config.router)
app.include_router(support.router)
app.include_router(chat.router)
app.include_router(chat_autofill.router)
app.include_router(chat_offer.router)
app.include_router(chat_potential.router)
app.include_router(team_view.router)
app.include_router(market_view.router)
app.include_router(internal.router)
app.include_router(mcp_router.router)
app.include_router(kpi_settings.router)
app.include_router(kpi_data.router)
app.include_router(old_rfqs.router)
app.include_router(old_rfqs.subitem_router)


@app.get("/health", tags=["system"])
async def health_check():
    return {"status": "ok", "version": "1.0.0"}


if FRONTEND_ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="assets")


@app.get("/", include_in_schema=False)
async def serve_spa_root():
    index_path = _frontend_index_path()
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend build not found.")
    return FileResponse(index_path)


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    if _is_reserved_spa_path(full_path):
        raise HTTPException(status_code=404, detail="Not Found")

    if not FRONTEND_DIST_DIR.exists():
        raise HTTPException(status_code=404, detail="Frontend build not found.")

    requested_path = (FRONTEND_DIST_DIR / full_path).resolve()
    if full_path and "." in Path(full_path).name:
        try:
            requested_path.relative_to(FRONTEND_DIST_DIR.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not Found") from exc

        if requested_path.exists() and requested_path.is_file():
            return FileResponse(requested_path)
        raise HTTPException(status_code=404, detail="Not Found")

    return FileResponse(_frontend_index_path())
