import asyncio

from sqlalchemy import select

from app.core.security import SecretBox
from app.db.models import JevConfiguration, JevPlatformSettings, User
from app.db.session import SessionLocal
from app.services.jev_configuration import get_jev_configuration


def test_jev_requires_admin(client, creator_headers):
    assert client.get("/api/v1/admin/jev", headers=creator_headers).status_code == 403
    assert (
        client.put("/api/v1/admin/jev", headers=creator_headers, json={"enabled": False}).status_code == 403
    )
    assert client.post("/api/v1/admin/jev/test", headers=creator_headers).status_code == 403


def test_jev_save_encrypt_preserve_test_and_clear(client, admin_headers, monkeypatch):
    from app.api.routes import jev

    key = "test-jev-secret"
    initial = client.get("/api/v1/admin/jev", headers=admin_headers)
    assert initial.status_code == 200
    assert initial.json()["source"] == "environment"
    saved = client.put(
        "/api/v1/admin/jev",
        headers=admin_headers,
        json={
            "enabled": True,
            "api_key": key,
            "model": "jev-latest",
            "timeout_seconds": 12,
            "route_confidence": 0.8,
        },
    )
    assert saved.status_code == 200, saved.text
    assert key not in saved.text and saved.json()["has_api_key"]
    assert saved.json()["source"] == "database"

    async def read():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            row = await db.get(JevConfiguration, user.tenant_id)
            assert row.encrypted_api_key != key
            assert SecretBox().decrypt(row.encrypted_api_key) == key
            config = await get_jev_configuration(db, user.tenant_id)
            assert config.enabled and config.api_key == key
            assert config.model == "jev-latest" and config.route_confidence == 0.8
            assert not (await get_jev_configuration(db, "other-tenant")).api_key

    asyncio.run(read())

    async def evaluate(state, *, config, use_laya):
        assert use_laya is False
        assert config.api_key == key and config.model == "jev-latest"
        return {"model": config.model, "answers": {"output": {"choice": "text"}}}

    monkeypatch.setattr(jev, "evaluate", evaluate)
    tested = client.post("/api/v1/admin/jev/test", headers=admin_headers)
    assert tested.status_code == 200 and tested.json()["ok"]
    preserved = client.put("/api/v1/admin/jev", headers=admin_headers, json={"enabled": False, "api_key": ""})
    assert preserved.status_code == 200 and preserved.json()["has_api_key"]
    assert not preserved.json()["enabled"]
    invalid = client.put(
        "/api/v1/admin/jev", headers=admin_headers, json={"enabled": True, "clear_api_key": True}
    )
    assert invalid.status_code == 422
    cleared = client.put(
        "/api/v1/admin/jev", headers=admin_headers, json={"enabled": False, "clear_api_key": True}
    )
    assert cleared.status_code == 200 and not cleared.json()["has_api_key"]

    # No persistent configuration left for other routing tests using environment fallback.
    async def cleanup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            await db.delete(await db.get(JevConfiguration, user.tenant_id))
            await db.delete(await db.get(JevPlatformSettings, user.tenant_id))
            await db.commit()

    asyncio.run(cleanup())


def test_platform_switch_keeps_keys_separate(client, admin_headers, monkeypatch):
    from app.api.routes import jev

    def save(**payload):
        return client.put("/api/v1/admin/jev", headers=admin_headers, json={"enabled": True, **payload})

    first = save(provider="typesafe", api_key="original-secret", model="jev-latest")
    assert first.status_code == 200
    # Never borrow the Typesafe credential when configuring Zen for the first time.
    assert save(provider="opencode_zen").status_code == 422
    zen = save(provider="opencode_zen", api_key="zen-secret")
    assert zen.status_code == 200, zen.text
    assert zen.json()["model"] == "jev-1.13-free"
    assert zen.json()["endpoint"] == "https://opencode.ai/zen/v1/systemone"
    assert "secret" not in zen.text and "encrypted_api_key" not in zen.text
    assert all(p["has_api_key"] for p in zen.json()["platforms"])

    async def evaluate(state, *, config, use_laya):
        assert config.provider == "opencode_zen" and config.api_key == "zen-secret"
        assert use_laya is False
        return {"model": config.model, "answers": {"output": {"choice": "text"}}}

    monkeypatch.setattr(jev, "evaluate", evaluate)
    assert client.post("/api/v1/admin/jev/test", headers=admin_headers).json()["provider"] == "opencode_zen"
    back = save(provider="typesafe")
    assert back.json()["model"] == "jev-latest"
    assert save(provider="untrusted").status_code == 422
    assert save(provider="opencode_zen", enabled=False, clear_api_key=True).status_code == 200
    assert save(provider="opencode_zen").status_code == 422
    assert save(provider="typesafe").json()["has_api_key"]

    async def verify_and_cleanup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            config = await get_jev_configuration(db, user.tenant_id)
            assert config.api_key == "original-secret"
            assert not (await get_jev_configuration(db, "another-tenant", provider="opencode_zen")).api_key
            await db.delete(await db.get(JevPlatformSettings, user.tenant_id))
            await db.delete(await db.get(JevConfiguration, user.tenant_id))
            await db.commit()

    asyncio.run(verify_and_cleanup())


def test_home_and_project_send_to_selected_platform(monkeypatch):
    import httpx

    from app.services import personal_routing, project_jev
    from app.services.jev_configuration import JevSettings

    calls = []
    client_class = httpx.AsyncClient

    def handle(request):
        import json

        assert str(request.url) == "https://opencode.ai/zen/v1/systemone"
        assert request.headers["authorization"] == "Bearer zen-secret"
        body = json.loads(request.content)
        assert body["model"] == "jev-1.13-free"
        assert isinstance(body["state"], dict) and body["questions"]
        calls.append(body)
        return httpx.Response(200, json={"answers": {"output": {"choice": "text"}}})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs)
    )

    async def run():
        config = JevSettings(True, "zen-secret", model="jev-1.13-free", provider="opencode_zen")
        await personal_routing.evaluate(
            {"message": "只写故事", "attachments": []}, config=config, use_laya=False
        )
        await project_jev.call(config, "project-chat", {"request": "只讨论剧情"})

    asyncio.run(run())
    assert len(calls) == 2


def test_first_zen_save_preserves_environment_typesafe(client, admin_headers, monkeypatch):
    from pydantic import SecretStr

    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("environment-secret"))
    result = client.put(
        "/api/v1/admin/jev",
        headers=admin_headers,
        json={
            "provider": "opencode_zen",
            "api_key": "zen-secret",
            "enabled": True,
        },
    )
    assert result.status_code == 200, result.text
    assert all(p["has_api_key"] for p in result.json()["platforms"])

    async def check():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            original = await get_jev_configuration(db, user.tenant_id, provider="typesafe")
            assert original.api_key == "environment-secret"
            await db.delete(await db.get(JevPlatformSettings, user.tenant_id))
            await db.delete(await db.get(JevConfiguration, user.tenant_id))
            await db.commit()

    asyncio.run(check())
