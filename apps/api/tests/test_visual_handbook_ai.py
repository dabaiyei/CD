import asyncio
import base64
import json
from io import BytesIO

from PIL import Image

from app.services.agent_runtime import AgentRuntimeResponse
from app.services.managed_skills import VISUAL_HANDBOOK_FILES
from app.services.task_worker import process_task
from app.core.security import SecretBox
from app.db.models import AIModel, Provider, ModelType
from app.db.session import SessionLocal
from sqlalchemy import select
import pytest


@pytest.fixture
def configured_vision_provider(client):
    async def configure():
        async with SessionLocal() as session:
            model = await session.scalar(select(AIModel).where(AIModel.model_type == ModelType.TEXT, AIModel.is_default.is_(True)))
            provider = await session.get(Provider, model.provider_id)
            previous = provider.encrypted_api_key
            provider.encrypted_api_key = SecretBox().encrypt("test-only-key")
            await session.commit()
            return provider.id, previous
    provider_id, previous = asyncio.run(configure())
    yield
    async def restore():
        async with SessionLocal() as session:
            provider = await session.get(Provider, provider_id)
            provider.encrypted_api_key = previous
            await session.commit()
    asyncio.run(restore())


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (128, 128), "#456789").save(output, format="PNG")
    return output.getvalue()


class VisualRuntime:
    def __init__(self, fail_batch=None):
        self.requests = []
        self.fail_batch = fail_batch

    async def run(self, request):
        self.requests.append(request)
        if request.session_id.endswith("analysis"):
            assert len(request.attachments) == 2
            assert all(Image.open(BytesIO(base64.b64decode(item.data))).format == "WEBP" for item in request.attachments)
            assert request.tool_mode == "none"
            result = {"name": "测试蓝调插画", "description": "冷蓝色块与柔和边缘的插画风格",
                      "observations": ["图1冷蓝色块", "图2冷蓝色块"], "style": "主色冷蓝，辅色灰白，轮廓柔和，暗部饱和度降低。" * 12}
        else:
            offset = int(request.session_id.rsplit("-", 1)[-1])
            if offset == self.fail_batch: raise RuntimeError("simulated model failure")
            assert "四视图" in request.prompt
            assert not request.attachments  # Images are analyzed once; later batches share evidence.
            result = {"files": {item.filename: f"# {item.label}\n\n" + "采用冷蓝色主调与灰白辅色，边缘柔和且高光克制，保持已分析画风特征。" * 10
                                for item in VISUAL_HANDBOOK_FILES[offset:offset+3]}}
        return AgentRuntimeResponse(session_id=request.session_id, final_response=json.dumps(result),
                                    finish_reason="completed", events=[], manifest={})


def submit(client, headers):
    return client.post("/api/v1/admin/handbooks/ai-create", headers=headers,
                       files=[("files", ("first.png", image_bytes(), "image/png")),
                              ("files", ("second.png", image_bytes(), "image/png"))])


def test_visual_handbook_creation_and_checkpoint_retry(client, admin_headers, creator_headers, configured_vision_provider):
    assert submit(client, creator_headers).status_code == 403
    response = submit(client, admin_headers)
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    runtime = VisualRuntime(fail_batch=3)
    asyncio.run(process_task(task_id, runtime_factory=lambda: runtime))
    failed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert failed["status"] == "failed", failed
    assert len(failed["result_payload"]["files"]) == 3
    assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers).status_code == 202
    runtime = VisualRuntime()
    asyncio.run(process_task(task_id, runtime_factory=lambda: runtime))
    finished = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert finished["status"] == "succeeded", finished
    assert len(runtime.requests) == 3  # Reuse analysis and first three completed files.
    handbook_id = finished["result_payload"]["handbook_id"]
    package = client.get(f"/api/v1/admin/handbooks/{handbook_id}/package", headers=admin_headers).json()
    assert package["handbook"]["handbook_type"] == "visual"
    assert package["handbook"]["cover_url"]
    assert len(package["files"]) == 12
    assert all(item["content"].strip() for item in package["files"])
    assert client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).status_code == 404
    listings = client.get("/api/v1/tasks?task_type=visual_handbook_generation", headers=admin_headers).json()["items"]
    assert all(job["task_type"] == "visual_handbook_generation" for job in listings)
