import asyncio
import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_infinite_canvas import model_id

from app.services.canvas_video_reading import extract
from app.services.agent_runtime import AgentRuntimeAttachment, AgentRuntimeRequest
from app.services.infinite_canvas import text_attachments
from app.services.task_worker import process_task
from app.services.video_concat import media_binary, run_media_command


@pytest.mark.parametrize('count', [4, 5, 7, 17, 50, 51])
def test_many_references_fit_real_runtime_contract_without_losing_frames(count, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'services/agent-runtime/agentscope'))
    from runtime.contracts import AgentRunRequest

    images = []
    for index in range(count):
        buffer = BytesIO()
        Image.new('RGB', (96, 96), (index * 4, 0, 0)).save(buffer, 'WEBP', lossless=True)
        images.append(AgentRuntimeAttachment(id=f'original-{index}', name=f'参考图{index + 1}', mime_type='image/webp', data=base64.b64encode(buffer.getvalue()).decode()))
    packed, mapping = text_attachments(images)
    request = AgentRuntimeRequest(tenant_id='tenant', project_id='canvas-user', task_id='task', session_id='task',
        prompt='读取所有图片', system_prompt='编号图是参考证据' + mapping,
        model_binding={'provider':'test', 'model':'vision', 'api_key':'test'},
        prompt_versions={}, skill_versions={}, skills=[], memory_context=[], attachments=packed,
        tool_mode='none', state_mode='ephemeral')
    AgentRunRequest.model_validate(request.model_dump())
    if count == 51:
        with pytest.raises(ValueError):
            AgentRunRequest.model_validate({**request.model_dump(), 'attachments': [a.model_dump() for a in images]})
    assert len(packed) <= 50
    if count <= 50:
        assert packed == images and not mapping
    else:
        for index in range(count):
            assert f'ref-{index + 1} = 参考图{index + 1}' in mapping
        colors = []
        for attachment in packed:
            with Image.open(BytesIO(base64.b64decode(attachment.data))) as sheet:
                for row in range(sheet.height // 312):
                    for col in range(2):
                        colors.append(sheet.getpixel((col * 512 + 256, row * 312 + 48))[0])
        assert all(any(abs(color - index * 4) <= 3 for color in colors) for index in range(count))


def test_canvas_reference_input_accepts_fifty_and_rejects_fifty_one():
    from app.api.routes.infinite_canvas import GenerationInput

    payload = {'model_id':'vision', 'prompt':'分析图片', 'reference_keys':[{'key':'image', 'namespace':'infinite-canvas.image_files'}] * 50}
    assert len(GenerationInput.model_validate(payload).reference_keys) == 50
    with pytest.raises(ValueError):
        GenerationInput.model_validate({**payload, 'reference_keys':payload['reference_keys'] + [{}]})


@pytest.fixture
def video_bytes(tmp_path):
    if not media_binary("ffmpeg") or not media_binary("ffprobe"):
        pytest.skip("FFmpeg is unavailable")
    path = tmp_path / "color-change.mp4"
    asyncio.run(
        run_media_command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=96x64:r=12:d=2",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,1)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        )
    )
    return path.read_bytes()


def upload_video(client, headers, data, key="video:analysis"):
    response = client.put(
        f"/api/v1/canvas/storage/infinite-canvas.media_files/{key}?revision=0",
        headers={**headers, "X-Canvas-Kind": "blob", "Content-Type": "video/mp4"},
        content=data,
    )
    assert response.status_code == 200, response.text
    return key


def test_extraction_reads_actual_timestamps_and_shared_contact_sheets(video_bytes):
    result = asyncio.run(extract(video_bytes, count=4, sampling="uniform"))
    assert result["duration"] == pytest.approx(2)
    assert result["has_audio"] is False
    assert [f["at"] for f in result["frames"]] == [0.25, 0.75, 1.25, 1.75]
    for frame in result["frames"]:
        with Image.open(BytesIO(frame["data"])) as picture:
            red, _, blue = picture.convert("RGB").getpixel((20, 20))
            assert (red > blue) == (frame["at"] < 1)
    with Image.open(BytesIO(result["sheets"][0])) as picture:
        assert picture.size == (1024, 624)
    partial = asyncio.run(extract(video_bytes, count=2, start=1, end=2))
    assert [f["at"] for f in partial["frames"]] == [1.25, 1.75]
    with pytest.raises(ValueError, match="分析范围"):
        asyncio.run(extract(video_bytes, start=2, end=3))


def test_adaptive_preserves_transition_pairs_and_exact_range(video_bytes):
    result = asyncio.run(extract(video_bytes, count=4))
    assert result["sampling"] == "adaptive"
    assert result["transition_count"] == 1
    transition = result["transitions"][0]
    assert transition["at"] == pytest.approx(1)
    assert transition["before"] == pytest.approx(1 - 1 / 12)
    for frame in result["frames"]:
        with Image.open(BytesIO(frame["data"])) as image:
            red, _, blue = image.convert("RGB").getpixel((20, 20))
            assert (red > blue) == (frame["at"] < 1), frame
    partial = asyncio.run(extract(video_bytes, count=4, start=0.5, end=1.5))
    assert partial["transitions"][0]["at"] == pytest.approx(1)
    exact = asyncio.run(extract(video_bytes, start=0.5, end=1.5, times=[1, 0.9166666667]))
    assert exact["sampling"] == "exact"
    with Image.open(BytesIO(exact["frames"][0]["data"])) as before, Image.open(BytesIO(exact["frames"][1]["data"])) as after:
        assert before.getpixel((20, 20))[0] > after.getpixel((20, 20))[0]
    with pytest.raises(ValueError, match="补取"):
        asyncio.run(extract(video_bytes, start=1, end=2, times=[0.5]))


def test_one_frame_flash_is_seen_when_uniform_sampling_misses_it(tmp_path):
    path = tmp_path / "flash.mp4"
    asyncio.run(run_media_command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=96x64:r=12:d=2",
        "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='eq(n,12)'", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)))
    data = path.read_bytes()
    uniform = asyncio.run(extract(data, count=4, sampling="uniform"))
    adaptive = asyncio.run(extract(data, count=4))
    def blue(frame):
        with Image.open(BytesIO(frame["data"])) as image:
            red, _, value = image.convert("RGB").getpixel((20, 20))
            return value > red
    assert not any(map(blue, uniform["frames"]))
    assert any(map(blue, adaptive["frames"]))
    assert len(adaptive["frames"]) <= 4


