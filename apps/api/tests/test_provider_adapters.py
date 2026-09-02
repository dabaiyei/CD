from __future__ import annotations

import asyncio
import base64

import pytest
from pydantic import ValidationError

from app.services import media_gateway
from app.services.media_gateway import (
    ImageGenerationRequest,
    OpenAICompatibleMediaGateway,
    VideoGenerationRequest,
)
from app.services.provider_adapters import (
    AUTODL_MINIMAX_H3_MODEL_ID,
    VideoModelCapabilities,
    autodl_minimax_h3_adapter_config,
    autodl_minimax_h3_capabilities,
    closest_supported_video_duration,
    extract_path,
    render_template,
    supported_video_durations,
    validate_video_generation_request,
)


class FakeResponse:
    def __init__(
        self,
        body: dict | None = None,
        *,
        content: bytes = b"",
        content_type: str = "application/json",
    ):
        self._body = body or {}
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class FakeAdapterClient:
    requests: list[tuple[str, str, dict | None]] = []

    def __init__(self, **_kwargs) -> None:
        self.is_closed = False

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append((method, url, kwargs.get("json")))
        if "/result/" in url:
            return FakeResponse(
                {
                    "code": "Success",
                    "data": {
                        "task_id": "video-job-1",
                        "status": "SUCCESS",
                        "results": [{"type": "video", "url": "https://cdn.example.test/result.mp4"}],
                    },
                }
            )
        return FakeResponse({"code": "Success", "data": {"task_id": "video-job-1", "status": "RUNNING"}})

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        assert url == "https://cdn.example.test/result.mp4"
        return FakeResponse(content=b"\x00\x00\x00\x18ftypmp42video", content_type="video/mp4")

    async def aclose(self) -> None:
        self.is_closed = True


class FakeRetryImageClient:
    attempts = 0
    idempotency_keys: list[str] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, _url: str, **kwargs) -> FakeResponse:
        self.__class__.attempts += 1
        self.__class__.idempotency_keys.append(kwargs["headers"]["Idempotency-Key"])
        if self.attempts == 1:
            response = FakeResponse()
            response.status_code = 524
            return response
        return FakeResponse(
            {"data": [{"b64_json": base64.b64encode(b"valid-image-bytes").decode()}]}
        )


def adapter_config() -> dict:
    return {
        "schema_version": 1,
        "credential_fields": [
            {"key": "token", "label": "Token", "input_type": "password", "required": True}
        ],
        "video": {
            "create": {
                "method": "POST",
                "path": "workflow/{{model}}",
                "headers": {"Authorization": "{{credentials.token}}"},
                "body": {"prompt": "{{prompt}}", "duration": "{{duration}}"},
                "assertions": [{"path": "code", "accepted_values": ["Success"]}],
            },
            "poll": {
                "method": "GET",
                "path": "result/{{job_id}}",
                "headers": {"Authorization": "{{credentials.token}}"},
                "assertions": [{"path": "code", "accepted_values": ["Success"]}],
            },
            "response": {
                "task_id_path": "data.task_id",
                "status_path": "data.status",
                "result_url_path": "data.results[?type=video].url",
                "error_path": "msg",
                "success_values": ["SUCCESS"],
                "pending_values": ["RUNNING"],
                "failed_values": ["FAILED"],
            },
            "references": [
                {
                    "media_type": "image",
                    "strategy": "indexed_fields",
                    "field": "ref_image_",
                    "start_index": 0,
                    "source": "url",
                }
            ],
        },
    }


def test_video_duration_helpers_support_schema_and_legacy_capabilities() -> None:
    schema_capabilities = {
        "schema_version": 1,
        "duration_resolution_map": [
            {"durations": [5, 10], "resolutions": ["720p"]},
            {"durations": [15], "resolutions": ["1080p"]},
        ],
    }
    assert supported_video_durations(schema_capabilities) == [5.0, 10.0, 15.0]
    assert closest_supported_video_duration(schema_capabilities, requested_duration=6) == 5.0
    assert closest_supported_video_duration(schema_capabilities, requested_duration=14) == 15.0

    legacy_capabilities = {"durations": [3, 6, 9]}
    assert supported_video_durations(legacy_capabilities) == [3.0, 6.0, 9.0]
    assert closest_supported_video_duration(legacy_capabilities, requested_duration=8) == 9.0


def test_template_rendering_and_filtered_result_paths() -> None:
    context = {"model": "h3-video", "credentials": {"token": "secret-token"}}
    assert render_template({"model": "{{model}}", "auth": "Bearer {{credentials.token}}"}, context) == {
        "model": "h3-video",
        "auth": "Bearer secret-token",
    }
    body = {"data": {"results": [{"type": "image", "url": "a"}, {"type": "video", "url": "v"}]}}
    assert extract_path(body, "data.results[?type=video].url") == "v"


def test_video_capabilities_reject_frame_modes_without_image_references() -> None:
    with pytest.raises(ValidationError, match="图片参考"):
        VideoModelCapabilities.model_validate({"generation_modes": ["first_frame"]})


