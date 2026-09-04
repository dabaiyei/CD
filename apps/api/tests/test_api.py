from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from ebooklib import epub
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, func, select, update

from app.api.routes import admin as admin_routes
from app.api.routes import auth as auth_routes
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from app.db.models import (
    AIModel,
    AITask,
    Asset,
    AssetRevision,
    AssetStatus,
    AuthLoginGuard,
    Chapter,
    CreditAccount,
    CreditLedger,
    DirectorWorkflowRun,
    InvitationCode,
    InvitationRedemption,
    MarketplaceAcquisition,
    MarketplaceListing,
    ModelType,
    Notification,
    Project,
    Provider,
    ProviderType,
    RefreshSession,
    SecurityEvent,
    TaskStatus,
    Tenant,
    User,
    UserSkill,
    UserTemplate,
)
from app.db.session import SessionLocal
from app.services import task_worker
from app.services.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeRequestError,
    AgentRuntimeResponse,
    raise_for_runtime_status,
)
from app.services.auth_security import login_identity_hash
from app.services.director_orchestration import recover_orphaned_agent_script_reviews
from app.services.image_model_routing import resolve_image_model
from app.services.media_gateway import (
    ImageGenerationRequest,
    SpeechGenerationRequest,
    SpeechGenerationResult,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from app.services.object_storage import object_storage
from app.services.provider_adapters import autodl_minimax_h3_adapter_config
from app.services.task_worker import (
    _safe_error_message,
    claim_task,
    ensure_video_prompt_dialogue_locks,
    ensure_video_prompt_reference_locks,
    fail_task,
    prepare_adapter_reference_media,
    process_next_task,
    process_task,
    recover_stale_tasks,
    signal_task_activity,
)


class FakeImageGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def generate_image(self, request: ImageGenerationRequest) -> bytes:
        assert request.idempotency_key
        assert "项目名称" in request.prompt
        if self.fail:
            raise RuntimeError("simulated upstream failure")
        image = BytesIO()
        Image.new("RGB", (1280, 720), "#12343b").save(image, format="PNG")
        return image.getvalue()


class FakeImageModelTestGateway:
    async def generate_image(self, request: ImageGenerationRequest) -> bytes:
        assert request.model
        assert request.idempotency_key.startswith("model-test-")
        return b"generated-image-test"


def test_autodl_reference_media_is_hydrated_from_project_storage() -> None:
    async def run() -> None:
        key = "test/autodl/reference.webp"
        await object_storage().put_bytes(key, b"webp-reference", "image/webp")
        try:
            provider = Provider(
                tenant_id="tenant-test",
                code="autodl-test",
                name="AutoDL test",
                provider_type=ProviderType.CUSTOM,
                base_url="https://autodl.example.test",
                adapter_config=autodl_minimax_h3_adapter_config(),
                extra_headers={},
                enabled=False,
            )
            prepared = await prepare_adapter_reference_media(
                provider,
                [{"type": "image", "url": f"/uploads/{key}"}],
            )
            assert prepared[0]["data_uri"] == (
                "data:image/webp;base64," + base64.b64encode(b"webp-reference").decode()
            )
        finally:
            await object_storage().delete(key)

    asyncio.run(run())


def test_autodl_reference_media_detects_image_type_without_extension() -> None:
    async def run() -> None:
        key = "test/autodl/reference-without-extension"
        image = BytesIO()
        Image.new("RGB", (96, 96), "#12343b").save(image, format="WEBP")
        data = image.getvalue()
        await object_storage().put_bytes(key, data, "application/octet-stream")
        try:
            provider = Provider(
                tenant_id="tenant-test",
                code="autodl-test",
                name="AutoDL test",
                provider_type=ProviderType.CUSTOM,
                base_url="https://autodl.example.test",
                adapter_config=autodl_minimax_h3_adapter_config(),
                extra_headers={},
                enabled=False,
            )
            prepared = await prepare_adapter_reference_media(
                provider,
                [{"type": "image", "url": f"/uploads/{key}"}],
            )
            assert prepared[0]["data_uri"].startswith("data:image/webp;base64,")
            assert prepared[0]["data_uri"].endswith(base64.b64encode(data).decode())
        finally:
            await object_storage().delete(key)

    asyncio.run(run())


def test_video_prompt_reference_locks_are_inserted_when_model_omits_tokens() -> None:
    prompt = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> "
        "(from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description: The character turns toward the window."
    )
    enriched = ensure_video_prompt_reference_locks(
        prompt,
        [
            {"token": "<Picture 1>", "role": "first_frame", "type": "image", "url": "/uploads/a.webp"},
            {
                "token": "<Picture 2>",
                "role": "asset_reference",
                "type": "image",
                "url": "/uploads/b.webp",
                "asset_names": "阿贝尔",
                "asset_type": "character",
                "asset_description": "黑发少年，旧制服",
            },
        ],
    )
    assert "<Picture 1>" in enriched
    assert "<Picture 2>" in enriched
    assert "Reference locks:" in enriched
    assert "integrated_multimodal_description: Reference locks:" in enriched


def test_video_prompt_dialogue_locks_keep_original_chinese_lines() -> None:
    prompt = (
        "integrated_multimodal_description: Abel slowly looks up and says, "
        "\"Why are you still here?\""
    )
    enriched = ensure_video_prompt_dialogue_locks(prompt, "阿贝尔：你为什么还在这里？")
    assert "Dialogue locks:" in enriched
    assert "<d>[Chinese]阿贝尔：你为什么还在这里？</d>" in enriched
    assert "integrated_multimodal_description: Dialogue locks:" in enriched


class FakeCompositionRenderer:
    def __init__(self) -> None:
        self.manifests: list[dict] = []

    async def render(
        self,
        manifest: dict,
        *,
        output_path: Path,
        resolution: str,
        aspect_ratio: str,
        fps: int,
    ) -> None:
        assert resolution in {"720p", "1080p", "2K", "4K"}
        assert aspect_ratio
        assert fps >= 20
        for shot in manifest.get("shots") or []:
            assert Path(shot["video_storage_path"]).is_file()
            for clip in shot.get("dialogue_clips") or []:
                assert Path(clip["audio_storage_path"]).is_file()
        background = manifest.get("background_music")
        if background:
            assert Path(background["storage_path"]).is_file()
        self.manifests.append(manifest)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"rendered-chapter-video")


class FakeAgentRuntime:
    def __init__(self, project_file_changes: list[dict] | None = None) -> None:
        self.requests: list[AgentRuntimeRequest] = []
        self.project_file_changes = project_file_changes or []

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        is_memory_maintenance = "后台记忆维护器" in request.system_prompt
        final_response = (
            json.dumps(
                {
                    "summary": "项目采用克制的三幕式结构，正在确定主角困境并推进故事骨架。",
                    "memories": [
                        {
                            "namespace": "story",
                            "key": "narrative-structure",
                            "content": "项目采用克制的三幕式叙事结构。",
                            "salience": 0.82,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            if is_memory_maintenance
            else "我已读取项目上下文，先从三幕故事骨架开始。"
        )
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=final_response,
            finish_reason="stop",
            events=[{"type": "assistant/message"}],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "agentscope_version": "test",
                "contract_version": request.contract_version,
                "provider": request.model_binding["provider"],
                "model": request.model_binding["model"],
                "prompt_versions": request.prompt_versions,
                "skill_versions": request.skill_versions,
            },
            project_file_changes=self.project_file_changes,
        )


class FakePersonalMediaRuntime:
    def __init__(self, *, message: str, prompt: str) -> None:
        self.message = message
        self.prompt = prompt
        self.requests: list[AgentRuntimeRequest] = []

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=json.dumps(
                {"message": self.message, "prompt": self.prompt},
                ensure_ascii=False,
            ),
            finish_reason="stop",
            events=[{"type": "assistant/message"}],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "contract_version": request.contract_version,
                "provider": request.model_binding["provider"],
                "model": request.model_binding["model"],
            },
        )


class FakePersonalChatMediaActionRuntime:
    def __init__(self, *, response: str, project_file_changes: list[dict] | None = None) -> None:
        self.response = response
        self.project_file_changes = project_file_changes or []
        self.requests: list[AgentRuntimeRequest] = []

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=self.response,
            finish_reason="stop",
            events=[{"type": "assistant/message"}],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "contract_version": request.contract_version,
                "provider": request.model_binding["provider"],
                "model": request.model_binding["model"],
            },
            project_file_changes=self.project_file_changes,
        )


class FakeStreamingAgentRuntime(FakeAgentRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.stream_calls = 0

    async def run_stream(self, request: AgentRuntimeRequest, on_event) -> AgentRuntimeResponse:
        self.stream_calls += 1
        await on_event({"type": "MODEL_CALL_START"})
        await on_event({"type": "TEXT_BLOCK_DELTA", "delta": "我已读取"})
        await on_event({"type": "TEXT_BLOCK_DELTA", "delta": "项目上下文"})
        await on_event({"type": "TOOL_CALL_START", "tool_call_name": "Read"})
        await on_event({"type": "TOOL_RESULT_END", "state": "completed"})
        await on_event({"type": "TEXT_BLOCK_DELTA", "delta": "，开始整理故事骨架。"})
        return await self.run(request)


class FakeLongArtifactAgentRuntime(FakeAgentRuntime):
    def __init__(self, project_file_changes: list[dict]) -> None:
        super().__init__(project_file_changes)
        self.chat_call_count = 0

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        if "后台记忆维护器" in request.system_prompt:
            return AgentRuntimeResponse(
                session_id=request.session_id,
                final_response='{"summary":"已生成剧本初稿。","memories":[]}',
                finish_reason="stop",
                events=[],
                manifest={},
                project_file_changes=[],
            )
        self.chat_call_count += 1
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=("这是已经落盘的完整剧本正文。" * 500),
            finish_reason="stop",
            events=[],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "contract_version": request.contract_version,
            },
            project_file_changes=self.project_file_changes if self.chat_call_count == 1 else [],
        )


class FakeFailingAgentRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRuntimeRequest] = []

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        raise RuntimeError("simulated agent runtime failure")


class FakeWorkflowRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRuntimeRequest] = []

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        self.requests.append(request)
        if "asset_type 只能是" in request.prompt:
            response = {
                "assets": [
                    {
                        "asset_type": "character",
                        "name": "林遥",
                        "description": "二十八岁的记忆修复师，短发，克制冷静。",
                        "parent_name": None,
                    },
                    {
                        "asset_type": "character",
                        "name": "林遥·雨夜造型",
                        "description": "深色防水风衣，发梢被雨水打湿。",
                        "parent_name": "林遥",
                    },
                    {
                        "asset_type": "scene",
                        "name": "记忆修复室",
                        "description": "玻璃幕墙、冷白设备光和雨夜城市倒影。",
                        "parent_name": None,
                    },
                ]
            }
        elif "结构化章节分析" in request.prompt:
            response = {
                "summary": "林遥在雨夜进入修复室，发现旧相机自行回卷。",
                "core_conflict": "林遥必须查明相机异常与失踪事件的联系。",
                "opening_hook": "无人触碰的旧相机突然自行回卷。",
                "adaptation_strategy": "以前置相机异动开场，压缩环境说明，用动作和台词补足关系。",
                "events": [
                    {
                        "title": "进入修复室",
                        "description": "林遥冒雨进入封闭的记忆修复室。",
                        "dramatic_value": "建立孤立环境与紧张预期。",
                    },
                    {
                        "title": "相机异动",
                        "description": "桌上无人触碰的旧相机开始自动回卷。",
                        "dramatic_value": "形成章节核心悬念。",
                    },
                ],
                "characters": [
                    {
                        "name": "林遥",
                        "role": "记忆修复师",
                        "motivation": "确认异常来源",
                        "relationship": "本章唯一在场人物",
                    }
                ],
                "risks": ["相机异常原因尚未揭示，剧本不得提前给出确定答案。"],
            }
        elif "可直接进入导演审核" in request.prompt:
            response = {
                "title": "第一集 · 雨夜回卷",
                "content": "01 内景 记忆修复室 夜\n林遥推门，旧相机突然开始回卷。\n林遥：谁动过它？",
                "review_notes": "审核相机异动是否保留足够悬念。",
            }
        elif "连续分镜" in request.prompt:
            response = {
                "shots": [
                    {
                        "title": "雨夜推门",
                        "shot_type": "中景",
                        "duration_seconds": 5,
                        "scene_description": "记忆修复室入口，玻璃映出雨夜城市",
                        "action_description": "镜头缓慢推进，林遥推门并停在冷白灯下",
                        "dialogue": "林遥：你为什么还在这里？",
                        "image_prompt": "电影感中景，雨夜玻璃倒影，林遥站在入口",
                        "video_prompt": "缓慢推镜，林遥推门后停步，风衣滴水，灯光稳定",
                        "asset_names": ["林遥", "记忆修复室"],
                    },
                    {
                        "title": "旧相机特写",
                        "shot_type": "特写",
                        "duration_seconds": 4,
                        "scene_description": "修复台表面",
                        "action_description": "镜头下压，手指触碰磨损的快门",
                        "dialogue": "",
                        "image_prompt": "旧相机快门特写，冷白设备光",
                        "video_prompt": "镜头轻微下压，手指触碰快门，浅景深",
                        "asset_names": ["旧相机"],
                    },
                ]
            }
        elif "视频提示词生成任务快照" in request.prompt:
            rows = json.loads(request.prompt.split("镜头数据：\n", 1)[1])
            response = {
                "prompts": [
                    {
                        "shot_order_index": item["shot_order_index"],
                        "mode": (
                            "image_to_video"
                            if item.get("reference_image_url") or item.get("assets")
                            else "text_to_video"
                        ),
                        "prompt": (
                            f"<Picture 1> 保持主体一致，{item['title']}，"
                            "镜头缓慢推进，环境有细微动态，结尾稳定停格"
                        ),
                        "negative_prompt": "",
                        "reference_asset_names": [
                            asset["name"]
                            for asset in item.get("assets") or []
                            if asset.get("has_image")
                        ],
                    }
                    for item in rows
                ]
            }
        elif "演员配音" in request.prompt:
            response = {
                "lines": [
                    {
                        "shot_order_index": None,
                        "speaker": "林遥",
                        "text": "谁动过它？",
                        "emotion": "警惕",
                        "direction": "压低声音，短暂停顿",
                    }
                ]
            }
        else:
            rows = json.loads(request.prompt.split("资产数据：\n", 1)[1])
            response = {
                "assets": [
                    {
                        "asset_id": item["asset_id"],
                        "generation_prompt": f"{item['name']}，电影级设定图，统一角色与材质，纯净背景",
                    }
                    for item in rows
                ]
            }
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=json.dumps(response, ensure_ascii=False),
            finish_reason="stop",
            events=[{"type": "assistant/message"}],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "agentscope_version": "test",
                "contract_version": "v1",
                "provider": request.model_binding["provider"],
                "model": request.model_binding["model"],
                "prompt_versions": request.prompt_versions,
                "skill_versions": request.skill_versions,
            },
        )


class FakeDirectorOrchestrationRuntime(FakeWorkflowRuntime):
    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        if "独立的短剧导演审核子智能体" not in request.prompt:
            return await super().run(request)
        self.requests.append(request)
        response = {
            "approved": True,
            "summary": "剧本事实一致、冲突明确，场次与动作可直接拆解为 AI 视频镜头。",
            "findings": [],
        }
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=json.dumps(response, ensure_ascii=False),
            finish_reason="stop",
            events=[{"type": "assistant/message"}],
            manifest={
                "runtime_type": "agentscope",
                "runtime_version": "test",
                "agentscope_version": "test",
                "contract_version": request.contract_version,
                "provider": request.model_binding["provider"],
                "model": request.model_binding["model"],
                "prompt_versions": request.prompt_versions,
                "skill_versions": request.skill_versions,
            },
        )


class FakeAssetImageGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[ImageGenerationRequest] = []

    async def generate_image(self, request: ImageGenerationRequest) -> bytes:
        self.requests.append(request)
        assert request.idempotency_key
        assert request.prompt
        if self.fail:
            raise RuntimeError("simulated asset image failure")
        image = BytesIO()
        Image.new("RGB", (1024, 1024), "#315b5a").save(image, format="PNG")
        return image.getvalue()


class FakeVideoGateway:
    def __init__(
        self,
        *,
        fail: bool = False,
        pending: bool = False,
        poll_error: bool = False,
    ) -> None:
        self.fail = fail
        self.pending = pending
        self.poll_error = poll_error
        self.submit_calls = 0
        self.poll_calls = 0
        self.requests: list[VideoGenerationRequest] = []

    async def submit_video(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        self.submit_calls += 1
        self.requests.append(request)
        assert request.idempotency_key
        assert request.prompt
        if self.fail:
            return VideoGenerationResult(
                status="failed",
                provider_job_id="failed-video-job",
                error_message="simulated video failure",
            )
        if self.pending:
            return VideoGenerationResult(status="pending", provider_job_id="video-job-001")
        return VideoGenerationResult(
            status="succeeded",
            video_data=b"\x00\x00\x00\x18ftypmp42test-video",
            content_type="video/mp4",
        )

    async def poll_video(
        self,
        request: VideoGenerationRequest,
        provider_job_id: str,
    ) -> VideoGenerationResult:
        self.poll_calls += 1
        assert request.model
        assert provider_job_id
        if self.poll_error:
            raise RuntimeError("simulated polling transport failure")
        return VideoGenerationResult(
            status="succeeded",
            provider_job_id=provider_job_id,
            video_data=b"\x00\x00\x00\x18ftypmp42test-video",
            content_type="video/mp4",
        )


class FakeTTSGateway:
    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.submit_calls = 0
        self.poll_calls = 0

    async def submit_speech(self, request: SpeechGenerationRequest) -> SpeechGenerationResult:
        self.submit_calls += 1
        assert request.idempotency_key
        assert request.text == "谁动过它？"
        assert request.voice == "voice-linyao"
        if self.pending:
            return SpeechGenerationResult(status="pending", provider_job_id="tts-job-001")
        return SpeechGenerationResult(
            status="succeeded",
            audio_data=b"ID3test-audio",
            content_type="audio/mpeg",
            duration_seconds=1.8,
        )

    async def poll_speech(
        self,
        request: SpeechGenerationRequest,
        provider_job_id: str,
    ) -> SpeechGenerationResult:
        self.poll_calls += 1
        assert request.model
        assert provider_job_id
        return SpeechGenerationResult(
            status="succeeded",
            provider_job_id=provider_job_id,
            audio_data=b"ID3restored-audio",
            content_type="audio/mpeg",
            duration_seconds=2.1,
        )


class FakeCatalogResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {"id": "gpt-5.2", "owned_by": "openai"},
                {"id": "flux-image-pro", "name": "Flux Image Pro", "owned_by": "black-forest-labs"},
                {"id": "sora-video-2", "owned_by": "openai"},
            ]
        }


class FakeCatalogClient:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def get(self, url: str, **_kwargs) -> FakeCatalogResponse:
        assert url.endswith("/models")
        return FakeCatalogResponse()


def test_health_and_login(client: TestClient, creator_headers: dict[str, str]) -> None:
    assert client.get("/health").json()["status"] == "ok"
    response = client.get("/api/v1/auth/me", headers=creator_headers)
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "user"
    assert Decimal(response.json()["credit_balance"]) == Decimal("1280.00")


def test_platform_login_background_can_be_configured_by_admin(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    default_branding = client.get("/api/v1/public/branding")
    assert default_branding.status_code == 200
    assert default_branding.json()["login_background_video_source"] == "default"
    assert default_branding.json()["login_background_video_url"] == "/videos/login-background.mp4"

    forbidden = client.put(
        "/api/v1/admin/branding/login-background/url",
        headers=creator_headers,
        json={"url": "https://media.example.com/login.mp4"},
    )
    assert forbidden.status_code == 403

    external_url = "https://media.example.com/path/login%20background.mp4?version=2"
    configured = client.put(
        "/api/v1/admin/branding/login-background/url",
        headers=admin_headers,
        json={"url": external_url},
    )
    assert configured.status_code == 200
    assert configured.json()["login_background_video_source"] == "url"
    assert configured.json()["login_background_video_url"] == external_url
    assert client.get("/api/v1/public/branding").json()["login_background_video_url"] == external_url

    invalid_url = client.put(
        "/api/v1/admin/branding/login-background/url",
        headers=admin_headers,
        json={"url": "file:///private/login.mp4"},
    )
    assert invalid_url.status_code == 422

    invalid_video = client.post(
        "/api/v1/admin/branding/login-background/upload",
        headers=admin_headers,
        files={"file": ("fake.mp4", b"not-a-video", "video/mp4")},
    )
    assert invalid_video.status_code == 422

    first_upload = client.post(
        "/api/v1/admin/branding/login-background/upload",
        headers=admin_headers,
        files={"file": ("login.mp4", b"\x00\x00\x00\x18ftypisomtest-video", "video/mp4")},
    )
    assert first_upload.status_code == 201
    assert first_upload.json()["login_background_video_source"] == "upload"
    first_media_url = first_upload.json()["login_background_video_url"]
    assert client.get(first_media_url).status_code == 200

    second_upload = client.post(
        "/api/v1/admin/branding/login-background/upload",
        headers=admin_headers,
        files={"file": ("login.webm", b"\x1aE\xdf\xa3test-video", "video/webm")},
    )
    assert second_upload.status_code == 201
    second_media_url = second_upload.json()["login_background_video_url"]
    assert second_media_url.endswith(".webm")
    assert client.get(first_media_url).status_code == 404
    assert client.get(second_media_url).status_code == 200

    reset = client.delete(
        "/api/v1/admin/branding/login-background",
        headers=admin_headers,
    )
    assert reset.status_code == 200
    assert reset.json()["login_background_video_source"] == "default"
    assert client.get(second_media_url).status_code == 404


def test_user_avatar_upload_replace_and_delete(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    first_image = BytesIO()
    Image.new("RGB", (960, 640), "#176b62").save(first_image, format="PNG")
    uploaded = client.put(
        "/api/v1/auth/me/avatar",
        headers=creator_headers,
        files={"file": ("portrait.png", first_image.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["avatar_url"]

    session_user = client.get("/api/v1/auth/me", headers=creator_headers)
    assert session_user.status_code == 200
    assert session_user.json()["user"]["avatar_url"] == uploaded.json()["avatar_url"]

    async def avatar_record() -> tuple[str | None, str | None]:
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == "creator@cineforge.local"))
            assert user is not None
            return user.avatar_url, user.avatar_storage_path

    first_url, first_storage_path = asyncio.run(avatar_record())
    assert first_url == uploaded.json()["avatar_url"]
    assert first_storage_path
    first_path = get_settings().uploads_root / first_storage_path
    assert first_path.is_file()
    with Image.open(first_path) as stored:
        assert stored.format == "WEBP"
        assert stored.size == (512, 512)

    second_image = BytesIO()
    Image.new("RGB", (700, 1100), "#a94835").save(second_image, format="JPEG")
    replaced = client.put(
        "/api/v1/auth/me/avatar",
        headers=creator_headers,
        files={"file": ("portrait.jpg", second_image.getvalue(), "image/jpeg")},
    )
    assert replaced.status_code == 200
    assert replaced.json()["avatar_url"] != first_url
    assert not first_path.exists()

    _, second_storage_path = asyncio.run(avatar_record())
    assert second_storage_path
    second_path = get_settings().uploads_root / second_storage_path
    assert second_path.is_file()

    deleted = client.delete("/api/v1/auth/me/avatar", headers=creator_headers)
    assert deleted.status_code == 200
    assert deleted.json()["avatar_url"] is None
    assert not second_path.exists()
    assert asyncio.run(avatar_record()) == (None, None)


def test_user_avatar_rejects_invalid_images(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    unsupported = client.put(
        "/api/v1/auth/me/avatar",
        headers=creator_headers,
        files={"file": ("avatar.gif", b"GIF89a", "image/gif")},
    )
    assert unsupported.status_code == 415

    damaged = client.put(
        "/api/v1/auth/me/avatar",
        headers=creator_headers,
        files={"file": ("avatar.png", b"not-a-real-png", "image/png")},
    )
    assert damaged.status_code == 422
    assert "无效或已损坏" in damaged.json()["detail"]


def test_login_resolves_tenant_from_unique_email(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "creator@cineforge.local", "password": "Creator123!"},
    )
    assert response.status_code == 200
    session = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert session.status_code == 200
    assert session.json()["tenant_slug"] == "demo"
    assert session.json()["user"]["role"] == "user"

    admin = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@cineforge.local", "password": "Admin123!"},
    )
    assert admin.status_code == 200
    admin_session = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {admin.json()['access_token']}"},
    )
    assert admin_session.json()["user"]["role"] == "admin"


def test_login_rejects_ambiguous_cross_tenant_email(client: TestClient) -> None:
    email = "shared-login@cineforge.local"

    async def create_duplicate_accounts() -> tuple[str, str, str, str]:
        async with SessionLocal() as session:
            first_tenant = Tenant(name="第一测试租户", slug="login-first")
            second_tenant = Tenant(name="第二测试租户", slug="login-second")
            session.add_all([first_tenant, second_tenant])
            await session.flush()
            first_user = User(
                tenant_id=first_tenant.id,
                email=email,
                display_name="测试用户一",
                password_hash=hash_password("SharedLogin123!"),
            )
            second_user = User(
                tenant_id=second_tenant.id,
                email=email,
                display_name="测试用户二",
                password_hash=hash_password("SharedLogin123!"),
            )
            session.add_all([first_user, second_user])
            await session.commit()
            return first_tenant.id, second_tenant.id, first_user.id, second_user.id

    async def remove_duplicate_accounts(ids: tuple[str, str, str, str]) -> None:
        first_tenant_id, second_tenant_id, first_user_id, second_user_id = ids
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.id.in_([first_user_id, second_user_id])))
            await session.execute(
                delete(Tenant).where(Tenant.id.in_([first_tenant_id, second_tenant_id]))
            )
            await session.commit()

    ids = asyncio.run(create_duplicate_accounts())
    try:
        ambiguous = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "SharedLogin123!"},
        )


        assert ambiguous.status_code == 409
        assert ambiguous.json()["detail"] == "该邮箱关联了多个租户，请联系管理员处理账号归属"

        legacy = client.post(
            "/api/v1/auth/login",
            json={
                "tenant": "login-first",
                "email": email,
                "password": "SharedLogin123!",
            },
        )
        assert legacy.status_code == 200
    finally:
        asyncio.run(remove_duplicate_accounts(ids))


