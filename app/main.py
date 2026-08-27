from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    activities,
    application_forms,
    auth,
    health,
    media_assets,
    media_rules,
    public_submissions,
    users,
)
from app.core.config import get_settings
from app.db.session import engine, init_models

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.create_tables:
        await init_models()
    yield
    await engine.dispose()


app = FastAPI(
    title="GeoMidia API",
    version="1.0.0",
    description="API para inventario, analise territorial e mapa GIS de midia exterior.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(health.router)
app.include_router(auth.router, prefix="/api")
app.include_router(media_assets.router, prefix="/api")
app.include_router(activities.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(media_rules.router, prefix="/api")
app.include_router(application_forms.router, prefix="/api")
app.include_router(public_submissions.router, prefix="/api")
