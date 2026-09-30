import asyncio
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from app.db.models import AIModel, AITask
from app.db.session import SessionLocal
from app.services.task_worker import process_task


def image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (96, 96), "#446688").save(buffer, "PNG")
    return buffer.getvalue()


def upload(client, headers, key="image:reference"):
    response = client.put(
        f"/api/v1/canvas/storage/infinite-canvas.image_files/{key}?revision=0",
        headers={**headers, "X-Canvas-Kind": "blob", "Content-Type": "image/png"},
        content=image_bytes(),
    )
    assert response.status_code == 200, response.text
    return {"namespace": "infinite-canvas.image_files", "key": key, "purpose": "人物参考图"}


def model_id(client, headers, kind):
    response = client.get("/api/v1/canvas/config", headers=headers)
    assert response.status_code == 200, response.text
    return next(model["id"] for model in response.json()["models"] if model["type"] == kind)


def test_canvas_state_isolated_and_stale_updates_preserve_saved_state(client, creator_headers, admin_headers):
    url = "/api/v1/canvas/storage/infinite-canvas.app_state/canvas-projects"
    assert client.get(url).status_code == 401
    state = {"nodes": [{"title": "测试人物"}], "connections": []}
    saved = client.put(url + "?revision=0", headers=creator_headers, json=state)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert client.get(url, headers=admin_headers).status_code == 204
    assert client.get("/api/v1/canvas/storage/infinite-canvas.app_state", headers=admin_headers).json() == []
    assert client.put(url + "?revision=0", headers=creator_headers, json={"nodes": []}).status_code == 409
    restored = client.get(url, headers=creator_headers)
    assert restored.headers["X-Canvas-Revision"] == "1"
    assert restored.json() == state
    assert client.delete(url, headers=admin_headers).status_code == 204
    assert client.get(url, headers=creator_headers).json() == state
    assert client.put(url + "?revision=1", headers=creator_headers, json={"nodes": []}).status_code == 200


def test_canvas_blob_round_trip_and_fake_image_rejected(client, creator_headers, admin_headers):
    ref = upload(client, creator_headers, "image:roundtrip")
    url = f"/api/v1/canvas/storage/{ref['namespace']}/{ref['key']}"
    result = client.get(url, headers=creator_headers)
    assert result.content == image_bytes()
    assert result.headers["X-Canvas-Kind"] == "blob"
    assert result.headers["content-type"] == "image/png"
    assert client.get(url, headers=admin_headers).status_code == 204
    invalid = client.put(
        url + "?revision=1",
        headers={**creator_headers, "X-Canvas-Kind": "blob", "Content-Type": "image/png"},
        content=b"not an image",
    )
    assert invalid.status_code == 422
    assert client.get(url, headers=creator_headers).content == image_bytes()


def test_canvas_generation_uses_exact_prompt_and_own_reference(client, creator_headers, admin_headers):
    from test_api import FakeAssetImageGateway, grant_creator_test_credits

    grant_creator_test_credits(client, creator_headers, admin_headers)
    reference = upload(client, creator_headers, "image:edit")
    prompt = "保持这个人物的脸与发型，只把衣服改为蓝色"
    payload = {
        "model_id": model_id(client, creator_headers, "image"),
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "resolution": "1K",
        "reference_keys": [reference],
    }
    assert client.post("/api/v1/canvas/generate", headers=admin_headers, json=payload).status_code == 422
    response = client.post("/api/v1/canvas/generate", headers=creator_headers, json=payload)
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    original = f"/api/v1/canvas/storage/{reference['namespace']}/{reference['key']}"
    assert client.delete(original, headers=creator_headers).status_code == 204
    gateway = FakeAssetImageGateway()
    assert asyncio.run(process_task(task_id, gateway_factory=lambda _: gateway))
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert task["status"] == "succeeded", task.get("error_message")
    assert task["result_payload"]["media_url"]
    request = gateway.requests[0]
    assert request.prompt == prompt
    assert request.aspect_ratio == "16:9"
    assert request.generation_mode == "image_to_image"
    assert request.reference_image_urls[0].startswith("data:image/webp;base64,")