def test_refresh_token_rotation_replay_and_logout(client: TestClient) -> None:
    login_response = client.post(
        "/api/v1/auth/login",
        json={"tenant": "demo", "email": "creator@cineforge.local", "password": "Creator123!"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["expires_in"] == 15 * 60
    old_refresh = login_response.cookies["cineforge_refresh"]
    assert decode_access_token(login_response.json()["access_token"])["type"] == "access"
    assert decode_refresh_token(old_refresh)["type"] == "refresh"

    rotated = client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200
    new_refresh = rotated.cookies["cineforge_refresh"]
    assert new_refresh != old_refresh

    client.cookies.set("cineforge_refresh", old_refresh, path="/api/v1/auth")
    replay = client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    client.cookies.set("cineforge_refresh", new_refresh, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_login_failures_lock_identity_and_are_visible_in_tenant_audit(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def no_redis_limit(_client_ip: str) -> None:
        return None

    monkeypatch.setattr(auth_routes, "consume_login_rate_limit", no_redis_limit)
    payload = {
        "tenant": "demo",
        "email": "creator@cineforge.local",
        "password": "definitely-wrong",
    }
    for _ in range(4):
        response = client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401

    locked = client.post("/api/v1/auth/login", json=payload)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    valid_but_locked = client.post(
        "/api/v1/auth/login",
        json={**payload, "password": "Creator123!"},
    )
    assert valid_but_locked.status_code == 429

    events = client.get("/api/v1/admin/security-events", headers=admin_headers)
    assert events.status_code == 200
    matching = [item for item in events.json()["items"] if item["event_type"].startswith("login_")]
    assert any(item["event_type"] == "login_failed" for item in matching)
    assert any(item["event_type"] == "login_locked" for item in matching)
    assert all("creator@cineforge.local" not in json.dumps(item) for item in matching)

    failed_events = client.get(
        "/api/v1/admin/security-events",
        headers=admin_headers,
        params={"event_type": "login_failed", "success": "false"},
    )
    assert failed_events.status_code == 200
    assert failed_events.json()["items"]
    assert all(item["event_type"] == "login_failed" for item in failed_events.json()["items"])
    assert all(item["success"] is False for item in failed_events.json()["items"])

    first_page = client.get(
        "/api/v1/admin/security-events", headers=admin_headers, params={"limit": 1}
    ).json()
    assert first_page["next_before"] is not None
    assert first_page["next_before_id"] == first_page["items"][0]["id"]
    second_page = client.get(
        "/api/v1/admin/security-events",
        headers=admin_headers,
        params={
            "limit": 1,
            "before": first_page["next_before"],
            "before_id": first_page["next_before_id"],
        },
    ).json()
    assert second_page["items"][0]["id"] != first_page["items"][0]["id"]

    async def clear_guard() -> None:
        async with SessionLocal() as session:
            await session.execute(
                delete(AuthLoginGuard).where(
                    AuthLoginGuard.identity_hash == login_identity_hash("demo", "creator@cineforge.local")
                )
            )
            await session.commit()

    asyncio.run(clear_guard())

    fresh_login = client.post(
        "/api/v1/auth/login",
        json={"tenant": "demo", "email": "creator@cineforge.local", "password": "Creator123!"},
    )
    refresh_token = fresh_login.cookies["cineforge_refresh"]
    denied = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )
    assert denied.status_code == 401

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_projects_are_available_to_creator(client: TestClient, creator_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/projects", headers=creator_headers)
    assert response.status_code == 200
    assert len(response.json()) == 3
    assert {item["name"] for item in response.json()} == {"雾港来信", "长安夜行录", "第二次告别"}


def test_project_requires_complete_production_configuration(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    incomplete = client.post(
        "/api/v1/projects",
        headers=creator_headers,
        json={"name": "缺少生产配置的项目"},
    )
    assert incomplete.status_code == 422

    options = client.get("/api/v1/projects/options", headers=creator_headers).json()
    created = client.post(
        "/api/v1/projects",
        headers=creator_headers,
        json={
            "name": "完整生产配置项目",
            "video_model_id": options["video_models"][0]["id"],
            "visual_handbook_id": options["visual_handbooks"][0]["id"],
            "director_handbook_id": options["director_handbooks"][0]["id"],
        },
    )
    assert created.status_code == 201
    assert created.json()["image_model_id"] is None
    assert created.json()["visual_handbook_id"] == options["visual_handbooks"][0]["id"]

    legacy_image_model_id = next(
        item["image_model_id"]
        for item in client.get("/api/v1/projects", headers=creator_headers).json()
        if item["image_model_id"]
    )
    override = client.patch(
        f"/api/v1/projects/{created.json()['id']}",
        headers=creator_headers,
        json={"image_model_id": legacy_image_model_id},
    )
    assert override.status_code == 422

    unset = client.patch(
        f"/api/v1/projects/{created.json()['id']}",
        headers=creator_headers,
        json={"director_handbook_id": None},
    )
    assert unset.status_code == 422
    assert unset.json()["detail"] == "director_handbook_id 是项目必填配置"
    assert (
        client.delete(
            f"/api/v1/projects/{created.json()['id']}", headers=creator_headers
        ).status_code
        == 204
    )


def test_creator_can_delete_project_and_its_media(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    options = client.get("/api/v1/projects/options", headers=creator_headers).json()
    created = client.post(
        "/api/v1/projects",
        headers=creator_headers,
        json={
            "name": "待删除项目",
            "video_model_id": options["video_models"][0]["id"],
            "visual_handbook_id": options["visual_handbooks"][0]["id"],
            "director_handbook_id": options["director_handbooks"][0]["id"],
        },
    )
    project_id = created.json()["id"]
    image = BytesIO()
    Image.new("RGB", (640, 360), "#315b62").save(image, format="PNG")
    cover = client.post(
        f"/api/v1/projects/{project_id}/cover/upload",
        headers=creator_headers,
        files={"file": ("delete-project.png", image.getvalue(), "image/png")},
    ).json()["cover_url"]
    assert client.get(cover).status_code == 200

    deleted = client.delete(f"/api/v1/projects/{project_id}", headers=creator_headers)

    assert deleted.status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}", headers=creator_headers).status_code == 404
    assert all(
        item["id"] != project_id
        for item in client.get("/api/v1/projects", headers=creator_headers).json()
    )
    assert client.get(cover).status_code == 404


def test_project_delete_requires_active_tasks_to_stop(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    options = client.get("/api/v1/projects/options", headers=creator_headers).json()
    created = client.post(
        "/api/v1/projects",
        headers=creator_headers,
        json={
            "name": "活动任务删除保护",
            "video_model_id": options["video_models"][0]["id"],
            "visual_handbook_id": options["visual_handbooks"][0]["id"],
            "director_handbook_id": options["director_handbooks"][0]["id"],
        },
    ).json()
    task = client.post(
        f"/api/v1/projects/{created['id']}/cover/generate",
        headers=creator_headers,
        json={},
    ).json()

    blocked = client.delete(f"/api/v1/projects/{created['id']}", headers=creator_headers)
    assert blocked.status_code == 409
    assert "先停止任务" in blocked.json()["detail"]
    assert client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=creator_headers).status_code == 200
    assert client.delete(f"/api/v1/projects/{created['id']}", headers=creator_headers).status_code == 204


def test_handbook_publish_state_protects_project_bindings(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    projects = client.get("/api/v1/projects", headers=creator_headers).json()
    referenced_id = next(
        project["visual_handbook_id"] for project in projects if project["visual_handbook_id"] is not None
    )
    handbooks = client.get("/api/v1/admin/handbooks", headers=admin_headers).json()
    referenced = next(handbook for handbook in handbooks if handbook["id"] == referenced_id)
    blocked = client.patch(
        f"/api/v1/admin/handbooks/{referenced['id']}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert blocked.status_code == 409
    assert "项目使用" in blocked.json()["detail"]

    created = client.post(
        "/api/v1/admin/handbooks",
        headers=admin_headers,
        json={
            "handbook_type": "visual",
            "name": "未发布视觉测试手册",
            "description": "用于验证未绑定手册可以安全停用。",
            "skill_path": "visual/noir-cinematic",
        },
    )
    assert created.status_code == 201
    disabled = client.patch(
        f"/api/v1/admin/handbooks/{created.json()['id']}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


def test_managed_handbook_packages_are_fixed_and_non_empty(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    handbooks = client.get("/api/v1/admin/handbooks", headers=admin_headers).json()
    visual = next(item for item in handbooks if item["handbook_type"] == "visual")
    director = next(item for item in handbooks if item["handbook_type"] == "director")

    visual_package = client.get(
        f"/api/v1/admin/handbooks/{visual['id']}/package",
        headers=admin_headers,
    )
    director_package = client.get(
        f"/api/v1/admin/handbooks/{director['id']}/package",
        headers=admin_headers,
    )
    assert visual_package.status_code == 200
    assert director_package.status_code == 200
    assert len(visual_package.json()["files"]) == 12
    assert len(director_package.json()["files"]) == 3
    assert all(item["content"].strip() for item in visual_package.json()["files"])

    files = {
        item["filename"]: item["content"] for item in visual_package.json()["files"]
    }
    files["README.md"] = "  "
    rejected = client.put(
        f"/api/v1/admin/handbooks/{visual['id']}/package",
        headers=admin_headers,
        json={
            "name": visual["name"],
            "description": visual["description"],
            "enabled": visual["enabled"],
            "files": files,
        },
    )
    assert rejected.status_code == 422
    assert "不能为空" in rejected.json()["detail"]

    invalid_files = {
        item["filename"]: item["content"] for item in visual_package.json()["files"]
    }
    invalid_files["README.md"] += "\n\n请读取 `director_planning_narrative.md`。"
    invalid_reference = client.put(
        f"/api/v1/admin/handbooks/{visual['id']}/package",
        headers=admin_headers,
        json={
            "name": visual["name"],
            "description": visual["description"],
            "enabled": visual["enabled"],
            "files": invalid_files,
        },
    )
    assert invalid_reference.status_code == 422
    assert "不存在的固定文件" in invalid_reference.json()["detail"]


def test_system_prompts_are_fixed_and_visible_in_skills(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    prompts = client.get("/api/v1/admin/prompts", headers=admin_headers).json()
    codes = {item["code"] for item in prompts}
    assert {
        "event-extraction",
        "script-review",
        "script-repair",
        "storyboard-review",
        "storyboard-repair",
        "voice-binding",
    }.issubset(codes)

    forbidden_create = client.post(
        "/api/v1/admin/prompts",
        headers=admin_headers,
        json={"code": "custom", "name": "自定义", "content": "不应创建"},
    )
    assert forbidden_create.status_code == 405

    tree = client.get("/api/v1/admin/skills/tree", headers=admin_headers)
    assert tree.status_code == 200
    assert {item["name"] for item in tree.json()} == {
        "visual-handbooks",
        "director-handbooks",
        "system-prompts",
    }

    prompt = next(item for item in prompts if item["code"] == "event-extraction")
    updated_content = "# 事件提取\n\n按因果顺序输出结构化事件。"
    updated = client.put(
        f"/api/v1/admin/prompts/{prompt['id']}",
        headers=admin_headers,
        json={"content": updated_content},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == prompt["version"] + 1
    skill_file = client.get(
        "/api/v1/admin/skills/file",
        headers=admin_headers,
        params={"path": "system-prompts/event-extraction.md"},
    )
    assert skill_file.json()["content"].strip() == updated_content

    changed_through_skills = client.put(
        "/api/v1/admin/skills/file",
        headers=admin_headers,
        params={"path": "system-prompts/event-extraction.md"},
        json={"content": "# 事件提取\n\n保留来源证据。"},
    )
    assert changed_through_skills.status_code == 200
    refreshed = client.get("/api/v1/admin/prompts", headers=admin_headers).json()
    refreshed_prompt = next(item for item in refreshed if item["id"] == prompt["id"])
    assert refreshed_prompt["content"] == "# 事件提取\n\n保留来源证据。"

    blank = client.put(
        "/api/v1/admin/skills/file",
        headers=admin_headers,
        params={"path": "system-prompts/event-extraction.md"},
        json={"content": "  "},
    )
    assert blank.status_code == 422

def test_user_cannot_open_admin_api(client: TestClient, creator_headers: dict[str, str]) -> None:
    for path in (
        "/api/v1/admin/providers",
        "/api/v1/admin/models",
        "/api/v1/admin/readiness",
        "/api/v1/admin/users",
        "/api/v1/admin/invitations",
    ):
        response = client.get(path, headers=creator_headers)
        assert response.status_code == 403


def test_admin_can_manage_users_and_credit_ledger(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    email = "managed-user@cineforge.local"
    created_user_id = ""

    async def cleanup() -> None:
        if not created_user_id:
            return
        async with SessionLocal() as session:
            subject_hash = hashlib.sha256(f"user:{created_user_id}".encode()).hexdigest()
            await session.execute(delete(SecurityEvent).where(SecurityEvent.subject_hash == subject_hash))
            await session.execute(delete(Notification).where(Notification.user_id == created_user_id))
            await session.execute(delete(RefreshSession).where(RefreshSession.user_id == created_user_id))
            await session.execute(delete(CreditLedger).where(CreditLedger.user_id == created_user_id))
            await session.execute(delete(CreditAccount).where(CreditAccount.user_id == created_user_id))
            await session.execute(delete(User).where(User.id == created_user_id))
            await session.commit()

    try:
        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": email,
                "display_name": "待管理用户",
                "password": "Managed123!",
                "role": "user",
                "initial_credits": "25.50",
            },
        )
        assert created.status_code == 201
        created_user_id = created.json()["id"]
        assert created.json()["credit_balance"] == "25.50"

        listed = client.get(
            "/api/v1/admin/users",
            headers=admin_headers,
            params={"search": "managed-user", "role": "user", "is_active": True},
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [created_user_id]
        assert listed.json()["total"] == 1

        updated = client.patch(
            f"/api/v1/admin/users/{created_user_id}",
            headers=admin_headers,
            json={"display_name": "已更新用户", "is_active": False},
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "已更新用户"
        assert updated.json()["is_active"] is False

        reactivated = client.patch(
            f"/api/v1/admin/users/{created_user_id}",
            headers=admin_headers,
            json={"is_active": True},
        )
        assert reactivated.status_code == 200
        assert reactivated.json()["is_active"] is True

        password_reset = client.post(
            f"/api/v1/admin/users/{created_user_id}/reset-password",
            headers=admin_headers,
            json={"password": "Updated123!"},
        )
        assert password_reset.status_code == 204

        async def password_is_updated() -> bool:
            async with SessionLocal() as session:
                user = await session.get(User, created_user_id)
                assert user is not None
                return verify_password("Updated123!", user.password_hash)

        assert asyncio.run(password_is_updated()) is True

        credited = client.post(
            f"/api/v1/admin/users/{created_user_id}/credits/adjust",
            headers=admin_headers,
            json={"amount": "10.00", "reason": "活动奖励"},
        )
        assert credited.status_code == 200
        assert credited.json()["user"]["credit_balance"] == "35.50"
        assert credited.json()["ledger"]["amount"] == "10.00"

        debited = client.post(
            f"/api/v1/admin/users/{created_user_id}/credits/adjust",
            headers=admin_headers,
            json={"amount": "-5.00", "reason": "人工冲正"},
        )
        assert debited.status_code == 200
        assert debited.json()["user"]["credit_balance"] == "30.50"

        overdrawn = client.post(
            f"/api/v1/admin/users/{created_user_id}/credits/adjust",
            headers=admin_headers,
            json={"amount": "-100.00", "reason": "错误扣减"},
        )
        assert overdrawn.status_code == 409

        ledger = client.get(
            f"/api/v1/admin/users/{created_user_id}/credit-ledger",
            headers=admin_headers,
        )
        assert ledger.status_code == 200
        assert [item["reason"] for item in ledger.json()["items"][:3]] == [
            "人工冲正",
            "活动奖励",
            "管理员开户发放积分",
        ]

        async def audit_and_notification_counts() -> tuple[int, int]:
            async with SessionLocal() as session:
                subject_hash = hashlib.sha256(f"user:{created_user_id}".encode()).hexdigest()
                audit_rows = list(
                    (
                        await session.scalars(
                            select(SecurityEvent.id).where(SecurityEvent.subject_hash == subject_hash)
                        )
                    ).all()
                )
                notification_rows = list(
                    (
                        await session.scalars(
                            select(Notification.id).where(Notification.user_id == created_user_id)
                        )
                    ).all()
                )
                return len(audit_rows), len(notification_rows)

        assert asyncio.run(audit_and_notification_counts()) == (6, 2)

        admin_id = client.get("/api/v1/auth/me", headers=admin_headers).json()["user"]["id"]
        self_deactivate = client.patch(
            f"/api/v1/admin/users/{admin_id}",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert self_deactivate.status_code == 409
        self_demote = client.patch(
            f"/api/v1/admin/users/{admin_id}",
            headers=admin_headers,
            json={"role": "user"},
        )
        assert self_demote.status_code == 409
    finally:
        asyncio.run(cleanup())


def test_resource_marketplaces_publish_copy_sync_and_unpublish(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    source_skill_id = ""
    source_template_id = ""
    source_asset_id = ""
    listing_ids: list[str] = []
    target_ids: list[str] = []
    consumer_tenant_id = ""
    consumer_user_id = ""

    async def create_cross_tenant_consumer() -> tuple[str, str]:
        async with SessionLocal() as session:
            tenant = Tenant(name="广场消费测试租户", slug="marketplace-consumer")
            session.add(tenant)
            await session.flush()
            user = User(
                tenant_id=tenant.id,
                email="marketplace-consumer@cineforge.local",
                display_name="跨租户广场用户",
                password_hash=hash_password("Marketplace123!"),
            )
            session.add(user)
            await session.commit()
            return tenant.id, user.id

    async def cleanup() -> None:
        async with SessionLocal() as session:
            all_asset_ids = [source_asset_id, *target_ids]
            existing_asset_ids = list(
                (
                    await session.scalars(
                        select(Asset.id).where(Asset.id.in_([item for item in all_asset_ids if item]))
                    )
                ).all()
            )
            if existing_asset_ids:
                await session.execute(
                    delete(AssetRevision).where(AssetRevision.asset_id.in_(existing_asset_ids))
                )
                await session.execute(delete(Asset).where(Asset.id.in_(existing_asset_ids)))
            if listing_ids:
                await session.execute(
                    delete(MarketplaceAcquisition).where(
                        MarketplaceAcquisition.listing_id.in_(listing_ids)
                    )
                )
                await session.execute(
                    delete(MarketplaceListing).where(MarketplaceListing.id.in_(listing_ids))
                )
            skill_ids = [source_skill_id, *target_ids]
            await session.execute(
                delete(UserSkill).where(UserSkill.id.in_([item for item in skill_ids if item]))
            )
            template_ids = [source_template_id, *target_ids]
            await session.execute(
                delete(UserTemplate).where(
                    UserTemplate.id.in_([item for item in template_ids if item])
                )
            )
            if consumer_user_id:
                await session.execute(delete(User).where(User.id == consumer_user_id))
            if consumer_tenant_id:
                await session.execute(delete(Tenant).where(Tenant.id == consumer_tenant_id))
            await session.commit()

    consumer_tenant_id, consumer_user_id = asyncio.run(create_cross_tenant_consumer())
    consumer_token = create_access_token(
        user_id=consumer_user_id,
        tenant_id=consumer_tenant_id,
        role="user",
    )
    consumer_headers = {
        "Authorization": f"Bearer {consumer_token}"
    }

    try:
        source_skill = client.post(
            "/api/v1/user-skills",
            headers=creator_headers,
            json={
                "name": "动作分镜节奏",
                "description": "近身动作先建立空间轴线，再按动作落点切换景别。",
                "trigger_stages": ["storyboard_generation", "video_generation"],
                "enabled": True,
            },
        )
        assert source_skill.status_code == 201
        source_skill_id = source_skill.json()["id"]
        published_skill = client.post(
            f"/api/v1/marketplace/skills/{source_skill_id}/publish",
            headers=creator_headers,
            json={"category": "动作设计", "tags": ["打斗", "分镜"]},
        )
        assert published_skill.status_code == 200
        skill_listing_id = published_skill.json()["id"]
        listing_ids.append(skill_listing_id)

        skill_feed = client.get(
            "/api/v1/marketplace/skill",
            headers=consumer_headers,
            params={"search": "动作分镜"},
        )
        assert skill_feed.status_code == 200
        skill_card = next(item for item in skill_feed.json()["items"] if item["id"] == skill_listing_id)
        assert skill_card["publisher_name"]
        assert skill_card["acquired"] is False
        assert skill_card["payload"]["trigger_stages"] == [
            "storyboard_generation",
            "video_generation",
        ]

        copied_skill = client.post(
            f"/api/v1/marketplace/listings/{skill_listing_id}/acquire",
            headers=consumer_headers,
        )
        assert copied_skill.status_code == 200
        copied_skill_id = copied_skill.json()["target_id"]
        target_ids.append(copied_skill_id)
        admin_skills = client.get("/api/v1/user-skills", headers=consumer_headers).json()
        copied_skill_row = next(item for item in admin_skills if item["id"] == copied_skill_id)
        assert "空间轴线" in copied_skill_row["description"]

        assert client.patch(
            f"/api/v1/user-skills/{source_skill_id}",
            headers=creator_headers,
            json={"description": "新版：先校验轴线，再生成动作拆解和镜头落点。"},
        ).status_code == 200
        republished_skill = client.post(
            f"/api/v1/marketplace/skills/{source_skill_id}/publish",
            headers=creator_headers,
            json={"category": "动作设计", "tags": ["打斗", "轴线"]},
        )
        assert republished_skill.status_code == 200
        updated_feed = client.get(
            "/api/v1/marketplace/skill",
            headers=consumer_headers,
            params={"search": "动作分镜"},
        ).json()
        assert next(item for item in updated_feed["items"] if item["id"] == skill_listing_id)[
            "has_update"
        ] is True
        synced_skill = client.post(
            f"/api/v1/marketplace/listings/{skill_listing_id}/acquire",
            headers=consumer_headers,
        )
        assert synced_skill.status_code == 200
        assert synced_skill.json()["target_id"] == copied_skill_id
        synced_admin_skill = next(
            item
            for item in client.get("/api/v1/user-skills", headers=consumer_headers).json()
            if item["id"] == copied_skill_id
        )
        assert synced_admin_skill["description"].startswith("新版")

        source_template = client.post(
            "/api/v1/user-templates",
            headers=creator_headers,
            json={
                "name": "悬疑开场结构",
                "description": "用结果前置与信息差快速建立钩子。",
                "category": "剧本结构",
                "content": "第一拍展示异常结果；第二拍回到事件发生前；第三拍埋入误导线索。",
            },
        )
        assert source_template.status_code == 201
        source_template_id = source_template.json()["id"]
        published_template = client.post(
            f"/api/v1/marketplace/templates/{source_template_id}/publish",
            headers=creator_headers,
            json={"category": "剧本结构", "tags": ["悬疑", "开场"]},
        )
        assert published_template.status_code == 200
        template_listing_id = published_template.json()["id"]
        listing_ids.append(template_listing_id)
        copied_template = client.post(
            f"/api/v1/marketplace/listings/{template_listing_id}/acquire",
            headers=consumer_headers,
        )
        assert copied_template.status_code == 200
        target_ids.append(copied_template.json()["target_id"])
        admin_templates = client.get("/api/v1/user-templates", headers=consumer_headers).json()
        assert any("异常结果" in item["content"] for item in admin_templates)

        project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
        source_asset = client.post(
            f"/api/v1/projects/{project_id}/assets",
            headers=creator_headers,
            json={
                "asset_type": "character",
                "name": "雨夜侦探",
                "description": "深灰风衣，克制且警觉。",
                "generation_prompt": "雨夜街灯下的东亚青年侦探，深灰风衣。",
            },
        )
        assert source_asset.status_code == 201
        source_asset_id = source_asset.json()["id"]
        assert source_asset.json()["scope"] == "project"
        material_sources = client.get(
            "/api/v1/marketplace/material-sources",
            headers=creator_headers,
        )
        assert material_sources.status_code == 200
        assert any(item["id"] == source_asset_id for item in material_sources.json())
        forbidden_material_publish = client.post(
            f"/api/v1/marketplace/materials/{source_asset_id}/publish",
            headers=consumer_headers,
            json={"category": "人物", "tags": []},
        )
        assert forbidden_material_publish.status_code == 404
        published_material = client.post(
            f"/api/v1/marketplace/materials/{source_asset_id}/publish",
            headers=creator_headers,
            json={"category": "人物", "tags": ["侦探", "现代"]},
        )
        assert published_material.status_code == 200
        material_listing_id = published_material.json()["id"]
        listing_ids.append(material_listing_id)
        copied_material = client.post(
            f"/api/v1/marketplace/listings/{material_listing_id}/acquire",
            headers=consumer_headers,
        )
        assert copied_material.status_code == 200
        copied_asset_id = copied_material.json()["target_id"]
        target_ids.append(copied_asset_id)
        copied_asset = next(
            item
            for item in client.get("/api/v1/assets", headers=consumer_headers).json()
            if item["id"] == copied_asset_id
        )
        assert copied_asset["scope"] == "global"
        assert copied_asset["asset_metadata"]["marketplace_listing_id"] == material_listing_id

        unpublish = client.delete(
            f"/api/v1/marketplace/listings/{skill_listing_id}", headers=creator_headers
        )
        assert unpublish.status_code == 204
        hidden_feed = client.get(
            "/api/v1/marketplace/skill",
            headers=consumer_headers,
            params={"search": "动作分镜"},
        )
        assert hidden_feed.status_code == 200
        assert all(item["id"] != skill_listing_id for item in hidden_feed.json()["items"])
    finally:
        asyncio.run(cleanup())


def test_required_default_models_are_ready(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/admin/readiness", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["missing"] == []
    assert response.json()["image_resolution_models"] == {"1K": True, "2K": True, "4K": True}


def test_image_resolutions_route_to_models_across_providers(
    client: TestClient,
    admin_headers: dict[str, str],
    creator_headers: dict[str, str],
) -> None:
    assert (
        client.get("/api/v1/admin/image-resolution-models", headers=creator_headers).status_code
        == 403
    )
    original_routes = client.get(
        "/api/v1/admin/image-resolution-models", headers=admin_headers
    ).json()
    assert {item["resolution"] for item in original_routes} == {"1K", "2K", "4K"}

    primary_provider_id = client.get(
        "/api/v1/admin/providers", headers=admin_headers
    ).json()[0]["id"]
    secondary_provider = client.post(
        "/api/v1/admin/providers",
        headers=admin_headers,
        json={
            "code": "resolution-route-secondary",
            "name": "Resolution Route Secondary",
            "provider_type": "openai_compatible",
            "base_url": "https://resolution-route.invalid/v1",
        },
    )
    assert secondary_provider.status_code == 201
    secondary_provider_id = secondary_provider.json()["id"]

    primary_model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": primary_provider_id,
            "model_id": "resolution-route-primary-image",
            "name": "Resolution Route Primary Image",
            "model_type": "image",
        },
    ).json()
    secondary_model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": secondary_provider_id,
            "model_id": "resolution-route-secondary-image",
            "name": "Resolution Route Secondary Image",
            "model_type": "image",
        },
    ).json()

    try:
        expected = {
            "1K": primary_model["id"],
            "2K": secondary_model["id"],
            "4K": primary_model["id"],
        }
        for resolution, model_id in expected.items():
            response = client.put(
                f"/api/v1/admin/image-resolution-models/{resolution}",
                headers=admin_headers,
                json={"model_id": model_id},
            )
            assert response.status_code == 200
            assert response.json()["model_id"] == model_id

        routes = client.get(
            "/api/v1/admin/image-resolution-models", headers=admin_headers
        ).json()
        assert {item["resolution"]: item["model_id"] for item in routes} == expected
        assert client.get("/api/v1/admin/readiness", headers=admin_headers).json()["ready"] is True

        tenant_id = client.get("/api/v1/auth/me", headers=admin_headers).json()["user"]["tenant_id"]

        async def resolved_route_models() -> dict[str, str | None]:
            async with SessionLocal() as session:
                resolved: dict[str, str | None] = {}
                for resolution in ("1K", "2K", "4K"):
                    model = await resolve_image_model(
                        session,
                        tenant_id=tenant_id,
                        resolution=resolution,
                    )
                    resolved[resolution] = model.id if model else None
                return resolved

        assert asyncio.run(resolved_route_models()) == expected

        blocked = client.patch(
            f"/api/v1/admin/models/{primary_model['id']}",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert blocked.status_code == 409
        assert "图片分辨率路由" in blocked.json()["detail"]
        deleted = client.delete(
            f"/api/v1/admin/models/{secondary_model['id']}", headers=admin_headers
        )
        assert deleted.status_code == 409
        assert "图片分辨率路由" in deleted.json()["detail"]
    finally:
        for route in original_routes:
            restored = client.put(
                f"/api/v1/admin/image-resolution-models/{route['resolution']}",
                headers=admin_headers,
                json={"model_id": route["model_id"]},
            )
            assert restored.status_code == 200
        assert (
            client.delete(
                f"/api/v1/admin/models/{primary_model['id']}", headers=admin_headers
            ).status_code
            == 204
        )
        assert (
            client.delete(
                f"/api/v1/admin/models/{secondary_model['id']}", headers=admin_headers
            ).status_code
            == 204
        )
        assert (
            client.delete(
                f"/api/v1/admin/providers/{secondary_provider_id}", headers=admin_headers
            ).status_code
            == 204
        )


def test_missing_required_default_blocks_projects_and_tasks_without_charging(
    client: TestClient,
    admin_headers: dict[str, str],
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get("/api/v1/projects/options", headers=creator_headers).json()
    before = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])

    async def set_default_video_enabled(enabled: bool) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(AIModel)
                .where(
                    AIModel.model_type == ModelType.VIDEO,
                    AIModel.is_default.is_(True),
                )
                .values(enabled=enabled)
            )
            await session.commit()

    asyncio.run(set_default_video_enabled(False))
    try:
        readiness = client.get("/api/v1/admin/readiness", headers=admin_headers)
        assert readiness.status_code == 200
        assert readiness.json()["ready"] is False
        assert readiness.json()["missing"] == ["video"]

        cover = client.post(
            f"/api/v1/projects/{project_id}/cover/generate",
            headers=creator_headers,
            json={},
        )
        assert cover.status_code == 503
        assert "video" in cover.json()["detail"]

        created = client.post(
            "/api/v1/projects",
            headers=creator_headers,
            json={
                "name": "不应创建的未就绪项目",
                "video_model_id": options["video_models"][0]["id"],
                "visual_handbook_id": options["visual_handbooks"][0]["id"],
                "director_handbook_id": options["director_handbooks"][0]["id"],
            },
        )
        assert created.status_code == 503
        assert "video" in created.json()["detail"]
        after = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
        assert after == before
    finally:
        asyncio.run(set_default_video_enabled(True))


def test_tenant_pricing_rules_snapshot_new_task_costs(
    client: TestClient,
    admin_headers: dict[str, str],
    creator_headers: dict[str, str],
) -> None:
    forbidden = client.get("/api/v1/admin/pricing-rules", headers=creator_headers)
    assert forbidden.status_code == 403
    public_rules = client.get("/api/v1/pricing", headers=creator_headers)
    assert public_rules.status_code == 200
    assert len(public_rules.json()) == 12

    rules = client.get("/api/v1/admin/pricing-rules", headers=admin_headers)
    assert rules.status_code == 200
    assert len(rules.json()) == 12
    cover_rule = next(rule for rule in rules.json() if rule["task_type"] == "project_cover_generation")
    updated = client.patch(
        "/api/v1/admin/pricing-rules/project_cover_generation",
        headers=admin_headers,
        json={"unit_cost": "23.50"},
    )
    assert updated.status_code == 200
    assert Decimal(updated.json()["unit_cost"]) == Decimal("23.50")
    assert updated.json()["version"] == cover_rule["version"] + 1

    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    before = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    task = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={"style_hint": "计费快照测试"},
    )
    assert task.status_code == 202
    assert Decimal(task.json()["cost"]) == Decimal("23.50")
    assert task.json()["request_payload"]["pricing"] == {
        "task_type": "project_cover_generation",
        "unit_cost": "23.50",
        "quantity": 1,
        "total_cost": "23.50",
        "rule_version": updated.json()["version"],
    }
    after = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert after == before - Decimal("23.50")

    cancelled = client.post(
        f"/api/v1/tasks/{task.json()['id']}/cancel",
        headers=creator_headers,
    )
    assert cancelled.status_code == 200
    restored = client.patch(
        "/api/v1/admin/pricing-rules/project_cover_generation",
        headers=admin_headers,
        json={"unit_cost": "20.00"},
    )
    assert restored.status_code == 200
    persisted_task = client.get(
        f"/api/v1/tasks/{task.json()['id']}",
        headers=creator_headers,
    )
    assert Decimal(persisted_task.json()["cost"]) == Decimal("23.50")
    refunded = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert refunded == before


def test_autodl_h3_preset_install_is_idempotent(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    first = client.post(
        "/api/v1/admin/provider-presets/autodl-minimax-h3/install",
        headers=admin_headers,
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["provider"]["code"] == "autodl-minimax-h3"
    assert first_body["provider"]["enabled"] is False
    assert first_body["provider"]["configured_credentials"] == []
    assert first_body["provider"]["adapter_config"]["video"]["references"][0]["source"] == "data_uri"
    assert first_body["model"]["model_id"] == "minimax_h3_lightx2v_v5_15s"
    assert first_body["model"]["enabled"] is False
    assert first_body["model"]["capabilities"]["reference_limits"]["image"] == {
        "enabled": True,
        "min_count": 1,
        "max_count": 9,
    }

    second = client.post(
        "/api/v1/admin/provider-presets/autodl-minimax-h3/install",
        headers=admin_headers,
    )
    assert second.status_code == 200
    assert second.json()["provider"]["id"] == first_body["provider"]["id"]
    assert second.json()["model"]["id"] == first_body["model"]["id"]

    cannot_enable = client.patch(
        f"/api/v1/admin/providers/{first_body['provider']['id']}",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert cannot_enable.status_code == 422
    assert "ComfyUI Token" in cannot_enable.json()["detail"]
    configured = client.patch(
        f"/api/v1/admin/providers/{first_body['provider']['id']}",
        headers=admin_headers,
        json={
            "base_url": "https://autodl.example.test/api/v1/",
            "credentials": {"apiKey": "Bearer test-token"},
        },
    )
    assert configured.status_code == 200
    assert configured.json()["base_url"] == "https://autodl.example.test"
    assert configured.json()["configured_credentials"] == ["apiKey"]
    enabled = client.patch(
        f"/api/v1/admin/providers/{first_body['provider']['id']}",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert client.patch(
        f"/api/v1/admin/providers/{first_body['provider']['id']}",
        headers=admin_headers,
        json={"enabled": False},
    ).status_code == 200
    assert client.delete(
        f"/api/v1/admin/models/{first_body['model']['id']}", headers=admin_headers
    ).status_code == 204
    assert client.delete(
        f"/api/v1/admin/providers/{first_body['provider']['id']}", headers=admin_headers
    ).status_code == 204


def test_image_model_test_performs_real_generation_probe(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    image_model = next(
        item
        for item in client.get("/api/v1/admin/models", headers=admin_headers).json()
        if item["model_type"] == "image"
    )
    monkeypatch.setattr(
        admin_routes,
        "provider_gateway",
        lambda _provider: FakeImageModelTestGateway(),
    )
    response = client.post(
        f"/api/v1/admin/models/{image_model['id']}/test",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "真实生图验证通过" in response.json()["message"]


def test_provider_model_discovery_import_and_delete_guards(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]
    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeCatalogClient)

    discovered = client.post(
        f"/api/v1/admin/providers/{provider['id']}/discover-models",
        headers=admin_headers,
    )
    assert discovered.status_code == 200
    items = discovered.json()["items"]
    assert [item["inferred_type"] for item in items] == ["text", "image", "video"]
    assert all(item["is_imported"] is False for item in items)

    payload = {
        "items": [
            {
                "model_id": item["model_id"],
                "name": item["name"],
                "model_type": item["inferred_type"],
            }
            for item in items[:2]
        ]
    }
    imported = client.post(
        f"/api/v1/admin/providers/{provider['id']}/import-models",
        headers=admin_headers,
        json=payload,
    )
    assert imported.status_code == 200
    assert len(imported.json()["imported"]) == 2

    verified_model = imported.json()["imported"][0]
    verified = client.post(
        f"/api/v1/admin/models/{verified_model['id']}/test",
        headers=admin_headers,
    )
    assert verified.status_code == 200
    assert verified.json()["ok"] is True
    assert verified.json()["message"] == "模型目录验证通过"
    refreshed_model = next(
        item
        for item in client.get("/api/v1/admin/models", headers=admin_headers).json()
        if item["id"] == verified_model["id"]
    )
    assert refreshed_model["last_test_ok"] is True
    assert refreshed_model["last_tested_at"] is not None
    assert refreshed_model["last_test_latency_ms"] >= 0

    missing_model = next(
        item
        for item in client.get("/api/v1/admin/models", headers=admin_headers).json()
        if item["model_type"] != "image"
        and item["model_id"] not in {row["model_id"] for row in items}
    )
    missing = client.post(
        f"/api/v1/admin/models/{missing_model['id']}/test",
        headers=admin_headers,
    )
    assert missing.status_code == 200
    assert missing.json()["ok"] is False
    assert "未找到" in missing.json()["message"]

    duplicate = client.post(
        f"/api/v1/admin/providers/{provider['id']}/import-models",
        headers=admin_headers,
        json=payload,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["skipped_model_ids"] == ["gpt-5.2", "flux-image-pro"]

    unused_model_id = imported.json()["imported"][0]["id"]
    assert client.delete(f"/api/v1/admin/models/{unused_model_id}", headers=admin_headers).status_code == 204
    delete_provider = client.delete(f"/api/v1/admin/providers/{provider['id']}", headers=admin_headers)
    assert delete_provider.status_code == 409


def test_custom_provider_credentials_and_video_capabilities_are_validated(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    adapter = {
        "schema_version": 1,
        "credential_fields": [
            {"key": "token", "label": "服务 Token", "input_type": "password", "required": True}
        ],
    }
    created = client.post(
        "/api/v1/admin/providers",
        headers=admin_headers,
        json={
            "code": "custom-video-adapter-test",
            "name": "Custom Video Adapter Test",
            "provider_type": "custom",
            "base_url": "https://provider.example.test",
            "credentials": {"token": "must-never-be-returned"},
            "adapter_config": adapter,
            "max_concurrency": 7,
        },
    )
    assert created.status_code == 201
    provider = created.json()
    assert provider["configured_credentials"] == ["token"]
    assert provider["max_concurrency"] == 7
    assert "must-never-be-returned" not in json.dumps(provider)

    invalid_concurrency = client.patch(
        f"/api/v1/admin/providers/{provider['id']}",
        headers=admin_headers,
        json={"max_concurrency": 65},
    )
    assert invalid_concurrency.status_code == 422

    invalid_model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider["id"],
            "model_id": "invalid-first-frame",
            "name": "Invalid First Frame",
            "model_type": "video",
            "capabilities": {"generation_modes": ["first_frame"]},
        },
    )
    assert invalid_model.status_code == 422

    capabilities = {
        "generation_modes": ["text_to_video", "first_frame", "multi_shot"],
        "reference_limits": {
            "image": {"enabled": True, "min_count": 1, "max_count": 9},
            "video": {"enabled": False, "min_count": 0, "max_count": 0},
            "audio": {"enabled": False, "min_count": 0, "max_count": 0},
        },
        "audio_policy": "disabled",
        "duration_resolution_map": [
            {"durations": [1, 5, 10, 15], "resolutions": ["768p横", "768p竖", "768p(1:1)"]}
        ],
        "aspect_ratios": ["16:9", "9:16", "1:1"],
    }
    model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider["id"],
            "model_id": "h3-multi-reference",
            "name": "H3 Multi Reference",
            "model_type": "video",
            "capabilities": capabilities,
        },
    )
    assert model.status_code == 201
    assert model.json()["capabilities"]["reference_limits"]["image"]["max_count"] == 9
    assert model.json()["capabilities"]["audio_policy"] == "disabled"

    deleted_model = client.delete(f"/api/v1/admin/models/{model.json()['id']}", headers=admin_headers)
    deleted_provider = client.delete(f"/api/v1/admin/providers/{provider['id']}", headers=admin_headers)
    assert deleted_model.status_code == 204
    assert deleted_provider.status_code == 204


def test_provider_and_model_disable_guards(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    provider = client.post(
        "/api/v1/admin/providers",
        headers=admin_headers,
        json={
            "code": "lifecycle-guard",
            "name": "Lifecycle Guard",
            "provider_type": "openai_compatible",
            "base_url": "https://lifecycle.invalid/v1",
        },
    )
    assert provider.status_code == 201
    provider_id = provider.json()["id"]
    model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider_id,
            "model_id": "lifecycle-text",
            "name": "Lifecycle Text",
            "model_type": "text",
        },
    )
    assert model.status_code == 201
    model_id = model.json()["id"]

    blocked_provider = client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert blocked_provider.status_code == 409
    assert "启用模型" in blocked_provider.json()["detail"]

    disabled_model = client.patch(
        f"/api/v1/admin/models/{model_id}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert disabled_model.status_code == 200
    disabled_provider = client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert disabled_provider.status_code == 200

    blocked_create = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider_id,
            "model_id": "disabled-provider-model",
            "name": "Disabled Provider Model",
            "model_type": "image",
        },
    )
    assert blocked_create.status_code == 409
    blocked_enable = client.patch(
        f"/api/v1/admin/models/{model_id}",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert blocked_enable.status_code == 409

    assert (
        client.patch(
            f"/api/v1/admin/providers/{provider_id}",
            headers=admin_headers,
            json={"enabled": True},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/admin/models/{model_id}", headers=admin_headers).status_code == 204
    assert client.delete(f"/api/v1/admin/providers/{provider_id}", headers=admin_headers).status_code == 204


def test_default_and_production_model_references_are_protected(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    models = client.get("/api/v1/admin/models", headers=admin_headers).json()
    required_default = next(item for item in models if item["model_type"] == "text" and item["is_default"])

    deleted_default = client.delete(f"/api/v1/admin/models/{required_default['id']}", headers=admin_headers)
    assert deleted_default.status_code == 409
    assert "默认配置" in deleted_default.json()["detail"]
    assert (
        client.patch(
            f"/api/v1/admin/models/{required_default['id']}",
            headers=admin_headers,
            json={"is_default": False},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/admin/models/{required_default['id']}",
            headers=admin_headers,
            json={"enabled": False},
        ).status_code
        == 409
    )

    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    invalid_default = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider_id,
            "model_id": "disabled-default-test",
            "name": "Disabled Default Test",
            "model_type": "image",
            "enabled": False,
            "is_default": True,
        },
    )
    assert invalid_default.status_code == 422
    assert invalid_default.json()["detail"] == "默认模型必须保持启用"

    tts_model = client.post(
        "/api/v1/admin/models",
        headers=admin_headers,
        json={
            "provider_id": provider_id,
            "model_id": "voice-binding-delete-guard",
            "name": "Voice Binding Delete Guard",
            "model_type": "tts",
        },
    )
    assert tts_model.status_code == 201
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    character = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={"asset_type": "character", "name": "删除保护测试角色"},
    )
    assert character.status_code == 201
    binding = client.put(
        f"/api/v1/projects/{project_id}/dubbing/voice-bindings",
        headers=creator_headers,
        json={
            "character_asset_id": character.json()["id"],
            "tts_model_id": tts_model.json()["id"],
            "provider_voice_id": "delete-guard-voice",
        },
    )
    assert binding.status_code == 200
    blocked = client.delete(f"/api/v1/admin/models/{tts_model.json()['id']}", headers=admin_headers)
    assert blocked.status_code == 409
    assert "音色绑定" in blocked.json()["detail"]
    blocked_disable = client.patch(
        f"/api/v1/admin/models/{tts_model.json()['id']}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert blocked_disable.status_code == 409
    assert "音色绑定" in blocked_disable.json()["detail"]


def test_cover_generation_executes_and_updates_project(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    before = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])

    response = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={"style_hint": "雨夜港口，克制电影感"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["idempotency_key"] == response.json()["id"]
    assert Decimal(response.json()["cost"]) == Decimal("20.00")
    after = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert after == before - Decimal("20.00")

    duplicate = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={"style_hint": "重复点击不应再次扣费"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "当前项目已有封面生成任务正在处理"
    unchanged = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert unchanged == after

    task_id = response.json()["id"]
    processed = asyncio.run(process_next_task(lambda _provider: FakeImageGateway()))
    assert processed == task_id
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers)
    project = client.get(f"/api/v1/projects/{project_id}", headers=creator_headers)
    assert task.status_code == 200
    assert task.json()["status"] == "succeeded"
    assert task.json()["progress"] == 100
    assert task.json()["latest_message"] == "封面图片已生成并同步到项目"
    assert task.json()["latest_event_at"] is not None
    assert task.json()["heartbeat_at"] is not None
    assert task.json()["started_at"] is not None
    assert task.json()["completed_at"] is not None
    assert datetime.fromisoformat(task.json()["completed_at"]) >= datetime.fromisoformat(
        task.json()["started_at"]
    )
    assert task.json()["result_payload"]["credit_refunded"] is False
    assert project.json()["cover_url"].startswith("/uploads/")

    listed = client.get("/api/v1/tasks?limit=30", headers=creator_headers).json()["items"]
    listed_task = next(item for item in listed if item["id"] == task_id)
    assert listed_task["progress"] == 100
    assert listed_task["latest_message"] == "封面图片已生成并同步到项目"

    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=creator_headers)
    assert events.status_code == 200
    assert [item["status"] for item in events.json()] == ["queued", "running", "succeeded"]
    assert [item["progress"] for item in events.json()] == [0, 10, 100]

    notifications = client.get("/api/v1/notifications", headers=creator_headers)
    completed = next(item for item in notifications.json()["items"] if item["task_id"] == task_id)
    assert completed["notification_metadata"]["status"] == "succeeded"
    assert completed["is_read"] is False
    marked = client.patch(
        f"/api/v1/notifications/{completed['id']}/read",
        headers=creator_headers,
    )
    assert marked.status_code == 204


def test_activity_stream_requires_auth_and_supports_redis_or_polling(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes.notifications import sse_message, user_event_stream

    assert client.get("/api/v1/notifications/stream").status_code == 401
    encoded = sse_message({"type": "task.updated", "message": "生成完成\n可查看"}, event="activity")
    assert encoded.startswith("event: activity\ndata: ")
    assert "生成完成\\n可查看" in encoded

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def first_messages() -> tuple[str, str]:
        stream = user_event_stream("demo-user", ConnectedRequest())  # type: ignore[arg-type]
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    retry, ready = asyncio.run(first_messages())
    assert retry == "retry: 3000\n\n"
    assert 'event: ready\ndata: {"type":"stream.ready","transport":"polling"}' in ready

    class FakePubSub:
        subscribed_channel = ""

        async def subscribe(self, channel: str) -> None:
            self.subscribed_channel = channel

        async def get_message(self, **_kwargs: object) -> dict[str, str]:
            return {"data": '{"type":"task.updated","task_id":"task-7","progress":35}'}

        async def aclose(self) -> None:
            return None

    fake_pubsub = FakePubSub()

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return fake_pubsub

    monkeypatch.setattr("app.api.routes.notifications.redis_client", lambda: FakeRedis())

    async def redis_messages() -> tuple[str, str, str]:
        stream = user_event_stream("creator-7", ConnectedRequest())  # type: ignore[arg-type]
        try:
            return await anext(stream), await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    redis_retry, redis_ready, activity = asyncio.run(redis_messages())
    assert redis_retry == "retry: 3000\n\n"
    assert '"transport":"redis"' in redis_ready
    assert activity == ('event: activity\ndata: {"type":"task.updated","task_id":"task-7","progress":35}\n\n')
    assert fake_pubsub.subscribed_channel.endswith(":creator-7")


def test_personal_agent_task_event_includes_runtime_media_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.services.task_events import publish_task_event

    published: list[tuple[str, dict[str, object]]] = []

    async def capture_event(user_id: str, payload: dict[str, object]) -> bool:
        published.append((user_id, payload))
        return True

    monkeypatch.setattr("app.services.task_events.publish_user_event", capture_event)
    task = SimpleNamespace(
        id="task-media-chat",
        user_id="user-media-chat",
        project_id=None,
        task_type="agent_chat_run",
        request_payload={"scope": "personal", "original_mode": "chat", "mode": "image"},
        result_payload={
            "media_intent": {
                "type": "image",
                "generation_mode": "text_to_image",
                "model_id": "image-model-1",
            }
        },
    )
    event = SimpleNamespace(
        status=TaskStatus.RUNNING,
        progress=52,
        message="Agent 已理解需求，正在整理最终媒体提示词",
        created_at=datetime.now(UTC),
    )

    asyncio.run(publish_task_event(task, event))

    assert published[0][0] == "user-media-chat"
    assert published[0][1]["task_mode"] == "image"
    assert published[0][1]["media_intent"] == task.result_payload["media_intent"]


def test_failed_task_refunds_and_can_be_retried_then_cancelled(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    before = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    queued = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={},
    ).json()

    asyncio.run(process_next_task(lambda _provider: FakeImageGateway(fail=True)))
    failed = client.get(f"/api/v1/tasks/{queued['id']}", headers=creator_headers)
    refunded = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert failed.json()["status"] == "failed"
    assert failed.json()["progress"] == 100
    assert failed.json()["latest_message"] == failed.json()["error_message"]
    assert failed.json()["result_payload"]["credit_refunded"] is True
    assert refunded == before

    retried = client.post(f"/api/v1/tasks/{queued['id']}/retry", headers=creator_headers)
    after_retry = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert retried.json()["heartbeat_at"] is None
    assert retried.json()["started_at"] is None
    assert retried.json()["completed_at"] is None
    assert after_retry == before - Decimal("20.00")

    cancelled = client.post(f"/api/v1/tasks/{queued['id']}/cancel", headers=creator_headers)
    after_cancel = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["completed_at"] is not None
    assert after_cancel == before

    events = client.get(f"/api/v1/tasks/{queued['id']}/events", headers=creator_headers).json()
    assert [item["status"] for item in events] == [
        "queued",
        "running",
        "failed",
        "queued",
        "cancelled",
    ]

    notifications = client.get("/api/v1/notifications", headers=creator_headers).json()
    statuses = {
        item["notification_metadata"]["status"]
        for item in notifications["items"]
        if item["task_id"] == queued["id"]
    }
    assert statuses == {"failed", "cancelled"}

    read_all = client.post("/api/v1/notifications/read-all", headers=creator_headers)
    assert read_all.status_code == 204
    assert client.get("/api/v1/notifications", headers=creator_headers).json()["unread_count"] == 0


def test_activity_records_can_be_deleted_without_cross_user_access(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    email = "activity-cleanup@cineforge.local"
    password = "Activity123!"
    created_user_id = ""
    admin_notification_id = ""

    async def cleanup() -> None:
        async with SessionLocal() as session:
            if admin_notification_id:
                await session.execute(
                    delete(Notification).where(Notification.id == admin_notification_id)
                )
            if created_user_id:
                await session.execute(delete(RefreshSession).where(RefreshSession.user_id == created_user_id))
                await session.execute(delete(CreditLedger).where(CreditLedger.user_id == created_user_id))
                await session.execute(delete(CreditAccount).where(CreditAccount.user_id == created_user_id))
                await session.execute(delete(User).where(User.id == created_user_id))
            await session.commit()

    try:
        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": email,
                "display_name": "动态中心测试用户",
                "password": password,
                "role": "user",
                "initial_credits": "0",
            },
        )
        assert created.status_code == 201
        created_user_id = created.json()["id"]
        login = client.post(
            "/api/v1/auth/login",
            json={"tenant": "demo", "email": email, "password": password},
        )
        assert login.status_code == 200
        user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        async def seed_records() -> tuple[str, str, str, str, str]:
            nonlocal admin_notification_id
            async with SessionLocal() as session:
                user = await session.get(User, created_user_id)
                admin = await session.scalar(select(User).where(User.email == "admin@cineforge.local"))
                assert user is not None and admin is not None
                succeeded = AITask(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_type="activity_delete_succeeded",
                    status=TaskStatus.SUCCEEDED,
                )
                second_succeeded = AITask(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_type="activity_clear_succeeded",
                    status=TaskStatus.SUCCEEDED,
                )
                failed = AITask(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_type="activity_clear_failed",
                    status=TaskStatus.FAILED,
                )
                queued = AITask(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_type="activity_keep_queued",
                    status=TaskStatus.QUEUED,
                )
                session.add_all([succeeded, second_succeeded, failed, queued])
                await session.flush()
                linked_notice = Notification(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_id=succeeded.id,
                    category="task",
                    title="已完成任务",
                    message="应随任务删除",
                )
                standalone_notice = Notification(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    category="system",
                    title="独立消息",
                    message="支持单独删除",
                )
                failed_notice = Notification(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_id=failed.id,
                    category="task",
                    title="失败任务",
                    message="支持清空",
                )
                admin_notice = Notification(
                    tenant_id=admin.tenant_id,
                    user_id=admin.id,
                    category="system",
                    title="管理员消息",
                    message="不得被其他用户清空",
                )
                session.add_all([linked_notice, standalone_notice, failed_notice, admin_notice])
                await session.commit()
                admin_notification_id = admin_notice.id
                return (
                    succeeded.id,
                    second_succeeded.id,
                    failed.id,
                    queued.id,
                    standalone_notice.id,
                )

        succeeded_id, second_succeeded_id, failed_id, queued_id, standalone_notice_id = asyncio.run(
            seed_records()
        )

        assert client.delete(f"/api/v1/tasks/{queued_id}", headers=user_headers).status_code == 409
        assert client.delete(f"/api/v1/tasks/{succeeded_id}", headers=admin_headers).status_code == 404
        assert (
            client.delete(
                f"/api/v1/notifications/{standalone_notice_id}", headers=admin_headers
            ).status_code
            == 404
        )

        assert client.delete(f"/api/v1/tasks/{succeeded_id}", headers=user_headers).status_code == 204
        assert client.get(f"/api/v1/tasks/{succeeded_id}", headers=user_headers).status_code == 404
        notifications = client.get("/api/v1/notifications", headers=user_headers).json()["items"]
        assert all(item["task_id"] != succeeded_id for item in notifications)

        assert (
            client.delete(
                f"/api/v1/notifications/{standalone_notice_id}", headers=user_headers
            ).status_code
            == 204
        )
        assert client.delete("/api/v1/notifications", headers=user_headers).status_code == 204
        assert client.get("/api/v1/notifications", headers=user_headers).json() == {
            "items": [],
            "unread_count": 0,
        }
        admin_notifications = client.get("/api/v1/notifications", headers=admin_headers).json()["items"]
        assert any(item["id"] == admin_notification_id for item in admin_notifications)

        assert client.delete("/api/v1/tasks?group=failed", headers=user_headers).status_code == 204
        remaining_ids = {
            item["id"] for item in client.get("/api/v1/tasks", headers=user_headers).json()["items"]
        }
        assert failed_id not in remaining_ids
        assert second_succeeded_id in remaining_ids
        assert queued_id in remaining_ids

        assert client.delete("/api/v1/tasks?group=completed", headers=user_headers).status_code == 204
        remaining_ids = {
            item["id"] for item in client.get("/api/v1/tasks", headers=user_headers).json()["items"]
        }
        assert second_succeeded_id not in remaining_ids
        assert queued_id in remaining_ids
    finally:
        asyncio.run(cleanup())


def test_admin_invitation_registration_flow(
    client: TestClient,
    admin_headers: dict[str, str],
    creator_headers: dict[str, str],
) -> None:
    invitation_id = ""
    registered_user_ids: list[str] = []
    original_prefix = client.get(
        "/api/v1/admin/invitations/settings", headers=admin_headers
    ).json()["url_prefix"]

    async def cleanup() -> None:
        async with SessionLocal() as session:
            if registered_user_ids:
                users = list(
                    (
                        await session.scalars(
                            select(User).where(User.id.in_(registered_user_ids))
                        )
                    ).all()
                )
                await session.execute(
                    delete(InvitationRedemption).where(
                        InvitationRedemption.user_id.in_(registered_user_ids)
                    )
                )
                await session.execute(
                    delete(RefreshSession).where(RefreshSession.user_id.in_(registered_user_ids))
                )
                await session.execute(
                    delete(CreditLedger).where(CreditLedger.user_id.in_(registered_user_ids))
                )
                await session.execute(
                    delete(CreditAccount).where(CreditAccount.user_id.in_(registered_user_ids))
                )
                await session.execute(
                    delete(SecurityEvent).where(SecurityEvent.user_id.in_(registered_user_ids))
                )
                await session.execute(delete(User).where(User.id.in_(registered_user_ids)))
                for user in users:
                    if user.avatar_storage_path:
                        (get_settings().uploads_root / user.avatar_storage_path).unlink(missing_ok=True)
            if invitation_id:
                subject_hash = hashlib.sha256(
                    f"invitation:{invitation_id}".encode()
                ).hexdigest()
                await session.execute(
                    delete(SecurityEvent).where(SecurityEvent.subject_hash == subject_hash)
                )
                await session.execute(
                    delete(InvitationRedemption).where(
                        InvitationRedemption.invitation_id == invitation_id
                    )
                )
                await session.execute(
                    delete(InvitationCode).where(InvitationCode.id == invitation_id)
                )
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == "demo"))
            assert tenant is not None
            tenant.invite_url_prefix = original_prefix or None
            await session.commit()

    try:
        assert (
            client.get("/api/v1/admin/invitations", headers=creator_headers).status_code
            == 403
        )
        invalid_prefix = client.put(
            "/api/v1/admin/invitations/settings",
            headers=admin_headers,
            json={"url_prefix": "studio.example.com"},
        )
        assert invalid_prefix.status_code == 422

        settings = client.put(
            "/api/v1/admin/invitations/settings",
            headers=admin_headers,
            json={"url_prefix": "https://studio.example.com/create/"},
        )
        assert settings.status_code == 200
        assert settings.json()["url_prefix"] == "https://studio.example.com/create"

        created = client.post(
            "/api/v1/admin/invitations",
            headers=admin_headers,
            json={
                "name": "首批创作者",
                "max_registrations": 2,
                "initial_credits": "36.50",
                "enabled": True,
            },
        )
        assert created.status_code == 201
        invitation = created.json()
        invitation_id = invitation["id"]
        code = invitation["code"]
        assert invitation["invite_url"] == f"https://studio.example.com/create/invite/{code}"
        assert invitation["remaining_registrations"] == 2

        public_info = client.get(f"/api/v1/invitations/{code}")
        assert public_info.status_code == 200
        assert public_info.json()["tenant_name"]
        assert public_info.json()["initial_credits"] == "36.50"

        avatar = BytesIO()
        Image.new("RGB", (720, 960), "#245953").save(avatar, format="PNG")
        registered = client.post(
            f"/api/v1/invitations/{code}/register",
            data={
                "email": "invited-one@cineforge.local",
                "display_name": "受邀创作者一",
                "password": "Invited123!",
            },
            files={"avatar": ("avatar.png", avatar.getvalue(), "image/png")},
        )
        assert registered.status_code == 200
        assert registered.json()["access_token"]

        async def first_registration_state() -> tuple[str, Decimal, int, int, str | None]:
            async with SessionLocal() as session:
                user = await session.scalar(
                    select(User).where(User.email == "invited-one@cineforge.local")
                )
                assert user is not None
                registered_user_ids.append(user.id)
                account = await session.scalar(
                    select(CreditAccount).where(CreditAccount.user_id == user.id)
                )
                redemption_count = await session.scalar(
                    select(func.count(InvitationRedemption.id)).where(
                        InvitationRedemption.user_id == user.id
                    )
                )
                ledger_count = await session.scalar(
                    select(func.count(CreditLedger.id)).where(
                        CreditLedger.user_id == user.id,
                        CreditLedger.reference_type == "invitation",
                    )
                )
                assert account is not None
                return (
                    user.id,
                    account.balance,
                    redemption_count or 0,
                    ledger_count or 0,
                    user.avatar_storage_path,
                )

        first_user_id, balance, redemptions, ledgers, avatar_storage_path = asyncio.run(
            first_registration_state()
        )
        assert balance == Decimal("36.50")
        assert redemptions == 1
        assert ledgers == 1
        assert avatar_storage_path
        assert (get_settings().uploads_root / avatar_storage_path).is_file()

        duplicate = client.post(
            f"/api/v1/invitations/{code}/register",
            data={
                "email": "invited-one@cineforge.local",
                "display_name": "重复账号",
                "password": "Invited123!",
            },
        )
        assert duplicate.status_code == 409
        after_duplicate = client.get(
            "/api/v1/admin/invitations", headers=admin_headers
        ).json()
        assert next(item for item in after_duplicate if item["id"] == invitation_id)[
            "registration_count"
        ] == 1

        second = client.post(
            f"/api/v1/invitations/{code}/register",
            data={
                "email": "invited-two@cineforge.local",
                "display_name": "受邀创作者二",
                "password": "Invited123!",
            },
        )
        assert second.status_code == 200

        async def remember_second_user() -> None:
            async with SessionLocal() as session:
                user_id = await session.scalar(
                    select(User.id).where(User.email == "invited-two@cineforge.local")
                )
                assert user_id is not None
                registered_user_ids.append(user_id)

        asyncio.run(remember_second_user())
        assert first_user_id in registered_user_ids
        assert client.get(f"/api/v1/invitations/{code}").status_code == 410

        below_usage = client.patch(
            f"/api/v1/admin/invitations/{invitation_id}",
            headers=admin_headers,
            json={"max_registrations": 1},
        )
        assert below_usage.status_code == 409
        expanded = client.patch(
            f"/api/v1/admin/invitations/{invitation_id}",
            headers=admin_headers,
            json={"max_registrations": 3, "initial_credits": "50.00"},
        )
        assert expanded.status_code == 200
        assert expanded.json()["remaining_registrations"] == 1

        disabled = client.patch(
            f"/api/v1/admin/invitations/{invitation_id}",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert client.get(f"/api/v1/invitations/{code}").status_code == 410
        assert client.patch(
            f"/api/v1/admin/invitations/{invitation_id}",
            headers=admin_headers,
            json={"enabled": True},
        ).status_code == 200

        deleted = client.delete(
            f"/api/v1/admin/invitations/{invitation_id}", headers=admin_headers
        )
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/invitations/{code}").status_code == 404
    finally:
        asyncio.run(cleanup())


def test_stale_synchronous_media_task_fails_and_refunds_from_database(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    before = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    queued = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={},
    ).json()

    async def make_stale() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(AITask)
                .where(AITask.id == queued["id"])
                .values(
                    status=TaskStatus.RUNNING,
                    updated_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )
            await session.commit()

    asyncio.run(make_stale())
    assert asyncio.run(recover_stale_tasks()) == 1

    recovered = client.get(f"/api/v1/tasks/{queued['id']}", headers=creator_headers)
    assert recovered.json()["status"] == "failed"
    assert recovered.json()["progress"] == 100
    assert recovered.json()["latest_message"] == "Worker 重启时无法确认服务商是否已生成，请手动重试"
    assert recovered.json()["result_payload"]["recovery_count"] == 1
    assert recovered.json()["result_payload"]["restart_disposition"] == "failed_unknown_upstream"
    assert recovered.json()["result_payload"]["credit_refunded"] is True
    assert recovered.json()["heartbeat_at"] is None
    assert recovered.json()["started_at"] is None
    assert recovered.json()["completed_at"] is not None
    after = Decimal(client.get("/api/v1/auth/me", headers=creator_headers).json()["credit_balance"])
    assert after == before
    events = client.get(f"/api/v1/tasks/{queued['id']}/events", headers=creator_headers).json()
    assert events[-1]["event_metadata"] == {"recovered": True, "restart_safe": False}

    retried = client.post(f"/api/v1/tasks/{queued['id']}/retry", headers=creator_headers)
    assert retried.status_code == 202
    cancelled = client.post(f"/api/v1/tasks/{queued['id']}/cancel", headers=creator_headers)
    assert cancelled.status_code == 200


def test_replaced_worker_cannot_mutate_a_reclaimed_task(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    queued = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={},
    ).json()
    assert asyncio.run(claim_task(queued["id"])) == queued["id"]
    claimed = client.get(f"/api/v1/tasks/{queued['id']}", headers=creator_headers).json()
    assert claimed["status"] == "running"
    assert claimed["heartbeat_at"] is not None
    assert claimed["started_at"] is not None

    async def replace_owner() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(AITask)
                .where(AITask.id == queued["id"])
                .values(
                    worker_id="replacement-worker:42",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
            )
            await session.commit()

    asyncio.run(replace_owner())
    asyncio.run(fail_task(queued["id"], "旧 Worker 的迟到失败结果"))
    protected = client.get(f"/api/v1/tasks/{queued['id']}", headers=creator_headers).json()
    assert protected["status"] == "running"
    assert protected["error_message"] is None
    events = client.get(f"/api/v1/tasks/{queued['id']}/events", headers=creator_headers).json()
    assert [event["status"] for event in events] == ["queued", "running"]

    async def release_for_cleanup() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(AITask)
                .where(AITask.id == queued["id"])
                .values(
                    status=TaskStatus.QUEUED,
                    worker_id=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    started_at=None,
                )
            )
            await session.commit()

    asyncio.run(release_for_cleanup())
    assert client.post(f"/api/v1/tasks/{queued['id']}/cancel", headers=creator_headers).status_code == 200


def test_provider_capacity_queues_same_provider_without_blocking_another_provider(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def setup() -> tuple[list[str], str, int, str]:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            assert project is not None and project.image_model_id
            first_model = await session.get(AIModel, project.image_model_id)
            assert first_model is not None
            first_provider = await session.get(Provider, first_model.provider_id)
            assert first_provider is not None
            original_limit = first_provider.max_concurrency
            first_provider.max_concurrency = 1
            second_provider = Provider(
                tenant_id=project.tenant_id,
                code="worker-capacity-secondary",
                name="Worker Capacity Secondary",
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                base_url="https://capacity-secondary.invalid/v1",
                max_concurrency=1,
            )
            session.add(second_provider)
            await session.flush()
            second_model = AIModel(
                tenant_id=project.tenant_id,
                provider_id=second_provider.id,
                model_id="capacity-image",
                name="Capacity Image",
                model_type=ModelType.IMAGE,
            )
            session.add(second_model)
            await session.flush()
            tasks: list[AITask] = []
            for model_id in (first_model.id, first_model.id, second_model.id):
                task = AITask(
                    tenant_id=project.tenant_id,
                    user_id=project.owner_id,
                    project_id=project.id,
                    task_type="provider_capacity_probe",
                    model_id=model_id,
                )
                session.add(task)
                await session.flush()
                tasks.append(task)
            await session.commit()
            return (
                [task.id for task in tasks],
                first_provider.id,
                original_limit,
                second_provider.id,
            )

    async def cleanup(
        task_ids: list[str],
        first_provider_id: str,
        original_limit: int,
        second_provider_id: str,
    ) -> None:
        async with SessionLocal() as session:
            await session.execute(delete(AITask).where(AITask.id.in_(task_ids)))
            first_provider = await session.get(Provider, first_provider_id)
            assert first_provider is not None
            first_provider.max_concurrency = original_limit
            await session.execute(delete(Provider).where(Provider.id == second_provider_id))
            await session.commit()

    task_ids, first_provider_id, original_limit, second_provider_id = asyncio.run(setup())
    try:
        assert asyncio.run(claim_task(task_ids[0])) == task_ids[0]
        assert asyncio.run(claim_task(task_ids[1])) is None
        assert asyncio.run(claim_task()) == task_ids[2]
    finally:
        asyncio.run(cleanup(task_ids, first_provider_id, original_limit, second_provider_id))


def test_concurrent_claims_do_not_exceed_provider_capacity(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def scenario() -> None:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            assert project is not None and project.image_model_id
            model = await session.get(AIModel, project.image_model_id)
            assert model is not None
            provider = await session.get(Provider, model.provider_id)
            assert provider is not None
            original_limit = provider.max_concurrency
            provider.max_concurrency = 1
            tasks = [
                AITask(
                    tenant_id=project.tenant_id,
                    user_id=project.owner_id,
                    project_id=project.id,
                    task_type="concurrent_claim_probe",
                    model_id=model.id,
                )
                for _ in range(2)
            ]
            session.add_all(tasks)
            await session.commit()
            task_ids = [task.id for task in tasks]
            provider_id = provider.id
        try:
            claimed = await asyncio.gather(*(claim_task(task_id) for task_id in task_ids))
            assert sum(item is not None for item in claimed) == 1
            async with SessionLocal() as session:
                statuses = list(
                    (
                        await session.scalars(
                            select(AITask.status).where(AITask.id.in_(task_ids))
                        )
                    ).all()
                )
            assert statuses.count(TaskStatus.RUNNING) == 1
            assert statuses.count(TaskStatus.QUEUED) == 1
        finally:
            async with SessionLocal() as session:
                await session.execute(delete(AITask).where(AITask.id.in_(task_ids)))
                provider = await session.get(Provider, provider_id)
                assert provider is not None
                provider.max_concurrency = original_limit
                await session.commit()

    asyncio.run(scenario())


def test_worker_slots_execute_tasks_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    async def scenario() -> int:
        queued = asyncio.Queue[str]()
        queued.put_nowait("task-a")
        queued.put_nowait("task-b")
        finished = asyncio.Event()
        active = 0
        maximum_active = 0
        completed = 0

        async def fake_dequeue(_timeout_seconds: float) -> str | None:
            return await queued.get()

        async def fake_process(_task_id: str) -> bool:
            nonlocal active, maximum_active, completed
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.03)
            active -= 1
            completed += 1
            if completed == 2:
                finished.set()
            return True

        monkeypatch.setattr(worker, "dequeue_task", fake_dequeue)
        monkeypatch.setattr(worker, "process_task", fake_process)
        slots = [asyncio.create_task(worker.worker_slot(index)) for index in range(2)]
        try:
            await asyncio.wait_for(finished.wait(), timeout=1)
        finally:
            for slot in slots:
                slot.cancel()
            await asyncio.gather(*slots, return_exceptions=True)
        return maximum_active

    assert asyncio.run(scenario()) == 2


def test_agent_chat_task_timeout_is_persisted_as_failure(
    client: TestClient,
    creator_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def create_task() -> str:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            model = await session.scalar(
                select(AIModel).where(
                    AIModel.tenant_id == project.tenant_id,
                    AIModel.model_type == ModelType.TEXT,
                    AIModel.is_default.is_(True),
                )
            )
            assert model is not None
            task = AITask(
                tenant_id=project.tenant_id,
                user_id=project.owner_id,
                project_id=project.id,
                task_type="agent_chat_run",
                model_id=model.id,
            )
            session.add(task)
            await session.commit()
            return task.id

    async def slow_execute(*_args, **_kwargs) -> None:
        await asyncio.sleep(1)

    task_id = asyncio.run(create_task())
    settings = get_settings()
    original_timeout = settings.agent_chat_task_timeout_seconds
    settings.agent_chat_task_timeout_seconds = 0.01
    monkeypatch.setattr(task_worker, "execute_task", slow_execute)
    try:
        assert asyncio.run(process_task(task_id)) is True

        async def result() -> tuple[TaskStatus, str | None]:
            async with SessionLocal() as session:
                task = await session.get(AITask, task_id)
                assert task is not None
                return task.status, task.error_message

        status, message = asyncio.run(result())
        assert status == TaskStatus.FAILED
        assert message == "AI 对话连续 0.01 秒未收到响应或进度，任务已中断"
    finally:
        settings.agent_chat_task_timeout_seconds = original_timeout

        async def cleanup() -> None:
            async with SessionLocal() as session:
                await session.execute(delete(AITask).where(AITask.id == task_id))
                await session.commit()

        asyncio.run(cleanup())


def test_agent_chat_stream_activity_resets_idle_timeout(
    client: TestClient,
    creator_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def create_task() -> str:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            model = await session.scalar(
                select(AIModel).where(
                    AIModel.tenant_id == project.tenant_id,
                    AIModel.model_type == ModelType.TEXT,
                    AIModel.is_default.is_(True),
                )
            )
            assert model is not None
            task = AITask(
                tenant_id=project.tenant_id,
                user_id=project.owner_id,
                project_id=project.id,
                task_type="agent_chat_run",
                model_id=model.id,
            )
            session.add(task)
            await session.commit()
            return task.id

    async def active_execute(task_id: str, *_args, **_kwargs) -> None:
        for _index in range(6):
            await asyncio.sleep(0.12)
            signal_task_activity(task_id)
        async with SessionLocal() as session:
            task = await session.get(AITask, task_id)
            assert task is not None
            task.status = TaskStatus.SUCCEEDED
            await session.commit()

    task_id = asyncio.run(create_task())
    settings = get_settings()
    original_timeout = settings.agent_chat_task_timeout_seconds
    settings.agent_chat_task_timeout_seconds = 0.5
    monkeypatch.setattr(task_worker, "execute_task", active_execute)
    try:
        assert asyncio.run(process_task(task_id)) is True

        async def result() -> TaskStatus:
            async with SessionLocal() as session:
                task = await session.get(AITask, task_id)
                assert task is not None
                return task.status

        assert asyncio.run(result()) == TaskStatus.SUCCEEDED
    finally:
        settings.agent_chat_task_timeout_seconds = original_timeout

        async def cleanup() -> None:
            async with SessionLocal() as session:
                await session.execute(delete(AITask).where(AITask.id == task_id))
                await session.commit()

        asyncio.run(cleanup())


def test_task_access_is_scoped_to_task_owner(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    denied_project = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=admin_headers,
        json={},
    )
    assert denied_project.status_code == 404
    task = client.post(
        f"/api/v1/projects/{project_id}/cover/generate",
        headers=creator_headers,
        json={},
    )
    assert task.status_code == 202
    denied = client.get(f"/api/v1/tasks/{task.json()['id']}", headers=admin_headers)
    assert denied.status_code == 404
    cancelled = client.post(f"/api/v1/tasks/{task.json()['id']}/cancel", headers=creator_headers)
    assert cancelled.status_code == 200


def test_dynamic_center_is_isolated_for_admin_and_creator(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    async def create_owned_tasks() -> tuple[str, str]:
        async with SessionLocal() as session:
            creator = await session.scalar(select(User).where(User.email == "creator@cineforge.local"))
            admin = await session.scalar(select(User).where(User.email == "admin@cineforge.local"))
            assert creator is not None
            assert admin is not None
            creator_task = AITask(
                tenant_id=creator.tenant_id,
                user_id=creator.id,
                task_type="isolation_creator_probe",
            )
            admin_task = AITask(
                tenant_id=admin.tenant_id,
                user_id=admin.id,
                task_type="isolation_admin_probe",
            )
            session.add_all([creator_task, admin_task])
            await session.commit()
            return creator_task.id, admin_task.id

    async def cleanup(task_ids: tuple[str, str]) -> None:
        async with SessionLocal() as session:
            await session.execute(delete(AITask).where(AITask.id.in_(task_ids)))
            await session.commit()

    task_ids = asyncio.run(create_owned_tasks())
    creator_task_id, admin_task_id = task_ids
    try:
        creator_items = client.get(
            "/api/v1/tasks?limit=100",
            headers=creator_headers,
        ).json()["items"]
        admin_items = client.get(
            "/api/v1/tasks?limit=100",
            headers=admin_headers,
        ).json()["items"]
        creator_visible_ids = {item["id"] for item in creator_items}
        admin_visible_ids = {item["id"] for item in admin_items}

        assert creator_task_id in creator_visible_ids
        assert admin_task_id not in creator_visible_ids
        assert admin_task_id in admin_visible_ids
        assert creator_task_id not in admin_visible_ids

        assert client.get(
            f"/api/v1/tasks/{admin_task_id}", headers=creator_headers
        ).status_code == 404
        assert client.get(
            f"/api/v1/tasks/{creator_task_id}", headers=admin_headers
        ).status_code == 404
        assert client.get(
            f"/api/v1/tasks/{creator_task_id}/events", headers=admin_headers
        ).status_code == 404
        assert client.post(
            f"/api/v1/tasks/{creator_task_id}/cancel", headers=admin_headers
        ).status_code == 404
        assert client.post(
            f"/api/v1/tasks/{creator_task_id}/retry", headers=admin_headers
        ).status_code == 404
    finally:
        asyncio.run(cleanup(task_ids))


def test_project_cover_upload_is_normalized_and_served(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    image = BytesIO()
    Image.new("RGB", (640, 360), "#164e63").save(image, format="PNG")

    response = client.post(
        f"/api/v1/projects/{project_id}/cover/upload",
        headers=creator_headers,
        files={"file": ("cover.png", image.getvalue(), "image/png")},
    )

    assert response.status_code == 200
    cover_url = response.json()["cover_url"]
    assert cover_url.startswith("/uploads/")
    assert cover_url.endswith(".webp")
    served = client.get(cover_url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"


def test_admin_can_upload_normalized_handbook_cover(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    handbook = client.get("/api/v1/admin/handbooks", headers=admin_headers).json()[0]
    image = BytesIO()
    Image.new("RGB", (960, 540), "#245953").save(image, format="PNG")
    path = f"/api/v1/admin/handbooks/{handbook['id']}/cover/upload"

    denied = client.post(
        path,
        headers=creator_headers,
        files={"file": ("cover.png", image.getvalue(), "image/png")},
    )
    assert denied.status_code == 403

    uploaded = client.post(
        path,
        headers=admin_headers,
        files={"file": ("cover.png", image.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["version"] == handbook["version"] + 1
    cover_url = uploaded.json()["cover_url"]
    assert cover_url.startswith("/uploads/")
    assert f"/handbooks/{handbook['id']}/" in cover_url
    assert cover_url.endswith(".webp")
    served = client.get(cover_url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"

    replacement = BytesIO()
    Image.new("RGB", (1280, 720), "#4e342e").save(replacement, format="JPEG")
    replaced = client.post(
        path,
        headers=admin_headers,
        files={"file": ("replacement.jpg", replacement.getvalue(), "image/jpeg")},
    )
    assert replaced.status_code == 200
    assert replaced.json()["cover_url"] != cover_url
    assert client.get(cover_url).status_code == 404

    invalid = client.post(
        path,
        headers=admin_headers,
        files={"file": ("cover.txt", b"not-an-image", "text/plain")},
    )
    assert invalid.status_code == 415


def test_director_source_import_and_project_file_workflow(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "测试原著",
            "pasted_text": "第一章 雨夜\n港口响起汽笛。\n第二章 来信\n她拆开那封旧信。",
        },
    )
    assert imported.status_code == 201
    assert [item["title"] for item in imported.json()["chapters"]] == ["第一章 雨夜", "第二章 来信"]
    assert all(item["status"] == "uninitialized" for item in imported.json()["chapters"])

    created = client.post(
        f"/api/v1/projects/{project_id}/files",
        headers=creator_headers,
        json={"name": "项目记忆.md", "kind": "memory", "content": "主角害怕深水。"},
    )
    assert created.status_code == 201
    file_id = created.json()["id"]
    updated = client.put(
        f"/api/v1/projects/{project_id}/files/{file_id}",
        headers=creator_headers,
        json={"content": "主角害怕深水，但会为了家人登船。"},
    )
    assert updated.status_code == 200
    assert updated.json()["size_bytes"] > created.json()["size_bytes"]
    downloaded = client.get(
        f"/api/v1/projects/{project_id}/files/{file_id}/download",
        headers=creator_headers,
    )
    assert downloaded.status_code == 200
    assert "为了家人" in downloaded.text
    assert (
        client.delete(f"/api/v1/projects/{project_id}/files/{file_id}", headers=creator_headers).status_code
        == 204
    )


def test_epub_import_preserves_original_and_extracts_chapters(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    book = epub.EpubBook()
    book.set_identifier("cineforge-test")
    book.set_title("测试剧本")
    book.set_language("zh")
    chapter = epub.EpubHtml(title="第一章 海边", file_name="chapter.xhtml", lang="zh")
    chapter.content = "<h1>第一章 海边</h1><p>潮水缓慢退去，她看见沙滩上的旧相机。</p>"
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    payload = BytesIO()
    epub.write_epub(payload, book)

    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={"mode": "script"},
        files={"file": ("story.epub", payload.getvalue(), "application/epub+zip")},
    )
    assert imported.status_code == 201
    assert imported.json()["original_file"]["editable"] is False
    assert imported.json()["source_file"]["name"] == "story-提取文本.txt"
    assert imported.json()["chapters"][0]["status"] == "uninitialized"
    source_file_id = imported.json()["source_file"]["id"]
    chapter_id = imported.json()["chapters"][0]["id"]
    delete_path = f"/api/v1/projects/{project_id}/files/{source_file_id}"
    assert client.delete(delete_path, headers=creator_headers).status_code == 409
    assert client.delete(f"{delete_path}?delete_chapters=true", headers=creator_headers).status_code == 204
    remaining = client.get(f"/api/v1/projects/{project_id}/chapters", headers=creator_headers).json()
    assert chapter_id not in {item["id"] for item in remaining}


def test_script_versions_invalidate_downstream_and_assets_copy_between_libraries(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[2]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={"mode": "novel", "pasted_text": "第一章 重逢\n她在记忆修复室再次见到旧日恋人。"},
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    extraction_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions"

    blocked = client.post(
        extraction_path,
        headers=creator_headers,
        json={"assets": [{"asset_type": "character", "name": "林遥"}]},
    )
    assert blocked.status_code == 409

    script_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts"
    first_script = client.post(
        script_path,
        headers=creator_headers,
        json={"title": "第一集初稿", "content": "场景一：记忆修复室。", "activate": True},
    )
    assert first_script.status_code == 201
    assert first_script.json()["version"] == 1
    assert first_script.json()["is_active"] is True

    extracted = client.post(
        extraction_path,
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "林遥", "description": "记忆修复师"},
                {
                    "asset_type": "prop",
                    "name": "旧相机",
                    "description": "磨损的胶片相机",
                    "generation_prompt": "银色胶片相机，边角磨损，产品设定图",
                },
            ]
        },
    )
    assert extracted.status_code == 201
    assert [item["status"] for item in extracted.json()["assets"]] == [
        "extracted",
        "prompt_ready",
    ]

    project_asset = extracted.json()["assets"][0]
    exported = client.post(
        f"/api/v1/projects/{project_id}/assets/{project_asset['id']}/export-global",
        headers=creator_headers,
    )
    assert exported.status_code == 200
    assert exported.json()["scope"] == "global"
    copied_back = client.post(
        f"/api/v1/projects/{project_id}/assets/import/{exported.json()['id']}",
        headers=creator_headers,
    )
    assert copied_back.status_code == 200
    assert copied_back.json()["scope"] == "project"
    assert copied_back.json()["id"] != project_asset["id"]

    second_script = client.post(
        script_path,
        headers=creator_headers,
        json={"title": "第一集修订版", "content": "场景一：雨夜中的记忆修复室。", "activate": True},
    )
    assert second_script.status_code == 201
    scripts = client.get(script_path, headers=creator_headers).json()
    assert [item["version"] for item in scripts] == [2, 1]
    assert [item["is_active"] for item in scripts] == [True, False]

    extraction_history = client.get(extraction_path, headers=creator_headers).json()
    assert extraction_history[0]["is_active"] is False
    assert "v2" in extraction_history[0]["invalidated_reason"]
    active_delete = client.delete(f"{script_path}/{second_script.json()['id']}", headers=creator_headers)
    assert active_delete.status_code == 409


def test_asset_derivative_hierarchy_and_copy_preservation(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    project_assets_path = f"/api/v1/projects/{project_id}/assets"

    base = client.post(
        project_assets_path,
        headers=creator_headers,
        json={"asset_type": "character", "name": "谱系测试角色", "description": "基础形象"},
    )
    assert base.status_code == 201
    base_asset = base.json()
    alternate_base = client.post(
        project_assets_path,
        headers=creator_headers,
        json={"asset_type": "character", "name": "谱系测试角色 B"},
    ).json()
    derivative = client.post(
        project_assets_path,
        headers=creator_headers,
        json={
            "asset_type": "character",
            "parent_asset_id": base_asset["id"],
            "name": "谱系测试角色·雨夜造型",
        },
    )
    assert derivative.status_code == 201
    derivative_asset = derivative.json()
    assert derivative_asset["parent_asset_id"] == base_asset["id"]
    second_derivative = client.post(
        project_assets_path,
        headers=creator_headers,
        json={
            "asset_type": "character",
            "parent_asset_id": base_asset["id"],
            "name": "谱系测试角色·礼服造型",
        },
    ).json()

    wrong_type = client.post(
        project_assets_path,
        headers=creator_headers,
        json={
            "asset_type": "scene",
            "parent_asset_id": base_asset["id"],
            "name": "错误类型衍生",
        },
    )
    assert wrong_type.status_code == 422
    nested = client.post(
        project_assets_path,
        headers=creator_headers,
        json={
            "asset_type": "character",
            "parent_asset_id": derivative_asset["id"],
            "name": "二级衍生",
        },
    )
    assert nested.status_code == 422
    material = client.post(
        project_assets_path,
        headers=creator_headers,
        json={"asset_type": "material", "name": "谱系测试素材"},
    ).json()
    unsupported = client.post(
        project_assets_path,
        headers=creator_headers,
        json={
            "asset_type": "material",
            "parent_asset_id": material["id"],
            "name": "素材衍生",
        },
    )
    assert unsupported.status_code == 422
    cannot_demote_parent = client.patch(
        f"/api/v1/assets/{base_asset['id']}",
        headers=creator_headers,
        json={"parent_asset_id": alternate_base["id"]},
    )
    assert cannot_demote_parent.status_code == 409
    assert client.delete(f"/api/v1/assets/{base_asset['id']}", headers=creator_headers).status_code == 409

    exported = client.post(
        f"{project_assets_path}/{derivative_asset['id']}/export-global",
        headers=creator_headers,
    )
    assert exported.status_code == 200
    exported_derivative = exported.json()
    assert exported_derivative["parent_asset_id"]
    exported_second = client.post(
        f"{project_assets_path}/{second_derivative['id']}/export-global",
        headers=creator_headers,
    )
    assert exported_second.status_code == 200
    assert exported_second.json()["parent_asset_id"] == exported_derivative["parent_asset_id"]
    global_assets = {
        item["id"]: item for item in client.get("/api/v1/assets", headers=creator_headers).json()
    }
    assert global_assets[exported_derivative["parent_asset_id"]]["name"] == base_asset["name"]

    imported = client.post(
        f"{project_assets_path}/import/{exported_derivative['id']}",
        headers=creator_headers,
    )
    assert imported.status_code == 200
    assert imported.json()["parent_asset_id"] == base_asset["id"]

    moved = client.patch(
        f"/api/v1/assets/{derivative_asset['id']}",
        headers=creator_headers,
        json={"parent_asset_id": alternate_base["id"]},
    )
    assert moved.status_code == 200
    assert moved.json()["parent_asset_id"] == alternate_base["id"]
    self_parent = client.patch(
        f"/api/v1/assets/{moved.json()['id']}",
        headers=creator_headers,
        json={"parent_asset_id": moved.json()["id"]},
    )
    assert self_parent.status_code == 422


def test_asset_revision_history_is_immutable_and_restore_creates_new_version(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    created = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={
            "asset_type": "prop",
            "name": "版本测试相机",
            "description": "初始银色机身",
        },
    )
    assert created.status_code == 201
    asset = created.json()
    history_path = f"/api/v1/assets/{asset['id']}/revisions"

    initial_history = client.get(history_path, headers=creator_headers)
    assert initial_history.status_code == 200
    initial_revision = initial_history.json()[0]
    assert initial_revision["version"] == 1
    assert initial_revision["change_type"] == "manual_create"
    assert initial_revision["name"] == "版本测试相机"
    assert initial_revision["generation_prompt"] == ""

    updated = client.patch(
        f"/api/v1/assets/{asset['id']}",
        headers=creator_headers,
        json={
            "name": "版本测试相机·修订",
            "description": "黑色磨砂机身",
            "generation_prompt": "黑色磨砂胶片相机，产品设定图",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["status"] == "prompt_ready"

    history = client.get(history_path, headers=creator_headers).json()
    assert [revision["version"] for revision in history] == [2, 1]
    assert history[0]["change_type"] == "manual_update"
    assert history[1] == initial_revision

    restored = client.post(
        f"{history_path}/{initial_revision['id']}/restore",
        headers=creator_headers,
    )
    assert restored.status_code == 200
    assert restored.json()["version"] == 3
    assert restored.json()["name"] == "版本测试相机"
    assert restored.json()["description"] == "初始银色机身"
    assert restored.json()["generation_prompt"] == ""
    assert restored.json()["status"] == "extracted"

    restored_history = client.get(history_path, headers=creator_headers).json()
    assert [revision["version"] for revision in restored_history] == [3, 2, 1]
    assert restored_history[0]["change_type"] == "restore"
    assert restored_history[0]["source_revision_id"] == initial_revision["id"]
    assert restored_history[1]["name"] == "版本测试相机·修订"
    assert restored_history[2] == initial_revision


def test_asset_image_upload_creates_history_and_prompt_edit_keeps_ready_status(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    created = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={
            "asset_type": "character",
            "name": "上传测试角色",
            "description": "用于验证用户上传资产图",
        },
    )
    assert created.status_code == 201
    asset = created.json()

    image = BytesIO()
    Image.new("RGB", (640, 640), "#245953").save(image, format="PNG")
    uploaded = client.post(
        f"/api/v1/assets/{asset['id']}/image/upload",
        headers=creator_headers,
        files={"file": ("character.png", image.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "ready"
    assert uploaded.json()["version"] == 2
    assert "/assets/" in uploaded.json()["media_url"]
    assert uploaded.json()["asset_metadata"]["image_source"] == "user_upload"

    history_path = f"/api/v1/assets/{asset['id']}/revisions"
    history = client.get(history_path, headers=creator_headers).json()
    assert [item["change_type"] for item in history] == ["image_upload", "manual_create"]
    assert history[0]["media_url"] == uploaded.json()["media_url"]

    prompt_updated = client.patch(
        f"/api/v1/assets/{asset['id']}",
        headers=creator_headers,
        json={"generation_prompt": "电影人物四视图，统一造型与材质"},
    )
    assert prompt_updated.status_code == 200
    assert prompt_updated.json()["status"] == "ready"
    assert prompt_updated.json()["media_url"] == uploaded.json()["media_url"]


def test_script_review_decisions_are_audited_and_approval_can_activate(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "script",
            "source_name": "审核流程测试",
            "pasted_text": "第一集 失控的相机\n林遥发现旧相机正在自行回卷。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    scripts_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts"
    script = client.post(
        scripts_path,
        headers=creator_headers,
        json={
            "title": "第一集待审稿",
            "content": "内景，记忆修复室，夜。旧相机突然自行回卷。",
            "status": "reviewing",
            "activate": False,
        },
    )
    assert script.status_code == 201
    script_id = script.json()["id"]
    reviews_path = f"{scripts_path}/{script_id}/reviews"

    missing_notes = client.post(
        reviews_path,
        headers=creator_headers,
        json={"decision": "changes_requested", "notes": "", "activate": False},
    )
    assert missing_notes.status_code == 422

    requested = client.post(
        reviews_path,
        headers=creator_headers,
        json={
            "decision": "changes_requested",
            "notes": "第二场的冲突升级不足，请补充人物行动。",
            "activate": False,
        },
    )
    assert requested.status_code == 201
    assert requested.json()["review"]["decision"] == "changes_requested"
    assert requested.json()["review"]["activated"] is False
    assert requested.json()["review"]["reviewer_name"]
    assert requested.json()["script"]["status"] == "draft"

    approved = client.post(
        reviews_path,
        headers=creator_headers,
        json={
            "decision": "approved",
            "notes": "冲突节奏已修正，通过并进入资产生产。",
            "activate": True,
        },
    )
    assert approved.status_code == 201
    assert approved.json()["review"]["decision"] == "approved"
    assert approved.json()["review"]["activated"] is True
    assert approved.json()["script"]["status"] == "approved"
    assert approved.json()["script"]["is_active"] is True

    history = client.get(reviews_path, headers=creator_headers)
    assert history.status_code == 200
    assert {item["decision"] for item in history.json()} == {"approved", "changes_requested"}
    rejected_active = client.post(
        reviews_path,
        headers=creator_headers,
        json={
            "decision": "changes_requested",
            "notes": "已生效版本不能直接退回。",
            "activate": False,
        },
    )
    assert rejected_active.status_code == 409


def test_director_workflow_auto_reviews_script_and_extracts_assets(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-director-workflow-key"},
    ).status_code == 200
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "导演自动流程测试",
            "pasted_text": "第一章 雨夜回卷\n林遥推开修复室的门，桌上的旧相机突然自行回卷。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/director-workflow"
    runtime = FakeDirectorOrchestrationRuntime()

    started = client.post(path, headers=creator_headers, json={"instruction": "强化开场悬念"})
    assert started.status_code == 202
    detail = started.json()
    assert detail["workflow"]["stage"] == "script_adapting"
    assert len(detail["child_runs"]) == 1
    assert client.post(path, headers=creator_headers, json={}).status_code == 409

    assert asyncio.run(
        process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime)
    ) is True
    detail = client.get(path, headers=creator_headers).json()
    assert detail["workflow"]["stage"] == "script_reviewing"
    assert detail["child_runs"][-1]["kind"] == "script_review"

    assert asyncio.run(
        process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime)
    ) is True
    detail = client.get(path, headers=creator_headers).json()
    assert detail["workflow"]["stage"] == "asset_extracting"
    assert detail["child_runs"][-1]["kind"] == "asset_extraction"

    assert asyncio.run(
        process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime)
    ) is True
    detail = client.get(path, headers=creator_headers).json()
    assert detail["workflow"]["stage"] == "ready_for_asset_images"
    assert detail["workflow"]["status"] == "waiting_user"
    assert detail["pending_decision"] is None
    assert all(item["status"] == "succeeded" for item in detail["child_runs"])

    scripts = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
    ).json()
    assert scripts[0]["is_active"] is True
    assert scripts[0]["status"] == "approved"
    assert client.get(
        f"/api/v1/projects/{project_id}/assets", headers=creator_headers
    ).json()


def test_director_storyboard_start_continues_ready_asset_workflow(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-director-storyboard-start-key"},
    ).status_code == 200
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "分镜启动流程测试",
            "pasted_text": "第一章 雨夜回卷\n林遥推开修复室的门，桌上的旧相机突然自行回卷。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/director-workflow"
    runtime = FakeDirectorOrchestrationRuntime()

    started = client.post(path, headers=creator_headers, json={"instruction": "生成适合短视频的剧本"})
    detail = started.json()
    assert asyncio.run(process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime))
    detail = client.get(path, headers=creator_headers).json()
    assert asyncio.run(process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime))
    detail = client.get(path, headers=creator_headers).json()
    assert asyncio.run(process_task(detail["workflow"]["current_task_id"], runtime_factory=lambda: runtime))
    detail = client.get(path, headers=creator_headers).json()
    assert detail["workflow"]["stage"] == "ready_for_asset_images"

    assets = client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
    asset_ids = [item["id"] for item in assets]

    async def mark_assets_ready() -> None:
        async with SessionLocal() as session:
            for asset_id in asset_ids:
                key = f"test/director-storyboard-start/{asset_id}.webp"
                await object_storage().put_bytes(key, b"ready-storyboard-start-image", "image/webp")
                asset = await session.get(Asset, asset_id)
                assert asset is not None
                asset.status = AssetStatus.READY
                asset.media_url = f"/uploads/{key}"
                asset.generation_prompt = asset.generation_prompt or "电影级角色/场景设定图"
                asset.version += 1
            await session.commit()

    asyncio.run(mark_assets_ready())

    resumed = client.post(f"{path}/storyboard", headers=creator_headers)
    assert resumed.status_code == 202
    resumed_detail = resumed.json()
    assert resumed_detail["workflow"]["stage"] == "storyboard_generating"
    assert resumed_detail["workflow"]["status"] == "running"
    assert resumed_detail["child_runs"][-1]["kind"] == "storyboard_generation"
    assert resumed_detail["child_runs"][-1]["status"] == "queued"


def test_chapter_analysis_and_ai_script_are_persistent_reviewable_tasks(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/providers/{provider_id}",
            headers=admin_headers,
            json={"api_key": "test-director-ai-key"},
        ).status_code
        == 200
    )
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "章节分析测试",
            "pasted_text": "第一章 雨夜回卷\n林遥推开修复室的门，桌上的旧相机突然自行回卷。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    analysis_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/analyses"
    runtime = FakeWorkflowRuntime()

    analysis_task = client.post(f"{analysis_path}/generate", headers=creator_headers)
    assert analysis_task.status_code == 202
    assert Decimal(analysis_task.json()["cost"]) == Decimal("5.00")
    assert client.post(f"{analysis_path}/generate", headers=creator_headers).status_code == 409
    assert asyncio.run(process_task(analysis_task.json()["id"], runtime_factory=lambda: runtime)) is True
    completed_analysis = client.get(
        f"/api/v1/tasks/{analysis_task.json()['id']}", headers=creator_headers
    ).json()
    assert completed_analysis["status"] == "succeeded"
    analyses = client.get(analysis_path, headers=creator_headers).json()
    assert analyses[0]["version"] == 1
    assert analyses[0]["content"]["events"][1]["title"] == "相机异动"
    chapter = next(
        item
        for item in client.get(f"/api/v1/projects/{project_id}/chapters", headers=creator_headers).json()
        if item["id"] == chapter_id
    )
    assert chapter["status"] == "analyzed"

    script_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts"
    script_task = client.post(
        f"{script_path}/generate",
        headers=creator_headers,
        json={"analysis_id": analyses[0]["id"]},
    )
    assert script_task.status_code == 202
    assert Decimal(script_task.json()["cost"]) == Decimal("10.00")
    assert (
        client.post(
            f"{script_path}/generate",
            headers=creator_headers,
            json={"analysis_id": analyses[0]["id"]},
        ).status_code
        == 409
    )
    assert asyncio.run(process_task(script_task.json()["id"], runtime_factory=lambda: runtime)) is True
    scripts = client.get(script_path, headers=creator_headers).json()
    assert scripts[0]["title"] == "第一集 · 雨夜回卷"
    assert scripts[0]["status"] == "reviewing"
    assert scripts[0]["is_active"] is False
    assert "相机异动" in scripts[0]["review_notes"]

    cancelled = client.post(f"{analysis_path}/generate", headers=creator_headers).json()
    assert client.post(f"/api/v1/tasks/{cancelled['id']}/cancel", headers=creator_headers).status_code == 200
    chapter = next(
        item
        for item in client.get(f"/api/v1/projects/{project_id}/chapters", headers=creator_headers).json()
        if item["id"] == chapter_id
    )
    assert chapter["status"] == "reviewing"

    stale_task = client.post(f"{analysis_path}/generate", headers=creator_headers).json()

    async def mutate_chapter_source() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(Chapter)
                .where(Chapter.id == chapter_id)
                .values(original_content="原文已由上游同步为新版本。")
            )
            await session.commit()

    asyncio.run(mutate_chapter_source())
    assert asyncio.run(process_task(stale_task["id"], runtime_factory=lambda: runtime)) is True
    failed = client.get(f"/api/v1/tasks/{stale_task['id']}", headers=creator_headers).json()
    assert failed["status"] == "failed"
    assert failed["result_payload"]["credit_refunded"] is True
    assert client.post(f"/api/v1/tasks/{stale_task['id']}/retry", headers=creator_headers).status_code == 409

    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert any(
        item["kind"] == "analysis" and item["file_metadata"].get("analysis_id") == analyses[0]["id"]
        for item in project_files
    )
    assert any(
        item["kind"] == "script" and item["file_metadata"].get("script_version_id") == scripts[0]["id"]
        for item in project_files
    )


def test_asset_ai_pipeline_extracts_prompts_and_generates_persistent_images(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    configured = client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-asset-pipeline-key"},
    )
    assert configured.status_code == 200

    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "资产流水线测试",
            "pasted_text": "第一章 雨夜\n林遥在记忆修复室看见了本应离开的周屿。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集生效稿",
            "content": "内景，记忆修复室，夜。林遥穿着深色风衣推门而入。",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201

    runtime = FakeWorkflowRuntime()
    extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions/generate",
        headers=creator_headers,
    )
    assert extraction.status_code == 202
    assert extraction.json()["task_type"] == "chapter_asset_extraction"
    duplicate_extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions/generate",
        headers=creator_headers,
    )
    assert duplicate_extraction.status_code == 409
    assert asyncio.run(process_task(extraction.json()["id"], runtime_factory=lambda: runtime)) is True
    extraction_task = client.get(f"/api/v1/tasks/{extraction.json()['id']}", headers=creator_headers).json()
    assert extraction_task["status"] == "succeeded"
    asset_ids = extraction_task["result_payload"]["asset_ids"]
    assert extraction_task["result_payload"]["asset_count"] == 3

    assets = client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
    pipeline_assets = {item["id"]: item for item in assets if item["id"] in asset_ids}
    assert len(pipeline_assets) == 3
    derivative = next(item for item in pipeline_assets.values() if item["parent_asset_id"])
    parent = pipeline_assets[derivative["parent_asset_id"]]
    assert parent["name"] == "林遥"
    assert all(item["status"] == "extracted" for item in pipeline_assets.values())
    extracted_history = client.get(
        f"/api/v1/assets/{asset_ids[0]}/revisions",
        headers=creator_headers,
    ).json()
    assert extracted_history[0]["change_type"] == "ai_extraction"
    assert extracted_history[0]["source_task_id"] == extraction.json()["id"]

    prompt_task = client.post(
        f"/api/v1/projects/{project_id}/assets/prompts/generate",
        headers=creator_headers,
        json={"asset_ids": asset_ids},
    )
    assert prompt_task.status_code == 202
    assert Decimal(prompt_task.json()["cost"]) == Decimal("6.00")
    duplicate_prompt = client.post(
        f"/api/v1/projects/{project_id}/assets/prompts/generate",
        headers=creator_headers,
        json={"asset_ids": asset_ids},
    )
    assert duplicate_prompt.status_code == 409
    assert asyncio.run(process_task(prompt_task.json()["id"], runtime_factory=lambda: runtime)) is True
    assets = client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
    prompted = {item["id"]: item for item in assets if item["id"] in asset_ids}
    assert all(item["status"] == "prompt_ready" for item in prompted.values())
    assert all(item["generation_prompt"] for item in prompted.values())
    prompted_history = client.get(
        f"/api/v1/assets/{asset_ids[0]}/revisions",
        headers=creator_headers,
    ).json()
    assert [item["change_type"] for item in prompted_history] == [
        "ai_prompt_generation",
        "ai_extraction",
    ]
    assert prompted_history[0]["source_task_id"] == prompt_task.json()["id"]

    audio_asset = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={
            "asset_type": "audio",
            "name": "雨夜环境音",
            "description": "持续雨声与远处低沉雷声",
            "generation_prompt": "雨夜环境音频",
        },
    )
    assert audio_asset.status_code == 201
    rejected_audio_image = client.post(
        f"/api/v1/projects/{project_id}/assets/images/generate",
        headers=creator_headers,
        json={"asset_ids": [audio_asset.json()["id"]]},
    )
    assert rejected_audio_image.status_code == 409

    image_tasks = client.post(
        f"/api/v1/projects/{project_id}/assets/images/generate",
        headers=creator_headers,
        json={"asset_ids": asset_ids},
    )
    assert image_tasks.status_code == 202
    assert len(image_tasks.json()) == 3
    duplicate_images = client.post(
        f"/api/v1/projects/{project_id}/assets/images/generate",
        headers=creator_headers,
        json={"asset_ids": [asset_ids[0]]},
    )
    assert duplicate_images.status_code == 409
    for task in image_tasks.json():
        assert (
            asyncio.run(
                process_task(
                    task["id"],
                    gateway_factory=lambda _provider: FakeAssetImageGateway(),
                )
            )
            is True
        )
    assets = client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
    generated = {item["id"]: item for item in assets if item["id"] in asset_ids}
    assert all(item["status"] == "ready" for item in generated.values())
    assert all("/assets/" in item["media_url"] for item in generated.values())
    image_history = client.get(
        f"/api/v1/assets/{asset_ids[0]}/revisions",
        headers=creator_headers,
    ).json()
    assert [item["change_type"] for item in image_history] == [
        "image_generation",
        "ai_prompt_generation",
        "ai_extraction",
    ]
    assert image_history[0]["source_task_id"] in {task["id"] for task in image_tasks.json()}

    failed_image = client.post(
        f"/api/v1/projects/{project_id}/assets/images/generate",
        headers=creator_headers,
        json={"asset_ids": [asset_ids[0]]},
    ).json()[0]
    assert (
        asyncio.run(
            process_task(
                failed_image["id"],
                gateway_factory=lambda _provider: FakeAssetImageGateway(fail=True),
            )
        )
        is True
    )
    failed_task = client.get(f"/api/v1/tasks/{failed_image['id']}", headers=creator_headers).json()
    assert failed_task["status"] == "failed"
    assert failed_task["result_payload"]["credit_refunded"] is True
    failed_asset = next(
        item
        for item in client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
        if item["id"] == asset_ids[0]
    )
    assert failed_asset["status"] == "ready"
    assert failed_asset["media_url"]
    retried = client.post(f"/api/v1/tasks/{failed_image['id']}/retry", headers=creator_headers)
    assert retried.status_code == 202
    assert (
        client.post(f"/api/v1/tasks/{failed_image['id']}/cancel", headers=creator_headers).status_code == 200
    )

    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert any(
        item["kind"] == "asset" and item["file_metadata"].get("source_task_id") == extraction.json()["id"]
        for item in project_files
    )
    assert len(runtime.requests) == 2


def test_storyboard_and_restart_safe_video_pipeline(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/providers/{provider_id}",
            headers=admin_headers,
            json={"api_key": "test-storyboard-key"},
        ).status_code
        == 200
    )
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "分镜流水线测试",
            "pasted_text": "第一章 雨夜重逢\n林遥进入记忆修复室，桌上放着一台旧相机。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集雨夜重逢",
            "content": "内景，记忆修复室，夜。林遥推门而入，看到桌上的旧相机。",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201
    extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "林遥", "description": "短发记忆修复师"},
                {"asset_type": "scene", "name": "记忆修复室", "description": "冷白设备光"},
                {"asset_type": "prop", "name": "旧相机", "description": "边角磨损的胶片相机"},
            ]
        },
    )
    assert extraction.status_code == 201
    queued_asset_prompt = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/generate",
        headers=creator_headers,
    )
    assert queued_asset_prompt.status_code == 202
    assert queued_asset_prompt.json()["task_type"] == "asset_prompt_generation"
    assert not queued_asset_prompt.json()["request_payload"].get("auto_queue_images_after_prompt")
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/generate",
            headers=creator_headers,
        ).status_code
        == 409
    )

    asset_ids = [item["id"] for item in extraction.json()["assets"]]
    prompt_runtime = FakeWorkflowRuntime()
    assert (
        asyncio.run(
            process_task(
                queued_asset_prompt.json()["id"],
                runtime_factory=lambda: prompt_runtime,
            )
        )
        is True
    )
    prompted = client.get(
        f"/api/v1/tasks/{queued_asset_prompt.json()['id']}",
        headers=creator_headers,
    ).json()
    assert prompted["result_payload"]["queued_image_task_ids"] == []

    workflow_id = queued_asset_prompt.json()["request_payload"]["workflow_id"]

    async def workflow_task_ids(task_type: str) -> list[str]:
        async with SessionLocal() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(AITask).where(
                            AITask.project_id == project_id,
                            AITask.task_type == task_type,
                            AITask.status == TaskStatus.QUEUED,
                        )
                    )
                ).all()
            )
            return [
                task.id
                for task in tasks
                if (task.request_payload or {}).get("workflow_id") == workflow_id
            ]

    image_task_ids = asyncio.run(workflow_task_ids("asset_image_generation"))
    assert len(image_task_ids) == len(asset_ids)
    for image_task_id in image_task_ids:
        assert (
            asyncio.run(
                process_task(
                    image_task_id,
                    gateway_factory=lambda _provider: FakeAssetImageGateway(),
                )
            )
            is True
        )

    async def current_storyboard_task_id() -> str:
        async with SessionLocal() as session:
            workflow = await session.scalar(
                select(DirectorWorkflowRun)
                .where(DirectorWorkflowRun.chapter_id == chapter_id)
                .order_by(DirectorWorkflowRun.created_at.desc())
                .limit(1)
            )
            assert workflow is not None
            assert workflow.current_task_id
            return workflow.current_task_id

    storyboard_task_id = asyncio.run(current_storyboard_task_id())

    runtime = FakeWorkflowRuntime()
    storyboard_task = client.get(f"/api/v1/tasks/{storyboard_task_id}", headers=creator_headers)
    assert storyboard_task.status_code == 200
    assert storyboard_task.json()["task_type"] == "chapter_storyboard_generation"
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/generate",
            headers=creator_headers,
        ).status_code
        == 409
    )
    assert asyncio.run(process_task(storyboard_task.json()["id"], runtime_factory=lambda: runtime)) is True
    completed = client.get(f"/api/v1/tasks/{storyboard_task.json()['id']}", headers=creator_headers).json()
    assert completed["status"] == "succeeded"
    assert completed["result_payload"]["shot_count"] == 2
    storyboards = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards",
        headers=creator_headers,
    ).json()
    assert storyboards[0]["is_active"] is True
    detail_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboards[0]['id']}"
    detail = client.get(detail_path, headers=creator_headers).json()
    assert [shot["title"] for shot in detail["shots"]] == ["雨夜推门", "旧相机特写"]
    assert len(detail["shots"][0]["asset_ids"]) == 2

    shot = detail["shots"][0]
    video_path = f"{detail_path}/shots/{shot['id']}/videos/generate"
    video_task = client.post(video_path, headers=creator_headers)
    assert video_task.status_code == 202
    assert client.post(video_path, headers=creator_headers).status_code == 409

    async def mark_provider_job_for_recovery() -> None:
        async with SessionLocal() as session:
            task = await session.get(AITask, video_task.json()["id"])
            assert task is not None
            task.result_payload = {"credit_refunded": False, "provider_job_id": "restored-job-9"}
            await session.commit()

    asyncio.run(mark_provider_job_for_recovery())
    gateway = FakeVideoGateway(pending=True)
    assert (
        asyncio.run(process_task(video_task.json()["id"], gateway_factory=lambda _provider: gateway)) is True
    )
    assert gateway.submit_calls == 0
    assert gateway.poll_calls == 1
    finished_video = client.get(f"/api/v1/tasks/{video_task.json()['id']}", headers=creator_headers).json()
    assert finished_video["status"] == "succeeded"
    assert finished_video["result_payload"]["provider_job_id"] == "restored-job-9"
    refreshed = client.get(detail_path, headers=creator_headers).json()
    assert refreshed["video_clips"][0]["status"] == "ready"
    assert refreshed["video_clips"][0]["is_active"] is True
    assert refreshed["video_clips"][0]["media_url"].endswith(".mp4")

    edited = client.patch(
        f"{detail_path}/shots/{shot['id']}",
        headers=creator_headers,
        json={"video_prompt": "更新后的推镜与停步动作"},
    )
    assert edited.status_code == 200
    invalidated = client.get(detail_path, headers=creator_headers).json()["video_clips"][0]
    assert invalidated["is_active"] is False
    assert "镜头内容已更新" in invalidated["invalidated_reason"]

    failed_task = client.post(video_path, headers=creator_headers)
    assert failed_task.status_code == 202
    assert (
        asyncio.run(
            process_task(
                failed_task.json()["id"],
                gateway_factory=lambda _provider: FakeVideoGateway(fail=True),
            )
        )
        is True
    )
    failed = client.get(f"/api/v1/tasks/{failed_task.json()['id']}", headers=creator_headers).json()
    assert failed["status"] == "failed"
    assert failed["result_payload"]["credit_refunded"] is True
    assert failed["result_payload"]["provider_job_terminal"] is True
    retried_terminal = client.post(
        f"/api/v1/tasks/{failed_task.json()['id']}/retry",
        headers=creator_headers,
    )
    assert retried_terminal.status_code == 202
    assert "provider_job_id" not in (retried_terminal.json()["result_payload"] or {})
    assert (
        client.post(f"/api/v1/tasks/{failed_task.json()['id']}/cancel", headers=creator_headers).status_code
        == 200
    )

    resumable_task = client.post(video_path, headers=creator_headers)
    assert resumable_task.status_code == 202

    async def mark_resumable_provider_job() -> None:
        async with SessionLocal() as session:
            task = await session.get(AITask, resumable_task.json()["id"])
            assert task is not None
            task.result_payload = {"credit_refunded": False, "provider_job_id": "resume-video-42"}
            await session.commit()

    asyncio.run(mark_resumable_provider_job())
    failed_poll_gateway = FakeVideoGateway(poll_error=True)
    assert (
        asyncio.run(
            process_task(
                resumable_task.json()["id"],
                gateway_factory=lambda _provider: failed_poll_gateway,
            )
        )
        is True
    )
    interrupted = client.get(
        f"/api/v1/tasks/{resumable_task.json()['id']}",
        headers=creator_headers,
    ).json()
    assert interrupted["status"] == "failed"
    assert interrupted["result_payload"]["resume_provider_job"] is True
    resumed = client.post(
        f"/api/v1/tasks/{resumable_task.json()['id']}/retry",
        headers=creator_headers,
    )
    assert resumed.status_code == 202
    assert resumed.json()["result_payload"]["provider_job_id"] == "resume-video-42"
    resumed_gateway = FakeVideoGateway()
    assert (
        asyncio.run(
            process_task(
                resumable_task.json()["id"],
                gateway_factory=lambda _provider: resumed_gateway,
            )
        )
        is True
    )
    assert resumed_gateway.submit_calls == 0
    assert resumed_gateway.poll_calls == 1

    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert any(item["kind"] == "storyboard" for item in project_files)
    assert any(item["kind"] == "video" for item in project_files)