def test_video_evidence_is_owned_and_persisted(client, creator_headers, admin_headers, video_bytes):
    key = upload_video(client, creator_headers, video_bytes, "video:evidence")
    payload = {"key": key, "count": 4}
    assert client.post("/api/v1/canvas/video-evidence", json=payload).status_code == 401
    assert (
        client.post("/api/v1/canvas/video-evidence", headers=admin_headers, json=payload).status_code == 422
    )
    result = client.post("/api/v1/canvas/video-evidence", headers=creator_headers, json=payload)
    assert result.status_code == 200, result.text
    evidence = result.json()
    assert evidence["source_revision"] == 1
    assert len(evidence["frames"]) == 4 and len(evidence["sheets"]) == 1
    for frame in evidence["frames"] + evidence["sheets"]:
        url = "/api/v1/canvas/storage/infinite-canvas.image_files/" + frame["storageKey"]
        assert client.get(url, headers=creator_headers).headers["content-type"] == "image/webp"
        assert client.get(url, headers=admin_headers).status_code == 204
    original = "/api/v1/canvas/storage/infinite-canvas.media_files/" + key
    assert client.get(original, headers=creator_headers).content == video_bytes
    assert (
        client.post(
            "/api/v1/canvas/video-evidence", headers=creator_headers, json={"key": key, "count": 100}
        ).status_code
        == 422
    )


def test_text_task_analyses_video_snapshot_without_native_video_support(
    client, creator_headers, admin_headers, video_bytes
):
    from test_api import grant_creator_test_credits

    grant_creator_test_credits(client, creator_headers, admin_headers)
    key = upload_video(client, creator_headers, video_bytes, "video:task-snapshot")
    response = client.post(
        "/api/v1/canvas/generate",
        headers=creator_headers,
        json={
            "model_id": model_id(client, creator_headers, "text"),
            "expected_kind": "text",
            "prompt": "分析红色到蓝色的变化，给出时间轴",
            "reference_keys": [{"namespace": "infinite-canvas.media_files", "key": key, "purpose": "原视频"}],
        },
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    client.delete("/api/v1/canvas/storage/infinite-canvas.media_files/" + key, headers=creator_headers)
    requests = []

    class Runtime:
        async def run(self, request):
            requests.append(request)
            return SimpleNamespace(final_response="0–1秒红色；1–2秒蓝色。声音未知。")

    assert asyncio.run(process_task(task_id, runtime_factory=Runtime))
    result = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert result["status"] == "succeeded", result.get("error_message")
    request = requests[0]
    assert len(request.attachments) == 4
    assert all(a.mime_type == "image/webp" for a in request.attachments)
    assert request.tool_mode == "none" and not request.memory_context and not request.skills
    assert "不是原片分屏" in request.system_prompt
    assert result["result_payload"]["video_evidence"][0]["duration"] == pytest.approx(2)