def test_video_generation_request_must_match_configured_capabilities() -> None:
    capabilities = VideoModelCapabilities.model_validate(
        {
            "generation_modes": ["first_frame"],
            "reference_limits": {
                "image": {"enabled": True, "min_count": 1, "max_count": 2},
                "video": {"enabled": False, "min_count": 0, "max_count": 0},
                "audio": {"enabled": False, "min_count": 0, "max_count": 0},
            },
            "audio_policy": "disabled",
            "duration_resolution_map": [{"durations": [5], "resolutions": ["768p横"]}],
            "aspect_ratios": ["16:9"],
        }
    ).model_dump()
    validate_video_generation_request(
        capabilities,
        generation_mode="first_frame",
        duration_seconds=5,
        resolution="768p横",
        aspect_ratio="16:9",
        reference_media=[{"type": "image", "url": "https://assets.example.test/frame.png"}],
        audio_enabled=False,
    )
    with pytest.raises(ValueError, match="不支持 10 秒"):
        validate_video_generation_request(
            capabilities,
            generation_mode="first_frame",
            duration_seconds=10,
            resolution="768p横",
            aspect_ratio="16:9",
            reference_media=[{"type": "image", "url": "https://assets.example.test/frame.png"}],
            audio_enabled=False,
        )


def test_declarative_video_adapter_creates_polls_and_extracts_result(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name

    FakeAdapterClient.requests.clear()
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeAdapterClient)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://provider.example.test",
        api_key=None,
        extra_headers={},
        adapter_config=adapter_config(),
        credentials={"token": "secret-token"},
    )
    request = VideoGenerationRequest(
        model="h3-video",
        prompt="雨夜港口，镜头缓慢前推",
        resolution="768p横",
        aspect_ratio="16:9",
        duration_seconds=8,
        reference_image_url="https://assets.example.test/frame.png",
        capabilities={},
        idempotency_key="task-1",
    )

    submitted = asyncio.run(gateway.submit_video(request))
    assert submitted.status == "pending"
    assert submitted.provider_job_id == "video-job-1"
    create_body = FakeAdapterClient.requests[0][2]
    assert create_body == {
        "prompt": "雨夜港口，镜头缓慢前推",
        "duration": 8,
        "ref_image_0": "https://assets.example.test/frame.png",
    }

    completed = asyncio.run(gateway.poll_video(request, submitted.provider_job_id))
    assert completed.status == "succeeded"
    assert completed.video_data == b"\x00\x00\x00\x18ftypmp42video"
    assert completed.content_type == "video/mp4"


def test_image_generation_retries_transient_gateway_status(monkeypatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    FakeRetryImageClient.attempts = 0
    FakeRetryImageClient.idempotency_keys.clear()
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeRetryImageClient)
    monkeypatch.setattr(media_gateway.asyncio, "sleep", no_sleep)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://provider.example.test/v1",
        api_key="secret",
        extra_headers={},
    )
    result = asyncio.run(
        gateway.generate_image(
            ImageGenerationRequest(
                model="gpt-image-2",
                prompt="red cup",
                resolution="1K",
                aspect_ratio="1:1",
                capabilities={},
                idempotency_key="image-task-1",
            )
        )
    )
    assert result == b"valid-image-bytes"
    assert FakeRetryImageClient.attempts == 2
    assert FakeRetryImageClient.idempotency_keys == ["image-task-1", "image-task-1"]


def test_autodl_minimax_h3_preset_renders_indexed_data_uri_references(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name

    FakeAdapterClient.requests.clear()
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeAdapterClient)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://autodl.example.test",
        api_key=None,
        extra_headers={},
        adapter_config=autodl_minimax_h3_adapter_config(),
        credentials={"apiKey": "raw-token"},
    )
    first = "data:image/webp;base64,Zmlyc3Q="
    second = "data:image/png;base64,c2Vjb25k"
    request = VideoGenerationRequest(
        model=AUTODL_MINIMAX_H3_MODEL_ID,
        prompt="雨夜追车",
        resolution="768p横",
        aspect_ratio="16:9",
        duration_seconds=15,
        reference_image_url=None,
        reference_media=[
            {"type": "image", "data_uri": first},
            {"type": "image", "data_uri": second},
        ],
        capabilities=autodl_minimax_h3_capabilities(),
        idempotency_key="video-task-1",
        generation_mode="multi_shot",
    )
    submitted = asyncio.run(gateway.submit_video(request))
    assert submitted.status == "pending"
    assert FakeAdapterClient.requests[0][1].endswith(
        f"/api/v1/comfyui/comfyui_workflow/{AUTODL_MINIMAX_H3_MODEL_ID}"
    )
    assert FakeAdapterClient.requests[0][2] == {
        "prompt": "雨夜追车",
        "duration": 15,
        "resolution": "768p横",
        "ref_image_0": first,
        "ref_image_1": second,
    }