def test_storyboard_batch_video_prompt_and_video_queue(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-batch-video-key"},
    ).status_code == 200
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "批量视频测试",
            "pasted_text": "第一章 海边追逐\n林遥追着旧相机的幻影跑向海边。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集海边追逐",
            "content": "外景，海边，黄昏。林遥追着旧相机的幻影奔跑。",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201
    extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "林遥", "description": "短发记忆修复师"},
                {"asset_type": "scene", "name": "黄昏海边", "description": "金色潮水与远处礁石"},
            ]
        },
    )
    assert extraction.status_code == 201

    async def mark_assets_ready() -> None:
        async with SessionLocal() as session:
            for asset in extraction.json()["assets"]:
                key = f"test/batch-video-assets/{asset['id']}.webp"
                await object_storage().put_bytes(key, b"ready-batch-video-image", "image/webp")
                row = await session.get(Asset, asset["id"])
                assert row is not None
                row.status = AssetStatus.READY
                row.media_url = f"/uploads/{key}"
                row.version += 1
            await session.commit()

    asyncio.run(mark_assets_ready())

    storyboard = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards",
        headers=creator_headers,
        json={
            "shots": [
                {
                    "title": "海边奔跑",
                    "shot_type": "中景",
                    "duration_seconds": 5,
                    "scene_description": "黄昏海边，潮水反光",
                    "action_description": "林遥沿潮线奔跑",
                    "image_prompt": "黄昏海边中景",
                    "video_prompt": "",
                },
                {
                    "title": "相机幻影",
                    "shot_type": "特写",
                    "duration_seconds": 4,
                    "scene_description": "沙滩上的旧相机幻影",
                    "action_description": "相机幻影闪烁后消散",
                    "image_prompt": "旧相机幻影特写",
                    "video_prompt": "",
                },
            ]
        },
    )
    assert storyboard.status_code == 201
    assert [shot["duration_seconds"] for shot in storyboard.json()["shots"]] == ["5.00", "5.00"]
    storyboard_id = storyboard.json()["version"]["id"]
    prompt_task = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-prompts/generate",
        headers=creator_headers,
        json={"shot_ids": [], "overwrite": False},
    )
    assert prompt_task.status_code == 202
    assert prompt_task.json()["task_type"] == "shot_video_prompt_generation"
    assert len(prompt_task.json()["request_payload"]["shot_ids"]) == 2
    runtime = FakeWorkflowRuntime()
    assert asyncio.run(process_task(prompt_task.json()["id"], runtime_factory=lambda: runtime)) is True
    prompted_storyboard = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}",
        headers=creator_headers,
    ).json()
    assert all(shot["video_prompt"] for shot in prompted_storyboard["shots"])

    video_tasks = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/videos/generate",
        headers=creator_headers,
        json={"shot_ids": [], "only_missing": True},
    )
    assert video_tasks.status_code == 202
    assert len(video_tasks.json()) == 2
    assert {task["task_type"] for task in video_tasks.json()} == {"shot_video_generation"}
    busy_shot = prompted_storyboard["shots"][0]
    prompt_conflict = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-prompts/generate",
        headers=creator_headers,
        json={"shot_ids": [busy_shot["id"]], "overwrite": True},
    )
    assert prompt_conflict.status_code == 409
    assert "正在生成视频" in prompt_conflict.json()["detail"]
    edit_conflict = client.patch(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/shots/{busy_shot['id']}",
        headers=creator_headers,
        json={"title": "不应覆盖生成中的镜头"},
    )
    assert edit_conflict.status_code == 409
    assert "正在生成视频" in edit_conflict.json()["detail"]


