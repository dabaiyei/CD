from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.services.provider_adapters import (
    AGNES_IMAGE_21_MODEL_ID,
    AGNES_IMAGE_MODEL_ID,
    AGNES_PROVIDER_CODE,
    DOLA_PROVIDER_CODE,
    HttpRequestTemplate,
    ProviderAdapterConfig,
    ReferencePayloadMapping,
    VideoResponseMapping,
    extract_path,
    render_template,
)

MAX_GENERATED_IMAGE_BYTES = 16 * 1024 * 1024
MAX_GENERATED_VIDEO_BYTES = 512 * 1024 * 1024
MAX_GENERATED_AUDIO_BYTES = 64 * 1024 * 1024
IMAGE_REQUEST_ATTEMPTS = 3
RETRYABLE_IMAGE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 520, 522, 524})
_PROMPT_WEIGHT_PATTERN = re.compile(
    r"[（(]\s*([^()（）]+?)\s*[:：]\s*[+-]?\d+(?:\.\d+)?\s*[)）]"
)
_AGNES_IMAGE_21_PROMPT_SANITIZATION: dict[str, bool] = {
    "single_line": True,
    "strip_control_characters": True,
    "strip_prompt_weights": True,
    "collapse_whitespace": True,
}


class ModelGatewayError(RuntimeError):
    pass


def image_prompt_for_provider(
    model: str,
    prompt: str,
    capabilities: dict[str, object],
) -> str:
    """Build the provider-facing prompt without mutating the stored source prompt."""
    configured = capabilities.get("prompt_sanitization")
    if isinstance(configured, dict):
        if configured.get("enabled") is False:
            return prompt
        sanitization = configured
    elif model == AGNES_IMAGE_21_MODEL_ID:
        # Existing Agnes 2.1 models may predate the capability flag. Keep the
        # compatibility behavior tied to this exact model identifier.
        sanitization = _AGNES_IMAGE_21_PROMPT_SANITIZATION
    else:
        return prompt

    result = prompt
    if sanitization.get("strip_prompt_weights"):
        result = _PROMPT_WEIGHT_PATTERN.sub(lambda match: match.group(1).strip(), result)
    if sanitization.get("strip_control_characters"):
        result = "".join(
            " " if unicodedata.category(character) == "Cc" else character
            for character in result
        )
    if sanitization.get("single_line"):
        result = result.replace("\u2028", " ").replace("\u2029", " ")
    if sanitization.get("collapse_whitespace") or sanitization.get("single_line"):
        result = " ".join(result.split())
    result = result.strip()
    if not result:
        raise ModelGatewayError("图片提示词经模型兼容处理后为空")
    return result


@dataclass(slots=True)
class ImageGenerationRequest:
    model: str
    prompt: str
    resolution: str
    aspect_ratio: str
    capabilities: dict[str, object]
    idempotency_key: str
    reference_image_url: str | None = None
    generation_mode: str = "text_to_image"
    reference_image_urls: list[str] | None = None


@dataclass(slots=True)
class VideoGenerationRequest:
    model: str
    prompt: str
    resolution: str
    aspect_ratio: str
    duration_seconds: float
    reference_image_url: str | None
    capabilities: dict[str, object]
    idempotency_key: str
    generation_mode: str = "text_to_video"
    audio_enabled: bool = False
    reference_media: list[dict[str, str]] | None = None


@dataclass(slots=True)
class VideoGenerationResult:
    status: Literal["pending", "succeeded", "failed"]
    provider_job_id: str | None = None
    video_data: bytes | None = None
    content_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class SpeechGenerationRequest:
    model: str
    text: str
    voice: str
    style: str
    instructions: str
    capabilities: dict[str, object]
    idempotency_key: str


@dataclass(slots=True)
class SpeechGenerationResult:
    status: Literal["pending", "succeeded", "failed"]
    provider_job_id: str | None = None
    audio_data: bytes | None = None
    content_type: str | None = None
    duration_seconds: float | None = None
    error_message: str | None = None


class OpenAICompatibleMediaGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        extra_headers: dict[str, str],
        provider_code: str | None = None,
        adapter_config: dict[str, Any] | None = None,
        credentials: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider_code = (provider_code or "").strip().lower()
        self.headers = dict(extra_headers)
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.adapter = ProviderAdapterConfig.model_validate(adapter_config) if adapter_config else None
        self.credentials = credentials or {}

    async def generate_image(self, request: ImageGenerationRequest) -> bytes:
        endpoint = str(request.capabilities.get("endpoint") or "images/generations").lstrip("/")
        size_map = request.capabilities.get("size_map")
        size = size_map.get(request.resolution) if isinstance(size_map, dict) else None
        provider_prompt = image_prompt_for_provider(
            request.model,
            request.prompt,
            request.capabilities,
        )
        payload: dict[str, object] = {
            "model": request.model,
            "prompt": provider_prompt,
            "n": 1,
        }
        if not request.capabilities.get("nested_response_format"):
            payload["response_format"] = "b64_json"
        if isinstance(size, str) and size:
            payload["size"] = size
        ratio_parameter = request.capabilities.get("aspect_ratio_parameter")
        if isinstance(ratio_parameter, str) and ratio_parameter:
            payload[ratio_parameter] = request.aspect_ratio
        overrides = request.capabilities.get("request_overrides")
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                existing = payload.get(key)
                if isinstance(existing, dict) and isinstance(value, dict):
                    payload[key] = {**existing, **value}
                else:
                    payload[key] = value
            payload["model"] = request.model
            payload["prompt"] = provider_prompt

        reference_urls = list(request.reference_image_urls or [])
        if request.reference_image_url and request.reference_image_url not in reference_urls:
            reference_urls.insert(0, request.reference_image_url)
        if reference_urls:
            is_agnes_image = (
                self.provider_code == AGNES_PROVIDER_CODE
                or request.model in {AGNES_IMAGE_21_MODEL_ID, AGNES_IMAGE_MODEL_ID}
            )
            if is_agnes_image:
                # Older persisted presets may still advertise image_url. Agnes
                # interprets that as a text-image queue request and rejects it.
                payload.pop("image_url", None)
                payload.pop("generation_mode", None)
                reference_parameter = "image"
                reference_container = "extra_body"
                reference_multiple = True
            else:
                reference_parameter = request.capabilities.get("image_reference_parameter")
                if not isinstance(reference_parameter, str) or not reference_parameter.strip():
                    reference_parameter = "image_url"
                reference_container = request.capabilities.get("image_reference_container")
                if not isinstance(reference_container, str) or not reference_container.strip():
                    reference_container = ""
                reference_multiple = request.capabilities.get("image_reference_multiple")
                if not isinstance(reference_multiple, bool):
                    reference_multiple = False
            reference_value: object = reference_urls if reference_multiple else reference_urls[0]
            if reference_container:
                nested = payload.get(reference_container)
                nested_payload = dict(nested) if isinstance(nested, dict) else {}
                nested_payload[reference_parameter] = reference_value
                payload[reference_container] = nested_payload
            else:
                payload[reference_parameter] = reference_value

            generation_mode_parameter = (
                ""
                if is_agnes_image
                else request.capabilities.get(
                    "image_generation_mode_parameter",
                    "generation_mode",
                )
            )
            if isinstance(generation_mode_parameter, str) and generation_mode_parameter.strip():
                payload[generation_mode_parameter] = request.generation_mode

        headers = {**self.headers, "Idempotency-Key": request.idempotency_key}
        timeout = httpx.Timeout(get_settings().media_request_timeout_seconds)
        try:
            # 供应商通常返回对象存储/CDN URL。部分 CDN 会先返回 301/302，
            # 如果不跟随重定向，响应体可能为空，最终会被误判为“空文件”。
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response: httpx.Response | None = None
                for attempt in range(IMAGE_REQUEST_ATTEMPTS):
                    try:
                        response = await client.post(
                            f"{self.base_url}/{endpoint}", headers=headers, json=payload
                        )
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt + 1 >= IMAGE_REQUEST_ATTEMPTS:
                            raise
                        await asyncio.sleep(0.75 * (2**attempt))
                        continue
                    if (
                        response.status_code not in RETRYABLE_IMAGE_STATUS_CODES
                        or attempt + 1 >= IMAGE_REQUEST_ATTEMPTS
                    ):
                        break
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(max(float(retry_after), 0.25), 5.0)
                    except ValueError:
                        delay = 0.75 * (2**attempt)
                    await asyncio.sleep(delay)
                if response is None:
                    raise ModelGatewayError("图片平台未返回响应")
                response.raise_for_status()
                if response.headers.get("content-type", "").startswith("image/"):
                    return _bounded_image(response.content)
                body = response.json()
                item = body.get("data", [None])[0] if isinstance(body, dict) else None
                if not isinstance(item, dict):
                    raise ModelGatewayError("图片平台返回了无法识别的数据结构")
                encoded = item.get("b64_json")
                # Agnes 等兼容接口会同时返回 b64_json="" 和有效的 url。
                # 空 Base64 不是图片数据，应继续回退到 URL 下载分支。
                if isinstance(encoded, str) and encoded.strip():
                    try:
                        return _bounded_image(base64.b64decode(encoded, validate=True))
                    except ValueError as exc:
                        raise ModelGatewayError("图片平台返回了无效的 Base64 数据") from exc
                image_url = item.get("url")
                if isinstance(image_url, str):
                    await _validate_download_url(image_url)
                    downloaded = await client.get(image_url)
                    downloaded.raise_for_status()
                    downloaded_content_type = downloaded.headers.get("content-type", "").split(";", 1)[0]
                    if (
                        not downloaded_content_type.startswith("image/")
                        and downloaded_content_type != "application/octet-stream"
                    ):
                        raise ModelGatewayError("图片下载地址未返回图片内容")
                    return _bounded_image(downloaded.content)
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                _http_error_message(exc.response, prefix="图片平台返回")
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("无法连接图片模型平台") from exc
        except ValueError as exc:
            raise ModelGatewayError("图片平台返回了无效 JSON") from exc
        raise ModelGatewayError("图片平台未返回图片数据")

    async def submit_video(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        if self.provider_code == DOLA_PROVIDER_CODE:
            return await self._submit_dola_video(request)
        if self.adapter and self.adapter.video:
            body, client = await self._adapter_request(
                self.adapter.video.create,
                self._video_context(request),
                references=self.adapter.video.references,
            )
            try:
                return await self._adapter_video_result(client, body, self.adapter.video.response)
            finally:
                await client.aclose()
        endpoint = str(request.capabilities.get("endpoint") or "videos/generations").lstrip("/")
        payload: dict[str, object] = {
            "model": request.model,
            "prompt": request.prompt,
            "duration": request.duration_seconds,
            "size": request.resolution,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.reference_image_url:
            payload["image_url"] = request.reference_image_url
        overrides = request.capabilities.get("request_overrides")
        if isinstance(overrides, dict):
            payload.update(overrides)
            payload["model"] = request.model
            payload["prompt"] = request.prompt
        return await self._video_request(
            method="POST",
            url=f"{self.base_url}/{endpoint}",
            headers={**self.headers, "Idempotency-Key": request.idempotency_key},
            payload=payload,
        )

    async def poll_video(
        self,
        request: VideoGenerationRequest,
        provider_job_id: str,
    ) -> VideoGenerationResult:
        if self.provider_code == DOLA_PROVIDER_CODE:
            return await self._poll_dola_video(provider_job_id)
        if self.adapter and self.adapter.video:
            if self.adapter.video.poll is None:
                raise ModelGatewayError("自定义视频适配器未配置轮询请求")
            context = self._video_context(request)
            context["job_id"] = provider_job_id
            body, client = await self._adapter_request(self.adapter.video.poll, context)
            try:
                result = await self._adapter_video_result(client, body, self.adapter.video.response)
                if result.provider_job_id is None:
                    result.provider_job_id = provider_job_id
                return result
            finally:
                await client.aclose()
        template = str(request.capabilities.get("status_endpoint") or "videos/{job_id}")
        endpoint = template.replace("{job_id}", provider_job_id).lstrip("/")
        result = await self._video_request(
            method="GET",
            url=f"{self.base_url}/{endpoint}",
            headers=self.headers,
        )
        if result.provider_job_id is None:
            result.provider_job_id = provider_job_id
        return result

    async def _dola_json_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            request_headers = {**self.headers, **dict(kwargs.pop("headers", {}))}
            response = await client.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                headers=request_headers,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                _http_error_message(exc.response, prefix="Dola 中转返回")
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("无法连接 Dola 本地中转服务") from exc
        except ValueError as exc:
            raise ModelGatewayError("Dola 中转返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise ModelGatewayError("Dola 中转返回了无法识别的数据结构")
        return payload

    async def _dola_account_for_uploads(
        self,
        client: httpx.AsyncClient,
        model: str,
    ) -> str:
        pool = await self._dola_json_request(client, "GET", "api/video-pool")
        raw_costs = pool.get("model_costs")
        costs = raw_costs if isinstance(raw_costs, list) else []
        configured_cost = next(
            (
                item.get("credits")
                for item in costs
                if isinstance(item, dict) and str(item.get("model") or "") == model
            ),
            None,
        )
        if not isinstance(configured_cost, int) or configured_cost < 1:
            raise ModelGatewayError(f"Dola 尚未配置模型 {model} 的单次积分消耗")
        account_payload = await self._dola_json_request(client, "GET", "api/video-accounts")
        raw_accounts = account_payload.get("accounts")
        accounts = raw_accounts if isinstance(raw_accounts, list) else []
        candidates = [
            item
            for item in accounts
            if isinstance(item, dict)
            and item.get("pool_eligible") is True
            and isinstance(item.get("credits_available"), int)
            and item["credits_available"] >= configured_cost
            and isinstance(item.get("id"), str)
        ]
        if not candidates:
            raise ModelGatewayError("Dola 没有已验证、已启用且额度足够的可用账号")
        candidates.sort(key=lambda item: int(item.get("credits_available") or 0), reverse=True)
        return str(candidates[0]["id"])

    @staticmethod
    def _dola_reference_image(reference: dict[str, str], index: int) -> tuple[bytes, str]:
        mime_type = str(reference.get("mime_type") or "").lower()
        encoded = str(reference.get("base64") or "")
        data_uri = str(reference.get("data_uri") or "")
        if data_uri:
            header, separator, encoded_body = data_uri.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ModelGatewayError(f"第 {index} 张 Dola 参考图不是有效的 Base64 Data URI")
            encoded = encoded_body
            declared_mime = header[5:].split(";", 1)[0].strip().lower()
            if declared_mime:
                mime_type = declared_mime
        if not encoded:
            raise ModelGatewayError(f"第 {index} 张 Dola 参考图缺少可上传的文件数据")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ModelGatewayError(f"第 {index} 张 Dola 参考图 Base64 无效") from exc
        extensions = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        extension = extensions.get(mime_type)
        if extension is None:
            raise ModelGatewayError(f"Dola 不支持第 {index} 张参考图的格式：{mime_type or '未知'}")
        if not data:
            raise ModelGatewayError(f"第 {index} 张 Dola 参考图为空")
        if len(data) > 20 * 1024 * 1024:
            raise ModelGatewayError(f"第 {index} 张 Dola 参考图超过 20 MiB")
        return data, f"reference-{index}{extension}"

    async def _submit_dola_video(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        prompt = request.prompt.strip()
        if not prompt:
            raise ModelGatewayError("Dola 视频提示词不能为空")
        if len(prompt) > 20_000:
            raise ModelGatewayError("Dola 视频提示词不能超过 20000 字符")
        duration = int(request.duration_seconds)
        if request.duration_seconds != duration or duration not in {5, 10, 15}:
            raise ModelGatewayError("Dola 视频时长仅支持 5、10 或 15 秒")
        references = [
            item
            for item in (request.reference_media or [])
            if item.get("type") == "image"
        ]
        if len(references) > 9:
            raise ModelGatewayError("Dola 每个视频任务最多支持 9 张参考图")
        timeout = httpx.Timeout(get_settings().media_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            attachment_ids: list[str] = []
            account_id: str | None = None
            if references:
                account_id = await self._dola_account_for_uploads(client, request.model)
                for index, reference in enumerate(references, start=1):
                    image_data, filename = self._dola_reference_image(reference, index)
                    uploaded = await self._dola_json_request(
                        client,
                        "POST",
                        f"api/video-accounts/{account_id}/attachments",
                        params={"filename": filename},
                        content=image_data,
                    )
                    attachment_id = uploaded.get("id")
                    if not isinstance(attachment_id, str) or not attachment_id:
                        raise ModelGatewayError("Dola 参考图上传成功但未返回附件 ID")
                    if attachment_id not in attachment_ids:
                        attachment_ids.append(attachment_id)
            payload: dict[str, object] = {
                "prompt": prompt,
                "model": request.model,
                "duration": duration,
                "ratio": request.aspect_ratio,
                "timeout_seconds": 900,
                "attachment_ids": attachment_ids,
            }
            if account_id:
                payload["account_id"] = account_id
            task = await self._dola_json_request(
                client,
                "POST",
                "api/video-tasks",
                headers={"Idempotency-Key": request.idempotency_key},
                json=payload,
            )
            return await self._dola_video_result(client, task)

    async def _poll_dola_video(self, provider_job_id: str) -> VideoGenerationResult:
        timeout = httpx.Timeout(get_settings().media_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            task = await self._dola_json_request(
                client,
                "GET",
                f"api/video-tasks/{provider_job_id}",
            )
            return await self._dola_video_result(client, task, fallback_job_id=provider_job_id)

    async def _dola_video_result(
        self,
        client: httpx.AsyncClient,
        task: dict[str, Any],
        *,
        fallback_job_id: str | None = None,
    ) -> VideoGenerationResult:
        provider_job_id = str(task.get("id") or fallback_job_id or "") or None
        status = str(task.get("status") or "").strip().lower()
        pending = {
            "queued", "starting", "preparing", "submitting", "generating",
            "downloading", "waiting_verification",
        }
        failed = {"failed", "needs_login", "needs_review", "cancelled"}
        if status in pending:
            if not provider_job_id:
                raise ModelGatewayError("Dola 任务未返回可恢复的任务 ID")
            return VideoGenerationResult(status="pending", provider_job_id=provider_job_id)
        if status in failed:
            detail = str(task.get("error") or "").strip()
            return VideoGenerationResult(
                status="failed",
                provider_job_id=provider_job_id,
                error_message=f"Dola 任务 {status}：{detail or '请检查账号池和任务记录'}",
            )
        if status != "completed" or not provider_job_id:
            raise ModelGatewayError(f"Dola 返回未知任务状态：{status or '空状态'}")
        try:
            response = await client.request(
                "GET",
                f"{self.base_url}/api/video-tasks/{provider_job_id}/file",
                headers=self.headers,
                params={"download": "true"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                _http_error_message(exc.response, prefix="Dola 视频下载返回")
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("无法从 Dola 下载已完成视频") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "video/mp4":
            raise ModelGatewayError("Dola 已完成任务未返回 MP4 视频")
        return VideoGenerationResult(
            status="succeeded",
            provider_job_id=provider_job_id,
            video_data=_bounded_video(response.content),
            content_type=content_type,
        )

    def _video_context(self, request: VideoGenerationRequest) -> dict[str, Any]:
        references = request.reference_media or []
        if request.reference_image_url and not references:
            references = [{"type": "image", "url": request.reference_image_url}]
        return {
            "model": request.model,
            "prompt": request.prompt,
            "resolution": request.resolution,
            "aspect_ratio": request.aspect_ratio,
            "duration": request.duration_seconds,
            "duration_seconds": request.duration_seconds,
            "duration_string": f"{request.duration_seconds:g}",
            "generation_mode": request.generation_mode,
            "provider_mode": _mapped_capability_value(
                request.capabilities.get("provider_mode_map"),
                "first_frame" if references else request.generation_mode,
            ),
            "provider_resolution": _mapped_capability_value(
                request.capabilities.get("provider_resolution_map"), request.resolution
            ),
            "audio_enabled": request.audio_enabled,
            "idempotency_key": request.idempotency_key,
            "credentials": self.credentials,
            "references": references,
        }

    async def test_adapter_connectivity(self) -> None:
        if not self.adapter or not self.adapter.connectivity:
            raise ModelGatewayError("供应商未配置独立连通性请求")
        _body, client = await self._adapter_request(
            self.adapter.connectivity,
            {"credentials": self.credentials, "idempotency_key": "connectivity-test"},
        )
        await client.aclose()

    async def fetch_adapter_catalog(self) -> list[dict[str, Any]]:
        if not self.adapter or not self.adapter.catalog:
            raise ModelGatewayError("供应商未配置模型目录适配器")
        catalog = self.adapter.catalog
        body, client = await self._adapter_request(
            catalog.request,
            {"credentials": self.credentials, "idempotency_key": "catalog-discovery"},
        )
        await client.aclose()
        raw_items = extract_path(body, catalog.items_path)
        if not isinstance(raw_items, list):
            raise ModelGatewayError("模型目录 items_path 未指向数组")
        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            model_id = extract_path(raw_item, catalog.model_id_path)
            if model_id in (None, ""):
                continue
            name = extract_path(raw_item, catalog.name_path)
            owner = extract_path(raw_item, catalog.owner_path)
            items.append(
                {
                    "id": str(model_id),
                    "name": str(name or model_id),
                    "owned_by": str(owner) if owner not in (None, "") else None,
                }
            )
        return items

    async def _adapter_request(
        self,
        template: HttpRequestTemplate,
        context: dict[str, Any],
        *,
        references: list[ReferencePayloadMapping] | None = None,
    ) -> tuple[dict[str, Any], httpx.AsyncClient]:
        path = str(render_template(template.path, context)).lstrip("/")
        url = f"{self.base_url}/{path}"
        if self.provider_code != DOLA_PROVIDER_CODE:
            await _validate_download_url(url, media_name="供应商接口")
        headers = {**self.headers, **render_template(template.headers, context)}
        headers.setdefault("Idempotency-Key", str(context.get("idempotency_key") or ""))
        query = render_template(template.query, context)
        body = render_template(template.body, context) if template.body is not None else None
        if body is not None and references:
            _inject_reference_payloads(body, context.get("references") or [], references)
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(get_settings().media_request_timeout_seconds),
            follow_redirects=True,
        )
        try:
            response: httpx.Response | None = None
            attempts = 4
            for attempt in range(attempts):
                try:
                    response = await client.request(
                        template.method,
                        url,
                        headers=headers,
                        params=query,
                        json=body,
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if template.method != "GET" or attempt + 1 >= attempts:
                        raise
                    await asyncio.sleep(min(5.0 * (attempt + 1), 20.0))
                    continue
                retryable_status = response.status_code in RETRYABLE_IMAGE_STATUS_CODES
                safe_to_retry = template.method == "GET" or response.status_code == 429
                if not retryable_status or not safe_to_retry or attempt + 1 >= attempts:
                    break
                retry_after = response.headers.get("retry-after", "")
                try:
                    maximum_delay = 60.0 if template.method == "POST" else 20.0
                    delay = min(max(float(retry_after), 2.0), maximum_delay)
                except ValueError:
                    delay = (
                        min(10.0 * (attempt + 1), 30.0)
                        if template.method == "POST"
                        else min(5.0 * (attempt + 1), 20.0)
                    )
                await asyncio.sleep(delay)
            if response is None:
                raise ModelGatewayError("自定义供应商未返回响应")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ModelGatewayError("自定义供应商返回了无法识别的数据结构")
            for assertion in template.assertions:
                actual = extract_path(payload, assertion.path)
                if actual not in assertion.accepted_values:
                    raise ModelGatewayError(assertion.message)
            return payload, client
        except httpx.HTTPStatusError as exc:
            await client.aclose()
            raise ModelGatewayError(
                _http_error_message(
                    exc.response,
                    prefix="自定义供应商返回",
                )
            ) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise ModelGatewayError("无法连接自定义供应商") from exc
        except ValueError as exc:
            await client.aclose()
            raise ModelGatewayError("自定义供应商返回了无效 JSON") from exc
        except Exception:
            await client.aclose()
            raise

    async def _adapter_video_result(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        mapping: VideoResponseMapping,
    ) -> VideoGenerationResult:
        task_id_value = extract_path(body, mapping.task_id_path)
        # 异步供应商的创建响应和轮询响应经常使用不同的任务 ID 字段。
        # Agnes 创建时返回 video_id，轮询时返回 id；两者都应能恢复同一个任务。
        if task_id_value in (None, ""):
            for fallback_path in ("id", "video_id", "data.id", "data.video_id"):
                task_id_value = extract_path(body, fallback_path)
                if task_id_value not in (None, ""):
                    break
        provider_job_id = str(task_id_value) if task_id_value not in (None, "") else None
        status_value = extract_path(body, mapping.status_path)
        if status_value in (None, ""):
            for fallback_path in ("status", "data.status", "internal_status"):
                status_value = extract_path(body, fallback_path)
                if status_value not in (None, ""):
                    break
        raw_status = str(status_value or "").strip().lower()
        error_value = extract_path(body, mapping.error_path)
        error_message = str(error_value) if error_value not in (None, "") else None
        failed = {item.lower() for item in mapping.failed_values}
        succeeded = {item.lower() for item in mapping.success_values}
        if raw_status in failed:
            return VideoGenerationResult(
                status="failed",
                provider_job_id=provider_job_id,
                error_message=error_message or "视频平台任务失败",
            )
        encoded_value = extract_path(body, mapping.result_base64_path)
        if isinstance(encoded_value, str) and encoded_value:
            encoded = encoded_value.split(",", 1)[-1]
            try:
                return VideoGenerationResult(
                    status="succeeded",
                    provider_job_id=provider_job_id,
                    video_data=_bounded_video(base64.b64decode(encoded, validate=True)),
                    content_type="video/mp4",
                )
            except ValueError as exc:
                raise ModelGatewayError("视频平台返回了无效的 Base64 数据") from exc
        result_url = extract_path(body, mapping.result_url_path)
        if result_url in (None, ""):
            for fallback_path in ("url", "metadata.url", "data.url", "data.video_url"):
                result_url = extract_path(body, fallback_path)
                if result_url not in (None, ""):
                    break
        if isinstance(result_url, str) and result_url:
            await _validate_download_url(result_url, media_name="视频")
            downloaded = await client.get(result_url)
            downloaded.raise_for_status()
            content_type = downloaded.headers.get("content-type", "").split(";", 1)[0]
            if not content_type.startswith("video/"):
                raise ModelGatewayError("视频下载地址未返回视频内容")
            return VideoGenerationResult(
                status="succeeded",
                provider_job_id=provider_job_id,
                video_data=_bounded_video(downloaded.content),
                content_type=content_type,
            )
        if raw_status in succeeded:
            raise ModelGatewayError("视频平台任务完成但未返回视频文件")
        if provider_job_id:
            return VideoGenerationResult(status="pending", provider_job_id=provider_job_id)
        raise ModelGatewayError(error_message or "视频平台未返回视频或可恢复的任务 ID")

    async def _video_request(
        self,
        *,
        method: Literal["GET", "POST"],
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None = None,
    ) -> VideoGenerationResult:
        timeout = httpx.Timeout(get_settings().media_request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, json=payload)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type.startswith("video/"):
                    return VideoGenerationResult(
                        status="succeeded",
                        video_data=_bounded_video(response.content),
                        content_type=content_type,
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise ModelGatewayError("视频平台返回了无法识别的数据结构")
                return await _parse_video_response(client, body)
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                _http_error_message(exc.response, prefix="视频平台返回")
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("无法连接视频模型平台") from exc
        except ValueError as exc:
            raise ModelGatewayError("视频平台返回了无效 JSON") from exc

    async def submit_speech(self, request: SpeechGenerationRequest) -> SpeechGenerationResult:
        endpoint = str(request.capabilities.get("endpoint") or "audio/speech").lstrip("/")
        payload: dict[str, object] = {
            "model": request.model,
            "input": request.text,
            "voice": request.voice,
            "response_format": str(request.capabilities.get("response_format") or "mp3"),
        }
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.style:
            style_parameter = str(request.capabilities.get("style_parameter") or "style")
            payload[style_parameter] = request.style
        overrides = request.capabilities.get("request_overrides")
        if isinstance(overrides, dict):
            payload.update(overrides)
            payload["model"] = request.model
            payload["input"] = request.text
            payload["voice"] = request.voice
        return await self._speech_request(
            method="POST",
            url=f"{self.base_url}/{endpoint}",
            headers={**self.headers, "Idempotency-Key": request.idempotency_key},
            payload=payload,
        )

    async def poll_speech(
        self,
        request: SpeechGenerationRequest,
        provider_job_id: str,
    ) -> SpeechGenerationResult:
        template = str(request.capabilities.get("status_endpoint") or "audio/speech/{job_id}")
        endpoint = template.replace("{job_id}", provider_job_id).lstrip("/")
        result = await self._speech_request(
            method="GET",
            url=f"{self.base_url}/{endpoint}",
            headers=self.headers,
        )
        if result.provider_job_id is None:
            result.provider_job_id = provider_job_id
        return result

    async def _speech_request(
        self,
        *,
        method: Literal["GET", "POST"],
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None = None,
    ) -> SpeechGenerationResult:
        timeout = httpx.Timeout(get_settings().media_request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, json=payload)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type.startswith("audio/") or content_type == "application/octet-stream":
                    return SpeechGenerationResult(
                        status="succeeded",
                        audio_data=_bounded_audio(response.content),
                        content_type=content_type,
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise ModelGatewayError("TTS 平台返回了无法识别的数据结构")
                return await _parse_speech_response(client, body)
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(f"TTS 平台返回 HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("无法连接 TTS 模型平台") from exc
        except ValueError as exc:
            raise ModelGatewayError("TTS 平台返回了无效 JSON") from exc


def _bounded_image(data: bytes) -> bytes:
    if not data:
        raise ModelGatewayError("图片平台返回了空文件")
    if len(data) > MAX_GENERATED_IMAGE_BYTES:
        raise ModelGatewayError("图片平台返回的文件超过 16 MB")
    return data


def _mapped_capability_value(mapping: object, value: str) -> str:
    if isinstance(mapping, dict):
        mapped = mapping.get(value)
        if isinstance(mapped, str) and mapped:
            return mapped
    return value


def _bounded_video(data: bytes) -> bytes:
    if not data:
        raise ModelGatewayError("视频平台返回了空文件")
    if len(data) > MAX_GENERATED_VIDEO_BYTES:
        raise ModelGatewayError("视频平台返回的文件超过 512 MB")
    return data


def _bounded_audio(data: bytes) -> bytes:
    if not data:
        raise ModelGatewayError("TTS 平台返回了空文件")
    if len(data) > MAX_GENERATED_AUDIO_BYTES:
        raise ModelGatewayError("TTS 平台返回的文件超过 64 MB")
    return data


async def _parse_video_response(
    client: httpx.AsyncClient,
    body: dict[str, object],
) -> VideoGenerationResult:
    raw_data = body.get("data")
    item = raw_data[0] if isinstance(raw_data, list) and raw_data else raw_data
    source = item if isinstance(item, dict) else body
    raw_status = str(source.get("status") or body.get("status") or "").lower()
    provider_job_id = _first_string(source, body, keys=("id", "job_id", "task_id"))
    error_message = _first_string(source, body, keys=("error_message", "message", "error"))
    if raw_status in {"failed", "error", "cancelled", "canceled"}:
        return VideoGenerationResult(
            status="failed",
            provider_job_id=provider_job_id,
            error_message=error_message or "视频平台任务失败",
        )

    encoded = _first_string(source, body, keys=("b64_json", "video_base64", "b64"))
    if encoded:
        try:
            return VideoGenerationResult(
                status="succeeded",
                provider_job_id=provider_job_id,
                video_data=_bounded_video(base64.b64decode(encoded, validate=True)),
                content_type="video/mp4",
            )
        except ValueError as exc:
            raise ModelGatewayError("视频平台返回了无效的 Base64 数据") from exc

    video_url = _first_string(source, body, keys=("url", "video_url", "output_url"))
    if video_url:
        await _validate_download_url(video_url, media_name="视频")
        downloaded = await client.get(video_url, follow_redirects=True)
        downloaded.raise_for_status()
        content_type = downloaded.headers.get("content-type", "").split(";", 1)[0]
        if not content_type.startswith("video/"):
            raise ModelGatewayError("视频下载地址未返回视频内容")
        return VideoGenerationResult(
            status="succeeded",
            provider_job_id=provider_job_id,
            video_data=_bounded_video(downloaded.content),
            content_type=content_type,
        )
    if raw_status in {"succeeded", "completed", "success", "done"}:
        raise ModelGatewayError("视频平台任务完成但未返回视频文件")
    if not provider_job_id:
        raise ModelGatewayError("视频平台未返回视频或可恢复的任务 ID")
    return VideoGenerationResult(status="pending", provider_job_id=provider_job_id)


async def _parse_speech_response(
    client: httpx.AsyncClient,
    body: dict[str, object],
) -> SpeechGenerationResult:
    raw_data = body.get("data")
    item = raw_data[0] if isinstance(raw_data, list) and raw_data else raw_data
    source = item if isinstance(item, dict) else body
    raw_status = str(source.get("status") or body.get("status") or "").lower()
    provider_job_id = _first_string(source, body, keys=("id", "job_id", "task_id"))
    error_message = _first_string(source, body, keys=("error_message", "message", "error"))
    duration_value = source.get("duration") or source.get("duration_seconds")
    duration = float(duration_value) if isinstance(duration_value, (int, float)) else None
    if raw_status in {"failed", "error", "cancelled", "canceled"}:
        return SpeechGenerationResult(
            status="failed",
            provider_job_id=provider_job_id,
            error_message=error_message or "TTS 平台任务失败",
        )
    encoded = _first_string(source, body, keys=("b64_json", "audio_base64", "b64"))
    if encoded:
        try:
            return SpeechGenerationResult(
                status="succeeded",
                provider_job_id=provider_job_id,
                audio_data=_bounded_audio(base64.b64decode(encoded, validate=True)),
                content_type="audio/mpeg",
                duration_seconds=duration,
            )
        except ValueError as exc:
            raise ModelGatewayError("TTS 平台返回了无效的 Base64 数据") from exc
    audio_url = _first_string(source, body, keys=("url", "audio_url", "output_url"))
    if audio_url:
        await _validate_download_url(audio_url, media_name="音频")
        downloaded = await client.get(audio_url, follow_redirects=True)
        downloaded.raise_for_status()
        content_type = downloaded.headers.get("content-type", "").split(";", 1)[0]
        if not content_type.startswith("audio/"):
            raise ModelGatewayError("音频下载地址未返回音频内容")
        return SpeechGenerationResult(
            status="succeeded",
            provider_job_id=provider_job_id,
            audio_data=_bounded_audio(downloaded.content),
            content_type=content_type,
            duration_seconds=duration,
        )
    if raw_status in {"succeeded", "completed", "success", "done"}:
        raise ModelGatewayError("TTS 平台任务完成但未返回音频文件")
    if not provider_job_id:
        raise ModelGatewayError("TTS 平台未返回音频或可恢复的任务 ID")
    return SpeechGenerationResult(status="pending", provider_job_id=provider_job_id)


def _first_string(*sources: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _http_error_message(response: httpx.Response, *, prefix: str) -> str:
    """Expose a useful upstream validation error without returning raw response data."""
    detail: object = None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("detail") or error.get("code")
        elif isinstance(error, str):
            detail = error
        detail = detail or payload.get("detail") or payload.get("message")
    if not detail:
        raw_text = str(getattr(response, "text", "") or "").strip()
        if raw_text and len(raw_text) <= 500:
            detail = raw_text
    suffix = f"：{str(detail).strip()}" if detail else ""
    return f"{prefix} HTTP {response.status_code}{suffix}"


def _inject_reference_payloads(
    body: dict[str, Any],
    references: list[dict[str, str]],
    mappings: list[ReferencePayloadMapping],
) -> None:
    for mapping in mappings:
        matching = [item for item in references if item.get("type") == mapping.media_type]
        values: list[str] = []
        for item in matching:
            value = item.get(mapping.source) or item.get("url") or item.get("data_uri") or item.get("base64")
            if value:
                values.append(value)
        if not values:
            continue
        if mapping.strategy == "single":
            body[mapping.field] = values[0]
        elif mapping.strategy == "array":
            body[mapping.field] = values
        else:
            for offset, value in enumerate(values, start=mapping.start_index):
                body[f"{mapping.field}{offset}"] = value


async def _validate_download_url(url: str, *, media_name: str = "图片") -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelGatewayError(f"{media_name}下载地址无效")
    if get_settings().allow_private_media_urls:
        return
    default_port = 443 if parsed.scheme == "https" else 80
    addresses = await run_in_threadpool(socket.getaddrinfo, parsed.hostname, parsed.port or default_port)
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ModelGatewayError(f"{media_name}下载地址指向非公网网络")
