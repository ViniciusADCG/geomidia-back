from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import activities, auth, health, media_assets
from app.core.config import get_settings
from app.db.session import init_models


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.create_tables:
        await init_models()
    yield


app = FastAPI(
    title="GeoMidia API",
    version="1.0.0",
    description="API para inventario, analise territorial e mapa GIS de midia exterior.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/api")
app.include_router(media_assets.router, prefix="/api")
app.include_router(activities.router, prefix="/api")
