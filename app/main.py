from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import actions, auth, chat, owner, products, rfq, users

app = FastAPI(
    title="RFQ Management API",
    version="1.0.0",
    description="Avocarbon RFQ chatbot backend - Phase 1",
)


def _build_allowed_origins() -> list[str]:
    origins = {settings.frontend_url}
    parsed = urlsplit(settings.frontend_url)

    if parsed.scheme and parsed.hostname in {"localhost", "127.0.0.1"}:
        sibling_host = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
        sibling_netloc = sibling_host if not parsed.port else f"{sibling_host}:{parsed.port}"
        origins.add(urlunsplit((parsed.scheme, sibling_netloc, "", "", "")))

    return sorted(origins)


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


@app.get("/health", tags=["system"])
async def health_check():
    return {"status": "ok", "version": "1.0.0"}
