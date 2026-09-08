from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from pydantic import ValidationError

from app.services import media_gateway
from app.services.media_gateway import (
    ImageGenerationRequest,
    OpenAICompatibleMediaGateway,
    VideoGenerationRequest,
    image_prompt_for_provider,
)
from app.services.provider_adapters import (
    AGNES_IMAGE_21_MODEL_ID,
    AGNES_PROVIDER_CODE,
    AGNES_VIDEO_MODEL_ID,
    AUTODL_MINIMAX_H3_MODEL_ID,
    DOLA_PROVIDER_CODE,
    VideoModelCapabilities,
    agnes_image_21_capabilities,
    agnes_image_capabilities,
    agnes_video_adapter_config,
    agnes_video_capabilities,
    autodl_minimax_h3_adapter_config,
    autodl_minimax_h3_capabilities,
    closest_supported_video_duration,
    dola_video_adapter_config,
    dola_video_capabilities,
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


class FakeResilientAdapterClient(FakeAdapterClient):
    post_attempts = 0
    get_attempts = 0

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse | httpx.Response:
        if method == "POST":
            self.__class__.post_attempts += 1
            if self.post_attempts == 1:
                return httpx.Response(
                    429,
                    headers={"retry-after": "0"},
                    json={"error": {"message": "rate limited"}},
                    request=httpx.Request(method, url),
                )
        if method == "GET":
            self.__class__.get_attempts += 1
            if self.get_attempts == 1:
                raise httpx.ConnectError(
                    "temporary connection failure",
                    request=httpx.Request(method, url),
                )
        return await super().request(method, url, **kwargs)


class FakeDolaClient:
    requests: list[dict] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/api/video-pool"):
            return FakeResponse(
                {"model_costs": [{"model": "Dreamina Seedance 2.5", "credits": 2}]}
            )
        if url.endswith("/api/video-accounts"):
            return FakeResponse(
                {
                    "accounts": [
                        {
                            "id": "a" * 32,
                            "pool_eligible": True,
                            "credits_available": 4,
                        }
                    ]
                }
            )
        if "/attachments" in url:
            return FakeResponse({"id": "b" * 32, "account_id": "a" * 32})
        if url.endswith("/api/video-tasks"):
            return FakeResponse({"id": "c" * 32, "status": "queued", "error": ""})
        if url.endswith(f"/api/video-tasks/{'c' * 32}/file"):
            return FakeResponse(content=b"\x00\x00\x00\x18ftypmp42dola", content_type="video/mp4")
        if url.endswith(f"/api/video-tasks/{'c' * 32}"):
            return FakeResponse({"id": "c" * 32, "status": "completed", "error": ""})
        raise AssertionError(f"Unexpected Dola request: {method} {url}")


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


class FakeAgnesImageUrlClient:
    requests: list[dict] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, _url: str, **kwargs) -> FakeResponse:
        self.__class__.requests.append(kwargs["json"])
        return FakeResponse(
            {
                "data": [
                    {
                        "url": "https://cdn.example.test/generated.png",
                        "b64_json": "",
                    }
                ]
            }
        )

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        assert url == "https://cdn.example.test/generated.png"
        return FakeResponse(content=b"valid-image-bytes", content_type="application/octet-stream")


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
                "image": {
                    "enabled": True,
                    "min_count": 1,
                    "max_count": 2,
                    "accepted_mime_types": ["image/png"],
                },
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
        reference_media=[
            {
                "type": "image",
                "url": "https://assets.example.test/frame.png",
                "mime_type": "image/png",
            }
        ],
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
    with pytest.raises(ValueError, match="image/jpeg"):
        validate_video_generation_request(
            capabilities,
            generation_mode="first_frame",
            duration_seconds=5,
            resolution="768p横",
            aspect_ratio="16:9",
            reference_media=[
                {
                    "type": "image",
                    "url": "https://assets.example.test/frame.jpg",
                    "mime_type": "image/jpeg",
                }
            ],
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


def test_declarative_video_adapter_retries_rate_limit_and_poll_network_error(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name

    async def no_sleep(_delay: float) -> None:
        return None

    FakeResilientAdapterClient.requests.clear()
    FakeResilientAdapterClient.post_attempts = 0
    FakeResilientAdapterClient.get_attempts = 0
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeResilientAdapterClient)
    monkeypatch.setattr(media_gateway.asyncio, "sleep", no_sleep)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://provider.example.test",
        api_key=None,
        extra_headers={},
        adapter_config=adapter_config(),
        credentials={"token": "secret-token"},
    )
    request = VideoGenerationRequest(
        model="h3-video",
        prompt="雨夜港口",
        resolution="768p横",
        aspect_ratio="16:9",
        duration_seconds=8,
        reference_image_url=None,
        capabilities={},
        idempotency_key="resilient-video-task",
    )

    submitted = asyncio.run(gateway.submit_video(request))
    completed = asyncio.run(gateway.poll_video(request, submitted.provider_job_id or ""))

    assert submitted.provider_job_id == "video-job-1"
    assert completed.status == "succeeded"
    assert FakeResilientAdapterClient.post_attempts == 2
    assert FakeResilientAdapterClient.get_attempts == 2