def test_dialogue_voice_binding_and_restart_safe_tts_pipeline(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/providers/{provider_id}",
            headers=admin_headers,
            json={"api_key": "test-tts-key"},
        ).status_code
        == 200
    )
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "script",
            "source_name": "配音流水线测试",
            "pasted_text": "第一章 雨夜\n林遥：谁动过它？",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集雨夜",
            "content": "内景，记忆修复室，夜。\n林遥：谁动过它？",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201
    character = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={
            "asset_type": "character",
            "name": "林遥",
            "description": "克制冷静的记忆修复师",
        },
    )
    assert character.status_code == 201

    runtime = FakeWorkflowRuntime()
    dialogue_task = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/dialogues/generate",
        headers=creator_headers,
    )
    assert dialogue_task.status_code == 202
    assert asyncio.run(process_task(dialogue_task.json()["id"], runtime_factory=lambda: runtime)) is True
    dialogues = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/dialogues",
        headers=creator_headers,
    ).json()
    assert dialogues[0]["is_active"] is True
    dialogue_id = dialogues[0]["id"]
    detail_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/dialogues/{dialogue_id}"
    detail = client.get(detail_path, headers=creator_headers).json()
    assert detail["lines"][0]["speaker"] == "林遥"
    assert detail["lines"][0]["emotion"] == "警惕"

    options = client.get(
        f"/api/v1/projects/{project_id}/dubbing/options",
        headers=creator_headers,
    ).json()
    assert options["tts_models"]
    binding = client.put(
        f"/api/v1/projects/{project_id}/dubbing/voice-bindings",
        headers=creator_headers,
        json={
            "character_asset_id": character.json()["id"],
            "tts_model_id": options["tts_models"][0]["id"],
            "provider_voice_id": "voice-linyao",
            "provider_voice_name": "林遥 · 克制女声",
            "style": "电影对白",
            "instructions": "中低音区，保持克制",
        },
    )
    assert binding.status_code == 200
    audio_tasks = client.post(
        f"{detail_path}/audio/generate",
        headers=creator_headers,
        json={"dialogue_line_ids": [detail["lines"][0]["id"]]},
    )
    assert audio_tasks.status_code == 202
    audio_task_id = audio_tasks.json()[0]["id"]

    async def mark_tts_job_for_recovery() -> None:
        async with SessionLocal() as session:
            task = await session.get(AITask, audio_task_id)
            assert task is not None
            task.result_payload = {"credit_refunded": False, "provider_job_id": "restored-tts-9"}
            await session.commit()

    asyncio.run(mark_tts_job_for_recovery())
    gateway = FakeTTSGateway(pending=True)
    assert asyncio.run(process_task(audio_task_id, gateway_factory=lambda _provider: gateway)) is True
    assert gateway.submit_calls == 0
    assert gateway.poll_calls == 1
    refreshed = client.get(detail_path, headers=creator_headers).json()
    assert refreshed["audio_clips"][0]["status"] == "ready"
    assert refreshed["audio_clips"][0]["is_active"] is True
    assert refreshed["audio_clips"][0]["media_url"].endswith(".mp3")

    updated_line = client.patch(
        f"{detail_path}/lines/{detail['lines'][0]['id']}",
        headers=creator_headers,
        json={"text": "到底是谁动过它？", "emotion": "压抑的愤怒"},
    )
    assert updated_line.status_code == 200
    invalidated = client.get(detail_path, headers=creator_headers).json()["audio_clips"][0]
    assert invalidated["is_active"] is False
    assert "台词已更新" in invalidated["invalidated_reason"]
    project_files = client.get(
        f"/api/v1/projects/{project_id}/files",
        headers=creator_headers,
    ).json()
    assert any(item["kind"] == "audio" for item in project_files)


