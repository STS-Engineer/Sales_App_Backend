from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, users, rfq, products, actions, owner, chat

app = FastAPI(
    title="RFQ Management API",
    version="1.0.0",
    description="Avocarbon RFQ chatbot backend — Phase 1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
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
