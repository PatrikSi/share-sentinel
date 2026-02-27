from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.security import hash_password


def main() -> None:
    settings = get_settings()
    email = settings.seed_admin_email
    password = settings.seed_admin_password

    if not email or not password:
        return

    with SessionLocal() as db:
        existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing:
            return

        user = User(
            email=email,
            password_hash=hash_password(password),
            is_active=True,
            is_sysadmin=True,
        )
        db.add(user)
        db.commit()


if __name__ == "__main__":
    main()
