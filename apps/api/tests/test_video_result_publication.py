import asyncio

import httpx
import pytest

from app.services import media_gateway
from app.services.media_gateway import OpenAICompatibleMediaGateway, VideoGenerationRequest
from app.services.provider_adapters import agnes_video_adapter_config


@pytest.mark.parametrize("adapter", [False, True])
def test_completed_job_waits_for_later_video_url_without_resubmitting(monkeypatch, adapter):
    requests = []
    responses = iter(
        [
            {"status": "completed", "video_id": "original-job"},
            {"status": "completed"},  # Polling may omit both ID and URL.
            {"status": "completed", "video_url": "https://cdn.example.test/result.mp4"},
        ]
    )

    def handle(request):
        requests.append(request)
        if request.url.host == "cdn.example.test":
            return httpx.Response(
                200, content=b"\x00\x00\x00\x18ftypmp42video", headers={"content-type": "video/mp4"}
            )
        return httpx.Response(200, json=next(responses))

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_gateway.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )

    async def allow(*args, **kwargs):
        pass

    monkeypatch.setattr(media_gateway, "_validate_download_url", allow)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://provider.example.test/v1",
        api_key="test",
        extra_headers={},
        adapter_config=agnes_video_adapter_config() if adapter else None,
    )
    request = VideoGenerationRequest(
        model="agnes-video-2.5-flash",
        prompt="test",
        resolution="720p",
        aspect_ratio="16:9",
        duration_seconds=5,
        reference_image_url=None,
        capabilities={},
        idempotency_key="same-submission",
    )

    async def run():
        result = await gateway.submit_video(request)
        assert result.status == "pending" and result.provider_job_id == "original-job"
        result = await gateway.poll_video(request, result.provider_job_id)
        assert result.status == "pending" and result.provider_job_id == "original-job"
        result = await gateway.poll_video(request, result.provider_job_id)
        assert result.status == "succeeded" and result.provider_job_id == "original-job"
        assert result.video_data.endswith(b"video")

    asyncio.run(run())
    assert [r.method for r in requests] == ["POST", "GET", "GET", "GET"]


def test_completed_without_video_or_recoverable_id_is_still_invalid():
    with pytest.raises(media_gateway.ModelGatewayError, match="可查询的任务 ID"):
        asyncio.run(media_gateway._parse_video_response(None, {"status": "completed"}))
    result = asyncio.run(
        media_gateway._parse_video_response(
            None, {"status": "failed", "error": "rejected"}, fallback_job_id="job"
        )
    )
    assert result.status == "failed" and result.error_message == "rejected"