def test_dola_preset_uploads_references_polls_and_downloads_video(monkeypatch) -> None:
    FakeDolaClient.requests.clear()
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeDolaClient)
    gateway = OpenAICompatibleMediaGateway(
        base_url="http://127.0.0.1:8199",
        api_key="dola-service-token",
        extra_headers={},
        provider_code=DOLA_PROVIDER_CODE,
        adapter_config=dola_video_adapter_config(),
    )
    request = VideoGenerationRequest(
        model="Dreamina Seedance 2.5",
        prompt="海边日出，镜头缓慢向前推进",
        resolution="720p",
        aspect_ratio="16:9",
        duration_seconds=10,
        reference_image_url=None,
        reference_media=[
            {
                "type": "image",
                "mime_type": "image/png",
                "data_uri": "data:image/png;base64," + base64.b64encode(b"png-image").decode(),
            }
        ],
        capabilities=dola_video_capabilities(),
        idempotency_key="dola-video-task-1",
        generation_mode="first_frame",
        audio_enabled=True,
    )

    submitted = asyncio.run(gateway.submit_video(request))
    assert submitted.status == "pending"
    assert submitted.provider_job_id == "c" * 32
    upload = next(item for item in FakeDolaClient.requests if "/attachments" in item["url"])
    assert upload["content"] == b"png-image"
    assert upload["params"] == {"filename": "reference-1.png"}
    create = next(item for item in FakeDolaClient.requests if item["url"].endswith("/api/video-tasks"))
    assert create["headers"]["Authorization"] == "Bearer dola-service-token"
    assert create["headers"]["Idempotency-Key"] == "dola-video-task-1"
    assert create["json"] == {
        "prompt": "海边日出，镜头缓慢向前推进",
        "model": "Dreamina Seedance 2.5",
        "duration": 10,
        "ratio": "16:9",
        "timeout_seconds": 900,
        "attachment_ids": ["b" * 32],
        "account_id": "a" * 32,
    }

    completed = asyncio.run(gateway.poll_video(request, submitted.provider_job_id or ""))
    assert completed.status == "succeeded"
    assert completed.provider_job_id == "c" * 32
    assert completed.video_data == b"\x00\x00\x00\x18ftypmp42dola"
    assert completed.content_type == "video/mp4"


def test_dola_verification_keeps_original_task_pending() -> None:
    gateway = OpenAICompatibleMediaGateway(
        base_url="http://127.0.0.1:8199",
        api_key="test-token",
        extra_headers={},
        provider_code=DOLA_PROVIDER_CODE,
        adapter_config=dola_video_adapter_config(),
    )
    result = asyncio.run(gateway._dola_video_result(
        None,
        {"id": "c" * 32, "status": "waiting_verification"},
    ))
    assert result.status == "pending"
    assert result.provider_job_id == "c" * 32
    assert "waiting_verification" in dola_video_adapter_config()["video"]["response"]["pending_values"]


