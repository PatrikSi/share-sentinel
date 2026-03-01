import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.middleware import RequestContextMiddleware
from app.routers import audit, auth, health, inventory, projects, runs, users

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Share Sentinel API",
    version="0.1.0",
    root_path=settings.api_root_path,
)

origins = [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
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
app.include_router(health.router)
