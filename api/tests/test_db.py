from app import db


def test_database_engine_uses_bounded_connect_timeout() -> None:
    assert db.DATABASE_CONNECT_TIMEOUT_SECONDS == 5
    assert db.ENGINE_CONNECT_ARGS == {"connect_timeout": 5}
