import logging
from pathlib import Path

from app.config import get_settings
from app.seed import main as seed_main

logger = logging.getLogger("share_sentinel.bootstrap")
PRODUCTION_ENVS = {"production", "prod", "staging", "stage"}


def ensure_artifact_storage_path(storage_path: str) -> bool:
    path = Path(storage_path)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    return created


def validate_seed_admin_settings(settings) -> None:
    if settings.app_env.lower() in PRODUCTION_ENVS and (
        not settings.seed_admin_email or not settings.seed_admin_password
    ):
        raise RuntimeError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must both be set for production bootstrap")


def main() -> None:
    settings = get_settings()
    validate_seed_admin_settings(settings)
    created_path = ensure_artifact_storage_path(settings.artifact_storage_path)
    logger.info("artifact storage ready path=%s created=%s", settings.artifact_storage_path, created_path)

    seed_main()


if __name__ == "__main__":
    main()
