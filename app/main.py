from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import actions, auth, chat, chat_potential, owner, products, rfq, users

app = FastAPI(
    title="RFQ Management API",
    version="1.0.0",
    description="Avocarbon RFQ chatbot backend - Phase 1",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = PROJECT_ROOT / "Frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"
SPA_RESERVED_PREFIXES = ("api", "docs", "redoc", "openapi.json")


def _build_allowed_origins() -> list[str]:
    origins = {settings.frontend_url}
    parsed = urlsplit(settings.frontend_url)

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

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(rfq.router)
app.include_router(products.router)
app.include_router(actions.router)
app.include_router(owner.router)
app.include_router(chat.router)
app.include_router(chat_potential.router)


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
