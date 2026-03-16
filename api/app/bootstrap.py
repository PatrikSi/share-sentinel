import logging
from pathlib import Path

from app.config import get_settings
from app.seed import main as seed_main

logger = logging.getLogger("share_sentinel.bootstrap")


def ensure_artifact_storage_path(storage_path: str) -> bool:
    path = Path(storage_path)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    return created


def main() -> None:
    settings = get_settings()
    created_path = ensure_artifact_storage_path(settings.artifact_storage_path)
    logger.info("artifact storage ready path=%s created=%s", settings.artifact_storage_path, created_path)

    seed_main()


if __name__ == "__main__":
    main()
