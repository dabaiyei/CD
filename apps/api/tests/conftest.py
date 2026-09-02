from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DATABASE = Path("test-cineforge.db").resolve()
TEST_UPLOADS = Path(tempfile.mkdtemp(prefix="cineforge-test-uploads-"))
TEST_SKILLS = Path(tempfile.mkdtemp(prefix="cineforge-test-skills-"))
if TEST_DATABASE.exists():
    TEST_DATABASE.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DATABASE.as_posix()}"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SKILLS_ROOT"] = str(TEST_SKILLS)
os.environ["UPLOADS_ROOT"] = str(TEST_UPLOADS)

from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(engine.dispose())
    if TEST_DATABASE.exists():
        TEST_DATABASE.unlink()
    shutil.rmtree(TEST_UPLOADS, ignore_errors=True)
    shutil.rmtree(TEST_SKILLS, ignore_errors=True)


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"tenant": "demo", "email": email, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def creator_headers(client: TestClient) -> dict[str, str]:
    return login(client, "creator@cineforge.local", "Creator123!")


@pytest.fixture(scope="session")
def admin_headers(client: TestClient) -> dict[str, str]:
    return login(client, "admin@cineforge.local", "Admin123!")
