from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")
_PATH_PART_PATTERN = re.compile(r"([^.[\]]+)|\[(\d+|\*|\?[^\]]+)\]")


class CredentialField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    input_type: Literal["text", "password", "url"] = "password"
    required: bool = True
    placeholder: str = Field(default="", max_length=240)
    help_text: str = Field(default="", max_length=500)


class ResponseAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=240)
    accepted_values: list[str | int | bool] = Field(min_length=1, max_length=30)
    message: str = Field(default="上游响应校验失败", max_length=240)


class HttpRequestTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    path: str = Field(min_length=1, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    assertions: list[ResponseAssertion] = Field(default_factory=list, max_length=10)

    @field_validator("path")
    @classmethod
    def keep_request_on_provider_host(cls, value: str) -> str:
        if "://" in value or value.startswith("//"):
            raise ValueError("适配器请求 path 必须是供应商 Base URL 下的相对路径")
        return value


class CatalogAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: HttpRequestTemplate
    items_path: str = Field(default="data", max_length=240)
    model_id_path: str = Field(default="id", max_length=240)
    name_path: str = Field(default="name", max_length=240)
    owner_path: str = Field(default="owned_by", max_length=240)


class ReferencePayloadMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["image", "video", "audio"]
    strategy: Literal["single", "array", "indexed_fields"] = "array"
    field: str = Field(min_length=1, max_length=120)
    start_index: int = Field(default=0, ge=0, le=100)
    source: Literal["url", "data_uri", "base64"] = "url"


class VideoResponseMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id_path: str = Field(default="data.id", max_length=240)
    status_path: str = Field(default="data.status", max_length=240)
    result_url_path: str = Field(default="data.url", max_length=240)
    result_base64_path: str = Field(default="", max_length=240)
    error_path: str = Field(default="message", max_length=240)
    success_values: list[str] = Field(default_factory=lambda: ["succeeded", "completed", "success", "done"])
    pending_values: list[str] = Field(default_factory=lambda: ["queued", "pending", "running", "processing"])
    failed_values: list[str] = Field(default_factory=lambda: ["failed", "error", "cancelled", "canceled"])


class VideoAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    create: HttpRequestTemplate
    poll: HttpRequestTemplate | None = None
    response: VideoResponseMapping = Field(default_factory=VideoResponseMapping)
    references: list[ReferencePayloadMapping] = Field(default_factory=list, max_length=3)
    poll_interval_seconds: float = Field(default=5, ge=1, le=60)
    poll_timeout_seconds: float = Field(default=1800, ge=30, le=7200)

    @model_validator(mode="after")
    def require_poll_for_async_jobs(self) -> VideoAdapter:
        if self.poll is None and self.response.task_id_path:
            raise ValueError("配置 task_id_path 时必须同时配置 poll 请求")
        return self


class ProviderAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    credential_fields: list[CredentialField] = Field(default_factory=list, max_length=20)
    connectivity: HttpRequestTemplate | None = None
    catalog: CatalogAdapter | None = None
    video: VideoAdapter | None = None

    @model_validator(mode="after")
    def unique_credentials(self) -> ProviderAdapterConfig:
        keys = [item.key for item in self.credential_fields]
        if len(keys) != len(set(keys)):
            raise ValueError("自定义凭据 key 不能重复")
        return self


AUTODL_MINIMAX_H3_PROVIDER_CODE = "autodl-minimax-h3"
AUTODL_MINIMAX_H3_MODEL_ID = "minimax_h3_lightx2v_v5_15s"
AGNES_PROVIDER_CODE = "agnes-ai"
AGNES_TEXT_MODEL_ID = "agnes-2.5-flash"
AGNES_IMAGE_21_MODEL_ID = "agnes-image-2.1-flash"
AGNES_IMAGE_MODEL_ID = "agnes-image-2.5-flash"
AGNES_VIDEO_MODEL_ID = "agnes-video-2.5-flash"


def agnes_video_adapter_config() -> dict[str, Any]:
    """Return Agnes' documented asynchronous video API as a declarative adapter."""
    return ProviderAdapterConfig.model_validate(
        {
            "schema_version": 1,
            "video": {
                "create": {
                    "method": "POST",
                    "path": "videos",
                    "headers": {"Content-Type": "application/json"},
                    "body": {
                        "model": "{{model}}",
                        "prompt": "{{prompt}}",
                        "seconds": "{{duration_string}}",
                        "mode": "{{provider_mode}}",
                        "size": "{{provider_resolution}}",
                        "aspect_ratio": "{{aspect_ratio}}",
                        "n": 1,
                    },
                },
                "poll": {
                    "method": "GET",
                    "path": "../agnesapi",
                    "query": {
                        "video_id": "{{job_id}}",
                        "model_name": "{{model}}",
                    },
                },
                "response": {
                    "task_id_path": "video_id",
                    "status_path": "status",
                    "result_url_path": "metadata.url",
                    "error_path": "error.message",
                    "success_values": ["completed", "success", "succeeded", "done"],
                    "pending_values": ["queued", "in_progress", "processing", "running"],
                    "failed_values": ["failed", "error", "cancelled", "canceled"],
                },
                "references": [
                    {
                        "media_type": "image",
                        "strategy": "array",
                        "field": "images",
                        # Agnes does not accept local /uploads paths. The worker
                        # materializes project media as a data URI before submit.
                        "source": "data_uri",
                    },
                    {
                        "media_type": "audio",
                        "strategy": "array",
                        "field": "audios",
                        "source": "data_uri",
                    },
                ],
                # Agnes limits status-query frequency; a conservative interval
                # avoids turning a healthy long-running job into HTTP 429.
                "poll_interval_seconds": 10,
                "poll_timeout_seconds": 1800,
            },
        }
    ).model_dump(exclude_none=True)


def agnes_video_capabilities() -> dict[str, Any]:
    return VideoModelCapabilities.model_validate(
        {
            "schema_version": 1,
            "generation_modes": ["text_to_video", "first_frame"],
            "reference_limits": {
                "image": {"enabled": True, "min_count": 1, "max_count": 5},
                "video": {"enabled": False, "min_count": 0, "max_count": 0},
                "audio": {"enabled": True, "min_count": 0, "max_count": 3},
            },
            "audio_policy": "optional",
            "duration_resolution_map": [
                {
                    "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12],
                    # Agnes Video 2.5 Flash currently accepts 720P only.
                    "resolutions": ["720p"],
                }
            ],
            "aspect_ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
            "prompt_languages": ["zh-CN", "en"],
            "negative_prompt_supported": False,
            "asynchronous": True,
            "provider_mode_map": {"text_to_video": "text", "first_frame": "reference"},
            "provider_resolution_map": {"720p": "720P"},
        }
    ).model_dump()


def agnes_image_capabilities() -> dict[str, Any]:
    """Return the shared Agnes Image 2.1/2.5 request contract."""
    return {
        "endpoint": "images/generations",
        "size_map": {"1K": "1K", "2K": "2K", "3K": "3K", "4K": "4K"},
        "aspect_ratio_parameter": "ratio",
        "generation_modes": ["text_to_image", "image_to_image"],
        # Agnes routes image editing by an image array nested in extra_body.
        # A top-level image_url is interpreted as an invalid text-image request.
        "image_reference_parameter": "image",
        "image_reference_container": "extra_body",
        "image_reference_multiple": True,
        "image_generation_mode_parameter": "",
        "nested_response_format": True,
        # URL output is considerably more reliable for Agnes and is downloaded
        # into our object storage by the media gateway.
        "request_overrides": {"extra_body": {"response_format": "url"}},
    }


def agnes_image_21_capabilities() -> dict[str, Any]:
    """Return Agnes Image 2.1 capabilities with its strict prompt contract."""
    capabilities = agnes_image_capabilities()
    capabilities["prompt_sanitization"] = {
        "single_line": True,
        "strip_control_characters": True,
        "strip_prompt_weights": True,
        "collapse_whitespace": True,
    }
    return capabilities


def autodl_minimax_h3_adapter_config() -> dict[str, Any]:
    """Return the declarative adapter for the official AutoDL ComfyUI workflow contract."""
    return ProviderAdapterConfig.model_validate(
        {
            "schema_version": 1,
            "credential_fields": [
                {
                    "key": "apiKey",
                    "label": "ComfyUI Token",
                    "input_type": "password",
                    "required": True,
                    "placeholder": "输入 AutoDL ComfyUI Token",
                    "help_text": "按上游协议原样写入 Authorization，不添加 Bearer 前缀",
                }
            ],
            "video": {
                "create": {
                    "method": "POST",
                    "path": "api/v1/comfyui/comfyui_workflow/{{model}}",
                    "headers": {
                        "Authorization": "{{credentials.apiKey}}",
                        "Content-Type": "application/json",
                    },
                    "body": {
                        "prompt": "{{prompt}}",
                        "duration": "{{duration}}",
                        "resolution": "{{resolution}}",
                    },
                    "assertions": [
                        {
                            "path": "code",
                            "accepted_values": ["Success"],
                            "message": "AutoDL 视频任务创建失败",
                        }
                    ],
                },
                "poll": {
                    "method": "GET",
                    "path": "api/v1/comfyui/comfyui_workflow/result/{{job_id}}",
                    "headers": {"Authorization": "{{credentials.apiKey}}"},
                    "assertions": [
                        {
                            "path": "code",
                            "accepted_values": ["Success"],
                            "message": "AutoDL 视频任务查询失败",
                        }
                    ],
                },
                "response": {
                    "task_id_path": "data.task_id",
                    "status_path": "data.status",
                    "result_url_path": "data.results[?type=video].url",
                    "result_base64_path": "",
                    "error_path": "msg",
                    "success_values": ["SUCCESS"],
                    "pending_values": ["QUEUED", "RUNNING", "PROCESSING"],
                    "failed_values": ["FAILED", "ERROR", "CANCELLED"],
                },
                "references": [
                    {
                        "media_type": "image",
                        "strategy": "indexed_fields",
                        "field": "ref_image_",
                        "start_index": 0,
                        "source": "data_uri",
                    }
                ],
                "poll_interval_seconds": 5,
                "poll_timeout_seconds": 1800,
            },
        }
    ).model_dump(exclude_none=True)


def autodl_minimax_h3_capabilities() -> dict[str, Any]:
    durations = list(range(1, 16))
    return VideoModelCapabilities.model_validate(
        {
            "schema_version": 1,
            "generation_modes": ["first_frame", "multi_shot"],
            "reference_limits": {
                "image": {"enabled": True, "min_count": 1, "max_count": 9},
                "video": {"enabled": False, "min_count": 0, "max_count": 0},
                "audio": {"enabled": False, "min_count": 0, "max_count": 0},
            },
            "audio_policy": "disabled",
            "duration_resolution_map": [
                {
                    "durations": durations,
                    "resolutions": ["768p竖", "768p横", "768p(1:1)"],
                }
            ],
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "prompt_languages": ["zh-CN", "en"],
            "negative_prompt_supported": False,
            "asynchronous": True,
        }
    ).model_dump()


class ReferenceLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_count: int = Field(default=0, ge=0, le=32)
    max_count: int = Field(default=0, ge=0, le=32)

    @model_validator(mode="after")
    def valid_range(self) -> ReferenceLimit:
        if self.enabled and self.max_count < 1:
            raise ValueError("启用参考媒体后 max_count 至少为 1")
        if self.min_count > self.max_count:
            raise ValueError("参考媒体 min_count 不能大于 max_count")
        if not self.enabled and (self.min_count or self.max_count):
            raise ValueError("未启用的参考媒体数量必须为 0")
        return self


class DurationResolutionGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    durations: list[float] = Field(min_length=1, max_length=120)
    resolutions: list[str] = Field(min_length=1, max_length=30)

    @field_validator("durations")
    @classmethod
    def valid_durations(cls, values: list[float]) -> list[float]:
        if any(value <= 0 or value > 300 for value in values):
            raise ValueError("视频时长必须在 0 到 300 秒之间")
        return list(dict.fromkeys(values))

    @field_validator("resolutions")
    @classmethod
    def valid_resolutions(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item.strip()]
        if not cleaned:
            raise ValueError("每组至少需要一个分辨率")
        return list(dict.fromkeys(cleaned))


class VideoModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    generation_modes: list[
        Literal[
            "text_to_video",
            "first_frame",
            "first_last_frame",
            "last_frame",
            "full_reference",
            "multi_shot",
        ]
    ] = Field(default_factory=lambda: ["text_to_video"], min_length=1, max_length=6)
    reference_limits: dict[Literal["image", "video", "audio"], ReferenceLimit] = Field(
        default_factory=lambda: {
            "image": ReferenceLimit(),
            "video": ReferenceLimit(),
            "audio": ReferenceLimit(),
        }
    )
    audio_policy: Literal["optional", "required", "disabled"] = "optional"
    duration_resolution_map: list[DurationResolutionGroup] = Field(
        default_factory=lambda: [DurationResolutionGroup(durations=[5, 10], resolutions=["720p", "1080p"])],
        min_length=1,
        max_length=20,
    )
    aspect_ratios: list[str] = Field(default_factory=lambda: ["16:9", "9:16"], min_length=1, max_length=20)
    prompt_languages: list[str] = Field(default_factory=lambda: ["zh-CN"], min_length=1, max_length=20)
    negative_prompt_supported: bool = False
    asynchronous: bool = True

    @field_validator("generation_modes", "aspect_ratios", "prompt_languages")
    @classmethod
    def unique_strings(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def modes_match_references(self) -> VideoModelCapabilities:
        image = self.reference_limits.get("image", ReferenceLimit())
        video = self.reference_limits.get("video", ReferenceLimit())
        if (
            any(mode in self.generation_modes for mode in ("first_frame", "first_last_frame", "last_frame"))
            and (not image.enabled or image.max_count < 1)
        ):
            raise ValueError("首尾帧模式至少需要启用 1 张图片参考")
        if "full_reference" in self.generation_modes and not video.enabled:
            raise ValueError("全参考模式需要启用视频参考")
        return self


def normalize_adapter_config(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    if len(json.dumps(value, ensure_ascii=False)) > 128 * 1024:
        raise ValueError("供应商适配配置不能超过 128 KB")
    return ProviderAdapterConfig.model_validate(value).model_dump(exclude_none=True)


def normalize_video_capabilities(value: dict[str, Any] | None) -> dict[str, Any]:
    return VideoModelCapabilities.model_validate(value or {}).model_dump()


def supported_video_durations(capabilities: dict[str, Any] | None) -> list[float]:
    if not capabilities:
        return []
    if capabilities.get("schema_version") == 1:
        parsed = VideoModelCapabilities.model_validate(capabilities)
        return sorted(
            {
                float(duration)
                for group in parsed.duration_resolution_map
                for duration in group.durations
            }
        )
    durations = capabilities.get("durations")
    if not isinstance(durations, list):
        return []
    cleaned: list[float] = []
    for item in durations:
        if isinstance(item, (int, float)) and item > 0:
            cleaned.append(float(item))
    return sorted(dict.fromkeys(cleaned))


def closest_supported_video_duration(
    capabilities: dict[str, Any] | None,
    *,
    requested_duration: float,
) -> float:
    durations = supported_video_durations(capabilities)
    if not durations:
        return min(max(float(requested_duration), 1.0), 30.0)
    requested = float(requested_duration)
    return min(durations, key=lambda duration: (abs(duration - requested), duration))


def compatible_video_resolution(
    capabilities: dict[str, Any],
    *,
    duration_seconds: float,
    requested_resolution: str,
    aspect_ratio: str,
) -> str:
    parsed = VideoModelCapabilities.model_validate(capabilities)
    matching_groups = [
        group
        for group in parsed.duration_resolution_map
        if any(abs(duration_seconds - value) < 0.001 for value in group.durations)
    ]
    if not matching_groups:
        supported = sorted(
            {
                f"{duration:g}s"
                for group in parsed.duration_resolution_map
                for duration in group.durations
            }
        )
        raise ValueError(f"当前视频模型不支持 {duration_seconds:g} 秒，支持时长：{', '.join(supported)}")
    if any(requested_resolution in group.resolutions for group in matching_groups):
        return requested_resolution

    candidates = list(
        dict.fromkeys(resolution for group in matching_groups for resolution in group.resolutions)
    )
    aspect_hints = {
        "16:9": ("横", "landscape", "horizontal", "16:9"),
        "9:16": ("竖", "portrait", "vertical", "9:16"),
        "1:1": ("1:1", "square", "正方", "方"),
    }
    hints = aspect_hints.get(aspect_ratio, ())
    hinted = [
        resolution
        for resolution in candidates
        if any(hint in resolution.lower() for hint in hints)
    ]
    if hinted:
        return hinted[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"当前视频模型不支持 {duration_seconds:g} 秒与 {requested_resolution} 的组合；"
        f"该时长支持分辨率：{', '.join(candidates)}"
    )


def validate_video_generation_request(
    capabilities: dict[str, Any],
    *,
    generation_mode: str,
    duration_seconds: float,
    resolution: str,
    aspect_ratio: str,
    reference_media: list[dict[str, str]],
    audio_enabled: bool,
) -> None:
    parsed = VideoModelCapabilities.model_validate(capabilities)
    if generation_mode not in parsed.generation_modes:
        raise ValueError(f"当前视频模型不支持生成模式：{generation_mode}")
    compatible_video_resolution(
        capabilities,
        duration_seconds=duration_seconds,
        requested_resolution=resolution,
        aspect_ratio=aspect_ratio,
    )
    if aspect_ratio not in parsed.aspect_ratios:
        raise ValueError(f"当前视频模型不支持画幅比例：{aspect_ratio}")
    for media_type, limit in parsed.reference_limits.items():
        count = sum(1 for item in reference_media if item.get("type") == media_type)
        if not limit.enabled and count:
            raise ValueError(f"当前视频模型不支持 {media_type} 参考媒体")
        if limit.enabled and not limit.min_count <= count <= limit.max_count:
            raise ValueError(
                f"{media_type} 参考媒体数量必须在 {limit.min_count} 到 {limit.max_count} 之间"
            )
    if parsed.audio_policy == "required" and not audio_enabled:
        raise ValueError("当前视频模型要求输出音频")
    if parsed.audio_policy == "disabled" and audio_enabled:
        raise ValueError("当前视频模型仅支持无声视频")


def extract_path(document: Any, path: str) -> Any:
    if not path:
        return None
    current = document
    normalized = path.removeprefix("$").lstrip(".")
    for match in _PATH_PART_PATTERN.finditer(normalized):
        key, selector = match.groups()
        if key is not None:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
            continue
        if not isinstance(current, list):
            return None
        if selector == "*":
            current = current[0] if current else None
        elif selector and selector.startswith("?"):
            expression = selector[1:]
            field, separator, expected = expression.partition("=")
            if not separator:
                return None
            expected = expected.strip("'\"")
            current = next(
                (item for item in current if isinstance(item, dict) and str(item.get(field)) == expected),
                None,
            )
        else:
            index = int(selector or 0)
            current = current[index] if 0 <= index < len(current) else None
        if current is None:
            return None
    return current


def render_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if not isinstance(value, str):
        return value
    full_match = _TEMPLATE_PATTERN.fullmatch(value)
    if full_match:
        resolved = extract_path(context, full_match.group(1))
        return resolved if resolved is not None else ""

    def replace(match: re.Match[str]) -> str:
        resolved = extract_path(context, match.group(1))
        return "" if resolved is None else str(resolved)

    return _TEMPLATE_PATTERN.sub(replace, value)
