import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.middleware import RequestContextMiddleware
from app.routers import audit, auth, health, inventory, projects, runs, settings, users

app_settings = get_settings()
app_env = app_settings.app_env.lower()
docs_enabled = app_env in {"development", "dev", "testing", "test"}

logging.basicConfig(
    level=getattr(logging, app_settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Share Sentinel API",
    version="0.1.0",
    root_path=app_settings.api_root_path,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

origins = [item.strip() for item in app_settings.cors_origins.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(runs.router)
app.include_router(inventory.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(settings.router)
app.include_router(health.router)