def test_chapter_composition_is_versioned_restart_safe_and_invalidated(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "script",
            "source_name": "章节成片测试",
            "pasted_text": "第一章 雨夜成片\n林遥推门进入修复室。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集雨夜成片",
            "content": "内景，记忆修复室，夜。林遥推门。",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201
    extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "林遥", "description": "记忆修复师"},
                {"asset_type": "scene", "name": "修复室", "description": "雨夜冷白灯光"},
            ]
        },
    )
    assert extraction.status_code == 201
    asset_ids = [item["id"] for item in extraction.json()["assets"]]

    async def mark_assets_ready() -> None:
        async with SessionLocal() as session:
            for asset_id in asset_ids:
                key = f"test/composition-assets/{asset_id}.webp"
                await object_storage().put_bytes(key, b"ready-composition-asset-image", "image/webp")
                asset = await session.get(Asset, asset_id)
                assert asset is not None
                asset.status = AssetStatus.READY
                asset.media_url = f"/uploads/{key}"
                asset.version += 1
            await session.commit()

    asyncio.run(mark_assets_ready())
    storyboard = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards",
        headers=creator_headers,
        json={
            "shots": [
                {
                    "title": "推门",
                    "shot_type": "中景",
                    "duration_seconds": 3,
                    "scene_description": "雨夜修复室入口",
                    "action_description": "林遥推门",
                    "dialogue": "",
                    "image_prompt": "雨夜入口",
                    "video_prompt": "缓慢推镜",
                    "asset_ids": [],
                },
                {
                    "title": "停步",
                    "shot_type": "近景",
                    "duration_seconds": 2,
                    "scene_description": "冷白灯下",
                    "action_description": "林遥停步",
                    "dialogue": "",
                    "image_prompt": "冷白灯近景",
                    "video_prompt": "人物停步",
                    "asset_ids": [],
                },
            ]
        },
    )
    assert storyboard.status_code == 201
    detail = storyboard.json()
    detail_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{detail['version']['id']}"
    for shot in detail["shots"]:
        task = client.post(
            f"{detail_path}/shots/{shot['id']}/videos/generate",
            headers=creator_headers,
        )
        assert task.status_code == 202
        assert (
            asyncio.run(
                process_task(
                    task.json()["id"],
                    gateway_factory=lambda _provider: FakeVideoGateway(),
                )
            )
            is True
        )

    uploaded_track = client.post(
        f"/api/v1/projects/{project_id}/finishing/audio",
        headers=creator_headers,
        files={"file": ("rain-bed.mp3", b"test-audio-track", "audio/mpeg")},
    )
    assert uploaded_track.status_code == 201
    options = client.get(
        f"/api/v1/projects/{project_id}/finishing/options",
        headers=creator_headers,
    ).json()
    assert options["audio_files"][0]["id"] == uploaded_track.json()["id"]

    composition_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/compositions"
    prepared = client.post(
        composition_path,
        headers=creator_headers,
        json={
            "title": "雨夜成片交付版",
            "background_music_file_id": uploaded_track.json()["id"],
            "background_music_volume": 0.2,
            "fps": 24,
        },
    )
    assert prepared.status_code == 201
    composition = prepared.json()
    assert composition["status"] == "draft"
    assert composition["duration_seconds"] == "10.00"
    assert len(composition["timeline_manifest"]["shots"]) == 2
    assert composition["timeline_manifest"]["background_music"]["name"] == "rain-bed.mp3"

    render_task = client.post(
        f"{composition_path}/{composition['id']}/render",
        headers=creator_headers,
    )
    assert render_task.status_code == 202
    assert Decimal(render_task.json()["cost"]) == Decimal("0")
    renderer = FakeCompositionRenderer()
    assert (
        asyncio.run(
            process_task(
                render_task.json()["id"],
                renderer_factory=lambda: renderer,
            )
        )
        is True
    )
    assert len(renderer.manifests) == 1
    finished = client.get(f"{composition_path}/{composition['id']}", headers=creator_headers).json()
    assert finished["status"] == "ready"
    assert finished["is_active"] is True
    assert finished["output_url"].endswith(".mp4")
    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert any(item["file_metadata"].get("role") == "chapter_render" for item in project_files)

    edited = client.patch(
        f"{detail_path}/shots/{detail['shots'][0]['id']}",
        headers=creator_headers,
        json={"video_prompt": "更新后的镜头运动"},
    )
    assert edited.status_code == 200
    stale = client.get(f"{composition_path}/{composition['id']}", headers=creator_headers).json()
    assert stale["status"] == "stale"
    assert stale["is_active"] is False