def test_canvas_text_has_only_explicit_canvas_context(client, creator_headers, admin_headers):
    from test_api import grant_creator_test_credits

    grant_creator_test_credits(client, creator_headers, admin_headers)
    reference = upload(client, creator_headers, "image:text-context")
    chosen = model_id(client, creator_headers, "text")
    prompt = '[{"role":"user","content":"请根据参考图设计服装"}]'
    response = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={"model_id": chosen, "expected_kind": "text", "prompt": prompt, "reference_keys": [reference]},
    )
    assert response.status_code == 202, response.text
    requests = []

    class Runtime:
        async def run(self, request):
            requests.append(request)
            return SimpleNamespace(final_response="保留脸型与发型，服装改为蓝色。")

    task_id = response.json()["id"]
    assert asyncio.run(process_task(task_id, runtime_factory=Runtime))
    result = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert result["status"] == "succeeded", result.get("error_message")
    request = requests[0]
    assert request.prompt == prompt and request.state_mode == "ephemeral"
    assert request.memory_context == [] and request.skills == [] and request.tool_mode == "none"
    assert len(request.attachments) == 1 and request.attachments[0].mime_type == "image/webp"
    mismatch = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={"model_id": chosen, "expected_kind": "image", "prompt": "生成图片"},
    )
    assert mismatch.status_code == 422


def test_canvas_running_task_can_be_cancelled(client, creator_headers, admin_headers):
    from test_api import grant_creator_test_credits

    from app.services.task_worker import claim_task

    grant_creator_test_credits(client, creator_headers, admin_headers)
    response = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={"model_id": model_id(client, creator_headers, "text"), "prompt": "测试停止"},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    assert asyncio.run(claim_task(task_id))
    stopped = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=creator_headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"


def test_canvas_video_resumes_original_provider_job(client, creator_headers, admin_headers):
    from test_api import FakeVideoGateway, grant_creator_test_credits

    grant_creator_test_credits(client, creator_headers, admin_headers)
    chosen = model_id(client, creator_headers, "video")

    async def video_config():
        from app.services.provider_adapters import VideoModelCapabilities

        async with SessionLocal() as db:
            model = await db.get(AIModel, chosen)
            return VideoModelCapabilities.model_validate(model.capabilities).model_dump()

    caps = asyncio.run(video_config())
    durations = caps.get("durations") or caps["duration_resolution_map"][0]["durations"]
    resolutions = (
        caps["duration_resolution_map"][0]["resolutions"]
        if caps.get("duration_resolution_map")
        else caps["resolutions"]
    )
    response = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={
            "model_id": chosen,
            "prompt": "镜头平稳推进，动作连贯",
            "duration_seconds": durations[0],
            "resolution": resolutions[0],
            "aspect_ratio": "16:9",
            "audio_enabled": caps.get("audio_policy") != "disabled",
        },
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]

    async def save_job():
        async with SessionLocal() as db:
            task = await db.get(AITask, task_id)
            task.provider_job_id = "already-submitted"
            await db.commit()

    asyncio.run(save_job())
    gateway = FakeVideoGateway()
    assert asyncio.run(process_task(task_id, gateway_factory=lambda _: gateway))
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert task["status"] == "succeeded", task.get("error_message")
    assert gateway.submit_calls == 0 and gateway.poll_calls == 1


def test_canvas_failed_image_can_retry_single_task(client, creator_headers, admin_headers):
    from test_api import FakeAssetImageGateway, grant_creator_test_credits

    grant_creator_test_credits(client, creator_headers, admin_headers)
    response = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={
            "model_id": model_id(client, creator_headers, "image"),
            "prompt": "测试画布图片",
            "aspect_ratio": "1:1",
        },
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    assert asyncio.run(process_task(task_id, gateway_factory=lambda _: FakeAssetImageGateway(fail=True)))
    failed = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert failed["status"] == "failed"
    assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=creator_headers).status_code == 202
    assert asyncio.run(process_task(task_id, gateway_factory=lambda _: FakeAssetImageGateway()))
    assert client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()["status"] == "succeeded"