def test_dola_preset_exposes_conservative_video_capabilities() -> None:
    capabilities = dola_video_capabilities()
    assert capabilities["duration_resolution_map"] == [
        {"durations": [5.0, 10.0], "resolutions": ["720p"]}
    ]
    assert capabilities["reference_limits"]["image"]["max_count"] == 9
    assert capabilities["reference_limits"]["audio"]["enabled"] is False
    assert capabilities["aspect_ratios"] == ["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"]


class FakeAgnesClient:
    requests: list[tuple[str, str, dict | None, dict | None]] = []

    def __init__(self, **_kwargs) -> None:
        self.is_closed = False

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append((method, url, kwargs.get("json"), kwargs.get("params")))
        if url.endswith("/agnesapi"):
            return FakeResponse(
                {
                    "id": "agnes-video-1",
                    "status": "completed",
                    "url": "https://cdn.example.test/agnes.mp4",
                }
            )
        return FakeResponse({"video_id": "agnes-video-1", "status": "processing"})

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        assert url == "https://cdn.example.test/agnes.mp4"
        return FakeResponse(content=b"agnes-video", content_type="video/mp4")

    async def aclose(self) -> None:
        self.is_closed = True


def test_agnes_preset_contract_supports_multimodal_video_references(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name

    FakeAgnesClient.requests.clear()
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeAgnesClient)
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://apihub.agnes-ai.com/v1",
        api_key="agnes-secret",
        extra_headers={},
        adapter_config=agnes_video_adapter_config(),
    )
    request = VideoGenerationRequest(
        model=AGNES_VIDEO_MODEL_ID,
        prompt="雨夜街道，镜头缓慢推进",
        resolution="720p",
        aspect_ratio="16:9",
        duration_seconds=6,
        reference_image_url=None,
        reference_media=[
            {"type": "image", "url": "https://assets.example.test/frame.png"},
            {"type": "audio", "url": "https://assets.example.test/voice.mp3"},
        ],
        capabilities=agnes_video_capabilities(),
        idempotency_key="agnes-video-task-1",
        generation_mode="first_frame",
        audio_enabled=True,
    )

    submitted = asyncio.run(gateway.submit_video(request))
    assert submitted.status == "pending"
    assert submitted.provider_job_id == "agnes-video-1"
    create = FakeAgnesClient.requests[0]
    assert create[0:2] == ("POST", "https://apihub.agnes-ai.com/v1/videos")
    assert create[2] == {
        "model": AGNES_VIDEO_MODEL_ID,
        "prompt": "雨夜街道，镜头缓慢推进",
        "seconds": "6",
        "mode": "reference",
        "size": "720P",
        "aspect_ratio": "16:9",
        "n": 1,
        "images": ["https://assets.example.test/frame.png"],
        "audios": ["https://assets.example.test/voice.mp3"],
    }

    completed = asyncio.run(gateway.poll_video(request, submitted.provider_job_id))
    assert completed.status == "succeeded"
    assert completed.video_data == b"agnes-video"
    assert completed.content_type == "video/mp4"
    poll = FakeAgnesClient.requests[1]
    assert poll[0:2] == ("GET", "https://apihub.agnes-ai.com/v1/../agnesapi")
    assert poll[3] == {"video_id": "agnes-video-1", "model_name": AGNES_VIDEO_MODEL_ID}


def test_agnes_image_capabilities_use_nested_response_format() -> None:
    capabilities = agnes_image_capabilities()
    assert capabilities["endpoint"] == "images/generations"
    assert capabilities["size_map"]["4K"] == "4K"
    assert capabilities["request_overrides"] == {"extra_body": {"response_format": "url"}}
    assert capabilities["nested_response_format"] is True
    assert capabilities["aspect_ratio_parameter"] == "ratio"
    assert capabilities["generation_modes"] == ["text_to_image", "image_to_image"]
    assert capabilities["image_reference_parameter"] == "image"
    assert capabilities["image_reference_container"] == "extra_body"
    assert capabilities["image_reference_multiple"] is True