def test_agent_memory_is_upserted_in_user_project_scope(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    path = f"/api/v1/projects/{project_id}/agent-memories/story/character-bible"

    created = client.put(
        path,
        headers=creator_headers,
        json={"content": "The lead avoids direct confrontation.", "metadata": {"source": "scene-3"}},
    )
    updated = client.put(
        path,
        headers=creator_headers,
        json={"content": "The lead confronts danger only to protect family.", "metadata": {}},
    )
    memories = client.get(
        f"/api/v1/projects/{project_id}/agent-memories",
        headers=creator_headers,
        params={"namespace": "story"},
    )

    assert created.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["id"] == created.json()["id"]
    assert memories.status_code == 200
    assert len(memories.json()) == 1
    assert memories.json()[0]["content"].startswith("The lead confronts")


def test_agent_chat_uses_runtime_and_persists_history(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers)
    assert options.status_code == 200
    assert options.json()["agents"]
    assert "system_prompt" not in options.json()["agents"][0]

    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    configured = client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-key"},
    )
    assert configured.status_code == 200
    memory = client.put(
        f"/api/v1/projects/{project_id}/agent-memories/chat/runtime-context",
        headers=creator_headers,
        json={"content": "The project uses a restrained three-act structure.", "metadata": {}},
    )
    context_file = client.post(
        f"/api/v1/projects/{project_id}/files",
        headers=creator_headers,
        json={"name": "chat-context.md", "kind": "memory", "content": "# Runtime context"},
    )
    assert memory.status_code == 200
    assert context_file.status_code == 201

    agent_id = options.json()["agents"][0]["id"]
    created = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": agent_id},
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    published_streams: list[dict[str, object]] = []

    async def capture_stream(**payload) -> None:
        published_streams.append(payload)

    monkeypatch.setattr(task_worker, "publish_agent_stream_event", capture_stream)
    runtime = FakeStreamingAgentRuntime()
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "把这个项目整理成三幕故事骨架"},
    )

    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert sent.json()["task"]["status"] == "queued"
    queued_detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assert [item["role"] for item in queued_detail["messages"]] == ["user"]
    assert queued_detail["active_task"]["id"] == task_id
    assert runtime.requests == []

    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True
    assert runtime.requests[0].project_id == project_id
    assert runtime.requests[0].session_id == session_id
    assert runtime.requests[0].model_binding["api_key"] == "test-agent-key"
    assert runtime.requests[0].memory_context
    assert runtime.requests[0].contract_version == "v2"
    assert runtime.requests[0].project_files
    assert len(runtime.requests[0].skill_versions) == 2
    assert any("visual-handbooks" in path for path in runtime.requests[0].skill_versions)
    assert any("director-handbooks" in path for path in runtime.requests[0].skill_versions)
    assert len(runtime.requests[0].skills) == 15
    assert "项目创作手册（强制执行）" in runtime.requests[0].system_prompt
    assert "README.md, prefix.md" in runtime.requests[0].system_prompt
    assert "director-planning.md, storyboard-table.md" in runtime.requests[0].system_prompt
    main_task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert main_task["status"] == "succeeded"
    memory_task_id = main_task["result_payload"]["memory_maintenance_task_id"]
    immediate_detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assert immediate_detail["active_task"] is None
    assert immediate_detail["messages"][-1]["runtime_manifest"]["memory"]["state"] == "queued"
    visible_task_ids = {
        item["id"] for item in client.get("/api/v1/tasks", headers=creator_headers).json()["items"]
    }
    assert memory_task_id not in visible_task_ids
    assert asyncio.run(process_task(memory_task_id, runtime_factory=lambda: runtime)) is True
    assert runtime.requests[1].state_mode == "ephemeral"
    assert runtime.requests[1].task_id == memory_task_id
    assert runtime.stream_calls == 1
    assert {item["task_id"] for item in published_streams} == {task_id}
    assert {item["session_id"] for item in published_streams} == {session_id}
    assert (
        "".join(
            str(item["event"].get("delta") or "")
            for item in published_streams
            if item["event"].get("type") == "TEXT_BLOCK_DELTA"
        )
        == "我已读取项目上下文，开始整理故事骨架。"
    )
    assert any(item["event"].get("type") == "TOOL_CALL_START" for item in published_streams)
    tool_start = next(
        item["event"] for item in published_streams if item["event"].get("type") == "TOOL_CALL_START"
    )
    tool_end = next(
        item["event"] for item in published_streams if item["event"].get("type") == "TOOL_RESULT_END"
    )
    assert tool_start["_cineforge_step_id"] == tool_end["_cineforge_step_id"]
    assert tool_end["_cineforge_step_state"] == "succeeded"

    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    )
    assert detail.status_code == 200
    assert [item["role"] for item in detail.json()["messages"]] == ["user", "assistant"]
    assert detail.json()["active_task"] is None
    assert detail.json()["messages"][-1]["content"].startswith("我已读取项目上下文")
    assert "runtime_events" in detail.json()["messages"][-1]
    assert detail.json()["session"]["runtime_manifest"]["runtime_type"] == "agentscope"
    memory_manifest = detail.json()["session"]["runtime_manifest"]["memory"]
    assert memory_manifest["retrieved_count"] >= 1
    assert memory_manifest["extracted_count"] == 1
    assert memory_manifest["summary_version"] == 1
    assert memory_manifest["summary_fallback"] is False
    automatic_memories = client.get(
        f"/api/v1/projects/{project_id}/agent-memories",
        headers=creator_headers,
    ).json()
    extracted = next(item for item in automatic_memories if item["is_automatic"])
    assert extracted["embedding_model"] == "cineforge-hash-v1"
    assert extracted["source_session_id"] == session_id
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert task["status"] == "succeeded"
    assert task["result_payload"]["assistant_message_id"] == detail.json()["messages"][-1]["id"]
    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=creator_headers).json()
    assert [event["status"] for event in events][0:2] == ["queued", "running"]
    assert events[-1]["status"] == "succeeded"
    assert events[-1]["progress"] == 100

    hidden = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=admin_headers,
    )
    assert hidden.status_code == 404


