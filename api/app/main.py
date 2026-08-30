import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.error_handlers import register_error_handlers
from app.middleware import RequestContextMiddleware
from app.routers import audit, auth, comparisons, health, inventory, monitoring, projects, runs, settings, users

app_settings = get_settings()
app_env = app_settings.app_env.lower()
docs_enabled = app_env in {"development", "dev", "testing", "test"}

logging.basicConfig(
    level=getattr(logging, app_settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Share Sentinel API",
    version="1.3.0",
    root_path=app_settings.api_root_path,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)
register_error_handlers(app)

origins = [item.strip() for item in app_settings.cors_origins.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        app_settings.auth_csrf_header_name,
        runs.RAW_ARTIFACT_FILENAME_HEADER,
    ],
    expose_headers=["Content-Disposition", "X-Request-ID"],
)
trusted_hosts = [item.strip() for item in app_settings.trusted_hosts.split(",") if item.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
app.add_middleware(RequestContextMiddleware)

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(runs.router)
app.include_router(comparisons.router)
app.include_router(monitoring.router)
app.include_router(inventory.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(settings.router)
app.include_router(health.router)
