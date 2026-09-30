import asyncio
import io

from PIL import Image
from sqlalchemy import select

from app.db.models import AIModel, AITask, Asset, AssetScope, AssetType, ModelType, TaskStatus, User
from app.db.session import SessionLocal
from app.services.task_worker import process_task
from app.services.video_replica import ANALYZE


def test_reference_upload_asset_generation_and_owner_isolation(client, admin_headers, creator_headers):
    image = io.BytesIO()
    Image.new("RGB", (256, 256), "blue").save(image, "PNG")
    upload = client.post(
        "/api/v1/agent/attachments",
        headers=admin_headers,
        files={"file": ("hero.png", image.getvalue(), "image/png")},
    )
    assert upload.status_code == 201, upload.text
    attachment = upload.json()

    async def setup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            model = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.IMAGE))
            video = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.VIDEO))
            text_model = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.TEXT))
            model.capabilities = {
                "resolutions": ["1K"],
                "aspect_ratios": ["16:9"],
                "generation_modes": ["text_to_image", "image_to_image"],
            }
            video.capabilities = {
                "durations": [4, 8],
                "generation_modes": ["first_frame"],
                "reference_limits": {"image": {"enabled": True, "max_count": 1}},
            }
            task = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type=ANALYZE,
                status=TaskStatus.SUCCEEDED,
                cost=0,
                request_payload={"brief": "replace hero", "text_model_id": text_model.id},
                result_payload={
                    "media": {"duration": 4},
                    "plan": {
                        "summary": "one shot",
                        "shots": [{"start": 0, "end": 4, "observation": "person", "prompt": "walk"}],
                    },
                },
            )
            asset = Asset(
                tenant_id=user.tenant_id,
                user_id=user.id,
                scope=AssetScope.GLOBAL,
                asset_type=AssetType.CHARACTER,
                name="hero",
                media_url=attachment["media_url"],
            )
            db.add_all([task, asset])
            await db.commit()
            return task.id, model.id, video.id, asset.id, task.result_payload["plan"]

    task_id, model_id, video_id, asset_id, plan = asyncio.run(setup())
    assets = client.get("/api/v1/video-replicas/assets", headers=admin_headers).json()
    assert any(a["id"] == asset_id for a in assets)
    assert not any(
        a["id"] == asset_id
        for a in client.get("/api/v1/video-replicas/assets", headers=creator_headers).json()
    )
    reference = {"kind": "attachment", "id": attachment["id"], "purpose": "replace hero"}
    options = {"model_id": model_id, "prompt": "new costume", "references": [reference]}
    assert (
        client.post(
            f"/api/v1/video-replicas/{task_id}/images", headers=creator_headers, json=options
        ).status_code
        == 404
    )
    created = client.post(f"/api/v1/video-replicas/{task_id}/images", headers=admin_headers, json=options)
    assert created.status_code == 202, created.text
    image_id = created.json()["id"]

    class Gateway:
        async def generate_image(self, request):
            assert request.generation_mode == "image_to_image"
            assert request.prompt.startswith("new costume")
            assert "replace hero" in request.prompt
            assert request.reference_image_urls[0].startswith("data:image/webp;base64,")
            return image.getvalue()

    asyncio.run(process_task(image_id, gateway_factory=lambda _: Gateway()))
    generated = client.get(f"/api/v1/tasks/{image_id}", headers=admin_headers).json()
    assert generated["status"] == "succeeded", generated
    render = {
        "plan": plan,
        "video_model_id": video_id,
        "image_model_id": model_id,
        "use_reference_frame": False,
        "use_reference_video": False,
        "references": [{"kind": "image_task", "id": image_id, "purpose": "new hero", "target": "hero"}],
    }
    response = client.post(f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=render)
    assert response.status_code == 202, response.text
    assert (
        response.json()["request_payload"]["reference_snapshots"][0]["url"]
        == generated["result_payload"]["media_url"]
    )
    render["references"] = [{"kind": "asset", "id": asset_id, "purpose": "hero", "target": "hero"}]
    response = client.post(f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=render)
    assert response.status_code == 202, response.text
    render["use_reference_frame"] = True
    assert (
        client.post(
            f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=render
        ).status_code
        == 202  # Original-person frame is suppressed when replacement references are bound.
    )
    render["use_reference_frame"] = False
    render["references"][0]["id"] = "missing-reference"
    assert (
        client.post(
            f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=render
        ).status_code
        == 404
    )