def test_agent_chat_routes_agent_by_scene_and_ignores_client_agent_id(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    workspace_options = client.get(
        f"/api/v1/projects/{project_id}/agent/options",
        headers=creator_headers,
        params={"scene": "workspace"},
    ).json()
    director_options = client.get(
        f"/api/v1/projects/{project_id}/agent/options",
        headers=creator_headers,
        params={"scene": "director"},
    ).json()

    assert [item["kind"] for item in workspace_options["agents"]] == ["general"]
    assert [item["kind"] for item in director_options["agents"]] == ["screenplay"]

    workspace_session = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={
            "scene": "workspace",
            "agent_profile_id": director_options["agents"][0]["id"],
        },
    )
    director_session = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    )

    assert workspace_session.status_code == 201
    assert workspace_session.json()["agent_profile_id"] == workspace_options["agents"][0]["id"]
    assert director_session.status_code == 201
    assert director_session.json()["agent_profile_id"] == director_options["agents"][0]["id"]


def test_agent_chat_session_delete_blocks_active_run_and_cleans_attachments(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-delete-key"},
    )
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    image = BytesIO()
    Image.new("RGB", (160, 90), "#182f32").save(image, format="PNG")
    attachment = client.post(
        f"/api/v1/projects/{project_id}/agent/attachments",
        headers=creator_headers,
        files={"file": ("delete-with-chat.png", image.getvalue(), "image/png")},
    ).json()
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "分析这张图", "attachment_ids": [attachment["id"]]},
    )
    task_id = sent.json()["task"]["id"]
    path = f"/api/v1/projects/{project_id}/agent/sessions/{session_id}"

    blocked = client.delete(path, headers=creator_headers)
    assert blocked.status_code == 409
    assert "先停止" in blocked.json()["detail"]
    assert client.post(f"/api/v1/tasks/{task_id}/cancel", headers=creator_headers).status_code == 200

    deleted = client.delete(path, headers=creator_headers)
    assert deleted.status_code == 204
    assert client.get(path, headers=creator_headers).status_code == 404
    assert client.get(attachment["media_url"]).status_code == 404
    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert all(item["id"] != attachment["id"] for item in project_files)


def test_agent_chat_image_attachment_is_persisted_and_sent_to_runtime(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-image-key"},
    )
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    image = BytesIO()
    Image.new("RGB", (320, 240), "#225b5b").save(image, format="PNG")
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/agent/attachments",
        headers=creator_headers,
        files={"file": ("rain-scene.png", image.getvalue(), "image/png")},
    )

    assert uploaded.status_code == 201
    attachment = uploaded.json()
    assert attachment["mime_type"] == "image/webp"
    assert client.get(attachment["media_url"]).status_code == 200

    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "分析这个镜头的构图", "attachment_ids": [attachment["id"]]},
    )
    assert sent.status_code == 202
    assert sent.json()["user_message"]["runtime_manifest"]["attachments"][0]["id"] == attachment["id"]

    runtime = FakeAgentRuntime()
    task_id = sent.json()["task"]["id"]
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True
    runtime_attachment = runtime.requests[0].attachments[0]
    assert runtime_attachment.id == attachment["id"]
    assert base64.b64decode(runtime_attachment.data).startswith(b"RIFF")
    assert (
        client.delete(
            f"/api/v1/projects/{project_id}/agent/attachments/{attachment['id']}",
            headers=creator_headers,
        ).status_code
        == 409
    )


def test_running_agent_chat_can_be_stopped(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-stop-key"},
    )
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "持续生成一个很长的故事"},
    )
    task_id = sent.json()["task"]["id"]

    class BlockingRuntime:
        started = False

        async def run_stream(self, request, on_event):
            del request, on_event
            self.started = True
            await asyncio.Event().wait()

    runtime = BlockingRuntime()

    async def run_and_stop() -> None:
        processing = asyncio.create_task(process_task(task_id, runtime_factory=lambda: runtime))
        while not runtime.started:
            await asyncio.sleep(0.01)
        response = await asyncio.to_thread(
            client.post,
            f"/api/v1/tasks/{task_id}/cancel",
            headers=creator_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert await asyncio.wait_for(processing, timeout=2) is True

    asyncio.run(run_and_stop())
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert task["status"] == "cancelled"
    assert task["latest_message"] == "Agent 生成已停止"


def test_agent_chat_applies_project_file_changes_with_audit_manifest(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    created_file = client.post(
        f"/api/v1/projects/{project_id}/files",
        headers=creator_headers,
        json={"name": "Agent-memory.md", "kind": "memory", "content": "old memory"},
    ).json()
    base_sha256 = hashlib.sha256(b"old memory").hexdigest()
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-key"},
    )
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    runtime = FakeAgentRuntime(
        [
            {
                "operation": "update",
                "file_id": created_file["id"],
                "content": "agent updated memory",
                "base_sha256": base_sha256,
            },
            {"operation": "create", "name": "故事骨架.md", "content": "# 三幕故事"},
        ]
    )
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "更新项目记忆并保存故事骨架"},
    )

    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True
    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    outcomes = detail["messages"][-1]["runtime_manifest"]["project_file_changes"]
    assert [item["status"] for item in outcomes] == ["applied", "applied"]
    updated = client.get(
        f"/api/v1/projects/{project_id}/files/{created_file['id']}", headers=creator_headers
    ).json()
    assert updated["content"] == "agent updated memory"
    assert updated["file_metadata"]["last_modified_by"] == "agent"
    files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    assert any(item["name"] == "故事骨架.md" for item in files)


def test_admin_cannot_use_director_agent_for_another_users_project(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    session = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=admin_headers,
        json={"scene": "director"},
    )
    assert session.status_code == 404


def test_director_agent_publishes_formal_script_version_for_bound_chapter(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "导演 Agent 正式剧本测试",
            "pasted_text": "第一章 雨夜来客\n林遥在停电的修复室里听见三次敲门声。",
        },
    )
    assert imported.status_code == 201
    chapter_id = imported.json()["chapters"][0]["id"]
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-director-agent-key"},
    ).status_code == 200
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    ).json()["id"]
    message_path = f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages"

    missing_chapter = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "为当前章节生成剧本"},
    )
    assert missing_chapter.status_code == 422
    assert missing_chapter.json()["detail"] == "导演台对话必须绑定当前章节"

    command = json.dumps(
        {
            "operation": "create_script_version",
            "title": "雨夜来客 · AI 改编初稿",
            "content": "1. 内景 修复室 夜\n停电。林遥举起手电，门外传来第三次敲门声。",
            "review_notes": "核对敲门者身份揭示的节奏。",
        },
        ensure_ascii=False,
    )
    runtime = FakeAgentRuntime(
        [
            {
                "operation": "create",
                "name": "cineforge-script-version.json",
                "content": command,
            }
        ]
    )
    sent = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "为当前章节生成正式剧本", "chapter_id": chapter_id},
    )
    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert sent.json()["task"]["request_payload"]["chapter_id"] == chapter_id
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True

    chat_request = next(
        item for item in runtime.requests if "后台记忆维护器" not in item.system_prompt
    )
    chapter_context = next(
        item for item in chat_request.project_files if item.id == "current-chapter-context"
    )
    chapter_original = next(
        item for item in chat_request.project_files if item.id == "current-chapter-original"
    )
    assert "雨夜来客" in chapter_context.content
    assert "林遥在停电的修复室" not in chapter_context.content
    assert "林遥在停电的修复室" in chapter_original.content
    assert chapter_context.editable is False
    assert chapter_original.editable is False
    assert chapter_context.directory_id == "current-chapter-context"
    assert chapter_original.directory_id == "current-chapter-context"
    assert not any(
        item.id == "current-chapter-active-script" for item in chat_request.project_files
    )
    assert "cineforge-script-version.json" in chat_request.system_prompt
    assert "只有在首次改编" in chat_request.system_prompt

    scripts = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
    ).json()
    assert scripts[0]["title"] == "雨夜来客 · AI 改编初稿"
    assert scripts[0]["status"] == "reviewing"
    assert scripts[0]["is_active"] is True
    chapters = client.get(f"/api/v1/projects/{project_id}/chapters", headers=creator_headers).json()
    chapter = next(item for item in chapters if item["id"] == chapter_id)
    assert chapter["status"] == "reviewing"

    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    domain_change = detail["messages"][-1]["runtime_manifest"]["domain_changes"][0]
    assert domain_change["resource_type"] == "script_version"
    assert domain_change["chapter_id"] == chapter_id
    assert domain_change["resource_id"] == scripts[0]["id"]
    assert domain_change["workflow_id"]
    assert domain_change["review_task_id"]
    workflow_path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/director-workflow"
    workflow_detail = client.get(workflow_path, headers=creator_headers).json()
    assert workflow_detail["workflow"]["stage"] == "script_reviewing"
    assert workflow_detail["workflow"]["chat_session_id"] == session_id
    assert workflow_detail["child_runs"][-1]["kind"] == "script_review"
    assert workflow_detail["child_runs"][-1]["status"] == "queued"

    review_runtime = FakeDirectorOrchestrationRuntime()
    assert asyncio.run(
        process_task(
            workflow_detail["workflow"]["current_task_id"],
            runtime_factory=lambda: review_runtime,
        )
    )
    workflow_detail = client.get(workflow_path, headers=creator_headers).json()
    assert workflow_detail["workflow"]["stage"] == "asset_extracting"
    assert workflow_detail["child_runs"][-1]["kind"] == "asset_extraction"
    scripts = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
    ).json()
    assert scripts[0]["status"] == "approved"
    project_files = client.get(f"/api/v1/projects/{project_id}/files", headers=creator_headers).json()
    script_file = next(
        item
        for item in project_files
        if item["file_metadata"].get("script_version_id") == scripts[0]["id"]
    )
    assert script_file["kind"] == "script"
    assert script_file["file_metadata"]["chapter_id"] == chapter_id


def test_director_agent_publishes_formal_storyboard_version_for_bound_chapter(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "导演 Agent 正式分镜测试",
            "pasted_text": "第一章 雨夜回卷\n林遥推开修复室的门，桌上的旧相机突然自行回卷。",
        },
    )
    assert imported.status_code == 201
    chapter_id = imported.json()["chapters"][0]["id"]
    script = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/scripts",
        headers=creator_headers,
        json={
            "title": "第一集雨夜回卷",
            "content": "内景，记忆修复室，夜。林遥推门而入，看到旧相机自行回卷。",
            "status": "approved",
            "activate": True,
        },
    )
    assert script.status_code == 201
    extraction = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
        headers=creator_headers,
        json={
            "assets": [
                {"asset_type": "character", "name": "林遥", "description": "短发记忆修复师"},
                {"asset_type": "scene", "name": "记忆修复室", "description": "冷白设备光"},
            ]
        },
    )
    assert extraction.status_code == 201
    asset_ids = [item["id"] for item in extraction.json()["assets"]]

    async def mark_assets_ready() -> None:
        async with SessionLocal() as session:
            for asset_id in asset_ids:
                key = f"test/agent-storyboard-assets/{asset_id}.webp"
                await object_storage().put_bytes(key, b"ready-agent-storyboard-image", "image/webp")
                asset = await session.get(Asset, asset_id)
                assert asset is not None
                asset.status = AssetStatus.READY
                asset.media_url = f"/uploads/{key}"
                asset.version += 1
            await session.commit()

    asyncio.run(mark_assets_ready())
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-director-storyboard-agent-key"},
    ).status_code == 200
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    ).json()["id"]
    command = json.dumps(
        {
            "operation": "create_storyboard_version",
            "shots": [
                {
                    "title": "雨夜推门",
                    "shot_type": "中景",
                    "duration_seconds": 5,
                    "scene_description": "记忆修复室入口，冷白灯光",
                    "action_description": "林遥推门，镜头缓慢推进",
                    "dialogue": "",
                    "image_prompt": "林遥推开记忆修复室的门",
                    "video_prompt": "缓慢推镜，林遥进入修复室",
                    "asset_names": ["林遥", "记忆修复室"],
                }
            ],
        },
        ensure_ascii=False,
    )
    runtime = FakeAgentRuntime(
        [
            {
                "operation": "create",
                "name": "cineforge-storyboard-version.json",
                "content": command,
            }
        ]
    )
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "为当前章节生成正式分镜", "chapter_id": chapter_id},
    )
    assert sent.status_code == 202
    assert asyncio.run(process_task(sent.json()["task"]["id"], runtime_factory=lambda: runtime)) is True
    storyboards = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards",
        headers=creator_headers,
    ).json()
    assert len(storyboards) == 1
    detail = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboards[0]['id']}",
        headers=creator_headers,
    ).json()
    assert detail["shots"][0]["title"] == "雨夜推门"
    assert len(detail["shots"][0]["asset_ids"]) == 2

    chat_detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    domain_change = chat_detail["messages"][-1]["runtime_manifest"]["domain_changes"][0]
    assert domain_change["resource_type"] == "storyboard_version"
    assert domain_change["shot_count"] == 1


def test_agent_chat_queues_asset_prompt_and_image_tasks_from_platform_command(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-asset-command-key"},
    ).status_code == 200
    asset = client.post(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
        json={
            "asset_type": "character",
            "name": "AgentAssetLinyao",
            "description": "短发记忆修复师，黑色风衣，神情克制。",
        },
    )
    assert asset.status_code == 201
    asset_id = asset.json()["id"]
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "帮我把 AgentAssetLinyao 这个资产完成生图"},
    )
    assert sent.status_code == 202
    assert sent.json()["task"]["task_type"] == "asset_prompt_generation"
    assert sent.json()["task"]["request_payload"]["auto_queue_images_after_prompt"] is True

    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    domain_change = detail["messages"][-1]["runtime_manifest"]["domain_changes"][0]
    assert domain_change["resource_type"] == "asset_image_tasks"
    assert domain_change["prompt_task_id"]
    assert domain_change["prompt_asset_ids"] == [asset_id]
    assert domain_change["task_ids"] == [domain_change["prompt_task_id"]]
    assert "已通过资产库【生成提示词】通道" in detail["messages"][-1]["content"]

    prompt_task_id = domain_change["prompt_task_id"]
    prompt_task = client.get(f"/api/v1/tasks/{prompt_task_id}", headers=creator_headers).json()
    assert prompt_task["task_type"] == "asset_prompt_generation"
    assert prompt_task["status"] == "queued"
    assert prompt_task["request_payload"]["auto_queue_images_after_prompt"] is True

    prompt_runtime = FakeWorkflowRuntime()
    assert asyncio.run(process_task(prompt_task_id, runtime_factory=lambda: prompt_runtime)) is True
    prompt_task = client.get(f"/api/v1/tasks/{prompt_task_id}", headers=creator_headers).json()
    assert prompt_task["status"] == "succeeded"
    image_task_ids = prompt_task["result_payload"]["queued_image_task_ids"]
    assert len(image_task_ids) == 1

    image_task = client.get(f"/api/v1/tasks/{image_task_ids[0]}", headers=creator_headers).json()
    assert image_task["task_type"] == "asset_image_generation"
    assert image_task["status"] == "queued"
    generated_assets = client.get(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
    ).json()
    generated_asset = next(item for item in generated_assets if item["id"] == asset_id)
    assert generated_asset["id"] == asset_id
    assert generated_asset["status"] == "generating"
    assert generated_asset["generation_prompt"]

    assert (
        asyncio.run(
            process_task(
                image_task_ids[0],
                gateway_factory=lambda _provider: FakeAssetImageGateway(),
            )
        )
        is True
    )
    generated_assets = client.get(
        f"/api/v1/projects/{project_id}/assets",
        headers=creator_headers,
    ).json()
    generated_asset = next(item for item in generated_assets if item["id"] == asset_id)
    assert generated_asset["status"] == "ready"
    assert generated_asset["media_url"]


def test_director_storyboard_platform_action_failure_is_visible_in_chat(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "分镜失败提示测试",
            "pasted_text": "第一章 未改编\n林遥站在雨里，看见灯光忽明忽暗。",
        },
    )
    assert imported.status_code == 201
    chapter_id = imported.json()["chapters"][0]["id"]
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    ).json()["id"]

    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "帮我生成分镜", "chapter_id": chapter_id},
    )
    assert sent.status_code == 202
    assert sent.json()["task"]["status"] == "failed"
    assert sent.json()["task"]["task_type"] == "agent_chat_run"
    assert "生成分镜前必须先选择生效剧本" in sent.json()["task"]["latest_message"]

    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][-1]["finish_reason"] == "failed"
    assert "这次没能启动导演分镜工作流" in detail["messages"][-1]["content"]
    assert "生成分镜前必须先选择生效剧本" in detail["messages"][-1]["content"]


def test_agent_chat_replaces_saved_long_artifact_with_pointer_before_summary_finishes(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "按需上下文测试",
            "pasted_text": "第一章 雨夜\n林遥听见门外传来敲门声。",
        },
    )
    chapter_id = imported.json()["chapters"][0]["id"]
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-context-budget-key"},
    ).status_code == 200
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    ).json()["id"]
    message_path = f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages"
    command = json.dumps(
        {
            "operation": "create_script_version",
            "title": "雨夜初稿",
            "content": "内景，修复室，夜。林遥打开手电。",
            "review_notes": "检查节奏。",
        },
        ensure_ascii=False,
    )
    runtime = FakeLongArtifactAgentRuntime(
        [{"operation": "create", "name": "cineforge-script-version.json", "content": command}]
    )

    first = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "请改编本章并保存正式剧本", "chapter_id": chapter_id},
    )
    assert asyncio.run(process_task(first.json()["task"]["id"], runtime_factory=lambda: runtime))
    second = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "审核刚才的剧本，重点检查节奏", "chapter_id": chapter_id},
    )
    second_task_id = second.json()["task"]["id"]
    assert asyncio.run(process_task(second_task_id, runtime_factory=lambda: runtime))

    chat_requests = [
        request for request in runtime.requests if "后台记忆维护器" not in request.system_prompt
    ]
    assert len(chat_requests) == 2
    assert chat_requests[1].recent_messages[0] == {
        "role": "user",
        "content": "请改编本章并保存正式剧本",
    }
    carried_assistant = chat_requests[1].recent_messages[1]["content"]
    assert "上一轮已生成并保存正式剧本版本" in carried_assistant
    assert "这是已经落盘的完整剧本正文" not in carried_assistant
    assert len(carried_assistant) < 300
    workflow_file = next(
        item for item in chat_requests[1].project_files if item.id == "current-director-workflow"
    )
    assert workflow_file.directory_id == "current-chapter-context"
    assert '"reviewer": "平台内部导演审核子智能体"' in workflow_file.content
    assert '"stage": "script_reviewing"' in workflow_file.content
    assert "不存在需要用户联系的审核管理员" in chat_requests[1].system_prompt
    task_detail = client.get(f"/api/v1/tasks/{second_task_id}", headers=creator_headers).json()
    budget = task_detail["result_payload"]["context_budget"]
    assert budget["compacted_message_count"] == 1
    assert budget["sent_character_count"] < budget["source_character_count"]


def test_worker_recovers_agent_script_review_when_original_chat_was_deleted(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    imported = client.post(
        f"/api/v1/projects/{project_id}/sources/import",
        headers=creator_headers,
        data={
            "mode": "novel",
            "source_name": "审核恢复测试",
            "pasted_text": "第一章 失联会话\n林遥在旧仓库里找到未寄出的信。",
        },
    ).json()
    chapter_id = imported["chapters"][0]["id"]
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-review-recovery-key"},
    ).status_code == 200
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"scene": "director"},
    ).json()["id"]
    command = json.dumps(
        {
            "operation": "create_script_version",
            "title": "失联会话初稿",
            "content": "内景，旧仓库，夜。林遥拾起未寄出的信。",
            "review_notes": "检查人物动机。",
        },
        ensure_ascii=False,
    )
    runtime = FakeAgentRuntime(
        [{"operation": "create", "name": "cineforge-script-version.json", "content": command}]
    )
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "生成并保存正式剧本", "chapter_id": chapter_id},
    )
    source_task_id = sent.json()["task"]["id"]
    assert asyncio.run(process_task(source_task_id, runtime_factory=lambda: runtime))

    async def remove_workflow() -> None:
        async with SessionLocal() as session:
            await session.execute(
                delete(DirectorWorkflowRun).where(DirectorWorkflowRun.chapter_id == chapter_id)
            )
            await session.commit()

    asyncio.run(remove_workflow())
    assert client.delete(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).status_code == 204

    assert asyncio.run(recover_orphaned_agent_script_reviews()) >= 1
    detail = client.get(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/director-workflow",
        headers=creator_headers,
    ).json()
    assert detail["workflow"]["stage"] == "script_reviewing"
    assert detail["workflow"]["status"] == "running"
    assert detail["workflow"]["chat_session_id"] is None
    assert detail["child_runs"][-1]["kind"] == "script_review"


def test_agent_chat_failure_is_persisted_and_notified(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "test-agent-key"},
    )
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "生成一版会被上游拒绝的故事骨架"},
    )
    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]

    runtime = FakeFailingAgentRuntime()
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True
    task = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert task["status"] == "failed"
    assert task["error_message"] == "simulated agent runtime failure"
    assert task["result_payload"]["attempt_history"][-1]["status"] == "failed"
    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][-1]["finish_reason"] == "failed"
    assert "这轮 Agent 创作失败了：simulated agent runtime failure" in detail["messages"][-1]["content"]
    assert detail["messages"][-1]["runtime_manifest"]["runtime_type"] == "platform_error"
    assert detail["active_task"] is None
    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=creator_headers).json()
    assert events[-1]["status"] == "failed"
    assert events[-1]["progress"] == 100
    notifications = client.get("/api/v1/notifications", headers=creator_headers).json()["items"]
    notice = next(item for item in notifications if item["task_id"] == task_id)
    assert notice["title"] == "Agent 创作回复失败"
    assert notice["notification_metadata"]["status"] == "failed"


def test_worker_hides_internal_http_details_from_persisted_task_errors() -> None:
    request = httpx.Request("POST", "http://agent-runtime:8010/internal/v2/runs")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("internal runtime rejected token", request=request, response=response)

    message = _safe_error_message(error)

    assert message == "AI 服务鉴权失败，请联系管理员检查平台凭据或内部配置"
    assert "agent-runtime" not in message
    assert "internal/v2" not in message


def test_runtime_validation_error_is_not_reported_as_provider_failure() -> None:
    request = httpx.Request("POST", "http://agent-runtime:8010/internal/v2/runs/stream")
    response = httpx.Response(
        422,
        request=request,
        json={
            "detail": [
                {
                    "loc": ["body", "project_files", 1],
                    "msg": "Value error, project file path does not match its id",
                    "type": "value_error",
                }
            ]
        },
    )

    with pytest.raises(AgentRuntimeRequestError) as raised:
        asyncio.run(raise_for_runtime_status(response))

    assert raised.value.status_code == 422
    assert _safe_error_message(raised.value) == (
        "Agent Runtime 请求校验失败：项目文件路径与标识不一致"
    )


def test_agent_chat_rejects_duplicate_active_run(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    message_path = f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages"
    first = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "先生成第一版故事骨架"},
    )
    duplicate = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "不要等待，直接再生成一版"},
    )

    assert first.status_code == 202
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "当前会话已有 Agent 任务正在处理"
    detail = client.get(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assert [item["content"] for item in detail["messages"]] == ["先生成第一版故事骨架"]
    assert (
        client.post(
            f"/api/v1/tasks/{first.json()['task']['id']}/cancel",
            headers=creator_headers,
        ).status_code
        == 200
    )


def test_agent_chat_recovers_stale_run_with_new_runtime_attempt(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    sent = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "在 Worker 重启后继续生成故事骨架"},
    )
    task_id = sent.json()["task"]["id"]

    async def make_stale() -> None:
        async with SessionLocal() as session:
            now = datetime.now(UTC)
            await session.execute(
                update(AITask)
                .where(AITask.id == task_id)
                .values(
                    status=TaskStatus.RUNNING,
                    worker_id="stopped-worker:42",
                    heartbeat_at=now - timedelta(hours=1),
                    lease_expires_at=now + timedelta(minutes=15),
                    updated_at=now - timedelta(hours=1),
                )
            )
            await session.commit()

    asyncio.run(make_stale())
    assert asyncio.run(recover_stale_tasks()) == 1
    recovered = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert recovered["status"] == "queued"
    assert recovered["result_payload"]["recovery_count"] == 1

    runtime = FakeAgentRuntime()
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True
    assert runtime.requests[0].session_id == session_id
    completed = client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).json()
    assert completed["status"] == "succeeded"
    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=creator_headers).json()
    assert any(event["event_metadata"] == {"recovered": True} for event in events)


