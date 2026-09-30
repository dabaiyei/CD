import asyncio
from io import BytesIO

from PIL import Image
from sqlalchemy import select

from app.api.routes.assets import same_name_asset
from app.db.models import (
    AITask,
    Asset,
    AssetRevision,
    AssetScope,
    AssetType,
    StoryboardShot,
    StoryboardVersion,
    TaskStatus,
    User,
)
from app.db.session import SessionLocal


def test_sync_latest_images_preserves_prompts_and_scope(client, creator_headers, admin_headers):
    project = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    base = f"/api/v1/projects/{project}"
    chapter = client.post(
        base + "/sources/import",
        headers=creator_headers,
        data={"mode": "script", "pasted_text": "第一场 同步\n人物向前走。"},
    ).json()["chapters"][0]["id"]
    base += f"/chapters/{chapter}"
    response = client.post(
        base + "/scripts",
        headers=creator_headers,
        json={"title": "同步", "content": "人物向前走。", "activate": True},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        base + "/asset-extractions",
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "同步角色一"},
                {"asset_type": "character", "name": "同步角色二"},
            ]
        },
    )
    assert response.status_code == 201, response.text
    assets = response.json()["assets"]
    a, b = assets[0]["id"], assets[1]["id"]
    image = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    for aid in [a, b]:
        uploaded = client.post(
            f"/api/v1/assets/{aid}/image/upload",
            headers=creator_headers,
            files={"file": ("reference.png", image.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
    response = client.post(
        base + "/storyboards",
        headers=creator_headers,
        json={
            "shots": [
                {
                    "title": "角色二旧图",
                    "asset_ids": [a, b],
                    "reference_image_url": "/uploads/old-b.webp",
                    "video_prompt": "保留提示词",
                },
                {"title": "缺少资产", "asset_ids": [], "reference_image_url": "/uploads/custom.webp"},
            ]
        },
    )
    assert response.status_code == 201, response.text
    board = response.json()["version"]["id"]
    path = base + f"/storyboards/{board}/sync-asset-images"

    async def seed():
        async with SessionLocal() as db:
            first, second = await db.get(Asset, a), await db.get(Asset, b)
            owner = await db.get(User, second.user_id)
            replacement = await same_name_asset(
                db,
                name=second.name,
                asset_type=AssetType.MATERIAL,
                user=owner,
                scope=AssetScope.PROJECT,
                project_id=project,
                parent_id=None,
            )
            assert replacement.id == second.id
            first.media_url = "/uploads/current-a.webp"
            second.media_url = "/uploads/current-b.webp"
            db.add(
                AssetRevision(
                    tenant_id=second.tenant_id,
                    asset_id=b,
                    version=99,
                    change_type="test",
                    name=second.name,
                    status=second.status,
                    media_url="/uploads/old-b.webp",
                )
            )
            await db.commit()

    asyncio.run(seed())
    assert client.post(path, headers=admin_headers).status_code == 404
    response = client.post(path, headers=creator_headers)
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1
    assert response.json()["skipped"] == [2]
    assert client.post(path, headers=creator_headers).json()["updated"] == 0

    async def verify_and_queue():
        async with SessionLocal() as db:
            shots = (
                await db.scalars(
                    select(StoryboardShot)
                    .where(StoryboardShot.storyboard_version_id == board)
                    .order_by(StoryboardShot.order_index)
                )
            ).all()
            assert shots[0].reference_image_url == "/uploads/current-b.webp"
            assert shots[0].video_prompt == "保留提示词"
            assert shots[0].version == 2
            assert shots[1].reference_image_url == "/uploads/custom.webp"
            asset = await db.get(Asset, a)
            task = AITask(
                tenant_id=asset.tenant_id,
                user_id=asset.user_id,
                project_id=project,
                task_type="shot_video_generation",
                status=TaskStatus.QUEUED,
                request_payload={"chapter_id": chapter},
            )
            db.add(task)
            await db.commit()
            return task.id

    task_id = asyncio.run(verify_and_queue())
    assert client.post(path, headers=creator_headers).status_code == 409

    async def finish():
        async with SessionLocal() as db:
            task = await db.get(AITask, task_id)
            task.status = TaskStatus.CANCELLED
            await db.commit()

    asyncio.run(finish())

    async def break_binding():
        async with SessionLocal() as db:
            shot = await db.scalar(
                select(StoryboardShot).where(
                    StoryboardShot.storyboard_version_id == board, StoryboardShot.order_index == 1
                )
            )
            second = await db.get(Asset, b)
            shot.asset_ids = [a, "deleted-character-id"]
            shot.scene_description = "镜头正文不含任何资产名称。"
            snapshot = await db.get(StoryboardVersion, board)
            db.add(
                StoryboardVersion(
                    tenant_id=snapshot.tenant_id,
                    user_id=snapshot.user_id,
                    project_id=snapshot.project_id,
                    chapter_id=snapshot.chapter_id,
                    script_version_id=snapshot.script_version_id,
                    version=100,
                    is_active=False,
                    content=[{"asset_ids": ["deleted-character-id"], "asset_names": [second.name]}],
                )
            )
            await db.commit()

    asyncio.run(break_binding())
    result = client.post(path, headers=creator_headers)
    assert result.status_code == 200, result.text
    assert result.json()["repaired_bindings"] == 1
    assert result.json()["unresolved"] == []
    assert client.post(path, headers=creator_headers).json()["repaired_bindings"] == 0

    async def unknown_binding():
        async with SessionLocal() as db:
            shot = await db.scalar(
                select(StoryboardShot).where(
                    StoryboardShot.storyboard_version_id == board, StoryboardShot.order_index == 2
                )
            )
            shot.asset_ids = ["unknown-id"]
            shot.scene_description = (await db.get(Asset, a)).name
            await db.commit()

    asyncio.run(unknown_binding())
    result = client.post(path, headers=creator_headers).json()
    assert result["repaired_bindings"] == 0
    assert result["unresolved"] == [2]

    # Real delete/re-import also retains identity in the board, independently of prose.
    assert client.delete(f"/api/v1/assets/{b}", headers=creator_headers).status_code == 204

    async def inspect_tombstone():
        async with SessionLocal() as db:
            snapshot = await db.get(StoryboardVersion, board)
            row = next(row for row in snapshot.content if row["order_index"] == 1)
            assert any(
                item["id"] == b and item["name"] == assets[1]["name"] for item in row["asset_bindings"]
            )

    asyncio.run(inspect_tombstone())
