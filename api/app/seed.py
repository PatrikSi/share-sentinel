from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.security import hash_password, validate_password_strength


def main() -> None:
    settings = get_settings()
    email = settings.seed_admin_email
    password = settings.seed_admin_password

    if not email or not password:
        return
    try:
        validate_password_strength(password, settings.password_min_length)
    except ValueError as exc:
        raise RuntimeError("SEED_ADMIN_PASSWORD does not meet password policy") from exc

    with SessionLocal() as db:
        email = email.lower()
        existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing:
            return

        user = User(
            email=email,
            password_hash=hash_password(password),
            is_active=True,
            is_sysadmin=True,
            is_approved=True,
            approved_at=datetime.now(tz=UTC),
            approved_by_user_id=None,
        )
        db.add(user)
        db.commit()


if __name__ == "__main__":
    main()