def test_agent_chat_second_turn_reuses_stable_state_with_summary_recovery_context(
    client: TestClient,
    creator_headers: dict[str, str],
) -> None:
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    options = client.get(f"/api/v1/projects/{project_id}/agent/options", headers=creator_headers).json()
    session_id = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        headers=creator_headers,
        json={"agent_profile_id": options["agents"][0]["id"]},
    ).json()["id"]
    message_path = f"/api/v1/projects/{project_id}/agent/sessions/{session_id}/messages"
    runtime = FakeAgentRuntime()

    first = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "第一轮：先确定主角的核心困境"},
    )
    first_task_id = first.json()["task"]["id"]
    assert asyncio.run(process_task(first_task_id, runtime_factory=lambda: runtime)) is True
    first_task = client.get(f"/api/v1/tasks/{first_task_id}", headers=creator_headers).json()
    assert asyncio.run(
        process_task(
            first_task["result_payload"]["memory_maintenance_task_id"],
            runtime_factory=lambda: runtime,
        )
    ) is True
    second = client.post(
        message_path,
        headers=creator_headers,
        json={"content": "第二轮：基于上一轮继续拆成三幕"},
    )
    assert asyncio.run(process_task(second.json()["task"]["id"], runtime_factory=lambda: runtime)) is True

    chat_requests = [
        request for request in runtime.requests if "后台记忆维护器" not in request.system_prompt
    ]
    assert len(chat_requests) == 2
    assert chat_requests[0].session_id == chat_requests[1].session_id == session_id
    assert chat_requests[0].state_mode == chat_requests[1].state_mode == "ephemeral"
    assert chat_requests[1].prompt == "第二轮：基于上一轮继续拆成三幕"
    assert chat_requests[1].conversation_summary
    assert chat_requests[1].recent_messages == []
    assert any("narrative-structure" in item for item in chat_requests[1].memory_context)
    assert (
        chat_requests[1].model_dump()["recent_messages"] == []
    )


def test_skills_path_traversal_is_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.get(
        "/api/v1/admin/skills/file",
        headers=admin_headers,
        params={"path": "../REQUIREMENTS.md"},
    )
    assert response.status_code == 400


def test_user_skills_are_private_versioned_and_stage_filtered(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    created = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={
            "name": "近身打斗分镜技法",
            "trigger_stages": ["storyboard_generation", "video_generation"],
            "description": "先建立攻防节拍，再按动作轴拆分镜头；每个镜头只保留一个主要动作。",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    skill = created.json()
    assert skill["version"] == 1
    assert skill["trigger_stages"] == ["storyboard_generation", "video_generation"]

    duplicate = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={
            "name": "近身打斗分镜技法",
            "trigger_stages": ["script_generation"],
            "description": "重复名称",
        },
    )
    invalid = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={"name": "缺少阶段", "trigger_stages": [], "description": "无阶段"},
    )
    assert duplicate.status_code == 409
    assert invalid.status_code == 422

    hidden = client.get("/api/v1/user-skills", headers=admin_headers)
    assert hidden.status_code == 200
    assert skill["id"] not in {item["id"] for item in hidden.json()}

    disabled = client.patch(
        f"/api/v1/user-skills/{skill['id']}",
        headers=creator_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["version"] == 2

    renamed = client.patch(
        f"/api/v1/user-skills/{skill['id']}",
        headers=creator_headers,
        json={
            "name": "动作场面分镜技法",
            "trigger_stages": ["storyboard_generation"],
            "description": "按空间关系、攻防节拍和动作轴拆分镜头。",
            "enabled": True,
        },
    )
    assert renamed.status_code == 200
    assert renamed.json()["version"] == 3
    assert renamed.json()["trigger_stages"] == ["storyboard_generation"]

    listed = client.get("/api/v1/user-skills", headers=creator_headers)
    assert listed.status_code == 200
    assert any(item["id"] == skill["id"] for item in listed.json())


def test_personal_agent_has_no_project_context_and_can_save_user_skill(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-agent-test-key"},
    )
    existing = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={
            "name": "个人 Agent 测试现有技能",
            "trigger_stages": ["script_review"],
            "description": "审核时优先检查人物动机连续性。",
            "enabled": False,
        },
    )
    assert existing.status_code == 201

    options = client.get("/api/v1/agent/options", headers=creator_headers)
    assert options.status_code == 200
    assert options.json()["agents"][0]["kind"] == "general"
    session = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    )
    assert session.status_code == 201
    assert session.json()["project_id"] is None
    session_id = session.json()["id"]

    image = BytesIO()
    Image.new("RGB", (120, 80), "#24525a").save(image, format="PNG")
    attachment = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("personal-reference.png", image.getvalue(), "image/png")},
    )
    assert attachment.status_code == 201
    assert attachment.json()["project_id"] is None

    command = json.dumps(
        {
            "operation": "upsert_user_skill",
            "skill_id": None,
            "name": "人物对白节奏检查",
            "trigger_stages": ["script_review"],
            "description": "检查连续对白是否超过三句，并在不改变人物意图的前提下插入动作反应。",
            "enabled": True,
        },
        ensure_ascii=False,
    )
    runtime = FakeAgentRuntime(
        project_file_changes=[
            {
                "operation": "create",
                "name": "cineforge-user-skill.json",
                "content": command,
            }
        ]
    )
    sent = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "分析后把这份规则保存成我的个人 Skill",
            "attachment_ids": [attachment.json()["id"]],
        },
    )
    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert sent.json()["task"]["project_id"] is None
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime)) is True

    request = runtime.requests[0]
    assert request.project_id.startswith("personal-")
    assert request.project_files == []
    assert "不能读取、修改、选择或操作项目" in request.system_prompt
    assert not any(path.startswith("user-skills/") for path in request.skill_versions)
    assert not request.skills
    assert request.attachments[0].name == "personal-reference.webp"

    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["session"]["project_id"] is None
    outcome = detail.json()["messages"][-1]["runtime_manifest"]["domain_changes"][0]
    assert outcome["resource_type"] == "user_skill"
    assert outcome["status"] == "applied"
    learned = client.get("/api/v1/user-skills", headers=creator_headers).json()
    saved = next(item for item in learned if item["name"] == "人物对白节奏检查")
    assert saved["trigger_stages"] == ["script_review"]
    assert saved["enabled"] is True


def test_personal_agent_image_mode_binds_selected_skill_and_persists_media(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-image-test-key"},
    )
    skill = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={
            "name": "黑白木刻插画",
            "trigger_stages": ["asset_prompt_generation"],
            "description": "使用强烈黑白块面对比、粗粝木刻线条和留白构图。",
            "enabled": True,
        },
    ).json()
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    runtime = FakePersonalMediaRuntime(
        message="我已经按木刻风格整理提示词并完成图片生成。",
        prompt="黑白木刻插画，一名撑伞人站在雨夜石桥中央，强烈明暗块面对比。",
    )
    gateway = FakeAssetImageGateway()
    sent = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "画一个雨夜石桥上的撑伞人",
            "mode": "image",
            "skill_ids": [skill["id"]],
            "media_options": {"resolution": "2K", "aspect_ratio": "16:9"},
        },
    )
    assert sent.status_code == 202
    task = sent.json()["task"]
    assert task["request_payload"]["mode"] == "image"
    assert task["request_payload"]["selected_skill_ids"] == [skill["id"]]
    assert asyncio.run(
        process_task(
            task["id"],
            runtime_factory=lambda: runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.model_binding["model"] == "story-pro"
    assert "连续对话式的生成图片模式" in request.system_prompt
    assert f"user-skills/{skill['id']}/README.md" in request.system_prompt
    assert gateway.requests[0].prompt == runtime.prompt
    assert gateway.requests[0].resolution == "2K"
    assert gateway.requests[0].aspect_ratio == "16:9"
    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assistant = detail["messages"][-1]
    media = assistant["runtime_manifest"]["generated_media"][0]
    assert assistant["content"] == "我已经按木刻风格整理提示词并完成图片生成。"
    assert media["mime_type"] == "image/webp"
    assert media["resolution"] == "2K"
    assert client.get(media["media_url"]).status_code == 200

    client.patch(
        f"/api/v1/user-skills/{skill['id']}",
        headers=creator_headers,
        json={"enabled": False},
    )
    rejected = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "再画一张", "mode": "image", "skill_ids": [skill["id"]]},
    )
    assert rejected.status_code == 422
    assert "已禁用" in rejected.json()["detail"]


def test_personal_chat_can_upgrade_to_real_image_generation(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-chat-image-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    action = {
        "type": "image",
        "generation_mode": "text_to_image",
        "prompt": "电影质感的未来城市雨夜，霓虹倒影，宽银幕构图，高细节",
        "resolution": "2K",
        "aspect_ratio": "16:9",
        "reference_attachment_ids": [],
    }
    runtime = FakePersonalChatMediaActionRuntime(
        response=(
            "我会沿用当前对话里的电影感设定生成一张宽银幕图片。\n"
            f"<CINEFORGE_MEDIA>{json.dumps(action, ensure_ascii=False)}</CINEFORGE_MEDIA>"
        )
    )
    gateway = FakeAssetImageGateway()
    original_prompt = "生成一张未来城市雨夜的图片，16:9，2K"
    sent = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": original_prompt},
    )
    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert sent.json()["task"]["request_payload"]["mode"] == "chat"
    assert asyncio.run(
        process_task(
            task_id,
            runtime_factory=lambda: runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    assert "平台当前可调用的媒体模型目录" in runtime.requests[0].system_prompt
    assert len(gateway.requests) == 1
    assert gateway.requests[0].prompt == original_prompt
    assert gateway.requests[0].resolution == "2K"
    assert gateway.requests[0].aspect_ratio == "16:9"
    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assistant = detail["messages"][-1]
    assert "CINEFORGE_MEDIA" not in assistant["content"]
    assert assistant["runtime_manifest"]["requested_mode"] == "chat"
    assert assistant["runtime_manifest"]["mode"] == "image"
    assert assistant["runtime_manifest"]["generated_media"][0]["resolution"] == "2K"

    async def load_task_pair() -> tuple[AITask, AITask]:
        async with SessionLocal() as db:
            source = await db.get(AITask, task_id)
            memory_id = assistant["runtime_manifest"]["memory"]["maintenance_task_id"]
            memory = await db.get(AITask, memory_id)
            assert source is not None and memory is not None
            return source, memory

    source_task, memory_task = asyncio.run(load_task_pair())
    assert source_task.request_payload["original_mode"] == "chat"
    assert memory_task.model_id == source_task.request_payload["text_model_id"]


def test_personal_image_mode_can_modify_previous_generated_image(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-image-follow-up-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    first_runtime = FakePersonalMediaRuntime(
        message="首张拳击训练照已经整理完成。",
        prompt="纪实摄影，两名拳击手在明亮训练馆中对练，横向构图。",
    )
    second_runtime = FakePersonalMediaRuntime(
        message="我会保留人物和构图，把环境调整为夜间赛场。",
        prompt="纪实摄影，同一对拳击手保持原有位置对练，夜间赛场，聚光灯和观众席暗部，横向构图。",
    )
    gateway = FakeAssetImageGateway()
    first = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "生成两名拳击手训练的照片", "mode": "image"},
    )
    assert first.status_code == 202
    assert asyncio.run(
        process_task(
            first.json()["task"]["id"],
            runtime_factory=lambda: first_runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True
    assert gateway.requests[0].prompt == "生成两名拳击手训练的照片"

    second = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "把上一张改成夜间赛场，人物位置不要变", "mode": "image"},
    )
    assert second.status_code == 202
    assert asyncio.run(
        process_task(
            second.json()["task"]["id"],
            runtime_factory=lambda: second_runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    follow_up_request = second_runtime.requests[0]
    assert follow_up_request.attachments[0].name.startswith("AI图片-")
    assert any(
        "上一轮图片结果" in item["content"]
        and "生成两名拳击手训练的照片" in item["content"]
        for item in follow_up_request.recent_messages
    )
    assert gateway.requests[-1].generation_mode == "image_to_image"
    assert gateway.requests[-1].reference_image_url

    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    latest_media = detail["messages"][-1]["runtime_manifest"]["generated_media"][0]
    assert latest_media["generation_mode"] == "image_to_image"


def test_personal_agent_loads_skills_only_when_selected_and_honors_rewrite_opt_in(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-prompt-policy-key"},
    )
    skill = client.post(
        "/api/v1/user-skills",
        headers=creator_headers,
        json={
            "name": "未主动选择的电影感增强",
            "trigger_stages": ["asset_prompt_generation"],
            "description": "自动补充电影光影和镜头语言。",
            "enabled": True,
        },
    ).json()
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    preference_runtime = FakeAgentRuntime()
    preference = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "以后遇到媒体任务时，请先帮我优化和完善提示词。"},
    )
    assert preference.status_code == 202
    assert asyncio.run(
        process_task(
            preference.json()["task"]["id"],
            runtime_factory=lambda: preference_runtime,
        )
    ) is True
    first_request = preference_runtime.requests[0]
    assert f"user-skills/{skill['id']}/README.md" not in first_request.skill_versions
    assert not first_request.skills

    action = {
        "type": "image",
        "generation_mode": "text_to_image",
        "prompt": "电影摄影，一座雨夜城市，湿润街道映出霓虹灯光，宽银幕构图。",
        "reference_attachment_ids": [],
    }
    generation_runtime = FakePersonalChatMediaActionRuntime(
        response=(
            "我会按你前面约定的方式整理提示词。\n"
            f"<CINEFORGE_MEDIA>{json.dumps(action, ensure_ascii=False)}</CINEFORGE_MEDIA>"
        )
    )
    gateway = FakeAssetImageGateway()
    generated = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "生成一张雨夜城市图片"},
    )
    assert generated.status_code == 202
    assert asyncio.run(
        process_task(
            generated.json()["task"]["id"],
            runtime_factory=lambda: generation_runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True
    second_request = generation_runtime.requests[0]
    assert f"user-skills/{skill['id']}/README.md" not in second_request.skill_versions
    assert not second_request.skills
    assert gateway.requests[0].prompt == action["prompt"]


def test_personal_video_mode_follow_up_receives_previous_video_prompt(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-video-follow-up-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    first_runtime = FakePersonalMediaRuntime(
        message="首版镜头已整理完成。",
        prompt="日出云海，镜头快速向山脊推进，金色晨光逐渐增强。",
    )
    second_runtime = FakePersonalMediaRuntime(
        message="我会保留日出云海，把推进速度放慢。",
        prompt="日出云海，镜头缓慢稳定地向同一山脊推进，金色晨光逐渐增强。",
    )
    gateway = FakeVideoGateway()
    for content, runtime in (
        ("生成一个日出云海的推进镜头", first_runtime),
        ("镜头再慢一点，其它内容保持不变", second_runtime),
    ):
        sent = client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            headers=creator_headers,
            json={"content": content, "mode": "video"},
        )
        assert sent.status_code == 202
        assert asyncio.run(
            process_task(
                sent.json()["task"]["id"],
                runtime_factory=lambda runtime=runtime: runtime,
                gateway_factory=lambda _provider: gateway,
            )
        ) is True

    assert any(
        "上一轮视频结果" in item["content"]
        and "生成一个日出云海的推进镜头" in item["content"]
        for item in second_runtime.requests[0].recent_messages
    )
    assert gateway.requests[-1].prompt == second_runtime.prompt


def test_personal_chat_reuses_historical_image_when_model_omits_action_marker(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-chat-reference-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    image = BytesIO()
    Image.new("RGB", (192, 108), "#243b53").save(image, format="PNG")
    attachment = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("historical-reference.png", image.getvalue(), "image/png")},
    ).json()
    first = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "你能看到这张图片吗？先说说画面内容。",
            "attachment_ids": [attachment["id"]],
        },
    )
    assert first.status_code == 202
    assert asyncio.run(
        process_task(first.json()["task"]["id"], runtime_factory=FakeAgentRuntime)
    ) is True

    fallback_runtime = FakePersonalChatMediaActionRuntime(
        response="可以，我会保留刚才图片的主体、光线和构图，再做一张同类画面。"
    )
    gateway = FakeAssetImageGateway()
    second = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "基于刚才那张图生成一张类似风格的图片，16:9，2K"},
    )
    assert second.status_code == 202
    assert asyncio.run(
        process_task(
            second.json()["task"]["id"],
            runtime_factory=lambda: fallback_runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    request = fallback_runtime.requests[0]
    assert [item.id for item in request.attachments] == [attachment["id"]]
    assert gateway.requests[0].generation_mode == "image_to_image"
    assert gateway.requests[0].reference_image_url
    assert gateway.requests[0].reference_image_url.startswith("data:image/")
    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    generated = detail["messages"][-1]["runtime_manifest"]["generated_media"][0]
    assert generated["generation_mode"] == "image_to_image"


def test_personal_chat_additive_image_edit_reuses_reference_and_does_not_save_skill(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-chat-additive-reference-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    image = BytesIO()
    Image.new("RGB", (192, 108), "#34284a").save(image, format="PNG")
    attachment = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("character-reference.png", image.getvalue(), "image/png")},
    ).json()
    described = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "你能看到这张图片吗？先分析人物和构图。",
            "attachment_ids": [attachment["id"]],
        },
    )
    assert described.status_code == 202
    assert asyncio.run(
        process_task(described.json()["task"]["id"], runtime_factory=FakeAgentRuntime)
    ) is True

    media_prompt = "沿用参考图人物与构图，加入三位神话女子，巨物美学，完整电影场景。"
    action = {
        "type": "image",
        # Simulate a model that overlooked the historical reference. The server
        # must reconcile the action with the user's additive edit intent.
        "generation_mode": "text_to_image",
        "prompt": media_prompt,
        "reference_attachment_ids": [],
    }
    unauthorized_skill_command = json.dumps(
        {
            "operation": "upsert_user_skill",
            "skill_id": None,
            "name": "巨物美学",
            "trigger_stages": ["asset_prompt_generation"],
            "description": "强调人物与宏大巨物的尺度对比。",
            "enabled": True,
        },
        ensure_ascii=False,
    )
    runtime = FakePersonalChatMediaActionRuntime(
        response=(
            "已保存技能，现在开始生成图片。\n"
            f"<CINEFORGE_MEDIA>{json.dumps(action, ensure_ascii=False)}</CINEFORGE_MEDIA>"
        ),
        project_file_changes=[
            {
                "operation": "create",
                "name": "cineforge-user-skill.json",
                "content": unauthorized_skill_command,
            }
        ],
    )
    gateway = FakeAssetImageGateway()
    generated = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "再加上这三人，生成应该适合她们的提示词并生成图片，也使用这个巨物美学",
        },
    )
    assert generated.status_code == 202
    assert asyncio.run(
        process_task(
            generated.json()["task"]["id"],
            runtime_factory=lambda: runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    assert [item.id for item in runtime.requests[0].attachments] == [attachment["id"]]
    assert gateway.requests[0].prompt == media_prompt
    assert gateway.requests[0].generation_mode == "image_to_image"
    assert gateway.requests[0].reference_image_url
    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    assistant = detail["messages"][-1]
    assert assistant["runtime_manifest"]["project_file_changes"] == []
    assert assistant["runtime_manifest"]["skill_change_requested"] is False
    assert "没有修改或保存个人 Skill" in assistant["content"]
    learned = client.get("/api/v1/user-skills", headers=creator_headers).json()
    assert all(item["name"] != "巨物美学" for item in learned)


def test_personal_media_fallback_treats_prompt_creation_plus_generation_as_an_action() -> None:
    content = "生成应该适合她们的提示词并生成图片，也使用这个巨物美学"
    action = task_worker.infer_explicit_media_action(
        content,
        available_attachments=[],
        available_models=[],
    )
    assert action is not None
    assert action.type == "image"
    assert task_worker.media_prompt_rewrite_allowed(
        content,
        recent_messages=[],
        has_selected_skills=False,
    ) is True


def test_personal_chat_can_upgrade_to_video_with_requested_duration(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-chat-video-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    action = {
        "type": "video",
        "generation_mode": "text_to_video",
        "prompt": "日出时分的云海，镜头缓慢向前推进，光线逐渐照亮山脊",
        "aspect_ratio": "16:9",
        "duration_seconds": 8,
        "reference_attachment_ids": [],
    }
    runtime = FakePersonalChatMediaActionRuntime(
        response=(
            "我会按宽银幕构图生成一段云海日出视频。\n"
            f"<CINEFORGE_MEDIA>{json.dumps(action, ensure_ascii=False)}</CINEFORGE_MEDIA>"
        )
    )
    gateway = FakeVideoGateway()
    sent = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={"content": "帮我生成一个云海日出的视频，16:9，8秒"},
    )
    assert sent.status_code == 202
    assert asyncio.run(
        process_task(
            sent.json()["task"]["id"],
            runtime_factory=lambda: runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True
    assert gateway.submit_calls == 1
    assert gateway.requests[0].aspect_ratio == "16:9"
    assert gateway.requests[0].duration_seconds == 10
    assert gateway.requests[0].generation_mode == "text_to_video"


def test_personal_agent_video_mode_uses_uploaded_reference_and_normalizes_duration(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}",
        headers=admin_headers,
        json={"api_key": "personal-video-test-key"},
    )
    session_id = client.post(
        "/api/v1/agent/sessions",
        headers=creator_headers,
        json={"scene": "workspace"},
    ).json()["id"]
    image = BytesIO()
    Image.new("RGB", (160, 90), "#31485b").save(image, format="PNG")
    attachment = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("video-reference.png", image.getvalue(), "image/png")},
    ).json()
    runtime = FakePersonalMediaRuntime(
        message="参考首帧已经用于生成这段镜头。",
        prompt="固定镜头，人物缓慢抬头看向窗外，窗帘被微风吹动。",
    )
    gateway = FakeVideoGateway()
    sent = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=creator_headers,
        json={
            "content": "让这张图动起来",
            "mode": "video",
            "attachment_ids": [attachment["id"]],
            "media_options": {
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "duration_seconds": 7,
            },
        },
    )
    assert sent.status_code == 202
    task_id = sent.json()["task"]["id"]
    assert asyncio.run(
        process_task(
            task_id,
            runtime_factory=lambda: runtime,
            gateway_factory=lambda _provider: gateway,
        )
    ) is True

    assert gateway.submit_calls == 1
    request = gateway.requests[0]
    assert request.duration_seconds == 5
    assert request.resolution == "720p"
    assert request.generation_mode == "first_frame"
    assert request.reference_image_url
    assert request.reference_image_url.startswith("data:image/webp;base64,")
    detail = client.get(
        f"/api/v1/agent/sessions/{session_id}",
        headers=creator_headers,
    ).json()
    media = detail["messages"][-1]["runtime_manifest"]["generated_media"][0]
    assert media["mime_type"] == "video/mp4"
    assert media["duration_seconds"] == 5
    assert media["generation_mode"] == "first_frame"
    assert client.get(media["media_url"]).status_code == 200


def test_same_tenant_accounts_cannot_access_each_others_creation_data(
    client: TestClient,
    creator_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    accounts: list[tuple[str, dict[str, str]]] = [
        ("admin-a", admin_headers),
        ("user-a", creator_headers),
    ]
    for key, role in (("admin-b", "admin"), ("user-b", "user")):
        email = f"isolation-{key}@cineforge.local"
        password = "Isolation123!"
        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": email,
                "display_name": f"隔离测试 {key}",
                "password": password,
                "role": role,
                "initial_credits": "100.00",
            },
        )
        assert created.status_code == 201
        login = client.post(
            "/api/v1/auth/login",
            json={"tenant": "demo", "email": email, "password": password},
        )
        assert login.status_code == 200
        accounts.append(
            (key, {"Authorization": f"Bearer {login.json()['access_token']}"})
        )

    projects: dict[str, dict] = {}
    account_ids: dict[str, str] = {}
    for key, headers in accounts:
        account_ids[key] = client.get("/api/v1/auth/me", headers=headers).json()["user"]["id"]
        options = client.get("/api/v1/projects/options", headers=headers)
        assert options.status_code == 200
        payload = options.json()
        created = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "name": f"账号隔离项目 {key}",
                "video_model_id": payload["video_models"][0]["id"],
                "visual_handbook_id": payload["visual_handbooks"][0]["id"],
                "director_handbook_id": payload["director_handbooks"][0]["id"],
            },
        )
        assert created.status_code == 201
        projects[key] = created.json()

    for key, headers in accounts:
        visible = client.get("/api/v1/projects", headers=headers)
        assert visible.status_code == 200
        assert projects[key]["id"] in {item["id"] for item in visible.json()}
        assert all(item["owner_id"] == account_ids[key] for item in visible.json())

        for foreign_key, foreign_project in projects.items():
            if foreign_key == key:
                continue
            project_path = f"/api/v1/projects/{foreign_project['id']}"
            assert client.get(project_path, headers=headers).status_code == 404
            assert (
                client.patch(project_path, headers=headers, json={"name": "越权修改"}).status_code
                == 404
            )
            assert client.delete(project_path, headers=headers).status_code == 404
            for suffix in (
                "/files",
                "/chapters",
                "/assets",
                "/agent/sessions",
                "/agent-memories",
            ):
                assert client.get(f"{project_path}{suffix}", headers=headers).status_code == 404

    global_asset = client.post(
        "/api/v1/assets",
        headers=admin_headers,
        json={
            "asset_type": "character",
            "name": "管理员 A 私有角色",
            "description": "只能由创建账号访问",
        },
    )
    assert global_asset.status_code == 201
    asset_id = global_asset.json()["id"]
    for key, headers in accounts:
        listed_ids = {
            item["id"] for item in client.get("/api/v1/assets", headers=headers).json()
        }
        if key == "admin-a":
            assert asset_id in listed_ids
            continue
        assert asset_id not in listed_ids
        assert (
            client.patch(
                f"/api/v1/assets/{asset_id}",
                headers=headers,
                json={"name": "越权资产修改"},
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/assets/{asset_id}/revisions", headers=headers).status_code
            == 404
        )
        assert client.delete(f"/api/v1/assets/{asset_id}", headers=headers).status_code == 404
        assert (
            client.post(
                "/api/v1/assets",
                headers=headers,
                json={
                    "asset_type": "character",
                    "parent_asset_id": asset_id,
                    "name": "越权衍生角色",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/projects/{projects[key]['id']}/assets/import/{asset_id}",
                headers=headers,
            ).status_code
            == 404
        )
