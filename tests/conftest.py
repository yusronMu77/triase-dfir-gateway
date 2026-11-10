import os

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_astria_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uji tidak boleh membaca ASTRIA_* milik kontainer dev."""
    for key in list(os.environ):
        if key.startswith("ASTRIA_") and not key.startswith("ASTRIA_TEST_"):
            monkeypatch.delenv(key, raising=False)


PG_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite", "postgresql"])
async def db(request: pytest.FixtureRequest, tmp_path):
    """Basis data gateway di kedua dialek: SQLite berkas sementara, PostgreSQL skema per uji (bila ada)."""
    import uuid

    from astria_common.db import Database
    from pydantic import SecretStr
    from sqlalchemy import text

    from astria_gateway.store import metadata

    schema = None
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path}/gateway_test.db"
    elif PG_URL:
        url, schema = PG_URL, f"t_{uuid.uuid4().hex[:8]}"
    else:
        pytest.skip("ASTRIA_TEST_DATABASE_URL tidak diisi")
    database = Database(SecretStr(url), schema=schema, metadata=[metadata])
    await database.start()
    await database.wait_ready(10)
    yield database
    if schema:
        async with database.engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await database.stop()
