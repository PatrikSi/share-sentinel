from app import db


def test_database_engine_uses_bounded_connect_timeout() -> None:
    assert db.DATABASE_CONNECT_TIMEOUT_SECONDS == 5
    assert db.ENGINE_CONNECT_ARGS == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=30000 -c lock_timeout=5000",
    }


def test_database_engine_uses_bounded_pool_defaults() -> None:
    pool = db.engine.pool

    assert pool.size() == 10
    assert pool._max_overflow == 20
    assert pool._timeout == 10
    assert pool._recycle == 1800