def test_agnes_image_21_prompt_is_flattened_without_changing_meaningful_text() -> None:
    prompt = "第一行\r\n（光氛围：1.6）\t冷白皮肤\x00，保留中文与标点。"

    sanitized = image_prompt_for_provider(
        AGNES_IMAGE_21_MODEL_ID,
        prompt,
        agnes_image_21_capabilities(),
    )

    assert sanitized == "第一行 光氛围 冷白皮肤 ，保留中文与标点。"
    assert prompt == "第一行\r\n（光氛围：1.6）\t冷白皮肤\x00，保留中文与标点。"


def test_other_image_models_keep_multiline_and_weight_syntax() -> None:
    prompt = "第一行\n(photorealistic:1.8)"

    assert image_prompt_for_provider("other-image-model", prompt, {}) == prompt


def test_agnes_flash_video_capabilities_only_advertise_supported_720p() -> None:
    capabilities = agnes_video_capabilities()

    assert capabilities["duration_resolution_map"] == [
        {"durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], "resolutions": ["720p"]}
    ]
    assert capabilities["provider_resolution_map"] == {"720p": "720P"}


def test_agnes_video_references_use_embedded_media_data() -> None:
    references = agnes_video_adapter_config()["video"]["references"]

    assert {item["media_type"]: item["source"] for item in references} == {
        "image": "data_uri",
        "audio": "data_uri",
    }


def test_agnes_video_poll_interval_is_rate_limit_safe() -> None:
    assert agnes_video_adapter_config()["video"]["poll_interval_seconds"] >= 10


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


def test_image_generation_falls_back_to_url_when_b64_json_is_empty(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name == "图片"

    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeAgnesImageUrlClient)
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    FakeAgnesImageUrlClient.requests.clear()
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://apihub.agnes-ai.com/v1",
        api_key="secret",
        extra_headers={},
        provider_code=AGNES_PROVIDER_CODE,
    )
    result = asyncio.run(
        gateway.generate_image(
            ImageGenerationRequest(
                model="agnes-image-2.1-flash",
                prompt="第一行\n（真实光影：1.5）\tred cup",
                resolution="2K",
                aspect_ratio="16:9",
                capabilities=agnes_image_capabilities(),
                idempotency_key="agnes-image-task-1",
            )
        )
    )
    assert result == b"valid-image-bytes"
    assert FakeAgnesImageUrlClient.requests[0]["prompt"] == "第一行 真实光影 red cup"


def test_agnes_image_references_use_nested_image_array(monkeypatch) -> None:
    async def allow_test_urls(_url: str, *, media_name: str = "图片") -> None:
        assert media_name == "图片"

    monkeypatch.setattr(media_gateway.httpx, "AsyncClient", FakeAgnesImageUrlClient)
    monkeypatch.setattr(media_gateway, "_validate_download_url", allow_test_urls)
    FakeAgnesImageUrlClient.requests.clear()
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://apihub.agnes-ai.com/v1",
        api_key="secret",
        extra_headers={},
        provider_code=AGNES_PROVIDER_CODE,
    )
    first = "data:image/png;base64,Zmlyc3Q="
    second = "data:image/webp;base64,c2Vjb25k"
    legacy_capabilities = agnes_image_21_capabilities()
    legacy_capabilities.update(
        {
            "image_reference_parameter": "image_url",
            "image_reference_container": "",
            "image_reference_multiple": False,
            "image_generation_mode_parameter": "generation_mode",
            "request_overrides": {
                "image_url": "https://stale.example.test/reference.png",
                "generation_mode": "image_to_image",
                "extra_body": {"response_format": "url"},
            },
        }
    )

    result = asyncio.run(
        gateway.generate_image(
            ImageGenerationRequest(
                model="custom-agnes-image-alias",
                prompt="合成两个人物并保持身份特征",
                resolution="2K",
                aspect_ratio="16:9",
                capabilities=legacy_capabilities,
                idempotency_key="agnes-image-reference-task",
                reference_image_url=first,
                reference_image_urls=[first, second],
                generation_mode="image_to_image",
            )
        )
    )

    assert result == b"valid-image-bytes"
    payload = FakeAgnesImageUrlClient.requests[0]
    assert "image_url" not in payload
    assert "generation_mode" not in payload
    assert payload["extra_body"] == {
        "response_format": "url",
        "image": [first, second],
    }


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
