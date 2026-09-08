from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, TypeGuard

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentChatSummary,
    AgentMemory,
    AgentMessageRole,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetScope,
    AssetStatus,
    AssetType,
    AudioClip,
    AudioClipStatus,
    Chapter,
    ChapterAnalysis,
    ChapterStatus,
    CompositionStatus,
    CompositionVersion,
    DialogueLine,
    DialogueVersion,
    DirectorChildRun,
    DirectorDecisionRequest,
    DirectorWorkflowRun,
    Handbook,
    ModelType,
    PersonalAgentAttachment,
    Project,
    ProjectFile,
    ProjectFileKind,
    PromptTemplate,
    Provider,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    TaskEvent,
    TaskStatus,
    User,
    UserSkill,
    VideoClip,
    VideoClipStatus,
    VoiceBinding,
)
from app.db.session import SessionLocal
from app.services.agent_memory import (
    EMBEDDING_MODEL,
    embed_memory_text,
    normalize_memory_key,
    normalize_memory_namespace,
    retrieve_project_memories,
)
from app.services.agent_runtime import (
    AgentRuntimeAttachment,
    AgentRuntimeClient,
    AgentRuntimeProjectFileSnapshot,
    AgentRuntimeRequest,
    AgentRuntimeRequestError,
)
from app.services.asset_revisions import snapshot_asset_revision
from app.services.billing import debit_additional_task_cost, refund_task_cost, resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.composition_renderer import FFmpegCompositionRenderer
from app.services.image_model_routing import normalize_image_resolution, resolve_image_model
from app.services.managed_skills import (
    handbook_usage_instructions,
    internal_system_prompt_content,
)
from app.services.media import (
    save_agent_chat_image,
    save_asset_image,
    save_project_audio,
    save_project_cover,
    save_project_video,
)
from app.services.media_gateway import (
    ImageGenerationRequest,
    OpenAICompatibleMediaGateway,
    SpeechGenerationRequest,
    VideoGenerationRequest,
)
from app.services.object_storage import (
    delete_media_file,
    materialize_media_file,
    object_key_from_media_url,
    object_storage,
    persist_media_file,
)
from app.services.provider_adapters import (
    AUTODL_MINIMAX_H3_MODEL_ID,
    AUTODL_MINIMAX_H3_PROVIDER_CODE,
    ProviderAdapterConfig,
    closest_supported_video_duration,
    compatible_video_resolution,
    default_video_audio_enabled,
    supported_video_durations,
    validate_video_generation_request,
)
from app.services.task_events import (
    publish_agent_stream_event,
    publish_task_event,
    record_task_event,
)
from app.services.task_queue import enqueue_task
from app.services.user_skills import (
    apply_personal_agent_skill_changes,
    enabled_user_skills,
    infer_chat_user_skill_stages,
    personal_agent_scope_id,
    personal_agent_system_instructions,
    personal_skill_change_requested,
    prompt_user_skill_stages,
    user_skill_snapshots,
    user_skill_usage_instructions,
)
from app.services.video_references import (
    build_shot_audio_references,
    build_shot_image_references,
    ensure_video_prompt_audio_reference_locks,
    load_shot_assets_with_parents,
)

GatewayFactory = Callable[[Provider], OpenAICompatibleMediaGateway]
RuntimeFactory = Callable[[], AgentRuntimeClient]

_provider_claim_locks: dict[str, asyncio.Lock] = {}
_task_activity_events: dict[str, asyncio.Event] = {}


class CompositionRenderer(Protocol):
    async def render(
        self,
        manifest: dict,
        *,
        output_path: Path,
        resolution: str,
        aspect_ratio: str,
        fps: int,
    ) -> None: ...


RendererFactory = Callable[[], CompositionRenderer]
logger = logging.getLogger(__name__)
WORKER_ID = os.environ.get("CINEFORGE_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
RESTART_UNSAFE_IMAGE_TASKS = {"project_cover_generation", "asset_image_generation"}
PROVIDER_JOB_TASKS = {"shot_video_generation", "dialogue_tts_generation"}
AGENT_HISTORY_MESSAGE_LIMIT = 10
AGENT_HISTORY_CHARACTER_LIMIT = 10_000
AGENT_HISTORY_MESSAGE_CHARACTER_LIMIT = 2_400
AGENT_ARTIFACT_POINTER_THRESHOLD = 1_200
MAX_CHAT_ATTACHMENTS = 4


class ProviderJobTerminalError(RuntimeError):
    """The provider confirmed that an asynchronous media job cannot be resumed."""


def normalize_execution_step_state(value: object) -> str:
    state = str(value or "").strip().lower()
    if state in {"error", "failed", "failure", "cancelled", "canceled"}:
        return "failed"
    if state in {"running", "started", "pending"}:
        return "running"
    return "succeeded"


def signal_task_activity(task_id: str) -> None:
    event = _task_activity_events.get(task_id)
    if event is not None:
        event.set()


async def run_with_idle_timeout(
    operation: Awaitable[None],
    activity: asyncio.Event,
    idle_timeout_seconds: float,
) -> None:
    execution = asyncio.create_task(operation)
    activity_wait: asyncio.Task[bool] | None = None
    try:
        while True:
            activity.clear()
            activity_wait = asyncio.create_task(activity.wait())
            done, _pending = await asyncio.wait(
                {execution, activity_wait},
                timeout=idle_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                activity_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await activity_wait
                return await execution
            if activity_wait in done:
                activity_wait = None
                continue
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            raise TimeoutError
    finally:
        if activity_wait is not None and not activity_wait.done():
            activity_wait.cancel()
            with suppress(asyncio.CancelledError):
                await activity_wait
        if not execution.done():
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution


class AgentStreamRelay:
    MEDIA_ACTION_MARKER = "<CINEFORGE_MEDIA>"

    def __init__(
        self,
        task: AITask,
        session_id: str,
        *,
        publish_text: bool = True,
        hide_media_actions: bool = False,
    ) -> None:
        self.user_id = str(task.user_id)
        self.task_id = str(task.id)
        self.project_id = str(task.project_id) if task.project_id else None
        self.session_id = session_id
        self.publish_text = publish_text
        self.hide_media_actions = hide_media_actions
        self.pending_visible_text = ""
        self.media_action_started = False
        self.text_buffer: list[str] = []
        self.full_text: list[str] = []
        self.buffer_size = 0
        self.last_flush = time.monotonic()
        self.last_snapshot = 0.0
        self.phase = "queued"
        self.tool_name = ""
        self.tool_state = ""
        self.execution_steps: list[dict[str, object]] = []
        self._step_index: dict[str, dict[str, object]] = {}
        self._active_model_step_id = ""
        self._active_tool_step_ids: list[str] = []
        self._step_sequence = 0
        self.fallback_needed = False

    def _new_step_id(self, kind: str) -> str:
        self._step_sequence += 1
        return f"{kind}-{self._step_sequence}"

    def _start_step(self, step_id: str, kind: str, name: str) -> None:
        now = datetime.now(UTC).isoformat()
        step = self._step_index.get(step_id)
        if step is None:
            step = {
                "id": step_id,
                "kind": kind,
                "name": name,
                "status": "running",
                "started_at": now,
            }
            self._step_index[step_id] = step
            self.execution_steps.append(step)
            return
        step.update({"kind": kind, "name": name, "status": "running"})
        step.pop("completed_at", None)

    def _finish_step(self, step_id: str, state: object) -> str:
        normalized = normalize_execution_step_state(state)
        step = self._step_index.get(step_id)
        if step is not None:
            step["status"] = normalized
            step["completed_at"] = datetime.now(UTC).isoformat()
        return normalized

    async def __call__(self, event: dict[str, object]) -> None:
        signal_task_activity(self.task_id)
        if event.get("type") == "TEXT_BLOCK_DELTA":
            delta = str(event.get("delta") or "")
            if not delta:
                return
            if self.hide_media_actions:
                delta = self._visible_text_delta(delta)
                if not delta:
                    return
            self.text_buffer.append(delta)
            self.full_text.append(delta)
            if not self.publish_text:
                self.phase = "thinking"
                return
            self.buffer_size += len(delta)
            self.phase = "writing"
            if self.buffer_size >= 24 or time.monotonic() - self.last_flush >= 0.06:
                await self.flush()
            return
        await self.flush()
        event_type = str(event.get("type") or "")
        published_event = dict(event)
        if event_type == "MODEL_CALL_START":
            self.phase = "thinking"
            step_id = str(event.get("model_call_id") or "") or self._new_step_id("model")
            self._active_model_step_id = step_id
            self._start_step(step_id, "model", "Model")
            published_event["_cineforge_step_id"] = step_id
        elif event_type == "MODEL_CALL_END":
            step_id = (
                str(event.get("model_call_id") or "")
                or self._active_model_step_id
                or self._new_step_id("model")
            )
            state = self._finish_step(step_id, event.get("state"))
            published_event.update({"_cineforge_step_id": step_id, "_cineforge_step_state": state})
            if step_id == self._active_model_step_id:
                self._active_model_step_id = ""
        elif event_type == "THINKING_BLOCK_START":
            self.phase = "thinking"
        elif event_type == "TOOL_CALL_START":
            self.phase = "tool"
            self.tool_name = str(event.get("tool_call_name") or "Tool")
            self.tool_state = "running"
            step_id = str(event.get("tool_call_id") or "") or self._new_step_id("tool")
            self._active_tool_step_ids.append(step_id)
            self._start_step(step_id, "tool", self.tool_name)
            published_event["_cineforge_step_id"] = step_id
        elif event_type == "TOOL_RESULT_END":
            self.phase = "writing" if self.full_text else "thinking"
            requested_step_id = str(event.get("tool_call_id") or "")
            step_id = requested_step_id or (
                self._active_tool_step_ids[-1] if self._active_tool_step_ids else self._new_step_id("tool")
            )
            step = self._step_index.get(step_id)
            tool_name = str(step.get("name") if step else self.tool_name or "Tool")
            self.tool_state = self._finish_step(step_id, event.get("state"))
            if step_id in self._active_tool_step_ids:
                self._active_tool_step_ids.remove(step_id)
            published_event.update(
                {
                    "_cineforge_step_id": step_id,
                    "_cineforge_step_state": self.tool_state,
                    "_cineforge_tool_name": tool_name,
                }
            )
        elif event_type == "REPLY_END":
            self.phase = "finalizing"
        delivered = await publish_agent_stream_event(
            user_id=self.user_id,
            task_id=self.task_id,
            project_id=self.project_id,
            session_id=self.session_id,
            event=published_event,
        )
        if delivered is False:
            self.fallback_needed = True
            await self.persist_snapshot()

    def _visible_text_delta(self, delta: str) -> str:
        if self.media_action_started:
            return ""
        pending = self.pending_visible_text + delta
        marker_index = pending.find(self.MEDIA_ACTION_MARKER)
        if marker_index >= 0:
            self.pending_visible_text = ""
            self.media_action_started = True
            return pending[:marker_index]
        reserve = 0
        upper_bound = min(len(pending), len(self.MEDIA_ACTION_MARKER) - 1)
        for size in range(upper_bound, 0, -1):
            if pending.endswith(self.MEDIA_ACTION_MARKER[:size]):
                reserve = size
                break
        if reserve:
            visible = pending[:-reserve]
            self.pending_visible_text = pending[-reserve:]
            return visible
        self.pending_visible_text = ""
        return pending

    async def finish(self) -> None:
        if self.pending_visible_text and not self.media_action_started:
            self.text_buffer.append(self.pending_visible_text)
            self.full_text.append(self.pending_visible_text)
        self.pending_visible_text = ""
        await self.flush()

    async def flush(self) -> None:
        if not self.text_buffer:
            return
        delta = "".join(self.text_buffer)
        self.text_buffer.clear()
        self.buffer_size = 0
        self.last_flush = time.monotonic()
        delivered = await publish_agent_stream_event(
            user_id=self.user_id,
            task_id=self.task_id,
            project_id=self.project_id,
            session_id=self.session_id,
            event={"type": "TEXT_BLOCK_DELTA", "delta": delta},
        )
        if delivered is False:
            self.fallback_needed = True
            await self.persist_snapshot()

    async def persist_snapshot(self, *, force: bool = False) -> None:
        if not self.fallback_needed:
            return
        now = time.monotonic()
        if not force and now - self.last_snapshot < 0.35:
            return
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, self.task_id)
            if not owns_running_task(task):
                return
            result_payload = dict(task.result_payload or {})
            result_payload["agent_stream"] = {
                "session_id": self.session_id,
                "text": "".join(self.full_text) if self.publish_text else "",
                "phase": self.phase,
                "tool_name": self.tool_name,
                "tool_state": self.tool_state,
                "execution_steps": self.execution_steps,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            task.result_payload = result_payload
            await session.commit()
        self.last_snapshot = now


def owns_running_task(task: AITask | None) -> TypeGuard[AITask]:
    return task is not None and task.status == TaskStatus.RUNNING and task.worker_id == WORKER_ID


async def owned_task_for_update(session: AsyncSession, task_id: str) -> AITask | None:
    return await session.scalar(
        select(AITask)
        .where(
            AITask.id == task_id,
            AITask.status == TaskStatus.RUNNING,
            AITask.worker_id == WORKER_ID,
        )
        .with_for_update()
    )


async def cleanup_media(storage_key: str, cached_path: Path) -> None:
    with suppress(Exception):
        await delete_media_file(storage_key, cached_path)


class ExtractedAssetPayload(BaseModel):
    asset_type: Literal["character", "scene", "prop"]
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=10_000)
    parent_name: str | None = Field(default=None, max_length=160)


class AssetExtractionPayload(BaseModel):
    assets: list[ExtractedAssetPayload] = Field(min_length=1, max_length=500)


class GeneratedPromptPayload(BaseModel):
    asset_id: str
    generation_prompt: str = Field(min_length=1, max_length=50_000)


class PromptGenerationPayload(BaseModel):
    assets: list[GeneratedPromptPayload] = Field(min_length=1, max_length=100)


class GeneratedStoryboardShotPayload(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    shot_type: str = Field(default="中景", min_length=1, max_length=80)
    duration_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=300)
    scene_description: str = Field(default="", max_length=20_000)
    action_description: str = Field(default="", max_length=20_000)
    dialogue: str = Field(default="", max_length=20_000)
    image_prompt: str = Field(min_length=1, max_length=30_000)
    # Final provider-specific prompts are generated only after storyboard approval.
    video_prompt: str = Field(default="", max_length=30_000)
    asset_names: list[str] = Field(default_factory=list, max_length=100)


class StoryboardGenerationPayload(BaseModel):
    shots: list[GeneratedStoryboardShotPayload] = Field(min_length=1, max_length=200)


class GeneratedShotVideoPromptPayload(BaseModel):
    shot_id: str
    video_prompt: str = Field(min_length=1, max_length=30_000)


class ShotVideoPromptGenerationPayload(BaseModel):
    shots: list[GeneratedShotVideoPromptPayload] = Field(min_length=1, max_length=300)


class GeneratedVideoPromptTemplatePayload(BaseModel):
    shot_order_index: int = Field(ge=1, le=10_000)
    mode: str = Field(default="text_to_video", max_length=80)
    prompt: str = Field(min_length=1, max_length=30_000)
    negative_prompt: str = Field(default="", max_length=10_000)
    reference_asset_names: list[str] = Field(default_factory=list, max_length=100)


class VideoPromptTemplateGenerationPayload(BaseModel):
    prompts: list[GeneratedVideoPromptTemplatePayload] = Field(min_length=1, max_length=300)


class DirectorReviewFindingPayload(BaseModel):
    severity: Literal["blocking", "major", "minor"] = "major"
    location: str = Field(default="", max_length=500)
    issue: str = Field(min_length=1, max_length=5_000)
    suggestion: str = Field(default="", max_length=5_000)


class DirectorReviewPayload(BaseModel):
    approved: bool
    summary: str = Field(min_length=1, max_length=10_000)
    findings: list[DirectorReviewFindingPayload] = Field(default_factory=list, max_length=100)


class GeneratedDialogueLinePayload(BaseModel):
    shot_order_index: int | None = Field(default=None, ge=1)
    speaker: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=20_000)
    emotion: str = Field(default="自然", max_length=120)
    direction: str = Field(default="", max_length=20_000)


class DialogueExtractionPayload(BaseModel):
    lines: list[GeneratedDialogueLinePayload] = Field(min_length=1, max_length=500)


class ChapterEventPayload(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=10_000)
    dramatic_value: str = Field(default="", max_length=2_000)


class ChapterCharacterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    role: str = Field(default="", max_length=500)
    motivation: str = Field(default="", max_length=2_000)
    relationship: str = Field(default="", max_length=2_000)


class ChapterAnalysisPayload(BaseModel):
    summary: str = Field(min_length=1, max_length=20_000)
    core_conflict: str = Field(min_length=1, max_length=10_000)
    opening_hook: str = Field(min_length=1, max_length=10_000)
    adaptation_strategy: str = Field(min_length=1, max_length=30_000)
    events: list[ChapterEventPayload] = Field(min_length=1, max_length=200)
    characters: list[ChapterCharacterPayload] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)


class GeneratedScriptPayload(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=500_000)
    review_notes: str = Field(default="", max_length=50_000)
    continuity_summary: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def separate_internal_memory(self):
        from app.services.creation_context import separate_script_memory

        body, memory = separate_script_memory(self.content)
        if memory:
            if not body:
                raise ValueError("模型只返回了记忆，没有剧本正文")
            self.content = body
            self.continuity_summary = "\n".join(item for item in (self.continuity_summary, memory) if item)
        return self


class ExtractedConversationMemory(BaseModel):
    namespace: str = Field(default="project", min_length=1, max_length=80)
    key: str = Field(default="", max_length=160)
    content: str = Field(min_length=1, max_length=10_000)
    salience: float = Field(default=0.5, ge=0, le=1)


class ConversationMaintenancePayload(BaseModel):
    summary: str = Field(min_length=1, max_length=30_000)
    memories: list[ExtractedConversationMemory] = Field(default_factory=list, max_length=20)


class PersonalMediaPromptPayload(BaseModel):
    message: str = Field(default="", max_length=10_000)
    prompt: str = Field(min_length=1, max_length=50_000)


class PersonalMediaAction(BaseModel):
    type: Literal["image", "video"]
    generation_mode: Literal[
        "text_to_image",
        "image_to_image",
        "text_to_video",
        "image_to_video",
    ]
    prompt: str = Field(min_length=1, max_length=50_000)
    model_id: str | None = Field(default=None, max_length=200)
    resolution: str | None = Field(default=None, max_length=40)
    aspect_ratio: str | None = Field(default=None, max_length=20)
    duration_seconds: float | None = Field(default=None, ge=1, le=300)
    reference_attachment_ids: list[str] = Field(default_factory=list, max_length=4)


class PersonalChatResponsePayload(BaseModel):
    message: str = Field(default="", max_length=20_000)
    media: PersonalMediaAction | None = None


_MEDIA_CAPABILITY_QUESTION = re.compile(
    r"(?:能不能|能否|可以不可以|可不可以|会不会|是否(?:可以|能够)?|支持不支持|支不支持)"
    r".{0,12}(?:生成|制作|创建|画|做).{0,8}(?:图片|图像|视频|动画)"
)
_IMAGE_GENERATION_REQUEST = re.compile(
    r"(?:生成|制作|创建|画|绘制|出|做|来|重做|重新生成).{0,20}"
    r"(?:图片|图像|插画|海报|封面|照片|壁纸|立绘|头像|图)"
    r"|(?:图片|图像|插画|海报|封面|照片|壁纸|立绘|头像|图).{0,16}"
    r"(?:生成|制作|创建|画|绘制|出|做|来|重做|重新生成)"
)
_VIDEO_GENERATION_REQUEST = re.compile(
    r"(?:生成|制作|创建|做|来|重做|重新生成).{0,20}(?:视频|动画|短片|片段)"
    r"|(?:视频|动画|短片|片段).{0,16}(?:生成|制作|创建|做|来|重做|重新生成)"
    r"|(?:让|把).{0,20}(?:图片|图|画面).{0,10}(?:动起来|做成视频|变成视频)"
)
_REFERENCE_REQUEST = re.compile(
    r"(?:基于|参考|按照|仿照|沿用|保持|类似|像|根据).{0,16}"
    r"(?:这张|上张|上一张|刚才|上传|参考|原图|图片|图像|画面)"
    r"|(?:这张|上张|上一张|刚才|上传|参考|原图).{0,16}"
    r"(?:生成|制作|做|改|变|动起来)"
)
_MEDIA_REVISION_REQUEST = re.compile(
    r"(?:上一张|上张|刚才(?:那张|的)?|这张|这个画面|它|原图|上一版|上个版本)"
    r"|(?:继续|保持|保留|沿用|参考|基于|按照|仿照|调整|修改|改成|改为|换成|替换|微调|重做)"
    r"|(?:再|另外|同时|并且)?(?:加上|加入|添加|补上|放入|移除|去掉|删掉).{0,24}"
    r"|(?:再|更).{0,12}(?:一点|一些|明显|自然|靠左|靠右|居中|明亮|暗|快|慢)"
)
_FRESH_MEDIA_REQUEST = re.compile(
    r"(?:不要|不再|无需).{0,8}(?:参考|沿用|基于|使用).{0,8}(?:上一张|上张|刚才|原图)"
    r"|(?:全新|完全不同|另一个|另一种|新主题|重新开始)"
)
_MEDIA_PROMPT_REWRITE_REQUEST = re.compile(
    r"(?:生成|创作|优化|润色|改写|扩写|完善|增强|整理|重写|加工|设计|编写).{0,16}(?:提示词|prompt)"
    r"|(?:提示词|prompt).{0,16}(?:生成|创作|优化|润色|改写|扩写|完善|增强|整理|重写|加工|设计|编写)"
    r"|(?:帮我|请你|让你|由你|AI|智能体).{0,10}(?:处理|想|写|改|优化|完善).{0,8}(?:提示词|prompt)"
)


def should_reuse_historical_image(content: str) -> bool:
    text = content.strip()
    return bool(text and _MEDIA_REVISION_REQUEST.search(text) and not _FRESH_MEDIA_REQUEST.search(text))


def media_prompt_rewrite_allowed(
    content: str,
    *,
    recent_messages: list[dict[str, str]],
    has_selected_skills: bool,
) -> bool:
    if has_selected_skills or should_reuse_historical_image(content):
        return True
    if _MEDIA_PROMPT_REWRITE_REQUEST.search(content):
        return True
    return any(
        item.get("role") == AgentMessageRole.USER.value
        and _MEDIA_PROMPT_REWRITE_REQUEST.search(item.get("content") or "")
        for item in recent_messages[-8:]
    )


def infer_explicit_media_action(
    content: str,
    *,
    available_attachments: list[AgentRuntimeAttachment],
    available_models: list[AIModel],
) -> PersonalMediaAction | None:
    """Conservative fallback for models that ignore the media action contract.

    It only handles explicit generation commands. Questions about capabilities or
    ordinary image analysis stay as chat, which avoids charging users for an
    accidental media task.
    """
    text = content.strip()
    has_image_generation_request = bool(_IMAGE_GENERATION_REQUEST.search(text))
    has_video_generation_request = bool(_VIDEO_GENERATION_REQUEST.search(text))
    only_discusses_media_prompt = bool(
        re.search(r"(?:图片|视频).{0,6}提示词|提示词.{0,6}(?:图片|视频)", text)
        and not has_image_generation_request
        and not has_video_generation_request
    )
    if (
        not text
        or _MEDIA_CAPABILITY_QUESTION.search(text)
        or re.search(r"(?:如何|怎么|怎样|为什么|介绍|解释).{0,12}(?:生成|制作).{0,8}(?:图片|视频)", text)
        or only_discusses_media_prompt
    ):
        return None
    media_type: Literal["image", "video"] | None = None
    if has_video_generation_request:
        media_type = "video"
    elif has_image_generation_request:
        media_type = "image"
    elif available_attachments and re.search(r"(?:让|把)?它.{0,4}(?:动起来|做成视频|变成视频)", text):
        media_type = "video"
    elif available_attachments and re.search(
        r"(?:照着|基于|参考|按照|仿照).{0,16}(?:再)?(?:做|生成|画|来|出).{0,4}(?:一张|一幅|一个)"
        r"|(?:再|重新)(?:做|生成|画|来|出)?一张",
        text,
    ):
        media_type = "image"
    if media_type is None:
        return None

    image_attachments = [item for item in available_attachments if item.mime_type.startswith("image/")]
    use_reference = bool(image_attachments) and bool(
        _REFERENCE_REQUEST.search(text) or should_reuse_historical_image(text)
    )
    if media_type == "video" and image_attachments and re.search(r"(?:动起来|图生视频)", text):
        use_reference = True
    generation_mode = (
        "image_to_image"
        if media_type == "image" and use_reference
        else "image_to_video"
        if media_type == "video" and use_reference
        else "text_to_image"
        if media_type == "image"
        else "text_to_video"
    )

    ratio_match = re.search(r"(?<!\d)(1:1|2:3|3:2|3:4|4:3|4:5|5:4|9:16|16:9|21:9)(?!\d)", text)
    image_resolution = re.search(r"(?i)(?<!\w)(1|2|4)\s*k(?!\w)", text)
    video_resolution = re.search(r"(?i)(?<!\w)(480|540|720|1080|1440|2160)\s*p(?!\w)", text)
    duration = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:秒|s(?:ec(?:ond)?s?)?)(?!\w)", text, re.I)
    requested_model = next(
        (
            model.id
            for model in sorted(available_models, key=lambda item: len(item.name), reverse=True)
            if model.model_type.value == media_type
            and (model.name.lower() in text.lower() or model.model_id.lower() in text.lower())
        ),
        None,
    )
    return PersonalMediaAction(
        type=media_type,
        generation_mode=generation_mode,
        prompt=text,
        model_id=requested_model,
        resolution=(
            f"{image_resolution.group(1)}K"
            if media_type == "image" and image_resolution
            else f"{video_resolution.group(1)}p"
            if media_type == "video" and video_resolution
            else None
        ),
        aspect_ratio=ratio_match.group(1) if ratio_match else None,
        duration_seconds=float(duration.group(1)) if media_type == "video" and duration else None,
        reference_attachment_ids=[item.id for item in image_attachments] if use_reference else [],
    )


def media_action_from_response(content: str) -> PersonalChatResponsePayload | None:
    """Parse the small response envelope used by ordinary personal chat.

    Older/third-party text models may still return plain text. Returning None
    keeps those conversations compatible while models that follow the contract
    can trigger a real media task below.
    """
    marker = re.search(
        r"<CINEFORGE_MEDIA>\s*(\{.*?\})\s*</CINEFORGE_MEDIA>",
        content,
        re.S,
    )
    if marker is None:
        return None
    try:
        action = PersonalMediaAction.model_validate(json.loads(marker.group(1)))
    except (json.JSONDecodeError, ValidationError):
        raise RuntimeError("AI 返回的媒体动作结构不符合要求") from None
    return PersonalChatResponsePayload(
        message=content[: marker.start()].rstrip(),
        media=action,
    )


def validate_personal_media_action(action: PersonalMediaAction) -> PersonalMediaAction:
    if action.type == "image" and action.generation_mode not in {"text_to_image", "image_to_image"}:
        raise RuntimeError("图片媒体动作的 generation_mode 无效")
    if action.type == "video" and action.generation_mode not in {"text_to_video", "image_to_video"}:
        raise RuntimeError("视频媒体动作的 generation_mode 无效")
    if action.generation_mode.startswith("text_to_") and action.reference_attachment_ids:
        # The user can still attach an image for visual context without forcing
        # a reference-generation mode. The Agent's explicit mode remains
        # authoritative, so do not silently change it here.
        action.reference_attachment_ids = []
    return action


def default_image_options(capabilities: dict[str, object]) -> tuple[str, str]:
    raw_resolution = capabilities.get("default_resolution")
    resolution = normalize_image_resolution(str(raw_resolution or ""))
    if resolution is None:
        resolutions = capabilities.get("resolutions")
        if isinstance(resolutions, list):
            resolution = next(
                (
                    item
                    for item in (normalize_image_resolution(str(value)) for value in resolutions)
                    if item is not None
                ),
                None,
            )
    if resolution is None:
        size_map = capabilities.get("size_map")
        if isinstance(size_map, dict):
            resolution = next(
                (
                    item
                    for item in (normalize_image_resolution(str(value)) for value in size_map)
                    if item is not None
                ),
                None,
            )
    aspect_ratios = capabilities.get("aspect_ratios")
    aspect_ratio = (
        str(aspect_ratios[0])
        if isinstance(aspect_ratios, list) and aspect_ratios and str(aspect_ratios[0])
        else "1:1"
    )
    return resolution or "1K", aspect_ratio


def default_video_options(capabilities: dict[str, object]) -> tuple[str, str, float]:
    mappings = capabilities.get("duration_resolution_map")
    first_mapping = mappings[0] if isinstance(mappings, list) and mappings else {}
    if not isinstance(first_mapping, dict):
        first_mapping = {}
    resolutions = first_mapping.get("resolutions")
    durations = first_mapping.get("durations")
    resolution = (
        str(resolutions[0])
        if isinstance(resolutions, list) and resolutions and str(resolutions[0])
        else "720p"
    )
    aspect_ratios = capabilities.get("aspect_ratios")
    aspect_ratio = (
        str(aspect_ratios[0])
        if isinstance(aspect_ratios, list) and aspect_ratios and str(aspect_ratios[0])
        else "16:9"
    )
    duration = (
        float(durations[0]) if isinstance(durations, list) and durations and float(durations[0]) >= 1 else 5.0
    )
    return resolution, aspect_ratio, duration


def personal_media_model_catalog(models: list[AIModel]) -> str:
    rows: list[str] = []
    for model in models:
        capabilities = dict(model.capabilities or {})
        supported = {
            key: capabilities[key]
            for key in (
                "resolutions",
                "aspect_ratios",
                "durations",
                "duration_resolution_map",
                "generation_modes",
                "default_resolution",
            )
            if key in capabilities
        }
        rows.append(
            f"- {model.model_type.value}：{model.name}（ID: {model.id}；"
            f"平台模型名: {model.model_id}；默认: {'是' if model.is_default else '否'}；"
            f"能力: {json.dumps(supported, ensure_ascii=False, separators=(',', ':')) or '{}'}）"
        )
    return "\n".join(rows) or "- 当前没有可调用的图片或视频模型"


async def resolve_personal_media_model(
    session: AsyncSession,
    *,
    task: AITask,
    media_type: ModelType,
    requested_model: str | None,
    resolution: str | None = None,
) -> tuple[AIModel, Provider]:
    model: AIModel | None = None
    normalized_requested = str(requested_model or "").strip()
    if normalized_requested:
        model = await session.get(AIModel, normalized_requested)
        if model is None:
            model = await session.scalar(
                select(AIModel).where(
                    AIModel.tenant_id == task.tenant_id,
                    AIModel.model_type == media_type,
                    AIModel.enabled.is_(True),
                    or_(
                        AIModel.model_id == normalized_requested,
                        AIModel.name == normalized_requested,
                    ),
                )
            )
    elif media_type == ModelType.IMAGE and resolution:
        model = await resolve_image_model(
            session,
            tenant_id=task.tenant_id,
            resolution=resolution,
        )
    else:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == task.tenant_id,
                AIModel.model_type == media_type,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if (
        model is None
        or model.tenant_id != task.tenant_id
        or model.model_type != media_type
        or not model.enabled
    ):
        label = "图片" if media_type == ModelType.IMAGE else "视频"
        raise RuntimeError(f"当前没有可用的{label}模型，请联系管理员配置默认模型")
    provider = await session.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
        raise RuntimeError("所选媒体模型所属平台当前不可用")
    return model, provider


def default_gateway_factory(provider: Provider) -> OpenAICompatibleMediaGateway:
    credentials: dict[str, str] = {}
    encrypted_credentials = SecretBox().decrypt(provider.encrypted_credentials)
    if encrypted_credentials:
        decoded = json.loads(encrypted_credentials)
        if isinstance(decoded, dict):
            credentials = {str(key): str(value) for key, value in decoded.items()}
    return OpenAICompatibleMediaGateway(
        base_url=provider.base_url,
        api_key=SecretBox().decrypt(provider.encrypted_api_key),
        extra_headers=provider.extra_headers,
        provider_code=provider.code,
        adapter_config=provider.adapter_config,
        credentials=credentials,
    )


def media_content_type_from_key_or_bytes(object_key: str, data: bytes) -> str:
    guessed = mimetypes.guess_type(object_key)[0]
    if guessed:
        return guessed
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def video_prompt_protocol(target_video_model: dict[str, object]) -> dict[str, object]:
    capabilities = target_video_model.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    provider_code = str(target_video_model.get("provider_code") or "").strip().lower()
    model_id = str(target_video_model.get("model_id") or "").strip().lower()
    configured_protocol = str(capabilities.get("video_prompt_protocol") or "").strip().lower()
    is_h3 = (
        configured_protocol == "minimax_h3"
        or provider_code == AUTODL_MINIMAX_H3_PROVIDER_CODE
        or model_id == AUTODL_MINIMAX_H3_MODEL_ID
    )
    languages = [
        str(value).strip() for value in capabilities.get("prompt_languages", []) if str(value).strip()
    ]
    configured_language = str(capabilities.get("preferred_prompt_language") or "").strip()
    if is_h3:
        preferred_language = "en"
    elif configured_language and (not languages or configured_language in languages):
        preferred_language = configured_language
    elif "zh-CN" in languages:
        preferred_language = "zh-CN"
    elif "zh" in languages:
        preferred_language = "zh"
    else:
        preferred_language = languages[0] if languages else "zh-CN"
    return {
        "name": "minimax_h3" if is_h3 else "generic",
        "preferred_prompt_language": preferred_language,
        "dialogue_language_policy": "preserve_original",
    }


def video_audio_enabled(capabilities: dict[str, object], requested: bool = False) -> bool:
    policy = str(capabilities.get("audio_policy") or "optional")
    if policy == "required":
        return True
    if policy == "disabled":
        return False
    return requested


def limit_video_reference_media(
    references: list[dict[str, str]],
    capabilities: dict[str, object],
) -> list[dict[str, str]]:
    limits = capabilities.get("reference_limits")
    image_limit = limits.get("image") if isinstance(limits, dict) else None
    if not isinstance(image_limit, dict) or not image_limit.get("enabled"):
        return []
    maximum = int(image_limit.get("max_count") or 0)
    return references[:maximum] if maximum > 0 else []


def resolve_video_generation_mode(
    capabilities: dict[str, object],
    *,
    image_reference_count: int,
) -> str:
    configured = capabilities.get("generation_modes", ["text_to_video"])
    modes = [str(value) for value in configured] if isinstance(configured, list) else []
    if image_reference_count > 1 and "multi_shot" in modes:
        return "multi_shot"
    if image_reference_count and "first_frame" in modes:
        return "first_frame"
    if image_reference_count and "multi_shot" in modes:
        return "multi_shot"
    if "text_to_video" in modes:
        return "text_to_video"
    raise RuntimeError("当前视频模型没有与镜头参考图匹配的生成模式")


def video_model_execution_contract(
    video_model: AIModel | None,
    *,
    requested_resolution: str = "",
    aspect_ratio: str = "",
    audio_enabled: bool = False,
) -> dict[str, object]:
    capabilities = dict(video_model.capabilities or {}) if video_model else {}
    durations = supported_video_durations(capabilities)
    return {
        "model_record_id": video_model.id if video_model else None,
        "model_id": video_model.model_id if video_model else None,
        "model_name": video_model.name if video_model else None,
        "generation_modes": capabilities.get("generation_modes", ["text_to_video"]),
        "supported_durations_seconds": durations,
        "duration_resolution_map": capabilities.get("duration_resolution_map", []),
        "requested_resolution": requested_resolution,
        "supported_aspect_ratios": capabilities.get("aspect_ratios", []),
        "selected_aspect_ratio": aspect_ratio,
        "reference_limits": capabilities.get("reference_limits", {}),
        "audio_policy": capabilities.get("audio_policy", "optional"),
        "audio_enabled_for_this_task": video_audio_enabled(capabilities, audio_enabled),
        "prompt_languages": capabilities.get("prompt_languages", ["zh-CN"]),
        "preferred_prompt_language": capabilities.get("preferred_prompt_language"),
        "negative_prompt_supported": bool(capabilities.get("negative_prompt_supported")),
    }


def image_model_execution_contract(
    image_model: AIModel | None,
    *,
    selected_resolution: str,
    aspect_ratio: str,
) -> dict[str, object]:
    capabilities = dict(image_model.capabilities or {}) if image_model else {}
    size_map = capabilities.get("size_map")
    configured_resolutions = capabilities.get("resolutions")
    supported_resolutions = (
        configured_resolutions
        if isinstance(configured_resolutions, list)
        else list(size_map.keys())
        if isinstance(size_map, dict)
        else []
    )
    return {
        "model_record_id": image_model.id if image_model else None,
        "model_id": image_model.model_id if image_model else None,
        "model_name": image_model.name if image_model else None,
        "selected_resolution": selected_resolution,
        "selected_aspect_ratio": aspect_ratio,
        "generation_modes": capabilities.get("generation_modes", ["text_to_image"]),
        "supported_resolutions": supported_resolutions,
        "supported_aspect_ratios": capabilities.get("aspect_ratios", []),
        "prompt_languages": capabilities.get("prompt_languages", ["zh-CN"]),
        "input_mime_types": capabilities.get("input_mime_types", []),
        "output_formats": capabilities.get("output_formats", []),
        "negative_prompt_supported": bool(capabilities.get("negative_prompt_supported")),
    }


def enforce_video_audio_policy(
    prompt: str,
    dialogue: str,
    *,
    audio_enabled: bool,
    protocol_name: str,
    language: str,
) -> str:
    silent_instruction_zh = (
        "本任务关闭音频：生成完全无声的视频，不得出现对白、旁白、歌唱、音乐、环境声、"
        "音效或任何虚构语言；人物不得开口说话。"
    )
    silent_instruction_en = (
        "Audio is disabled for this task. Generate a completely silent video with no speech, "
        "voiceover, singing, music, ambient sound, sound effects, or invented language."
    )
    if audio_enabled:
        cleaned_prompt = prompt.replace(silent_instruction_zh, "").replace(
            silent_instruction_en,
            "",
        )
        locked_prompt = ensure_video_prompt_dialogue_locks(
            cleaned_prompt.strip(),
            dialogue,
            language=language,
        )
        has_dialogue = bool(dialogue_lock_lines(dialogue))
        if language.lower().startswith("zh"):
            audio_lock = (
                "音频约束：只能由对应角色逐句、按顺序说出锁定的原文台词；任一时刻最多一人发声；"
                "禁止新增人声、翻译台词、重叠对白、歌唱或出现其它语言。"
                if has_dialogue
                else (
                    "音频约束：当前镜头没有锁定台词，禁止任何对白、旁白、歌唱、人声或虚构语言；"
                    "除非提示词明确给出，否则不得自行添加音乐、环境声或音效。"
                )
            )
        else:
            audio_lock = (
                "Audio lock: use only the original locked dialogue with its assigned speakers, one "
                "speaker at a time and in sequence; no invented voices, translated lines, overlapping "
                "speech, singing, or other languages."
                if has_dialogue
                else (
                    "Audio lock: this shot has no locked dialogue. Do not generate speech, voiceover, "
                    "singing, human vocalization, or invented language. Do not invent music, ambience, "
                    "or sound effects unless they are explicitly specified in the prompt."
                )
            )
        return f"{locked_prompt.strip()}\n\n{audio_lock}"
    silent_prompt = prompt
    for line in dialogue_lock_lines(dialogue):
        silent_prompt = silent_prompt.replace(f"<d>[Chinese]{line}</d>", "")
        silent_prompt = silent_prompt.replace(line, "")
    if protocol_name == "minimax_h3":
        silent_prompt = re.sub(
            r"overall_soundscape:\s*.*?(?=\n\s*non_diegetic_music:)",
            "overall_soundscape: N/A",
            silent_prompt,
            flags=re.DOTALL,
        )
        silent_prompt = re.sub(
            r"non_diegetic_music:\s*.*$",
            "non_diegetic_music: N/A",
            silent_prompt,
            flags=re.DOTALL,
        )
        return (
            f"{silent_prompt.strip()}\n\n"
            "Audio is disabled for this task. Generate a completely silent video: no speech, "
            "voiceover, singing, music, ambient sound, sound effects, or invented language."
        )
    suffix = silent_instruction_zh if language.lower().startswith("zh") else silent_instruction_en
    return f"{silent_prompt.strip()}\n\n{suffix}"


def reference_lock_text(reference: dict[str, str], *, language: str = "en") -> str:
    token = reference.get("token", "")
    role = reference.get("role", "")
    asset_names = reference.get("asset_names") or reference.get("asset_name") or ""
    asset_type = reference.get("asset_type", "")
    description = reference.get("asset_description", "")
    if language.lower().startswith("zh"):
        if role == "first_frame":
            return f"{token} 是 0.00 秒的精确首帧视觉锚点"
        label = f"资产参考图 {asset_names}" if asset_names else "资产参考图"
        if asset_type:
            label = f"{label}（{asset_type}）"
        if description:
            return f"{token} 是固定的{label}，必须保持：{description}"
        return f"{token} 是固定的{label}"
    if role == "first_frame":
        return f"{token} is the exact 0.00-second first-frame visual anchor"
    label = "asset reference"
    if asset_names:
        label = f"asset reference for {asset_names}"
    if asset_type:
        label = f"{label} ({asset_type})"
    if description:
        return f"{token} is the fixed {label}; preserve {description}"
    return f"{token} is the fixed {label}"


def ensure_video_prompt_reference_locks(
    prompt: str,
    references: list[dict[str, str]],
    *,
    language: str = "en",
) -> str:
    if not references:
        return prompt
    missing_tokens = [reference["token"] for reference in references if reference["token"] not in prompt]
    if not missing_tokens:
        return prompt
    lock_items = "; ".join(reference_lock_text(reference, language=language) for reference in references)
    locks = (
        f"参考图锁定：{lock_items}。 "
        if language.lower().startswith("zh")
        else f"Reference locks: {lock_items}. "
    )
    marker = "integrated_multimodal_description:"
    if marker in prompt:
        return prompt.replace(marker, f"{marker} {locks}", 1)
    return f"{locks}{prompt}"


def dialogue_lock_lines(dialogue: str) -> list[str]:
    return [line.strip() for line in re.split(r"[\r\n]+", dialogue) if line.strip()]


def ensure_video_prompt_dialogue_locks(
    prompt: str,
    dialogue: str,
    *,
    language: str = "en",
) -> str:
    lines = dialogue_lock_lines(dialogue)
    if not lines:
        return prompt
    missing_lines = [line for line in lines if line not in prompt]
    if not missing_lines:
        return prompt
    if language.lower().startswith("zh"):
        locks = (
            "台词锁定：以下对白、旁白、歌词和画内文字必须逐字保留，不得翻译或改写："
            + "；".join(lines)
            + "。 "
        )
    else:
        locks = (
            "Dialogue locks: Spoken dialogue, narration, lyrics, and visible text must remain in "
            "the original language with exact wording; do not translate, rewrite, romanize, or "
            "summarize these lines: " + "; ".join(f"<d>[Chinese]{line}</d>" for line in lines) + ". "
        )
    for marker in ("integrated_multimodal_description:", "detailed_description:"):
        if marker in prompt:
            return prompt.replace(marker, f"{marker} {locks}", 1)
    return f"{locks}{prompt}"


def _seconds_label(value: float) -> str:
    return f"{value:g}s"


def storyboard_duration_contract(video_model: AIModel | None) -> dict[str, object]:
    durations = supported_video_durations(video_model.capabilities if video_model else None)
    if not durations:
        durations = [float(value) for value in range(1, 31)]
    minimum = min(durations)
    maximum = max(durations)
    span = maximum - minimum
    short_ceiling = minimum + span * 0.25
    long_floor = minimum + span * 0.7
    short_durations = [value for value in durations if value <= short_ceiling]
    long_durations = [value for value in durations if value >= long_floor]
    standard_durations = [
        value for value in durations if value not in short_durations and value not in long_durations
    ]
    if not standard_durations:
        standard_durations = [durations[len(durations) // 2]]
    return {
        "model_name": video_model.name if video_model else None,
        "model_id": video_model.model_id if video_model else None,
        "supported_durations_seconds": durations,
        "min_duration_seconds": minimum,
        "max_duration_seconds": maximum,
        "duration_bands_seconds": {
            "short": short_durations,
            "standard": standard_durations,
            "long": long_durations,
        },
        "schema_example_duration_seconds": durations[len(durations) // 2],
        "selection_guide": [
            "短档仅用于单一瞬时动作、插入特写、短反应或快速转折。",
            "中档用于常规对白加反应、两拍连续动作、适度运镜或空间关系交代。",
            "长档用于不可拆分的持续表演、带停顿的关键台词、完整场面调度、情绪停留或缓慢空间揭示。",
            "先估算台词、动作、反应、停顿和运镜所需时间，再选择不短于该需求的受支持时长；过长则按语义或动作节点拆镜。",
            "不得因示例值或列表顺序默认选择最短时长，也不得为使用长时长而填充静止空白。",
            "相邻镜头应形成有依据的长短节奏变化；除非剧本明确是快速蒙太奇，不应让大多数镜头落在最短档。",
        ],
        "rule": (
            "每个镜头的 duration_seconds 必须从 supported_durations_seconds 中选择；"
            "不要生成该列表之外的时长，并按 duration_bands_seconds 与 selection_guide 决策。"
        ),
    }


def normalize_storyboard_duration_for_model(
    duration_seconds: Decimal,
    video_model: AIModel | None,
) -> Decimal:
    requested = float(duration_seconds)
    durations = supported_video_durations(video_model.capabilities if video_model else None)
    if durations:
        normalized = next((value for value in durations if value >= requested), durations[-1])
    else:
        normalized = closest_supported_video_duration(None, requested_duration=requested)
    return Decimal(str(normalized))


async def project_video_model_for_storyboard(
    session: AsyncSession,
    *,
    project: Project | None,
    tenant_id: str,
) -> AIModel | None:
    model = await session.get(AIModel, project.video_model_id) if project and project.video_model_id else None
    if project and project.creation_mode == "ai":
        if model is None or model.model_type != ModelType.VIDEO or not model.enabled:
            raise RuntimeError("AI 项目所选视频模型不可用，请调整项目设置")
        provider = await session.get(Provider, model.provider_id)
        if model.tenant_id != tenant_id or provider is None or not provider.enabled:
            raise RuntimeError("AI 项目所选视频模型平台不可用")
        return model
    if model is None:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == tenant_id,
                AIModel.model_type == ModelType.VIDEO,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if model is None or model.model_type != ModelType.VIDEO or not model.enabled:
        return None
    return model


async def project_image_model_for_storyboard(
    session: AsyncSession,
    *,
    project: Project | None,
    tenant_id: str,
) -> AIModel | None:
    if project is None:
        return None
    if project.creation_mode == "ai":
        model = await session.get(AIModel, project.image_model_id) if project.image_model_id else None
        if (
            model is None
            or model.model_type != ModelType.IMAGE
            or not model.enabled
            or model.tenant_id != tenant_id
        ):
            raise RuntimeError("AI 项目的图片模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or not provider.enabled:
            raise RuntimeError("AI 项目所选图片模型平台不可用")
        return model
    return await resolve_image_model(
        session,
        tenant_id=tenant_id,
        resolution=project.image_resolution,
        fallback_model_id=project.image_model_id,
    )


async def prepare_adapter_reference_media(
    provider: Provider,
    references: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not provider.adapter_config or not references:
        return references
    adapter = ProviderAdapterConfig.model_validate(provider.adapter_config)
    if not adapter.video:
        return references
    source_by_type = {mapping.media_type: mapping.source for mapping in adapter.video.references}
    prepared: list[dict[str, str]] = []
    for reference in references:
        item = dict(reference)
        media_type = item.get("type", "")
        source = source_by_type.get(media_type)
        if source not in {"data_uri", "base64"} or item.get(source):
            prepared.append(item)
            continue
        object_key = object_key_from_media_url(item.get("url"))
        if not object_key:
            raise RuntimeError(f"{media_type} 参考媒体必须来自项目文件库，无法转换为 {source}")
        data = await object_storage().get_bytes(object_key)
        if not data:
            raise RuntimeError(f"{media_type} 参考媒体文件为空")
        content_type = item.get("mime_type") or media_content_type_from_key_or_bytes(object_key, data)
        if media_type == "image" and not content_type.startswith("image/"):
            raise RuntimeError(f"视频参考图的文件类型无效：{object_key}，识别为 {content_type}")
        encoded = base64.b64encode(data).decode("ascii")
        item[source] = f"data:{content_type};base64,{encoded}" if source == "data_uri" else encoded
        prepared.append(item)
    return prepared


def default_runtime_factory() -> AgentRuntimeClient:
    settings = get_settings()
    return AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_internal_token,
        settings.agent_runtime_timeout_seconds,
    )


def default_renderer_factory() -> CompositionRenderer:
    return FFmpegCompositionRenderer()


async def queued_provider_candidates() -> list[str]:
    async with SessionLocal() as session:
        ranked = (
            select(
                AITask.id.label("task_id"),
                AITask.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=AIModel.provider_id,
                    order_by=(AITask.created_at, AITask.id),
                )
                .label("provider_rank"),
            )
            .outerjoin(AIModel, AIModel.id == AITask.model_id)
            .where(AITask.status == TaskStatus.QUEUED)
            .subquery()
        )
        return list(
            (
                await session.scalars(
                    select(ranked.c.task_id)
                    .where(ranked.c.provider_rank == 1)
                    .order_by(ranked.c.created_at, ranked.c.task_id)
                )
            ).all()
        )


async def _claim_task_candidate(task_id: str) -> tuple[AITask, TaskEvent] | None:
    async with SessionLocal() as session, session.begin():
        task = await session.scalar(
            select(AITask)
            .where(AITask.id == task_id, AITask.status == TaskStatus.QUEUED)
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return None
        provider_id = None
        if task.model_id:
            provider_id = await session.scalar(select(AIModel.provider_id).where(AIModel.id == task.model_id))
        if provider_id:
            provider = await session.scalar(
                select(Provider).where(Provider.id == provider_id).with_for_update()
            )
            if provider is not None:
                running_count = await session.scalar(
                    select(func.count(AITask.id))
                    .select_from(AITask)
                    .join(AIModel, AIModel.id == AITask.model_id)
                    .where(
                        AIModel.provider_id == provider.id,
                        AITask.status == TaskStatus.RUNNING,
                    )
                )
                if int(running_count or 0) >= provider.max_concurrency:
                    return None
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=get_settings().task_lease_timeout_seconds)
        task.status = TaskStatus.RUNNING
        task.worker_id = WORKER_ID
        task.lease_expires_at = lease_expires_at
        task.heartbeat_at = now
        task.started_at = now
        task.completed_at = None
        event = record_task_event(
            session,
            task,
            status=TaskStatus.RUNNING,
            progress=10,
            message="任务已开始执行",
        )
        await session.flush()
    return task, event


async def _provider_id_for_task(task_id: str) -> str | None:
    async with SessionLocal() as session:
        return await session.scalar(
            select(AIModel.provider_id)
            .join(AITask, AITask.model_id == AIModel.id)
            .where(AITask.id == task_id, AITask.status == TaskStatus.QUEUED)
        )


async def _claim_with_local_provider_lock(task_id: str) -> tuple[AITask, TaskEvent] | None:
    provider_id = await _provider_id_for_task(task_id)
    if provider_id is None:
        return await _claim_task_candidate(task_id)
    lock = _provider_claim_locks.setdefault(provider_id, asyncio.Lock())
    async with lock:
        return await _claim_task_candidate(task_id)


async def claim_task(task_id: str | None = None) -> str | None:
    candidate_ids = [task_id] if task_id is not None else await queued_provider_candidates()
    for candidate_id in candidate_ids:
        claimed = await _claim_with_local_provider_lock(candidate_id)
        if claimed is None:
            continue
        task, event = claimed
        await publish_task_event(task, event)
        return task.id
    return None


async def process_next_task(
    gateway_factory: GatewayFactory = default_gateway_factory,
    runtime_factory: RuntimeFactory = default_runtime_factory,
    renderer_factory: RendererFactory = default_renderer_factory,
) -> str | None:
    task_id = await claim_task()
    if task_id is None:
        return None
    await run_claimed_task(task_id, gateway_factory, runtime_factory, renderer_factory)
    return task_id


async def process_task(
    task_id: str,
    gateway_factory: GatewayFactory = default_gateway_factory,
    runtime_factory: RuntimeFactory = default_runtime_factory,
    renderer_factory: RendererFactory = default_renderer_factory,
) -> bool:
    claimed_id = await claim_task(task_id)
    if claimed_id is None:
        return False
    await run_claimed_task(claimed_id, gateway_factory, runtime_factory, renderer_factory)
    return True


async def run_claimed_task(
    task_id: str,
    gateway_factory: GatewayFactory,
    runtime_factory: RuntimeFactory,
    renderer_factory: RendererFactory,
) -> None:
    from app.services.director_orchestration import mark_child_running, on_director_task_terminal

    watcher_stop = asyncio.Event()
    heartbeat = asyncio.create_task(heartbeat_task(task_id, watcher_stop))
    current_task = asyncio.current_task()
    cancellation_watch = asyncio.create_task(cancellation_watcher(task_id, current_task, watcher_stop))
    activity = asyncio.Event()
    _task_activity_events[task_id] = activity
    try:
        async with SessionLocal() as session:
            task_type = await session.scalar(select(AITask.task_type).where(AITask.id == task_id))
        timeout_seconds = (
            get_settings().agent_chat_task_timeout_seconds
            if task_type in {"agent_chat_run", "agent_memory_maintenance"}
            else None
        )
        await mark_child_running(task_id)
        if timeout_seconds is not None:
            await run_with_idle_timeout(
                execute_task(task_id, gateway_factory, runtime_factory, renderer_factory),
                activity,
                timeout_seconds,
            )
        else:
            await execute_task(task_id, gateway_factory, runtime_factory, renderer_factory)
        try:
            await on_director_task_terminal(task_id)
        except Exception:
            logger.exception("Director workflow advancement failed for completed task %s", task_id)
    except asyncio.CancelledError:
        if await task_is_cancelled(task_id):
            logger.info("Task %s stopped after user cancellation", task_id)
            return
        raise
    except TimeoutError:
        timeout_seconds = get_settings().agent_chat_task_timeout_seconds
        task_label = "AI 对话" if task_type == "agent_chat_run" else "AI 任务"
        message = f"{task_label}连续 {timeout_seconds:g} 秒未收到响应或进度，任务已中断"
        logger.warning("Task %s was idle for %s seconds", task_id, timeout_seconds)
        await fail_task(task_id, message)
        with suppress(Exception):
            await on_director_task_terminal(task_id)
    except ProviderJobTerminalError as exc:
        logger.exception("Provider job for task %s failed terminally", task_id)
        await fail_task(task_id, _safe_error_message(exc), provider_job_terminal=True)
        with suppress(Exception):
            await on_director_task_terminal(task_id)
    except Exception as exc:
        logger.exception("Task %s failed", task_id)
        await fail_task(task_id, _safe_error_message(exc))
        with suppress(Exception):
            await on_director_task_terminal(task_id)
    finally:
        if _task_activity_events.get(task_id) is activity:
            _task_activity_events.pop(task_id, None)
        watcher_stop.set()
        await asyncio.gather(heartbeat, cancellation_watch, return_exceptions=True)


async def task_is_cancelled(task_id: str) -> bool:
    async with SessionLocal() as session:
        status = await session.scalar(select(AITask.status).where(AITask.id == task_id))
        return status == TaskStatus.CANCELLED


async def cancellation_watcher(
    task_id: str,
    owner: asyncio.Task | None,
    stop: asyncio.Event,
) -> None:
    if owner is None:
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
            return
        except TimeoutError:
            pass
        async with SessionLocal() as session:
            status = await session.scalar(select(AITask.status).where(AITask.id == task_id))
        if status == TaskStatus.CANCELLED:
            owner.cancel()
            return
        if status != TaskStatus.RUNNING:
            return


async def heartbeat_task(task_id: str, stop: asyncio.Event) -> None:
    interval = get_settings().task_heartbeat_seconds
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        async with SessionLocal() as session:
            now = datetime.now(UTC)
            heartbeat = await session.execute(
                update(AITask)
                .where(
                    AITask.id == task_id,
                    AITask.status == TaskStatus.RUNNING,
                    AITask.worker_id == WORKER_ID,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=get_settings().task_lease_timeout_seconds),
                )
            )
            if heartbeat.rowcount != 1:
                await session.rollback()
                return
            await session.commit()


async def recover_stale_tasks() -> int:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=get_settings().task_lease_timeout_seconds)
    recovered: list[tuple[AITask, TaskEvent, bool]] = []
    async with SessionLocal() as session:
        tasks = list(
            (
                await session.scalars(
                    select(AITask)
                    .where(
                        AITask.status == TaskStatus.RUNNING,
                        or_(
                            AITask.lease_expires_at < now,
                            AITask.heartbeat_at < cutoff,
                            and_(
                                AITask.lease_expires_at.is_(None),
                                AITask.heartbeat_at.is_(None),
                                AITask.updated_at < cutoff,
                            ),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for task in tasks:
            result = dict(task.result_payload or {})
            result["recovery_count"] = int(result.get("recovery_count") or 0) + 1
            task.result_payload = result
            provider_job_id = str(task.provider_job_id or result.get("provider_job_id") or "") or None
            if provider_job_id and task.provider_job_id != provider_job_id:
                task.provider_job_id = provider_job_id
            media_record: VideoClip | AudioClip | None = None
            if task.task_type == "shot_video_generation":
                media_record = await session.get(
                    VideoClip,
                    str(task.request_payload.get("video_clip_id") or ""),
                )
            elif task.task_type == "dialogue_tts_generation":
                media_record = await session.get(
                    AudioClip,
                    str(task.request_payload.get("audio_clip_id") or ""),
                )
            if not provider_job_id and media_record is not None:
                provider_job_id = str(media_record.provider_job_id or "") or None
                if provider_job_id:
                    result["provider_job_id"] = provider_job_id
                    task.provider_job_id = provider_job_id
                    task.result_payload = result

            personal_media_mode = (
                str(task.request_payload.get("mode") or "")
                if task.task_type == "agent_chat_run" and task.request_payload.get("scope") == "personal"
                else ""
            )
            restart_unsafe = (
                task.task_type in RESTART_UNSAFE_IMAGE_TASKS
                or (task.task_type in PROVIDER_JOB_TASKS and not provider_job_id)
                or (personal_media_mode == "image" and not result.get("generated_media_draft"))
                or (
                    personal_media_mode == "video"
                    and not provider_job_id
                    and not result.get("generated_media_draft")
                )
            )
            if restart_unsafe:
                task.status = TaskStatus.FAILED
                task.error_message = "Worker 重启时无法确认服务商是否已生成，请手动重试"
                result["restart_disposition"] = "failed_unknown_upstream"
                result["resume_provider_job"] = False
                task.result_payload = result
                if isinstance(media_record, AudioClip) and media_record.status in {
                    AudioClipStatus.QUEUED,
                    AudioClipStatus.GENERATING,
                }:
                    media_record.status = AudioClipStatus.FAILED
                    media_record.error_message = task.error_message
                elif isinstance(media_record, VideoClip) and media_record.status in {
                    VideoClipStatus.QUEUED,
                    VideoClipStatus.GENERATING,
                }:
                    media_record.status = VideoClipStatus.FAILED
                    media_record.error_message = task.error_message
                elif task.task_type == "asset_image_generation":
                    asset = await session.get(
                        Asset,
                        str(task.request_payload.get("asset_id") or ""),
                    )
                    if asset is not None and asset.status == AssetStatus.GENERATING:
                        asset.status = AssetStatus.READY if asset.media_url else AssetStatus.FAILED
                event = record_task_event(
                    session,
                    task,
                    status=TaskStatus.FAILED,
                    progress=100,
                    message=task.error_message,
                    metadata={"recovered": True, "restart_safe": False},
                )
                await refund_task_cost(session, task, reason="媒体任务重启状态未知退款")
                recovered.append((task, event, False))
                continue
            if provider_job_id:
                result["restart_disposition"] = "resume_provider_job"
                result["resume_provider_job"] = True
                task.result_payload = result
            task.status = TaskStatus.QUEUED
            task.error_message = None
            event = record_task_event(
                session,
                task,
                status=TaskStatus.QUEUED,
                progress=0,
                message=(
                    "Worker 重启后已恢复，继续查询服务商任务"
                    if provider_job_id
                    else "Worker 重启后已恢复排队"
                ),
                metadata={
                    "recovered": True,
                    **({"provider_job_id": provider_job_id} if provider_job_id else {}),
                },
            )
            recovered.append((task, event, True))
        await session.commit()
    for task, event, should_enqueue in recovered:
        if should_enqueue:
            await enqueue_task(task.id)
        await publish_task_event(task, event)
    return len(recovered)


async def execute_task(
    task_id: str,
    gateway_factory: GatewayFactory,
    runtime_factory: RuntimeFactory,
    renderer_factory: RendererFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        task_type = task.task_type
        chapter_id = task.request_payload.get("chapter_id")
        if task.project_id and chapter_id:
            from app.services.ai_creation import require_ai_chapter_unlocked

            chapter = await session.get(Chapter, chapter_id)
            if chapter is not None and chapter.project_id == task.project_id:
                await require_ai_chapter_unlocked(session, chapter)
    if task_type == "project_ai_creation":
        from app.services.ai_creation_worker import execute_ai_creation_task

        await execute_ai_creation_task(task_id, runtime_factory)
    elif task_type == "project_cover_generation":
        await execute_cover_task(task_id, gateway_factory)
    elif task_type == "agent_chat_run":
        await execute_agent_chat_task(task_id, runtime_factory, gateway_factory)
    elif task_type == "agent_memory_maintenance":
        await execute_agent_memory_maintenance_task(task_id, runtime_factory)
    elif task_type == "chapter_analysis_generation":
        await execute_chapter_analysis_task(task_id, runtime_factory)
    elif task_type == "chapter_script_generation":
        await execute_script_generation_task(task_id, runtime_factory)
    elif task_type == "chapter_asset_extraction":
        await execute_asset_extraction_task(task_id, runtime_factory)
    elif task_type == "asset_prompt_generation":
        await execute_asset_prompt_task(task_id, runtime_factory)
    elif task_type == "asset_image_generation":
        await execute_asset_image_task(task_id, gateway_factory)
    elif task_type == "chapter_storyboard_generation":
        await execute_storyboard_task(task_id, runtime_factory)
    elif task_type == "shot_video_prompt_generation":
        await execute_shot_video_prompt_task(task_id, runtime_factory)
    elif task_type == "director_script_review":
        await execute_director_script_review_task(task_id, runtime_factory)
    elif task_type == "director_script_repair":
        await execute_director_script_repair_task(task_id, runtime_factory)
    elif task_type == "director_storyboard_review":
        await execute_director_storyboard_review_task(task_id, runtime_factory)
    elif task_type == "director_storyboard_repair":
        await execute_director_storyboard_repair_task(task_id, runtime_factory)
    elif task_type == "shot_video_generation":
        await execute_shot_video_task(task_id, gateway_factory)
    elif task_type == "chapter_dialogue_extraction":
        await execute_dialogue_extraction_task(task_id, runtime_factory)
    elif task_type == "dialogue_tts_generation":
        await execute_dialogue_tts_task(task_id, gateway_factory)
    elif task_type == "storyboard_video_concat":
        from app.services.video_concat import execute_video_concat_task

        await execute_video_concat_task(task_id)
    elif task_type == "chapter_composition_render":
        await execute_composition_render_task(task_id, renderer_factory)
    else:
        raise RuntimeError(f"unsupported task type: {task_type}")


async def execute_cover_task(task_id: str, gateway_factory: GatewayFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        if not task.project_id or not task.model_id:
            raise RuntimeError("cover task is missing project or model")
        project = await session.get(Project, task.project_id)
        model = await session.get(AIModel, task.model_id)
        if project is None or project.tenant_id != task.tenant_id:
            raise RuntimeError("task project is unavailable")
        if model is None or model.tenant_id != task.tenant_id or model.model_type != ModelType.IMAGE:
            raise RuntimeError("task image model is unavailable")
        provider = await session.get(Provider, model.provider_id)
        if (
            provider is None
            or provider.tenant_id != task.tenant_id
            or not provider.enabled
            or not model.enabled
        ):
            raise RuntimeError("task model provider is disabled")
        handbook = (
            await session.get(Handbook, project.visual_handbook_id) if project.visual_handbook_id else None
        )
        prompt = build_cover_prompt(project, handbook)
        request = ImageGenerationRequest(
            model=model.model_id,
            prompt=prompt,
            resolution=project.image_resolution,
            aspect_ratio=project.aspect_ratio,
            capabilities=model.capabilities,
            idempotency_key=task.idempotency_key or task.id,
        )
        gateway = gateway_factory(provider)

    image_data = await gateway.generate_image(request)

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        project = await session.get(Project, task.project_id)
        if project is None:
            raise RuntimeError("task project disappeared before completion")
        _local_url, stored_path = await run_in_threadpool(
            save_project_cover,
            image_data,
            uploads_root=get_settings().uploads_root,
            tenant_id=task.tenant_id,
            project_id=project.id,
        )
        try:
            storage_key, cover_url = await persist_media_file(stored_path, "image/webp")
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        project.cover_url = cover_url
        result = dict(task.result_payload or {})
        result.update(
            {
                "cover_url": cover_url,
                "credit_refunded": False,
                "provider_id": provider.id,
                "model_id": task.model_id,
            }
        )
        task.result_payload = result
        task.status = TaskStatus.SUCCEEDED
        task.error_message = None
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message="封面图片已生成并同步到项目",
            metadata={"cover_url": cover_url},
        )
        try:
            await session.commit()
        except Exception:
            await cleanup_media(storage_key, stored_path)
            await session.rollback()
            raise
        await publish_task_event(task, event)


async def runtime_request(
    task_id: str,
    *,
    prompt_code: str,
    prompt: str,
    prompt_appendix: str = "",
) -> AgentRuntimeRequest:
    from app.api.routes.agent_chat import memory_context, project_handbooks, skill_snapshots
    from app.services.ai_creation import project_creation_guidance

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id or not task.model_id:
            raise RuntimeError("任务上下文已失效")
        project = await session.get(Project, task.project_id)
        user = await session.get(User, task.user_id)
        agent_id = str(task.request_payload.get("agent_profile_id") or "")
        agent = await session.get(AgentProfile, agent_id)
        model = await session.get(AIModel, task.model_id)
        if project is None or user is None or project.tenant_id != task.tenant_id:
            raise RuntimeError("任务项目或用户不可用")
        if agent is None or agent.tenant_id != task.tenant_id or not agent.enabled:
            raise RuntimeError("任务所用 Agent 已停用")
        if (
            model is None
            or model.tenant_id != task.tenant_id
            or model.model_type != ModelType.TEXT
            or not model.enabled
        ):
            raise RuntimeError("任务文本模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("任务文本模型平台不可用")
        api_key = SecretBox().decrypt(provider.encrypted_api_key)
        if not api_key:
            raise RuntimeError("任务文本模型平台缺少 API Key")
        template = await session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.tenant_id == task.tenant_id,
                PromptTemplate.code == prompt_code,
                PromptTemplate.enabled.is_(True),
            )
        )
        handbooks = await project_handbooks(session, project)
        snapshots, skill_versions = await run_in_threadpool(skill_snapshots, handbooks)
        user_stages = prompt_user_skill_stages(prompt_code)
        personal_skills = await enabled_user_skills(
            session,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            stages=user_stages,
        )
        personal_snapshots, personal_versions = user_skill_snapshots(personal_skills)
        snapshots.extend(personal_snapshots)
        skill_versions.update(personal_versions)
        config = agent.config or {}
        capabilities = model.capabilities or {}
        template_content = template.content if template else ""
        from app.services.creation_context import SCRIPT_OUTPUT_BOUNDARY, select_task_skills

        asset_context = []
        if prompt_code == "asset-prompt-generation":
            asset_context = (
                await session.scalars(
                    select(Asset).where(
                        Asset.id.in_(task.request_payload.get("asset_ids") or []),
                        Asset.project_id == project.id,
                        Asset.user_id == task.user_id,
                    )
                )
            ).all()
        selected_skill_snapshots = select_task_skills(snapshots, prompt_code, asset_context)
        skill_context = "\n\n".join(
            f'<skill-file path="{snapshot["path"]}">\n{snapshot["content"]}\n</skill-file>'
            for snapshot in selected_skill_snapshots
        )
        include_story = prompt_code != "asset-prompt-generation"
        creation_guidance = await project_creation_guidance(session, project, include_story=include_story)
        if prompt_code in {"script-generation", "script-repair", "script-review"}:
            creation_guidance += "\n\n" + SCRIPT_OUTPUT_BOUNDARY
        if project.creation_mode == "ai" and task.request_payload.get("chapter_id"):
            from app.services.ai_creation import chapter_continuity_context

            current_chapter = await session.get(Chapter, task.request_payload["chapter_id"])
            if current_chapter and current_chapter.project_id == project.id:
                continuity = await chapter_continuity_context(session, current_chapter)
                creation_guidance += "\n\n前章连续性记忆（只读）：\n" + continuity
        prompt_sections = [template_content, prompt_appendix, skill_context, prompt]
        full_prompt = "\n\n".join(section for section in prompt_sections if section)
        logger.info(
            "Task %s context chars: template=%d skills=%d source=%d guidance=%d agent=%d files=%d",
            task_id,
            len(template_content),
            len(skill_context),
            len(prompt) + len(prompt_appendix),
            len(creation_guidance),
            len(agent.system_prompt),
            len(selected_skill_snapshots),
        )
        handbook_instructions = (
            "项目创作手册（强制执行）：平台已经按当前任务阶段检索所选视觉手册、"
            "导演手册和用户启用的相关 Skill，并以内联 <skill-file> 提供。"
            "必须直接依据这些内容完成任务，不要尝试再次调用 Skill 或文件工具。"
            "平台注入的模型能力、合法值列表和输出结构属于更高优先级硬约束；"
            "手册中的固定秒数或旧模型参数若与其冲突，必须映射到当前模型的合法值，不得照抄。"
        )
        return AgentRuntimeRequest(
            tenant_id=task.tenant_id,
            project_id=project.id,
            task_id=task.id,
            session_id=task.id,
            prompt=full_prompt,
            system_prompt=(
                f"{agent.system_prompt}\n\n{handbook_instructions}"
                f"\n\n{creation_guidance}\n\n"
                "个人技能的本阶段规则已内联提供；无需也不能再次调用技能工具。\n\n"
                "当前任务要求只返回符合指定结构的 JSON，不要返回 Markdown、解释或代码围栏。"
            ),
            model_binding={
                "provider": str(
                    capabilities.get("agentscope_provider")
                    or capabilities.get("harness_provider")
                    or provider.code
                ),
                "model": model.model_id,
                "base_url": provider.base_url,
                "api_key": api_key,
                "extra_headers": provider.extra_headers or {},
                "api_mode": str(capabilities.get("agent_api_mode") or "chat_completions"),
                "reasoning_effort": config.get("reasoning_effort"),
                "max_tokens": config.get("max_tokens"),
            },
            prompt_versions={
                f"agent:{agent.id}": str(agent.version),
                **({f"prompt:{template.code}": str(template.version)} if template else {}),
            },
            skill_versions=skill_versions,
            skills=selected_skill_snapshots,
            memory_context=(
                await memory_context(session, user, project.id, query=full_prompt)
                if agent.memory_enabled and include_story
                else []
            ),
            state_mode="ephemeral",
            tool_mode="none",
        )


def current_agent_chat_prompt(current_message: AgentChatMessage) -> str:
    return current_message.content or "请查看并分析所附图片。"


def project_agent_platform_action_instructions() -> str:
    return (
        "平台动作规则：项目真实资产清单位于 "
        "project-files/current-chapter-context/project-assets.md。"
        "当用户要求生成资产生图提示词、补全资产提示词、生成资产图片或完成资产出图时，"
        "必须通过 Write 工具创建平台动作 JSON 文件来触发后台任务，"
        "不要在聊天正文里长篇输出提示词当作完成。"
        "资产提示词任务文件：project-files/new/cineforge-asset-prompts.json，严格 JSON："
        '{"operation":"generate_asset_prompts","target":"listed_assets",'
        '"asset_ids":["资产ID"],"asset_names":[],"only_missing_prompt":true,'
        '"auto_queue_images_after_prompt":false}。'
        "资产生图任务文件：project-files/new/cineforge-asset-images.json，严格 JSON："
        '{"operation":"generate_asset_images","target":"listed_assets",'
        '"asset_ids":["资产ID"],"asset_names":[],"only_missing_image":true,'
        '"auto_generate_missing_prompts":true}。'
        "target 可为 listed_assets、active_chapter_extraction 或 all_project_assets。"
        "平台会负责扣费、排队、通知和持久化状态；你只需向用户简洁说明已安排哪些任务。"
    )


def director_chapter_instructions(chapter: Chapter, workflow_summary: str | None = None) -> str:
    return (
        "你是当前项目的主导演 Agent 和流程负责人，不是旁观顾问。"
        "你负责理解目标、主动推进、委派内部子智能体、解释结果并在需要用户决定时给出明确选择。"
        "剧本和分镜审核由平台内部审核子智能体执行，不存在需要用户联系的审核管理员、审核人或外部审核队列；"
        "绝不能让用户自行寻找审核人员。状态为 reviewing 只表示内部审核流程或待恢复状态。"
        "当你发布正式剧本版本后，平台会自动创建审核子任务；应明确告知用户审核已自动开始，"
        "不要把文件保存成功误报为整个流程完成。审核不通过时，平台会向用户提供局部修复、整版重做、"
        "忽略问题继续、补充意见后修复四种选择，修复完成后会自动重新审核。"
        "当前消息来自导演台，已由平台绑定当前章节。"
        "章节元数据和流程状态位于只读文件 "
        "project-files/current-chapter-context/current-chapter.md；"
        "章节原文单独位于 project-files/current-chapter-context/chapter-original.md。"
        "只有在首次改编、原文分析或用户明确要求对照原文时才读取章节原文，"
        "后续审核、修订、资产提取和分镜工作不得默认重复读取原文。"
        "若存在当前生效剧本，它单独位于 "
        "project-files/current-chapter-context/active-script.md；"
        "只有当前任务依赖剧本正文时才读取。若需审核尚未生效的候选剧本，"
        "应从项目文件清单中按章节和剧本版本读取对应文件。"
        "不要为了解状态同时读取原文和剧本，优先读取 current-chapter.md。"
        "项目真实资产清单位于 project-files/current-chapter-context/project-assets.md；"
        "这里包含 asset_id、资产类型、提示词状态、图片状态和衍生关系。"
        "当用户要求生成资产生图提示词、补全资产提示词、生成资产图片、完成资产出图，"
        "你必须调用平台动作，不得在对话里自行长篇编写提示词作为完成结果。"
        "生成资产提示词时，必须使用 Write 工具创建 "
        "project-files/new/cineforge-asset-prompts.json，内容必须是严格 JSON："
        '{"operation":"generate_asset_prompts","target":"listed_assets",'
        '"asset_ids":["从 project-assets.md 读取的资产ID"],'
        '"asset_names":[],"only_missing_prompt":true,'
        '"auto_queue_images_after_prompt":false}。'
        "如果用户要“完成生图步骤”且资产还没有提示词，可将 auto_queue_images_after_prompt 设为 true，"
        "平台会在提示词任务完成后自动排同批资产的生图任务。"
        "生成资产图片时，必须使用 Write 工具创建 "
        "project-files/new/cineforge-asset-images.json，内容必须是严格 JSON："
        '{"operation":"generate_asset_images","target":"listed_assets",'
        '"asset_ids":["从 project-assets.md 读取的资产ID"],'
        '"asset_names":[],"only_missing_image":true,'
        '"auto_generate_missing_prompts":true}。'
        "target 也可为 active_chapter_extraction，用于处理当前章节生效资产提取版本的全部非音频资产；"
        "或 all_project_assets，用于处理当前项目全部塑造资产。"
        "平台收到这些指令后会创建真实后台任务、扣费、排队并在通知中心显示状态；"
        "你只需简洁告诉用户任务已安排、哪些资产会处理，以及等待任务通知完成。"
        "当用户要求创建或修订正式剧本时，必须使用 Write 工具创建 "
        "project-files/new/cineforge-script-version.json，且内容必须是严格 JSON："
        '{"operation":"create_script_version","title":"版本标题",'
        '"content":"完整剧本正文","review_notes":"待审核事项"}。'
        "不要把普通 txt/md 附件当作正式剧本提交；不要在 JSON 中指定章节 ID、版本号或生效状态。"
        "平台校验成功后会自动建立该章节的待审核剧本版本。"
        "当用户要求制作或修订正式分镜时，不要创建普通 Markdown/TXT 分镜附件来冒充完成。"
        "必须先确认当前内部流程与资产状态：非音频资产只有状态为 ready 且存在图片文件时才算完成；"
        "若资产未完成，应告诉用户平台会先派发独立的资产提示词或生图子智能体，"
        "不要跳过资产检查直接制作分镜。"
        "若资产已完成并需要正式发布分镜，必须使用 Write 工具创建 "
        "project-files/new/cineforge-storyboard-version.json，且内容必须是严格 JSON："
        '{"operation":"create_storyboard_version","shots":[{"title":"镜头标题",'
        '"shot_type":"中景","duration_seconds":5,"scene_description":"场景",'
        '"action_description":"动作与运镜","dialogue":"台词或空字符串",'
        '"image_prompt":"首帧图片提示词","video_prompt":"",'
        '"asset_names":["必须与资产清单精确一致的资产名"]}]}。'
        "分镜阶段只规划镜头结构、首帧和动作意图，video_prompt 必须为空字符串；"
        "最终视频模型提示词由分镜审核通过后的独立平台任务生成。"
        "平台校验成功后会自动建立该章节的正式分镜版本、镜头记录和分镜文件。"
        f"当前章节：{chapter.title}（ID: {chapter.id}）。"
        f"当前内部导演流程：{workflow_summary or '尚无流程；如创建正式剧本，平台将自动启动审核'}。"
    )


def director_chapter_snapshots(
    chapter: Chapter,
    active_script: ScriptVersion | None,
) -> list[AgentRuntimeProjectFileSnapshot]:
    metadata_content = (
        f"# 当前导演台章节\n\n"
        f"- 章节 ID：{chapter.id}\n"
        f"- 标题：{chapter.title}\n"
        f"- 来源模式：{chapter.source_mode.value}\n"
        f"- 当前状态：{chapter.status.value}\n"
        f"- 是否存在当前生效剧本：{'是' if active_script else '否'}\n"
    )
    original_content = f"# {chapter.title} · 原文\n\n{chapter.original_content}\n"
    snapshots = [
        AgentRuntimeProjectFileSnapshot(
            id="current-chapter-context",
            directory_id="current-chapter-context",
            name="current-chapter.md",
            kind="chapter_context",
            path="project-files/current-chapter-context/current-chapter.md",
            content=metadata_content,
            sha256=hashlib.sha256(metadata_content.encode("utf-8")).hexdigest(),
            editable=False,
        ),
        AgentRuntimeProjectFileSnapshot(
            id="current-chapter-original",
            directory_id="current-chapter-context",
            name="chapter-original.md",
            kind="chapter_original",
            path="project-files/current-chapter-context/chapter-original.md",
            content=original_content,
            sha256=hashlib.sha256(original_content.encode("utf-8")).hexdigest(),
            editable=False,
        ),
    ]
    if active_script is not None:
        script_content = (
            f"# {active_script.title}\n\n"
            f"- 剧本版本：v{active_script.version}\n"
            f"- 状态：{active_script.status}\n\n"
            f"{active_script.content}\n"
        )
        snapshots.append(
            AgentRuntimeProjectFileSnapshot(
                id="current-chapter-active-script",
                directory_id="current-chapter-context",
                name="active-script.md",
                kind="active_script",
                path="project-files/current-chapter-context/active-script.md",
                content=script_content,
                sha256=hashlib.sha256(script_content.encode("utf-8")).hexdigest(),
                editable=False,
            )
        )
    for snapshot in snapshots:
        if len(snapshot.content.encode("utf-8")) > 2_000_000:
            raise RuntimeError(f"当前章节文件超过 Agent 单文件上下文上限：{snapshot.name}")
    return snapshots


async def director_workflow_snapshot(
    session: AsyncSession,
    chapter: Chapter,
) -> tuple[AgentRuntimeProjectFileSnapshot | None, str | None]:
    workflow = await session.scalar(
        select(DirectorWorkflowRun)
        .where(DirectorWorkflowRun.chapter_id == chapter.id)
        .order_by(DirectorWorkflowRun.created_at.desc())
        .limit(1)
    )
    if workflow is None:
        return None, None
    children = list(
        (
            await session.scalars(
                select(DirectorChildRun)
                .where(DirectorChildRun.workflow_id == workflow.id)
                .order_by(DirectorChildRun.created_at.desc())
                .limit(12)
            )
        ).all()
    )
    decision = await session.scalar(
        select(DirectorDecisionRequest)
        .where(
            DirectorDecisionRequest.workflow_id == workflow.id,
            DirectorDecisionRequest.resolved.is_(False),
        )
        .order_by(DirectorDecisionRequest.created_at.desc())
        .limit(1)
    )
    payload = {
        "workflow_id": workflow.id,
        "stage": workflow.stage.value,
        "status": workflow.status.value,
        "reviewer": "平台内部导演审核子智能体",
        "last_message": workflow.last_message,
        "last_error": workflow.last_error,
        "current_task_id": workflow.current_task_id,
        "script_version_id": workflow.script_version_id,
        "user_action_required": decision is not None,
        "pending_decision": (
            {
                "prompt": decision.prompt,
                "options": decision.options,
            }
            if decision
            else None
        ),
        "child_runs": [
            {
                "kind": child.kind,
                "title": child.title,
                "status": child.status.value,
                "attempt": child.attempt,
                "max_attempts": child.max_attempts,
                "summary": child.summary,
                "details": child.details,
            }
            for child in reversed(children)
        ],
    }
    content = (
        "# 当前内部导演流程\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n"
    )
    snapshot = AgentRuntimeProjectFileSnapshot(
        id="current-director-workflow",
        directory_id="current-chapter-context",
        name="director-workflow.md",
        kind="director_workflow",
        path="project-files/current-chapter-context/director-workflow.md",
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        editable=False,
    )
    summary = f"{workflow.stage.value}/{workflow.status.value}，{workflow.last_message}"
    if decision is not None:
        summary += "；正在等待用户从平台给出的审核处理选项中选择"
    return snapshot, summary


async def project_assets_snapshot(
    session: AsyncSession,
    project: Project,
    user: User,
) -> AgentRuntimeProjectFileSnapshot:
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.user_id == user.id,
                    Asset.project_id == project.id,
                    Asset.scope == AssetScope.PROJECT,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )
    by_id = {asset.id: asset for asset in assets}
    payload = {
        "project_id": project.id,
        "usage": (
            "这是平台真实项目资产清单。需要生成资产提示词或生图时，优先使用这里的 asset_id，"
            "并通过 cineforge-asset-prompts.json / cineforge-asset-images.json 触发平台任务。"
        ),
        "assets": [
            {
                "asset_id": asset.id,
                "name": asset.name,
                "asset_type": asset.asset_type.value,
                "status": asset.status.value,
                "parent_asset_id": asset.parent_asset_id,
                "parent_name": by_id[asset.parent_asset_id].name if asset.parent_asset_id in by_id else None,
                "has_generation_prompt": bool(asset.generation_prompt.strip()),
                "has_image_url": bool(asset.media_url),
                "media_url": asset.media_url,
                "description": asset.description,
                "source_extraction_id": asset.asset_metadata.get("extraction_id"),
            }
            for asset in assets
        ],
    }
    content = "# 项目资产清单\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n"
    return AgentRuntimeProjectFileSnapshot(
        id="current-project-assets",
        directory_id="current-chapter-context",
        name="project-assets.md",
        kind="project_assets",
        path="project-files/current-chapter-context/project-assets.md",
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        editable=False,
    )


def assistant_artifact_pointer(message: AgentChatMessage) -> str | None:
    manifest = message.runtime_manifest or {}
    changes = manifest.get("domain_changes") or manifest.get("project_file_changes") or []
    applied = [item for item in changes if isinstance(item, dict) and item.get("status") == "applied"]
    if not applied:
        return None
    resource_labels = {
        "script_version": "正式剧本版本",
        "asset": "项目资产",
        "asset_prompt_task": "资产提示词任务",
        "asset_image_tasks": "资产生图任务",
        "storyboard_version": "分镜版本",
        "project_file": "项目文件",
    }
    labels: list[str] = []
    for item in applied[:4]:
        label = resource_labels.get(str(item.get("resource_type") or ""), "项目创作结果")
        name = str(item.get("name") or "").strip()
        display = f"{label}《{name}》" if name else label
        if display not in labels:
            labels.append(display)
    resources = "、".join(labels) or "项目创作结果"
    return (
        f"上一轮已生成并保存{resources}。本轮如确有需要，请从项目文件或对应领域数据"
        "按需读取，不要依赖上一轮长回复正文。"
    )


def assistant_media_pointer(message: AgentChatMessage) -> str | None:
    generated = (message.runtime_manifest or {}).get("generated_media") or []
    media = next((item for item in generated if isinstance(item, dict)), None)
    if media is None:
        return None
    mime_type = str(media.get("mime_type") or "")
    media_type = "视频" if mime_type.startswith("video/") else "图片"
    prompt = str(media.get("prompt") or "").strip()
    if len(prompt) > 2400:
        prompt = prompt[:2400] + "[提示词已截断]"
    specs = " · ".join(
        str(value)
        for value in (
            media.get("model_name"),
            media.get("resolution"),
            media.get("aspect_ratio"),
            f"{media.get('duration_seconds')} 秒" if media.get("duration_seconds") else None,
        )
        if value
    )
    return (
        f"[上一轮{media_type}结果]\n"
        f"生成提示词：{prompt or '未记录'}\n"
        f"生成规格：{specs or '使用模型默认参数'}\n"
        "后续若用户只提出局部修改，应保留该提示词中未被要求改变的内容并合并修改。"
    )


def compact_history_content(message: AgentChatMessage) -> tuple[str, bool]:
    content = (message.content or "").strip()
    media_pointer = assistant_media_pointer(message)
    if media_pointer:
        content = f"{content}\n\n{media_pointer}".strip()
    if not content:
        return "", False
    if message.role == AgentMessageRole.ASSISTANT and len(content) > AGENT_ARTIFACT_POINTER_THRESHOLD:
        pointer = assistant_artifact_pointer(message)
        if pointer:
            return f"{pointer}\n\n{media_pointer}" if media_pointer else pointer, True
    if len(content) <= AGENT_HISTORY_MESSAGE_CHARACTER_LIMIT:
        return content, False
    head_size = AGENT_HISTORY_MESSAGE_CHARACTER_LIMIT - 700
    compacted = (
        f"{content[:head_size]}\n\n"
        "[较长历史消息已由平台压缩；其中可从项目文件读取的正文不再重复传入。]\n\n"
        f"{content[-500:]}"
    )
    return compacted, True


async def compact_agent_chat_history(
    session: AsyncSession,
    chat_session_id: str,
    current_message: AgentChatMessage,
    summary: AgentChatSummary | None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    filters = [
        AgentChatMessage.session_id == chat_session_id,
        AgentChatMessage.id != current_message.id,
    ]
    summary_covered_count = 0
    if summary is not None and summary.through_message_id:
        through_message = await session.get(AgentChatMessage, summary.through_message_id)
        if through_message is not None and through_message.session_id == chat_session_id:
            covered_filter = or_(
                AgentChatMessage.created_at < through_message.created_at,
                and_(
                    AgentChatMessage.created_at == through_message.created_at,
                    AgentChatMessage.id <= through_message.id,
                ),
            )
            summary_covered_count = int(
                await session.scalar(select(func.count(AgentChatMessage.id)).where(*filters, covered_filter))
                or 0
            )
            filters.append(
                or_(
                    AgentChatMessage.created_at > through_message.created_at,
                    and_(
                        AgentChatMessage.created_at == through_message.created_at,
                        AgentChatMessage.id > through_message.id,
                    ),
                )
            )

    available_count = int(await session.scalar(select(func.count(AgentChatMessage.id)).where(*filters)) or 0)
    source_character_count = int(
        await session.scalar(
            select(func.coalesce(func.sum(func.length(AgentChatMessage.content)), 0)).where(*filters)
        )
        or 0
    )
    candidates_desc = list(
        (
            await session.scalars(
                select(AgentChatMessage)
                .where(*filters)
                .order_by(AgentChatMessage.created_at.desc(), AgentChatMessage.id.desc())
                .limit(AGENT_HISTORY_MESSAGE_LIMIT * 2)
            )
        ).all()
    )
    selected_reversed: list[dict[str, str]] = []
    sent_character_count = 0
    compacted_message_count = 0
    for message in candidates_desc:
        content, compacted = compact_history_content(message)
        if not content:
            continue
        if len(selected_reversed) >= AGENT_HISTORY_MESSAGE_LIMIT:
            break
        remaining = AGENT_HISTORY_CHARACTER_LIMIT - sent_character_count
        if remaining <= 0:
            break
        if len(content) > remaining:
            if remaining < 400:
                break
            content = content[: remaining - 80] + "\n[历史消息已按本轮上下文预算截断]"
            compacted = True
        selected_reversed.append({"role": message.role.value, "content": content})
        sent_character_count += len(content)
        compacted_message_count += int(compacted)

    recent_messages = list(reversed(selected_reversed))
    budget_metadata = {
        "summary_covered_message_count": summary_covered_count,
        "available_message_count": available_count,
        "sent_message_count": len(recent_messages),
        "omitted_message_count": max(available_count - len(recent_messages), 0),
        "compacted_message_count": compacted_message_count,
        "source_character_count": source_character_count,
        "sent_character_count": sent_character_count,
        "summary_character_count": len(summary.content) if summary else 0,
    }
    return recent_messages, budget_metadata


async def latest_chat_summary(
    session: AsyncSession,
    chat_session_id: str,
) -> AgentChatSummary | None:
    return await session.scalar(
        select(AgentChatSummary)
        .where(AgentChatSummary.session_id == chat_session_id)
        .order_by(AgentChatSummary.version.desc())
        .limit(1)
    )


async def agent_chat_runtime_request(
    task_id: str,
) -> tuple[AgentRuntimeRequest, list[AgentRuntimeProjectFileSnapshot], AITask]:
    from app.api.routes.agent_chat import (
        project_file_snapshots,
        project_handbooks,
        skill_snapshots,
    )

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            raise RuntimeError("Agent 对话任务上下文已失效")
        personal_scope = task.request_payload.get("scope") == "personal"
        project = await session.get(Project, task.project_id) if task.project_id else None
        user = await session.get(User, task.user_id)
        chat_session_id = str(task.request_payload.get("agent_chat_session_id") or "")
        user_message_id = str(task.request_payload.get("user_message_id") or "")
        agent_id = str(task.request_payload.get("agent_profile_id") or "")
        chat_session = await session.get(AgentChatSession, chat_session_id)
        user_message = await session.get(AgentChatMessage, user_message_id)
        agent = await session.get(AgentProfile, agent_id)
        text_model_id = str(task.request_payload.get("text_model_id") or task.model_id or "")
        model = await session.get(AIModel, text_model_id) if text_model_id else None
        if user is None or user.tenant_id != task.tenant_id:
            raise RuntimeError("Agent 对话用户不可用")
        if not personal_scope and (project is None or project.tenant_id != task.tenant_id):
            raise RuntimeError("Agent 对话项目不可用")
        if personal_scope and task.project_id is not None:
            raise RuntimeError("个人 Agent 不能绑定项目")
        if (
            chat_session is None
            or chat_session.tenant_id != task.tenant_id
            or chat_session.user_id != task.user_id
            or chat_session.project_id != task.project_id
        ):
            raise RuntimeError("Agent 对话会话已失效")
        if (
            user_message is None
            or user_message.session_id != chat_session.id
            or user_message.role != AgentMessageRole.USER
            or user_message.run_id != task.id
        ):
            raise RuntimeError("Agent 对话用户消息已失效")
        prompt_hash = hashlib.sha256(user_message.content.encode("utf-8")).hexdigest()
        if prompt_hash != task.request_payload.get("prompt_hash"):
            raise RuntimeError("Agent 对话内容已变化，旧任务不能继续")
        if (
            agent is None
            or agent.tenant_id != task.tenant_id
            or not agent.enabled
            or agent.version != int(task.request_payload.get("agent_version") or -1)
        ):
            raise RuntimeError("Agent 配置已更新，请重新提交本轮对话")
        if (
            model is None
            or model.tenant_id != task.tenant_id
            or model.model_type != ModelType.TEXT
            or not model.enabled
        ):
            raise RuntimeError("Agent 对话文本模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("Agent 对话模型平台不可用")
        api_key = SecretBox().decrypt(provider.encrypted_api_key)
        if not api_key:
            raise RuntimeError("Agent 对话模型平台缺少 API Key")

        summary = await latest_chat_summary(session, chat_session.id)
        recent_messages, context_budget = await compact_agent_chat_history(
            session,
            chat_session.id,
            user_message,
            summary,
        )
        handbooks = await project_handbooks(session, project) if project is not None else []
        skill_files, skill_versions = await run_in_threadpool(skill_snapshots, handbooks)
        project_files = await project_file_snapshots(session, user, project.id) if project is not None else []
        if project is not None:
            project_files.append(await project_assets_snapshot(session, project, user))
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        chapter = await session.get(Chapter, chapter_id) if chapter_id else None
        workflow_summary: str | None = None
        if chapter_id and (
            chapter is None
            or project is None
            or chapter.project_id != project.id
            or chapter.tenant_id != task.tenant_id
            or chapter.user_id != task.user_id
        ):
            raise RuntimeError("Agent 对话绑定的导演台章节已失效")
        if chapter is not None:
            source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
            if source_hash != task.request_payload.get("chapter_source_hash"):
                raise RuntimeError("当前章节原文已更新，请重新提交本轮导演对话")
            active_script = (
                await session.get(ScriptVersion, chapter.active_script_version_id)
                if chapter.active_script_version_id
                else None
            )
            project_files.extend(director_chapter_snapshots(chapter, active_script))
            workflow_file, workflow_summary = await director_workflow_snapshot(session, chapter)
            if workflow_file is not None:
                project_files.append(workflow_file)
        personal_request_mode = str(task.request_payload.get("mode") or "chat")
        if personal_scope:
            personal_media_models = list(
                (
                    await session.scalars(
                        select(AIModel)
                        .join(Provider, Provider.id == AIModel.provider_id)
                        .where(
                            AIModel.tenant_id == task.tenant_id,
                            AIModel.model_type.in_([ModelType.IMAGE, ModelType.VIDEO]),
                            AIModel.enabled.is_(True),
                            Provider.tenant_id == task.tenant_id,
                            Provider.enabled.is_(True),
                        )
                        .order_by(AIModel.model_type, AIModel.is_default.desc(), AIModel.name)
                    )
                ).all()
            )
            personal_skill_catalog = list(
                (
                    await session.scalars(
                        select(UserSkill)
                        .where(
                            UserSkill.tenant_id == task.tenant_id,
                            UserSkill.user_id == task.user_id,
                        )
                        .order_by(UserSkill.name, UserSkill.id)
                    )
                ).all()
            )
            user_stages = None
            selected_skill_ids = [
                str(item) for item in task.request_payload.get("selected_skill_ids") or [] if str(item)
            ]
            selected_versions = {
                str(key): int(value)
                for key, value in dict(task.request_payload.get("selected_skill_versions") or {}).items()
            }
            personal_skills_by_id = {item.id: item for item in personal_skill_catalog}
            selected_personal_skills: list[UserSkill] = []
            for skill_id in selected_skill_ids:
                skill = personal_skills_by_id.get(skill_id)
                if skill is None or not skill.enabled or selected_versions.get(skill_id) != skill.version:
                    raise RuntimeError("本轮显式选择的 Skill 已更新或禁用，请重新提交")
                selected_personal_skills.append(skill)
            personal_skills = (
                personal_skill_catalog if personal_request_mode == "skill" else selected_personal_skills
            )
        else:
            user_stages = infer_chat_user_skill_stages(user_message.content, chapter=chapter)
            personal_skills = await enabled_user_skills(
                session,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                stages=user_stages,
            )
            selected_personal_skills = []
        personal_snapshots, personal_versions = user_skill_snapshots(personal_skills)
        skill_files.extend(personal_snapshots)
        skill_versions.update(personal_versions)
        attachment_ids = [str(item) for item in task.request_payload.get("attachment_ids") or [] if str(item)]
        if personal_scope:
            attachment_rows = (
                list(
                    (
                        await session.scalars(
                            select(PersonalAgentAttachment).where(
                                PersonalAgentAttachment.id.in_(attachment_ids),
                                PersonalAgentAttachment.tenant_id == task.tenant_id,
                                PersonalAgentAttachment.user_id == task.user_id,
                                PersonalAgentAttachment.session_id == chat_session.id,
                                PersonalAgentAttachment.message_id == user_message.id,
                            )
                        )
                    ).all()
                )
                if attachment_ids
                else []
            )
        else:
            attachment_rows = (
                list(
                    (
                        await session.scalars(
                            select(ProjectFile).where(
                                ProjectFile.id.in_(attachment_ids),
                                ProjectFile.tenant_id == task.tenant_id,
                                ProjectFile.user_id == task.user_id,
                                ProjectFile.project_id == project.id,
                            )
                        )
                    ).all()
                )
                if attachment_ids
                else []
            )
        # The current message attachments are authoritative, but ordinary chat
        # also needs to understand phrases such as “基于刚才那张图”. Expose a
        # small, bounded set of recent session images to the runtime so the
        # Agent can actually see them and return their IDs in a media action.
        runtime_attachment_rows = attachment_rows
        historical_rows: list[PersonalAgentAttachment] = []
        if personal_scope and len(runtime_attachment_rows) < MAX_CHAT_ATTACHMENTS:
            historical_rows = list(
                (
                    await session.scalars(
                        select(PersonalAgentAttachment)
                        .where(
                            PersonalAgentAttachment.session_id == chat_session.id,
                            PersonalAgentAttachment.tenant_id == task.tenant_id,
                            PersonalAgentAttachment.user_id == task.user_id,
                            PersonalAgentAttachment.message_id.is_not(None),
                            PersonalAgentAttachment.mime_type.like("image/%"),
                            PersonalAgentAttachment.id.not_in(attachment_ids or [""]),
                        )
                        .order_by(PersonalAgentAttachment.created_at.desc())
                        .limit(MAX_CHAT_ATTACHMENTS)
                    )
                ).all()
            )
            runtime_attachment_rows = (
                attachment_rows + historical_rows[: MAX_CHAT_ATTACHMENTS - len(attachment_rows)]
            )
        if (
            personal_scope
            and personal_request_mode in {"image", "video"}
            and not attachment_ids
            and historical_rows
            and should_reuse_historical_image(user_message.content)
        ):
            reference = historical_rows[0]
            attachment_ids = [reference.id]
            payload = {
                **task.request_payload,
                "attachment_ids": attachment_ids,
                "media_generation_mode": (
                    "image_to_image" if personal_request_mode == "image" else "image_to_video"
                ),
                "continued_from_attachment_id": reference.id,
            }
            task.request_payload = payload
            runtime_attachment_rows = [reference] + [
                item for item in runtime_attachment_rows if item.id != reference.id
            ]
        attachment_files: list[ProjectFile | PersonalAgentAttachment] = []
        for attachment in runtime_attachment_rows:
            if (
                attachment is None
                or not attachment.storage_path
                or not attachment.mime_type.startswith("image/")
            ):
                raise RuntimeError("Agent 对话图片附件已失效")
            if not personal_scope and (
                not isinstance(attachment, ProjectFile)
                or attachment.file_metadata.get("role") != "agent_chat_attachment"
                or attachment.file_metadata.get("agent_chat_message_id") != user_message.id
            ):
                raise RuntimeError("Agent 对话图片附件已失效")
            attachment_files.append(attachment)
        attachment_bytes = await asyncio.gather(
            *(object_storage().get_bytes(item.storage_path or "") for item in attachment_files)
        )
        runtime_attachments = [
            AgentRuntimeAttachment(
                id=item.id,
                name=item.name,
                mime_type=item.mime_type,
                data=base64.b64encode(data).decode("ascii"),
            )
            for item, data in zip(attachment_files, attachment_bytes, strict=True)
        ]
        config = agent.config or {}
        capabilities = model.capabilities or {}
        prompt = current_agent_chat_prompt(user_message)
        if personal_scope and runtime_attachment_rows:
            catalog = "、".join(f"{item.id}（{item.name}）" for item in runtime_attachment_rows)
            attachment_instruction = (
                "如果用户要求基于、参考或仿照某张历史图片，必须在媒体动作标记的 "
                "reference_attachment_ids 中填写对应 ID。"
                if personal_request_mode == "chat"
                else "这些图片可作为本轮连续修改的视觉上下文；请结合最近对话输出完整的新提示词。"
            )
            prompt = f"{prompt}\n\n本轮可引用的会话图片附件：{catalog}。{attachment_instruction}"
        allow_media_prompt_rewrite = bool(
            personal_scope
            and media_prompt_rewrite_allowed(
                user_message.content,
                recent_messages=recent_messages,
                has_selected_skills=bool(selected_personal_skills),
            )
        )
        allow_personal_skill_changes = bool(
            personal_scope
            and personal_skill_change_requested(
                user_message.content,
                mode=personal_request_mode,
            )
        )
        if personal_scope:
            task.request_payload = {
                **task.request_payload,
                "allow_media_prompt_rewrite": allow_media_prompt_rewrite,
                "allow_personal_skill_changes": allow_personal_skill_changes,
            }
        retrieved_memories = (
            await retrieve_project_memories(
                session,
                user=user,
                project_id=task.project_id,
                query=prompt,
            )
            if agent.memory_enabled
            else []
        )
        accessed_at = datetime.now(UTC)
        for retrieved in retrieved_memories:
            retrieved.memory.last_accessed_at = accessed_at
            retrieved.memory.access_count = int(retrieved.memory.access_count or 0) + 1
        task.result_payload = {
            **(task.result_payload or {}),
            "retrieved_memory_ids": [item.memory.id for item in retrieved_memories],
            "context_budget": context_budget,
        }
        await session.commit()
        chapter_instruction = (
            f"\n\n{director_chapter_instructions(chapter, workflow_summary)}" if chapter is not None else ""
        )
        if personal_scope:
            mode = personal_request_mode
            personal_skill_instructions = user_skill_usage_instructions(
                personal_skills,
                stages=None,
                selected_skills=selected_personal_skills,
            )
            personal_system_instructions = personal_agent_system_instructions(
                personal_skills,
                mode=mode,
                allow_skill_changes=allow_personal_skill_changes,
            )
            system_prompt = (
                f"{agent.system_prompt}\n\n"
                f"{personal_system_instructions}"
                f"{personal_skill_instructions}"
                + (
                    "\n\n本轮媒体提示词策略：用户已明确授权结合上下文、局部修改或所选 Skill "
                    "处理提示词，可以输出合并后的完整提示词。"
                    if allow_media_prompt_rewrite
                    else "\n\n本轮媒体提示词策略：用户没有授权 AI 改写提示词。若触发图片或视频生成，"
                    "prompt 必须原样保留用户本轮输入，不得扩写、润色、翻译、增加风格或补充细节。"
                )
                + (
                    "\n\n平台当前可调用的媒体模型目录（只能填写这里列出的 ID 或名称）：\n"
                    f"{personal_media_model_catalog(personal_media_models)}"
                    if mode == "chat"
                    else ""
                )
            )
            runtime_project_id = personal_agent_scope_id(user.id)
        else:
            from app.services.ai_creation import project_creation_guidance

            creation_guidance = await project_creation_guidance(session, project)
            system_prompt = (
                f"{agent.system_prompt}\n\n"
                f"{handbook_usage_instructions(handbooks)}"
                f"{user_skill_usage_instructions(personal_skills, stages=user_stages)}"
                f"\n\n{project_agent_platform_action_instructions()}"
                f"{chapter_instruction}"
                f"\n\n{creation_guidance}"
            )
            runtime_project_id = project.id
        request = AgentRuntimeRequest(
            tenant_id=task.tenant_id,
            project_id=runtime_project_id,
            task_id=task.id,
            session_id=chat_session.id,
            prompt=prompt,
            system_prompt=system_prompt,
            model_binding={
                "provider": str(
                    capabilities.get("agentscope_provider")
                    or capabilities.get("harness_provider")
                    or provider.code
                ),
                "model": model.model_id,
                "base_url": provider.base_url,
                "api_key": api_key,
                "extra_headers": provider.extra_headers or {},
                "api_mode": str(capabilities.get("agent_api_mode") or "chat_completions"),
                "reasoning_effort": config.get("reasoning_effort"),
                "max_tokens": config.get("max_tokens"),
            },
            prompt_versions={f"agent:{agent.id}": str(agent.version)},
            skill_versions=skill_versions,
            skills=skill_files,
            memory_context=[item.as_context() for item in retrieved_memories],
            # Database summaries, memories and files restore the conversation without
            # carrying prior SDK tool results and large artifacts into every model call.
            state_mode="ephemeral",
            conversation_summary=summary.content if summary else None,
            recent_messages=recent_messages,
            project_files=project_files,
            attachments=runtime_attachments,
        )
        return request, project_files, task


def conversation_maintenance_prompt(
    previous_summary: str,
    messages: list[AgentChatMessage],
    assistant_response: str,
) -> str:
    transcript = [
        f"{'用户' if message.role == AgentMessageRole.USER else '助手'}：{message.content}"
        for message in messages
        if message.content
    ]
    transcript.append(f"助手：{assistant_response}")
    return (
        "维护用户创作会话的跨轮记忆。根据旧摘要和最近对话生成一份可独立恢复上下文的新摘要，"
        "并只提炼未来轮次仍有价值的稳定事实。不要保存寒暄、临时请求、模型回复措辞、敏感凭据"
        "或可以直接从项目文件读取的全文。记忆 namespace 建议使用 preference、story、character、"
        "world、decision、workflow；key 使用稳定的英文短标识。salience 为 0 到 1。\n"
        '只返回 JSON：{"summary":"...","memories":[{"namespace":"story",'
        '"key":"protagonist-goal","content":"...","salience":0.8}]}\n\n'
        f"旧摘要：\n{previous_summary or '暂无'}\n\n最近对话：\n" + "\n".join(transcript)
    )


def fallback_conversation_summary(
    previous_summary: str,
    messages: list[AgentChatMessage],
    assistant_response: str,
) -> str:
    latest = [
        f"{'用户' if message.role == AgentMessageRole.USER else '助手'}：{message.content}"
        for message in messages[-4:]
        if message.content
    ]
    latest.append(f"助手：{assistant_response}")
    sections = [section for section in (previous_summary.strip(), "\n".join(latest)) if section]
    return "\n\n".join(sections)[-30_000:] or "本会话尚无可摘要内容。"


async def summary_covers_message(
    session: AsyncSession,
    summary: AgentChatSummary | None,
    message: AgentChatMessage,
) -> bool:
    if summary is None or not summary.through_message_id:
        return False
    if summary.through_message_id == message.id:
        return True
    through_message = await session.get(AgentChatMessage, summary.through_message_id)
    if through_message is None:
        return False
    return (through_message.created_at, through_message.id) >= (message.created_at, message.id)


async def memory_maintenance_runtime_request(
    task_id: str,
) -> tuple[AgentRuntimeRequest, str, list[AgentChatMessage], str, int] | None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or task.task_type != "agent_memory_maintenance" or not task.model_id:
            raise RuntimeError("Agent 记忆维护任务上下文已失效")
        source_task = await session.get(
            AITask,
            str(task.request_payload.get("source_task_id") or ""),
        )
        chat_session_id = str(task.request_payload.get("agent_chat_session_id") or "")
        assistant_message = await session.get(
            AgentChatMessage,
            str(task.request_payload.get("assistant_message_id") or ""),
        )
        chat_session = await session.get(AgentChatSession, chat_session_id)
        model = await session.get(AIModel, task.model_id)
        if (
            source_task is None
            or source_task.tenant_id != task.tenant_id
            or source_task.user_id != task.user_id
            or source_task.project_id != task.project_id
            or source_task.task_type != "agent_chat_run"
            or assistant_message is None
            or assistant_message.session_id != chat_session_id
            or assistant_message.role != AgentMessageRole.ASSISTANT
            or assistant_message.run_id != source_task.id
            or chat_session is None
            or chat_session.tenant_id != task.tenant_id
            or chat_session.user_id != task.user_id
            or chat_session.project_id != task.project_id
            or model is None
            or model.tenant_id != task.tenant_id
            or model.model_type != ModelType.TEXT
            or not model.enabled
        ):
            raise RuntimeError("Agent 记忆维护来源已失效")
        previous = await latest_chat_summary(session, chat_session_id)
        if await summary_covers_message(session, previous, assistant_message):
            return None
        recent_desc = list(
            (
                await session.scalars(
                    select(AgentChatMessage)
                    .where(
                        AgentChatMessage.session_id == chat_session_id,
                        AgentChatMessage.id != assistant_message.id,
                        AgentChatMessage.created_at <= assistant_message.created_at,
                    )
                    .order_by(AgentChatMessage.created_at.desc(), AgentChatMessage.id.desc())
                    .limit(12)
                )
            ).all()
        )
        recent = list(reversed(recent_desc))
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("Agent 记忆维护模型平台不可用")
        api_key = SecretBox().decrypt(provider.encrypted_api_key)
        if not api_key:
            raise RuntimeError("Agent 记忆维护模型平台缺少 API Key")
        agent = await session.get(
            AgentProfile,
            str(source_task.request_payload.get("agent_profile_id") or ""),
        )
        config = agent.config if agent is not None else {}
        capabilities = model.capabilities or {}
        configured_max = config.get("max_tokens")
        previous_content = previous.content if previous else ""
        prompt = conversation_maintenance_prompt(
            previous_content,
            recent,
            assistant_message.content,
        )
        maintenance_request = AgentRuntimeRequest(
            tenant_id=task.tenant_id,
            project_id=task.project_id or personal_agent_scope_id(task.user_id),
            task_id=task.id,
            session_id=f"memory-{source_task.id}",
            prompt=prompt,
            system_prompt=(
                "你是 CineForge 的后台记忆维护器。只执行对话摘要和长期记忆提炼，"
                "严格返回指定 JSON，不输出 Markdown、解释或代码围栏。"
            ),
            model_binding={
                "provider": str(
                    capabilities.get("agentscope_provider")
                    or capabilities.get("harness_provider")
                    or provider.code
                ),
                "model": model.model_id,
                "base_url": provider.base_url,
                "api_key": api_key,
                "extra_headers": provider.extra_headers or {},
                "api_mode": str(capabilities.get("agent_api_mode") or "chat_completions"),
                "reasoning_effort": config.get("reasoning_effort"),
                "max_tokens": min(int(configured_max), 4_000) if configured_max else 4_000,
            },
            prompt_versions={"system:conversation-memory": "1"},
            skill_versions={},
            skills=[],
            memory_context=[],
            state_mode="ephemeral",
            conversation_summary=None,
            recent_messages=[],
            project_files=[],
            attachments=[],
        )
        return (
            maintenance_request,
            previous_content,
            recent,
            assistant_message.content,
            previous.version if previous else 0,
        )


async def maintain_conversation_memory(
    task_id: str,
    runtime: AgentRuntimeClient,
) -> tuple[ConversationMaintenancePayload, dict[str, object]] | None:
    context = await memory_maintenance_runtime_request(task_id)
    if context is None:
        return None
    maintenance_request, previous_content, recent, assistant_response, previous_version = context
    try:
        result = await runtime.run(maintenance_request)
        signal_task_activity(task_id)
        payload = ConversationMaintenancePayload.model_validate(parse_json_object(result.final_response))
        return payload, {
            "fallback": False,
            "runtime_manifest": result.manifest,
            "previous_summary_version": previous_version,
        }
    except Exception as exc:
        logger.warning("Conversation memory maintenance failed for task %s: %s", task_id, exc)
        return (
            ConversationMaintenancePayload(
                summary=fallback_conversation_summary(previous_content, recent, assistant_response),
                memories=[],
            ),
            {
                "fallback": True,
                "error_type": type(exc).__name__,
                "previous_summary_version": previous_version,
            },
        )


async def persist_conversation_maintenance(
    session: AsyncSession,
    *,
    task: AITask,
    chat_session: AgentChatSession,
    assistant_message: AgentChatMessage,
    payload: ConversationMaintenancePayload,
    maintenance_metadata: dict[str, object],
    source_task_id: str,
) -> tuple[AgentChatSummary, list[str]]:
    previous = await latest_chat_summary(session, chat_session.id)
    version = (previous.version if previous else 0) + 1
    message_count = int(
        await session.scalar(
            select(func.count(AgentChatMessage.id)).where(AgentChatMessage.session_id == chat_session.id)
        )
        or 0
    )
    source_char_count = int(
        await session.scalar(
            select(func.coalesce(func.sum(func.length(AgentChatMessage.content)), 0)).where(
                AgentChatMessage.session_id == chat_session.id
            )
        )
        or 0
    )
    summary = AgentChatSummary(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        project_id=task.project_id,
        session_id=chat_session.id,
        version=version,
        content=payload.summary,
        through_message_id=assistant_message.id,
        model_id=task.model_id,
        source_message_count=message_count,
        source_char_count=source_char_count,
        summary_metadata={
            **maintenance_metadata,
            "source_task_id": source_task_id,
            "maintenance_task_id": task.id,
            "memory_count": len(payload.memories),
        },
    )
    session.add(summary)

    memory_ids: list[str] = []
    for candidate in payload.memories:
        namespace = normalize_memory_namespace(candidate.namespace)
        memory_key = normalize_memory_key(candidate.key, candidate.content)
        memory = await session.scalar(
            select(AgentMemory).where(
                AgentMemory.tenant_id == task.tenant_id,
                AgentMemory.user_id == task.user_id,
                AgentMemory.project_id == task.project_id,
                AgentMemory.namespace == namespace,
                AgentMemory.memory_key == memory_key,
            )
        )
        if memory is not None and not memory.is_automatic:
            memory_key = normalize_memory_key(
                f"{memory_key}-auto-{hashlib.sha256(candidate.content.encode()).hexdigest()[:8]}",
                candidate.content,
            )
            memory = None
        if memory is None:
            memory = AgentMemory(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=task.project_id,
                namespace=namespace,
                memory_key=memory_key,
                content=candidate.content,
            )
            session.add(memory)
        memory.content = candidate.content
        memory.memory_metadata = {
            "source": "conversation_extraction",
            "summary_version": version,
            "source_task_id": source_task_id,
            "maintenance_task_id": task.id,
        }
        memory.embedding = embed_memory_text(candidate.content)
        memory.embedding_model = EMBEDDING_MODEL
        memory.salience = candidate.salience
        memory.is_automatic = True
        memory.source_session_id = chat_session.id
        memory.source_message_id = assistant_message.id
        await session.flush()
        memory_ids.append(memory.id)
    await session.flush()
    return summary, memory_ids


async def ensure_memory_maintenance_task(
    session: AsyncSession,
    *,
    source_task: AITask,
    assistant_message: AgentChatMessage,
) -> tuple[AITask, bool]:
    idempotency_key = f"agent-memory:{source_task.id}"
    existing = await session.scalar(select(AITask).where(AITask.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    text_model_id = str(source_task.request_payload.get("text_model_id") or "").strip()
    if not text_model_id:
        text_model_id = str(source_task.model_id or "").strip()
    memory_task = AITask(
        tenant_id=source_task.tenant_id,
        user_id=source_task.user_id,
        project_id=source_task.project_id,
        task_type="agent_memory_maintenance",
        model_id=text_model_id or None,
        idempotency_key=idempotency_key,
        cost=Decimal("0"),
        request_payload={
            "internal": True,
            "source_task_id": source_task.id,
            "agent_chat_session_id": assistant_message.session_id,
            "assistant_message_id": assistant_message.id,
        },
    )
    session.add(memory_task)
    await session.flush()
    record_task_event(
        session,
        memory_task,
        status=TaskStatus.QUEUED,
        progress=0,
        message="会话记忆已进入后台整理队列",
    )
    return memory_task, True


async def execute_agent_memory_maintenance_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    await record_progress(task_id, 30, "正在后台整理会话摘要与长期记忆")
    maintenance = await maintain_conversation_memory(task_id, runtime_factory())
    await record_progress(task_id, 76, "记忆提炼已完成，正在持久化")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        source_task_id = str(task.request_payload.get("source_task_id") or "")
        source_task = await session.get(AITask, source_task_id)
        assistant_message = await session.get(
            AgentChatMessage,
            str(task.request_payload.get("assistant_message_id") or ""),
        )
        chat_session = await session.scalar(
            select(AgentChatSession)
            .where(AgentChatSession.id == str(task.request_payload.get("agent_chat_session_id") or ""))
            .with_for_update()
        )
        if (
            source_task is None
            or source_task.tenant_id != task.tenant_id
            or source_task.user_id != task.user_id
            or source_task.project_id != task.project_id
            or assistant_message is None
            or assistant_message.run_id != source_task.id
            or chat_session is None
            or assistant_message.session_id != chat_session.id
        ):
            raise RuntimeError("Agent 记忆维护持久化目标已失效")

        previous = await latest_chat_summary(session, chat_session.id)
        covered = maintenance is None or await summary_covers_message(
            session,
            previous,
            assistant_message,
        )
        assistant_manifest = dict(assistant_message.runtime_manifest or {})
        if covered:
            memory_manifest = {
                **dict(assistant_manifest.get("memory") or {}),
                "state": "covered",
                "maintenance_task_id": task.id,
                "summary_version": previous.version if previous else 0,
            }
            task.result_payload = {
                **(task.result_payload or {}),
                "source_task_id": source_task.id,
                "assistant_message_id": assistant_message.id,
                "skipped": True,
                "summary_id": previous.id if previous else None,
            }
        else:
            maintenance_payload, maintenance_metadata = maintenance
            summary, extracted_memory_ids = await persist_conversation_maintenance(
                session,
                task=task,
                chat_session=chat_session,
                assistant_message=assistant_message,
                payload=maintenance_payload,
                maintenance_metadata=maintenance_metadata,
                source_task_id=source_task.id,
            )
            memory_manifest = {
                **dict(assistant_manifest.get("memory") or {}),
                "state": "completed",
                "maintenance_task_id": task.id,
                "extracted_count": len(extracted_memory_ids),
                "summary_version": summary.version,
                "summary_fallback": bool(maintenance_metadata.get("fallback")),
            }
            task.result_payload = {
                **(task.result_payload or {}),
                "source_task_id": source_task.id,
                "assistant_message_id": assistant_message.id,
                "summary_id": summary.id,
                "extracted_memory_ids": extracted_memory_ids,
            }
        assistant_manifest["memory"] = memory_manifest
        assistant_message.runtime_manifest = assistant_manifest
        chat_manifest = dict(chat_session.runtime_manifest or {})
        if covered:
            chat_manifest["memory"] = {
                **dict(chat_manifest.get("memory") or {}),
                "state": "completed",
                "summary_version": previous.version if previous else 0,
            }
        else:
            chat_manifest["memory"] = memory_manifest
        chat_session.runtime_manifest = chat_manifest
        task.status = TaskStatus.SUCCEEDED
        task.error_message = None
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message="会话摘要与长期记忆已在后台整理完成",
        )
        await session.commit()
        await publish_task_event(task, event)


async def generate_personal_agent_media(
    task_id: str,
    *,
    prompt: str,
    gateway_factory: GatewayFactory,
) -> dict[str, object]:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.model_id:
            raise RuntimeError("个人媒体任务上下文已失效")
        mode = str(task.request_payload.get("mode") or "")
        expected_type = ModelType.IMAGE if mode == "image" else ModelType.VIDEO
        if mode not in {"image", "video"}:
            raise RuntimeError("个人媒体任务模式无效")
        model = await session.get(AIModel, task.model_id)
        if (
            model is None
            or model.tenant_id != task.tenant_id
            or model.model_type != expected_type
            or not model.enabled
        ):
            raise RuntimeError("个人媒体任务模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("个人媒体任务模型平台不可用")
        result_payload = dict(task.result_payload or {})
        cached_media = result_payload.get("generated_media_draft")
        if isinstance(cached_media, dict) and cached_media.get("storage_path"):
            try:
                await materialize_media_file(str(cached_media["storage_path"]))
                return dict(cached_media)
            except (FileNotFoundError, ValueError):
                result_payload.pop("generated_media_draft", None)
        cached_prompt = str(result_payload.get("media_prompt") or "").strip()
        if cached_prompt:
            prompt = cached_prompt
        else:
            result_payload["media_prompt"] = prompt
            task.result_payload = result_payload
            await session.commit()

        options = dict(task.request_payload.get("media_options") or {})
        attachment_ids = [str(item) for item in task.request_payload.get("attachment_ids") or [] if str(item)]
        attachments = (
            list(
                (
                    await session.scalars(
                        select(PersonalAgentAttachment).where(
                            PersonalAgentAttachment.id.in_(attachment_ids),
                            PersonalAgentAttachment.tenant_id == task.tenant_id,
                            PersonalAgentAttachment.user_id == task.user_id,
                            PersonalAgentAttachment.session_id
                            == str(task.request_payload.get("agent_chat_session_id") or ""),
                        )
                    )
                ).all()
            )
            if attachment_ids
            else []
        )
        attachments_by_id = {item.id: item for item in attachments}
        ordered_attachments = [
            attachments_by_id[item_id] for item_id in attachment_ids if item_id in attachments_by_id
        ]
        gateway = gateway_factory(provider)
        model_name = model.name
        model_identifier = model.model_id
        capabilities = dict(model.capabilities or {})
        task_snapshot = task

    if mode == "image":
        resolution = str(options.get("resolution") or "1K")
        aspect_ratio = str(options.get("aspect_ratio") or "1:1")
        await record_progress(task_id, 62, "最终提示词已就绪，正在调用图片模型")
        reference_image_urls: list[str] = []
        generation_mode = "text_to_image"
        if str(task_snapshot.request_payload.get("media_generation_mode") or "") == "image_to_image":
            reference_ids = [
                str(item) for item in task_snapshot.request_payload.get("attachment_ids") or [] if str(item)
            ]
            if reference_ids:
                for reference in ordered_attachments:
                    data = await object_storage().get_bytes(reference.storage_path)
                    content_type = reference.mime_type or media_content_type_from_key_or_bytes(
                        reference.storage_path,
                        data,
                    )
                    reference_image_urls.append(
                        f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
                    )
                if reference_image_urls:
                    generation_mode = "image_to_image"
        image_data = await gateway.generate_image(
            ImageGenerationRequest(
                model=model_identifier,
                prompt=prompt,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                capabilities=capabilities,
                idempotency_key=task_snapshot.idempotency_key or task_snapshot.id,
                reference_image_url=reference_image_urls[0] if reference_image_urls else None,
                generation_mode=generation_mode,
                reference_image_urls=reference_image_urls,
            )
        )
        await record_progress(task_id, 88, "图片已生成，正在写入个人会话")
        stored_path = await run_in_threadpool(
            save_agent_chat_image,
            image_data,
            uploads_root=get_settings().uploads_root,
            tenant_id=task_snapshot.tenant_id,
            project_id=personal_agent_scope_id(task_snapshot.user_id),
        )
        mime_type = "image/webp"
        media_name = f"AI图片-{task_snapshot.id[:8]}.webp"
        duration_seconds: float | None = None
        provider_job_id: str | None = None
    else:
        requested_duration = float(options.get("duration_seconds") or 5)
        duration_seconds = closest_supported_video_duration(
            capabilities,
            requested_duration=requested_duration,
        )
        aspect_ratio = str(options.get("aspect_ratio") or "16:9")
        supported_aspects = capabilities.get("aspect_ratios")
        if (
            isinstance(supported_aspects, list)
            and supported_aspects
            and aspect_ratio not in supported_aspects
        ):
            aspect_ratio = str(supported_aspects[0])
        resolution = str(options.get("resolution") or "720p")
        requested_generation_mode = str(task_snapshot.request_payload.get("media_generation_mode") or "")
        reference_media: list[dict[str, str]] = []
        if requested_generation_mode != "text_to_video":
            for index, attachment in enumerate(ordered_attachments, start=1):
                data = await object_storage().get_bytes(attachment.storage_path)
                encoded = base64.b64encode(data).decode("ascii")
                reference_media.append(
                    {
                        "type": "image",
                        "url": attachment.media_url,
                        "data_uri": f"data:{attachment.mime_type};base64,{encoded}",
                        "base64": encoded,
                        "token": f"<Picture {index}>",
                        "role": "first_frame" if index == 1 else "reference",
                    }
                )
        reference_limits = capabilities.get("reference_limits")
        image_limit = reference_limits.get("image") if isinstance(reference_limits, dict) else None
        max_references = int(image_limit.get("max_count") or 0) if isinstance(image_limit, dict) else 0
        if max_references:
            reference_media = reference_media[:max_references]
        configured_modes = [str(item) for item in capabilities.get("generation_modes") or [] if str(item)]
        if requested_generation_mode == "image_to_video" and not reference_media:
            raise RuntimeError("图生视频需要至少一张参考图片")
        if requested_generation_mode == "text_to_video":
            generation_mode = "text_to_video"
            reference_media = []
        elif requested_generation_mode == "image_to_video":
            if len(reference_media) > 1 and "multi_shot" in configured_modes:
                generation_mode = "multi_shot"
            elif "first_frame" in configured_modes:
                generation_mode = "first_frame"
            elif "full_reference" in configured_modes:
                generation_mode = "full_reference"
            elif not configured_modes:
                generation_mode = "first_frame"
            else:
                raise RuntimeError("当前视频模型不支持图生视频参考模式")
        elif reference_media and len(reference_media) > 1 and "multi_shot" in configured_modes:
            generation_mode = "multi_shot"
        elif reference_media and "first_frame" in configured_modes:
            generation_mode = "first_frame"
        elif reference_media and "full_reference" in configured_modes:
            generation_mode = "full_reference"
        elif reference_media and not configured_modes:
            generation_mode = "first_frame"
        else:
            generation_mode = "text_to_video"
            if configured_modes and generation_mode not in configured_modes:
                if reference_media:
                    raise RuntimeError("当前默认视频模型不支持所上传图片对应的参考生成模式")
                generation_mode = configured_modes[0]
            reference_media = []
        if capabilities.get("schema_version") == 1:
            resolution = compatible_video_resolution(
                capabilities,
                duration_seconds=duration_seconds,
                requested_resolution=resolution,
                aspect_ratio=aspect_ratio,
            )
            validate_video_generation_request(
                capabilities,
                generation_mode=generation_mode,
                duration_seconds=duration_seconds,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                reference_media=reference_media,
                audio_enabled=default_video_audio_enabled(capabilities),
            )
        reference_media = await prepare_adapter_reference_media(provider, reference_media)
        first_reference = reference_media[0] if reference_media else None
        reference_image_url = (
            first_reference.get("data_uri") or first_reference.get("url") if first_reference else None
        )
        request = VideoGenerationRequest(
            model=model_identifier,
            prompt=prompt,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            reference_image_url=reference_image_url,
            capabilities=capabilities,
            idempotency_key=task_snapshot.idempotency_key or task_snapshot.id,
            generation_mode=generation_mode,
            audio_enabled=default_video_audio_enabled(capabilities),
            reference_media=reference_media,
        )
        provider_job_id = (
            str(
                task_snapshot.provider_job_id
                or (task_snapshot.result_payload or {}).get("provider_job_id")
                or ""
            )
            or None
        )
        await record_progress(
            task_id,
            62,
            "正在继续查询视频模型任务" if provider_job_id else "最终提示词已就绪，正在提交视频模型",
        )
        video_result = (
            await gateway.poll_video(request, provider_job_id)
            if provider_job_id
            else await gateway.submit_video(request)
        )
        if video_result.provider_job_id and video_result.provider_job_id != provider_job_id:
            provider_job_id = video_result.provider_job_id
            async with SessionLocal() as session:
                task = await owned_task_for_update(session, task_id)
                if not owns_running_task(task):
                    return {}
                task.provider_job_id = provider_job_id
                task.result_payload = {
                    **(task.result_payload or {}),
                    "provider_job_id": provider_job_id,
                }
                event = record_task_event(
                    session,
                    task,
                    status=TaskStatus.RUNNING,
                    progress=68,
                    message="视频平台已受理，正在生成",
                    metadata={"provider_job_id": provider_job_id},
                )
                await session.commit()
                await publish_task_event(task, event)
        adapter_video = (
            provider.adapter_config.get("video") if isinstance(provider.adapter_config, dict) else None
        )
        adapter_video = adapter_video if isinstance(adapter_video, dict) else {}
        configured_poll_interval = (
            adapter_video.get("poll_interval_seconds") or capabilities.get("poll_interval_seconds") or 5
        )
        configured_poll_timeout = (
            adapter_video.get("poll_timeout_seconds") or capabilities.get("poll_timeout_seconds") or 1800
        )
        poll_interval = max(
            1.0,
            min(float(configured_poll_interval), 60.0),
        )
        poll_timeout = max(
            30.0,
            min(float(configured_poll_timeout), 7200.0),
        )
        poll_started = time.monotonic()
        last_progress_event = poll_started
        while video_result.status == "pending":
            if not provider_job_id:
                raise RuntimeError("视频平台未返回可恢复的任务 ID")
            if time.monotonic() - poll_started >= poll_timeout:
                raise RuntimeError("视频平台任务等待超时，可稍后重试")
            await asyncio.sleep(poll_interval)
            signal_task_activity(task_id)
            video_result = await gateway.poll_video(request, provider_job_id)
            if time.monotonic() - last_progress_event >= 15:
                await record_progress(task_id, 74, "视频模型仍在生成，任务状态正常")
                last_progress_event = time.monotonic()
        if video_result.status == "failed":
            raise ProviderJobTerminalError(video_result.error_message or "视频平台任务失败")
        if video_result.video_data is None:
            raise RuntimeError("视频平台未返回视频文件")
        await record_progress(task_id, 88, "视频已生成，正在写入个人会话")
        _local_url, stored_path, mime_type = await run_in_threadpool(
            save_project_video,
            video_result.video_data,
            uploads_root=get_settings().uploads_root,
            tenant_id=task_snapshot.tenant_id,
            project_id=personal_agent_scope_id(task_snapshot.user_id),
            clip_id=task_snapshot.id,
            content_type=video_result.content_type,
        )
        media_name = f"AI视频-{task_snapshot.id[:8]}{stored_path.suffix}"
        image_data = video_result.video_data

    try:
        storage_key, media_url = await persist_media_file(stored_path, mime_type)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    media = {
        "name": media_name,
        "mime_type": mime_type,
        "size_bytes": len(image_data),
        "storage_path": storage_key,
        "cached_path": str(stored_path),
        "media_url": media_url,
        "prompt": prompt,
        "model_id": task_snapshot.model_id,
        "model_name": model_name,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
        "generation_mode": generation_mode,
        "provider_job_id": provider_job_id,
    }
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            await cleanup_media(storage_key, stored_path)
            return {}
        task.result_payload = {**(task.result_payload or {}), "generated_media_draft": media}
        task.provider_job_id = provider_job_id
        await session.commit()
    return media


async def execute_agent_chat_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
    gateway_factory: GatewayFactory,
) -> None:
    from app.api.routes.agent_chat import apply_project_file_changes

    await record_progress(task_id, 24, "正在锁定会话、模型与创作上下文")
    request, project_files, task = await agent_chat_runtime_request(task_id)
    context_budget = dict((task.result_payload or {}).get("context_budget") or {})
    await record_progress(task_id, 38, "记忆与技能目录已装载，Agent 正在创作")
    runtime = runtime_factory()
    mode = str(task.request_payload.get("mode") or "chat")
    relay = AgentStreamRelay(
        task=task,
        session_id=str(task.request_payload.get("agent_chat_session_id") or ""),
        publish_text=mode not in {"image", "video"},
        hide_media_actions=(mode == "chat" and task.request_payload.get("scope") == "personal"),
    )
    run_stream = getattr(runtime, "run_stream", None)
    try:
        if callable(run_stream):
            result = await run_stream(request, relay)
        else:
            result = await runtime.run(request)
    finally:
        await relay.finish()
        await relay.persist_snapshot(force=True)
    assistant_response = result.final_response
    generated_media: dict[str, object] | None = None
    media_prompt: PersonalMediaPromptPayload | None = None
    current_prompt = request.prompt.split(
        "\n\n本轮可引用的会话图片附件：",
        1,
    )[0].strip()
    allow_media_prompt_rewrite = bool(task.request_payload.get("allow_media_prompt_rewrite"))
    if mode == "chat" and task.request_payload.get("scope") == "personal":
        envelope = media_action_from_response(result.final_response)
        if envelope is None:
            async with SessionLocal() as session:
                available_models = list(
                    (
                        await session.scalars(
                            select(AIModel)
                            .join(Provider, Provider.id == AIModel.provider_id)
                            .where(
                                AIModel.tenant_id == task.tenant_id,
                                AIModel.model_type.in_([ModelType.IMAGE, ModelType.VIDEO]),
                                AIModel.enabled.is_(True),
                                Provider.tenant_id == task.tenant_id,
                                Provider.enabled.is_(True),
                            )
                        )
                    ).all()
                )
            fallback_action = infer_explicit_media_action(
                current_prompt,
                available_attachments=request.attachments,
                available_models=available_models,
            )
            if fallback_action is not None:
                contextual_interpretation = result.final_response.strip()
                if re.search(r"(?:不能|无法|不支持|没有).{0,12}(?:生成|制作)", contextual_interpretation):
                    contextual_interpretation = ""
                fallback_action.prompt = f"用户的生成要求：{current_prompt}"
                if contextual_interpretation:
                    fallback_action.prompt += (
                        f"\n\n结合会话上下文形成的创作理解：{contextual_interpretation[:4000]}"
                    )
                envelope = PersonalChatResponsePayload(
                    message=(
                        "我已根据当前对话和你的参数开始生成图片。"
                        if fallback_action.type == "image"
                        else "我已根据当前对话和你的参数开始生成视频。"
                    ),
                    media=fallback_action,
                )
        if envelope is not None:
            assistant_response = envelope.message.strip() or "我已理解你的要求。"
            if envelope.media is not None:
                action = validate_personal_media_action(envelope.media)
                available_image_ids = [
                    item.id for item in request.attachments if item.mime_type.startswith("image/")
                ]
                if available_image_ids and should_reuse_historical_image(current_prompt):
                    action.generation_mode = "image_to_image" if action.type == "image" else "image_to_video"
                    selected_reference_ids = [
                        item for item in action.reference_attachment_ids if item in available_image_ids
                    ]
                    action.reference_attachment_ids = selected_reference_ids or [available_image_ids[0]]
                reference_ids = list(action.reference_attachment_ids)
                if action.generation_mode.startswith("image_to_") and not reference_ids:
                    reference_ids = [
                        str(item) for item in task.request_payload.get("attachment_ids") or [] if str(item)
                    ]
                if action.generation_mode.startswith("image_to_") and not reference_ids:
                    reference_ids = [
                        item.id for item in request.attachments if item.mime_type.startswith("image/")
                    ]
                if action.generation_mode.startswith("image_to_") and not reference_ids:
                    raise RuntimeError("请先上传一张参考图片，再使用参考生成")
                media_type = ModelType.IMAGE if action.type == "image" else ModelType.VIDEO
                requested_resolution = action.resolution
                if media_type == ModelType.IMAGE and requested_resolution:
                    requested_resolution = normalize_image_resolution(requested_resolution)
                    if requested_resolution is None:
                        raise RuntimeError("图片分辨率仅支持 1K、2K 或 4K")
                async with SessionLocal() as session:
                    media_task = await owned_task_for_update(session, task_id)
                    if not owns_running_task(media_task):
                        return
                    media_model, _provider = await resolve_personal_media_model(
                        session,
                        task=media_task,
                        media_type=media_type,
                        requested_model=action.model_id,
                        resolution=requested_resolution,
                    )
                    if reference_ids:
                        reference_rows = list(
                            (
                                await session.scalars(
                                    select(PersonalAgentAttachment).where(
                                        PersonalAgentAttachment.id.in_(reference_ids),
                                        PersonalAgentAttachment.tenant_id == media_task.tenant_id,
                                        PersonalAgentAttachment.user_id == media_task.user_id,
                                        PersonalAgentAttachment.session_id
                                        == str(media_task.request_payload.get("agent_chat_session_id") or ""),
                                        PersonalAgentAttachment.mime_type.like("image/%"),
                                    )
                                )
                            ).all()
                        )
                        valid_reference_ids = {item.id for item in reference_rows}
                        if any(item not in valid_reference_ids for item in reference_ids):
                            raise RuntimeError("本轮引用的历史图片已失效，请重新上传")
                    capabilities = dict(media_model.capabilities or {})
                    if media_type == ModelType.IMAGE:
                        default_resolution, default_aspect = default_image_options(capabilities)
                        resolved_resolution = requested_resolution or default_resolution
                        resolved_aspect = action.aspect_ratio or default_aspect
                        resolved_duration = None
                    else:
                        default_resolution, default_aspect, default_duration = default_video_options(
                            capabilities
                        )
                        resolved_resolution = requested_resolution or default_resolution
                        resolved_aspect = action.aspect_ratio or default_aspect
                        resolved_duration = action.duration_seconds or default_duration
                    pricing = await resolve_task_pricing(
                        session,
                        tenant_id=media_task.tenant_id,
                        task_type=(
                            "asset_image_generation"
                            if media_type == ModelType.IMAGE
                            else "shot_video_generation"
                        ),
                    )
                    await debit_additional_task_cost(
                        session,
                        media_task,
                        pricing.total_cost,
                        reason=(
                            "普通对话自动图片生成"
                            if media_type == ModelType.IMAGE
                            else "普通对话自动视频生成"
                        ),
                    )
                    payload = dict(media_task.request_payload)
                    payload.update(
                        {
                            "original_mode": "chat",
                            "mode": action.type,
                            "media_model_id": media_model.id,
                            "media_generation_mode": action.generation_mode,
                            "media_options": {
                                key: value
                                for key, value in {
                                    "resolution": resolved_resolution,
                                    "aspect_ratio": resolved_aspect,
                                    "duration_seconds": resolved_duration,
                                }.items()
                                if value is not None
                            },
                            "attachment_ids": reference_ids,
                        }
                    )
                    media_task.request_payload = payload
                    media_task.model_id = media_model.id
                    media_task.result_payload = {
                        **(media_task.result_payload or {}),
                        "media_intent": {
                            "type": action.type,
                            "generation_mode": action.generation_mode,
                            "model_id": media_model.id,
                            "reference_attachment_ids": reference_ids,
                        },
                    }
                    await session.commit()
                task.request_payload = payload
                task.model_id = media_model.id
                mode = action.type
                media_prompt = PersonalMediaPromptPayload(
                    message=assistant_response,
                    prompt=(action.prompt if allow_media_prompt_rewrite else current_prompt),
                )
    if mode in {"image", "video"}:
        await record_progress(task_id, 52, "Agent 已理解需求，正在整理最终媒体提示词")
        if media_prompt is None:
            try:
                media_prompt = PersonalMediaPromptPayload.model_validate(
                    parse_json_object(result.final_response)
                )
            except (RuntimeError, ValidationError):
                # Agent tool use can complete without a final text response. The user's
                # current message remains a valid direct-generation instruction.
                fallback_prompt = request.prompt.strip()
                if not fallback_prompt:
                    raise RuntimeError("未能获取本轮媒体生成描述，请重新输入后再试") from None
                media_prompt = PersonalMediaPromptPayload(
                    message="",
                    prompt=fallback_prompt,
                )
                assistant_response = (
                    "未收到可用的 AI 提示词，已按你的原始描述继续生成图片。"
                    if mode == "image"
                    else "未收到可用的 AI 提示词，已按你的原始描述继续生成视频。"
                )
            else:
                assistant_response = media_prompt.message.strip() or (
                    "图片已按你的要求生成。" if mode == "image" else "视频已按你的要求生成。"
                )
        if not allow_media_prompt_rewrite and current_prompt:
            media_prompt.prompt = current_prompt
        generated_media = await generate_personal_agent_media(
            task_id,
            prompt=media_prompt.prompt,
            gateway_factory=gateway_factory,
        )
        if not generated_media:
            return
        await record_progress(task_id, 93, "媒体文件已保存，正在同步会话结果")
    else:
        await record_progress(task_id, 82, "主回复已完成，正在校验并保存结果")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        personal_scope = task.request_payload.get("scope") == "personal"
        project = await session.get(Project, task.project_id) if task.project_id else None
        user = await session.get(User, task.user_id)
        chat_session = await session.get(
            AgentChatSession,
            str(task.request_payload.get("agent_chat_session_id") or ""),
        )
        user_message = await session.get(
            AgentChatMessage,
            str(task.request_payload.get("user_message_id") or ""),
        )
        if user is None or chat_session is None or user_message is None:
            raise RuntimeError("Agent 对话上下文在完成前已被删除")
        if personal_scope and (task.project_id is not None or chat_session.project_id is not None):
            raise RuntimeError("个人 Agent 会话意外绑定了项目")
        if not personal_scope and project is None:
            raise RuntimeError("Agent 对话项目在完成前已被删除")
        current_hash = hashlib.sha256(user_message.content.encode("utf-8")).hexdigest()
        if current_hash != task.request_payload.get("prompt_hash"):
            raise RuntimeError("Agent 对话内容在执行期间发生变化")
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        chapter = await session.get(Chapter, chapter_id) if chapter_id else None
        if chapter_id and (
            chapter is None
            or project is None
            or chapter.project_id != project.id
            or chapter.tenant_id != task.tenant_id
            or chapter.user_id != task.user_id
        ):
            raise RuntimeError("Agent 对话绑定的导演台章节已失效")
        if chapter is not None:
            source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
            if source_hash != task.request_payload.get("chapter_source_hash"):
                raise RuntimeError("当前章节原文已更新，本轮 Agent 结果已作废")
        existing = await session.scalar(
            select(AgentChatMessage).where(
                AgentChatMessage.session_id == chat_session.id,
                AgentChatMessage.run_id == task.id,
                AgentChatMessage.role == AgentMessageRole.ASSISTANT,
            )
        )
        if existing is not None:
            memory_task, memory_task_created = await ensure_memory_maintenance_task(
                session,
                source_task=task,
                assistant_message=existing,
            )
            task.status = TaskStatus.SUCCEEDED
            task.error_message = None
            task.result_payload = {
                **(task.result_payload or {}),
                "assistant_message_id": existing.id,
                "agent_chat_session_id": chat_session.id,
                "memory_maintenance_task_id": memory_task.id,
            }
            event = record_task_event(
                session,
                task,
                status=TaskStatus.SUCCEEDED,
                progress=100,
                message="Agent 回复已从持久记录恢复",
                metadata={"assistant_message_id": existing.id, "recovered": True},
            )
            await session.commit()
            await publish_task_event(task, event)
            if memory_task_created:
                await enqueue_task(memory_task.id)
            return

        agent_task_dispatches: list[tuple[AITask, TaskEvent]] = []
        if personal_scope:
            allow_personal_skill_changes = bool(task.request_payload.get("allow_personal_skill_changes"))
            ignored_personal_file_changes = bool(
                result.project_file_changes and not allow_personal_skill_changes
            )
            file_change_outcomes = (
                await apply_personal_agent_skill_changes(
                    session,
                    user=user,
                    changes=result.project_file_changes,
                )
                if allow_personal_skill_changes
                else []
            )
            if ignored_personal_file_changes:
                assistant_response = (
                    "我已结合会话中的参考图片和你的要求完成本轮媒体生成；本轮没有修改或保存个人 Skill。"
                    if generated_media is not None
                    else "本轮没有修改或保存个人 Skill；只有你明确要求创建、保存或修改 Skill 时才会更改。"
                )
        else:
            file_change_outcomes = await apply_project_file_changes(
                session,
                user,
                project,
                chat_session,
                task.id,
                project_files,
                result.project_file_changes,
                chapter,
                agent_task_dispatches,
            )
        domain_change_outcomes = [item for item in file_change_outcomes if item.get("resource_type")]
        director_review_dispatch: tuple[AITask, TaskEvent] | None = None
        script_change = next(
            (
                item
                for item in domain_change_outcomes
                if item.get("resource_type") == "script_version" and item.get("status") == "applied"
            ),
            None,
        )
        if chapter is not None and script_change is not None:
            from app.services.director_orchestration import prepare_script_review_workflow

            script = await session.get(ScriptVersion, str(script_change.get("resource_id") or ""))
            if script is None:
                raise RuntimeError("Agent 已发布的剧本版本无法进入自动审核")
            prepared = await prepare_script_review_workflow(
                session,
                chapter=chapter,
                script=script,
                user=user,
                chat_session_id=chat_session.id,
                source_task_id=task.id,
            )
            if prepared is not None:
                workflow, review_task, review_event = prepared
                script_change["workflow_id"] = workflow.id
                script_change["review_task_id"] = review_task.id
                director_review_dispatch = (review_task, review_event)
        runtime_manifest = dict(result.manifest)
        runtime_manifest.update(
            {
                "agent_chat_session_id": chat_session.id,
                "runtime_session_id": request.session_id,
                "project_file_changes": file_change_outcomes,
                "domain_changes": domain_change_outcomes,
                "context_budget": context_budget,
            }
        )
        if personal_scope:
            runtime_manifest.update(
                {
                    "scope": "personal",
                    "scene": "workspace",
                    "mode": mode,
                    "requested_mode": str(task.request_payload.get("original_mode") or mode),
                    "media_intent": (task.result_payload or {}).get("media_intent"),
                    "selected_skill_ids": list(task.request_payload.get("selected_skill_ids") or []),
                    "skill_change_requested": bool(task.request_payload.get("allow_personal_skill_changes")),
                }
            )
        assistant_message = AgentChatMessage(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            session_id=chat_session.id,
            role=AgentMessageRole.ASSISTANT,
            content=assistant_response,
            run_id=task.id,
            finish_reason=result.finish_reason,
            runtime_events=result.events,
            runtime_manifest=runtime_manifest,
        )
        session.add(assistant_message)
        await session.flush()
        generated_media_public: list[dict[str, object]] = []
        if generated_media is not None:
            attachment = PersonalAgentAttachment(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                session_id=chat_session.id,
                message_id=assistant_message.id,
                name=str(generated_media["name"]),
                mime_type=str(generated_media["mime_type"]),
                size_bytes=int(generated_media["size_bytes"]),
                storage_path=str(generated_media["storage_path"]),
                media_url=str(generated_media["media_url"]),
            )
            session.add(attachment)
            await session.flush()
            generated_media_public.append(
                {
                    "id": attachment.id,
                    "name": attachment.name,
                    "mime_type": attachment.mime_type,
                    "size_bytes": attachment.size_bytes,
                    "media_url": attachment.media_url,
                    "prompt": generated_media["prompt"],
                    "model_id": generated_media["model_id"],
                    "model_name": generated_media["model_name"],
                    "resolution": generated_media["resolution"],
                    "aspect_ratio": generated_media["aspect_ratio"],
                    "duration_seconds": generated_media["duration_seconds"],
                    "generation_mode": generated_media["generation_mode"],
                }
            )
            runtime_manifest = {
                **runtime_manifest,
                "generated_media": generated_media_public,
            }
            assistant_message.runtime_manifest = runtime_manifest
        memory_task, _memory_task_created = await ensure_memory_maintenance_task(
            session,
            source_task=task,
            assistant_message=assistant_message,
        )
        runtime_manifest = {
            **runtime_manifest,
            "memory": {
                "state": "queued",
                "retrieved_count": len(request.memory_context),
                "maintenance_task_id": memory_task.id,
            },
        }
        assistant_message.runtime_manifest = runtime_manifest
        chat_session.runtime_manifest = runtime_manifest
        chat_session.last_message_at = datetime.now(UTC)
        applied_count = sum(item["status"] == "applied" for item in file_change_outcomes)
        conflict_count = sum(item["status"] == "conflict" for item in file_change_outcomes)
        task.result_payload = {
            **(task.result_payload or {}),
            "assistant_message_id": assistant_message.id,
            "agent_chat_session_id": chat_session.id,
            "applied_file_changes": applied_count,
            "conflicted_file_changes": conflict_count,
            "domain_changes": domain_change_outcomes,
            "runtime_manifest": runtime_manifest,
            "generated_media": generated_media_public,
            "memory_maintenance_task_id": memory_task.id,
        }
        task.status = TaskStatus.SUCCEEDED
        task.error_message = None
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=(
                "对话中的图片已生成并同步到会话"
                if mode == "image"
                else "对话中的视频已生成并同步到会话"
                if mode == "video"
                else "Agent 回复已完成并同步到创作会话"
            ),
            metadata={
                "assistant_message_id": assistant_message.id,
                "applied_file_changes": applied_count,
                "conflicted_file_changes": conflict_count,
            },
        )
        await session.commit()
        await publish_task_event(task, event)
        if director_review_dispatch is not None:
            review_task, review_event = director_review_dispatch
            await enqueue_task(review_task.id)
            await publish_task_event(review_task, review_event)
        for queued_task, queued_event in agent_task_dispatches:
            await enqueue_task(queued_task.id)
            await publish_task_event(queued_task, queued_event)
        await enqueue_task(memory_task.id)


def parse_json_object(content: str) -> dict:
    candidates = [content.strip()]
    candidates.extend(
        match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*(.*?)```", content, re.S)
    )
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise RuntimeError("AI 未返回可解析的 JSON 结果")


async def missing_storyboard_ready_asset_names(assets: list[Asset]) -> list[str]:
    missing: list[str] = []
    for asset in assets:
        if asset.asset_type == AssetType.AUDIO:
            continue
        storage_key = object_key_from_media_url(asset.media_url)
        if asset.status != AssetStatus.READY or not storage_key:
            missing.append(asset.name)
            continue
        try:
            await materialize_media_file(storage_key)
        except (FileNotFoundError, ValueError):
            missing.append(asset.name)
    return missing


async def record_progress(task_id: str, progress: int, message: str) -> None:
    signal_task_activity(task_id)
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        event = record_task_event(
            session,
            task,
            status=TaskStatus.RUNNING,
            progress=progress,
            message=message,
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_chapter_analysis_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        chapter = await session.get(Chapter, chapter_id)
        if chapter is None or chapter.project_id != task.project_id:
            raise RuntimeError("待分析章节已不存在")
        source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
        if source_hash != task.request_payload.get("source_hash"):
            raise RuntimeError("章节原文已更新，本次分析任务已失效")
        prompt = (
            "阅读以下章节原文，生成可供短剧改编决策使用的结构化章节分析。"
            "必须基于原文，不得虚构未出现的关键事实。事件按因果和发生顺序排列；"
            "改编策略需说明保留、压缩、前置和悬念处理；风险需指出信息缺口、节奏或拍摄难点。\n"
            '返回结构：{"summary":"章节摘要","core_conflict":"核心冲突",'
            '"opening_hook":"开场钩子","adaptation_strategy":"改编策略",'
            '"events":[{"title":"事件","description":"事件详情",'
            '"dramatic_value":"戏剧作用"}],"characters":[{"name":"人物",'
            '"role":"身份与功能","motivation":"动机","relationship":"关系"}],'
            '"risks":["改编风险"]}\n\n'
            f"章节：{chapter.title}\n来源模式：{chapter.source_mode.value}\n原文：\n{chapter.original_content}"
        )
    request = await runtime_request(task_id, prompt_code="chapter-analysis", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = ChapterAnalysisPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的章节分析结构不符合要求") from exc
    await record_progress(task_id, 76, "章节事件与人物关系已整理，正在建立分析版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, chapter_id)
        if not owns_running_task(task) or chapter is None:
            return
        source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
        if source_hash != task.request_payload.get("source_hash"):
            raise RuntimeError("章节原文已更新，本次分析结果未覆盖新内容")
        latest = await session.scalar(
            select(func.max(ChapterAnalysis.version)).where(ChapterAnalysis.chapter_id == chapter.id)
        )
        payload = parsed.model_dump(mode="json")
        analysis = ChapterAnalysis(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            version=(latest or 0) + 1,
            summary=parsed.summary,
            content=payload,
        )
        session.add(analysis)
        await session.flush()
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-章节分析-v{analysis.version}.json",
                kind=ProjectFileKind.ANALYSIS,
                mime_type="application/json",
                size_bytes=len(serialized.encode("utf-8")),
                content=serialized,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "analysis_id": analysis.id,
                    "source_task_id": task.id,
                    "source_hash": source_hash,
                },
            )
        )
        chapter.status = ChapterStatus.ANALYZED
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "analysis_id": analysis.id,
            "analysis_version": analysis.version,
            "event_count": len(parsed.events),
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"章节分析 v{analysis.version} 已完成",
            metadata={"analysis_id": analysis.id, "event_count": len(parsed.events)},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_script_generation_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        analysis_id = str(task.request_payload.get("analysis_id") or "")
        base_script_id = str(task.request_payload.get("base_script_version_id") or "")
        chapter = await session.get(Chapter, chapter_id)
        analysis = await session.get(ChapterAnalysis, analysis_id) if analysis_id else None
        base_script = await session.get(ScriptVersion, base_script_id) if base_script_id else None
        if chapter is None or chapter.project_id != task.project_id:
            raise RuntimeError("待改编章节已不存在")
        source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
        if source_hash != task.request_payload.get("source_hash"):
            raise RuntimeError("章节原文已更新，本次剧本任务已失效")
        if analysis_id and (analysis is None or analysis.chapter_id != chapter.id):
            raise RuntimeError("剧本任务所用章节分析已不存在")
        if base_script_id and (base_script is None or base_script.chapter_id != chapter.id):
            raise RuntimeError("剧本任务所用参考版本已不存在")
        project = await session.get(Project, chapter.project_id)
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        image_model = await project_image_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        image_contract = image_model_execution_contract(
            image_model,
            selected_resolution=str(project.image_resolution or "1K") if project else "1K",
            aspect_ratio=str(project.aspect_ratio or "1:1") if project else "1:1",
        )
        analysis_context = json.dumps(analysis.content, ensure_ascii=False) if analysis else "未提供"
        base_context = base_script.content if base_script else "未提供"
        prompt = (
            "将以下章节改编为可直接进入导演审核的短剧剧本。必须保留原文核心事实，"
            "同时强化开场钩子、冲突递进和结尾悬念。正文需要包含场次编号、内外景、地点、时间、"
            "出场人物、可拍摄动作和完整台词；不得输出创作解释。review_notes 只记录需人工审核的改编决策。\n"
            '返回结构：{"title":"剧本版本标题","content":"完整剧本正文",'
            '"review_notes":"审核关注点","continuity_summary":"1500字以内的已发生事件、人物状态、未回收伏笔与结尾承接，不得编造后续剧情"}\n\n'
            f"章节：{chapter.title}\n来源模式：{chapter.source_mode.value}\n"
            f"章节分析：{analysis_context}\n参考剧本：\n{base_context}\n\n"
            f"用户本次导演要求：{task.request_payload.get('director_instruction') or '无额外要求'}\n\n"
            f"目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}\n\n"
            f"目标图片模型完整执行契约：{json.dumps(image_contract, ensure_ascii=False)}\n\n"
            f"原文：\n{chapter.original_content}"
        )
    request = await runtime_request(task_id, prompt_code="script-generation", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = GeneratedScriptPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的剧本结构不符合要求") from exc
    await record_progress(task_id, 78, "剧本正文已生成，正在建立待审核版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, chapter_id)
        if not owns_running_task(task) or chapter is None:
            return
        source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
        if source_hash != task.request_payload.get("source_hash"):
            raise RuntimeError("章节原文已更新，本次剧本结果未覆盖新内容")
        analysis = await session.get(ChapterAnalysis, analysis_id) if analysis_id else None
        base_script = await session.get(ScriptVersion, base_script_id) if base_script_id else None
        if analysis_id and (analysis is None or analysis.chapter_id != chapter.id):
            raise RuntimeError("章节分析已被删除，本次剧本结果已作废")
        if base_script_id and (base_script is None or base_script.chapter_id != chapter.id):
            raise RuntimeError("参考剧本已被删除，本次剧本结果已作废")
        latest = await session.scalar(
            select(func.max(ScriptVersion.version)).where(ScriptVersion.chapter_id == chapter.id)
        )
        script = ScriptVersion(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            version=(latest or 0) + 1,
            title=parsed.title,
            content=parsed.content,
            status="reviewing",
            review_notes=parsed.review_notes,
            is_active=False,
        )
        session.add(script)
        await session.flush()
        file_content = (
            f"# {parsed.title}\n\n{parsed.content}\n\n## 审核关注点\n\n{parsed.review_notes or '无'}\n"
        )
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-AI剧本-v{script.version}.md",
                kind=ProjectFileKind.SCRIPT,
                mime_type="text/markdown",
                size_bytes=len(file_content.encode("utf-8")),
                content=file_content,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "script_version_id": script.id,
                    "analysis_id": analysis.id if analysis else None,
                    "base_script_version_id": base_script.id if base_script else None,
                    "source_task_id": task.id,
                    "source_hash": source_hash,
                },
            )
        )
        if parsed.continuity_summary:
            from app.services.ai_creation import save_script_memory

            await save_script_memory(session, script, parsed.continuity_summary)
        chapter.status = ChapterStatus.REVIEWING
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "script_version_id": script.id,
            "script_version": script.version,
            "analysis_id": analysis.id if analysis else None,
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"AI 剧本 v{script.version} 已进入审核",
            metadata={"script_version_id": script.id, "script_version": script.version},
        )
        await session.commit()
        await publish_task_event(task, event)


async def _complete_director_agent_task(
    task_id: str,
    *,
    result_payload: dict,
    message: str,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {"credit_refunded": False, **result_payload}
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=message,
            metadata={key: value for key, value in result_payload.items() if key.endswith("_id")},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_director_script_review_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        script = await session.get(ScriptVersion, str(task.request_payload.get("script_version_id") or ""))
        if chapter is None or script is None or script.chapter_id != chapter.id:
            raise RuntimeError("剧本审核目标已不存在")
        project = await session.get(Project, chapter.project_id)
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        prompt = (
            "你是独立的短剧导演审核子智能体。请依据已加载的导演手册、视觉手册和剧本规则，"
            "审核该剧本是否适合 AI 视频制作。重点检查事实一致性、叙事完整性、开场钩子、冲突递进、"
            "场次可拍摄性、人物动机、台词、资产连续性和单镜头可实现性。"
            "只有阻断项和主要问题均不存在时才能 approved=true。\n"
            '返回结构：{"approved":true,"summary":"审核结论",'
            '"findings":[{"severity":"blocking|major|minor","location":"场次或位置",'
            '"issue":"问题","suggestion":"可执行修复建议"}]}\n\n'
            f"目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}\n\n"
            f"章节原文：\n{chapter.original_content}\n\n"
            f"待审核剧本 v{script.version}《{script.title}》：\n{script.content}"
        )
    request = await runtime_request(task_id, prompt_code="script-review", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        review = DirectorReviewPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的剧本审核结构不符合要求") from exc
    await _complete_director_agent_task(
        task_id,
        result_payload={
            "approved": review.approved,
            "summary": review.summary,
            "review": review.model_dump(mode="json"),
            "runtime_manifest": result.manifest,
        },
        message="剧本审核通过" if review.approved else "剧本审核发现需要确认的问题",
    )


async def execute_director_script_repair_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        script = await session.get(ScriptVersion, str(task.request_payload.get("script_version_id") or ""))
        if chapter is None or script is None or script.chapter_id != chapter.id:
            raise RuntimeError("剧本修复目标已不存在")
        project = await session.get(Project, chapter.project_id)
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        review = json.dumps(task.request_payload.get("review") or {}, ensure_ascii=False)
        prompt = (
            "你是剧本修复子智能体。依据导演 Skills 和审核意见修复当前剧本，保持原文核心事实。"
            "partial 模式仅修改被指出的位置并保持其余内容；full 模式重新组织完整剧本。"
            "结果仍需适合 AI 视频生成，包含可拍摄场次、动作和完整台词。\n"
            '返回结构：{"title":"新版本标题","content":"完整剧本正文","review_notes":"本次修复摘要",'
            '"continuity_summary":"独立的连续性记忆，1500字以内，不得写入content"}\n\n'
            f"修复模式：{task.request_payload.get('repair_mode')}\n"
            f"用户意见：{task.request_payload.get('feedback') or '无补充'}\n"
            f"审核结果：{review}\n\n原剧本：\n{script.content}\n\n章节原文：\n{chapter.original_content}"
            f"\n\n目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}"
        )
    request = await runtime_request(task_id, prompt_code="script-repair", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        repaired = GeneratedScriptPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的修复剧本结构不符合要求") from exc

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        if not owns_running_task(task) or chapter is None:
            return
        latest = await session.scalar(
            select(func.max(ScriptVersion.version)).where(ScriptVersion.chapter_id == chapter.id)
        )
        script = ScriptVersion(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            version=(latest or 0) + 1,
            title=repaired.title,
            content=repaired.content,
            status="reviewing",
            review_notes=repaired.review_notes,
            is_active=False,
        )
        session.add(script)
        await session.flush()
        from app.services.ai_creation import save_script_memory

        await save_script_memory(session, script, repaired.continuity_summary)
        file_content = f"# {script.title}\n\n{script.content}\n"
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-AI剧本修复-v{script.version}.md",
                kind=ProjectFileKind.SCRIPT,
                mime_type="text/markdown",
                size_bytes=len(file_content.encode("utf-8")),
                content=file_content,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "script_version_id": script.id,
                    "source_task_id": task.id,
                    "repair_mode": task.request_payload.get("repair_mode"),
                },
            )
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "script_version_id": script.id,
            "script_version": script.version,
            "summary": repaired.review_notes or "剧本修复完成",
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"修复剧本 v{script.version} 已生成，正在重新审核",
            metadata={"script_version_id": script.id},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_director_storyboard_review_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        storyboard = await session.get(
            StoryboardVersion,
            str(task.request_payload.get("storyboard_version_id") or ""),
        )
        if storyboard is None:
            raise RuntimeError("分镜审核目标已不存在")
        project = await session.get(Project, storyboard.project_id)
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        duration_contract = storyboard_duration_contract(video_model)
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        image_model = await project_image_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        image_contract = image_model_execution_contract(
            image_model,
            selected_resolution=str(project.image_resolution or "1K") if project else "1K",
            aspect_ratio=str(project.aspect_ratio or "1:1") if project else "1:1",
        )
        prompt = (
            "你是独立的导演分镜审核子智能体。依据导演规划、分镜表和视觉风格 Skills，"
            "审核镜头是否完整覆盖生效剧本，并检查角色/场景/道具连续性、轴线、景别变化、光影氛围、"
            "动作节奏、运镜、转场、提示词可执行性和镜头时长。除校验时长是否合法外，还要检查时长是否足以"
            "容纳台词、动作、反应与运镜，以及是否无理由让大多数镜头使用最短档。"
            "阻断项或主要问题存在时 approved 必须为 false。\n"
            '返回结构：{"approved":true,"summary":"审核结论",'
            '"findings":[{"severity":"blocking|major|minor","location":"镜头编号",'
            '"issue":"问题","suggestion":"可执行修复建议"}]}\n\n'
            f"目标视频模型时长约束：{json.dumps(duration_contract, ensure_ascii=False)}\n"
            f"目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}\n"
            f"目标图片模型完整执行契约：{json.dumps(image_contract, ensure_ascii=False)}\n"
            f"分镜：{json.dumps(storyboard.content, ensure_ascii=False)}"
        )
    request = await runtime_request(task_id, prompt_code="storyboard-review", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        review = DirectorReviewPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的分镜审核结构不符合要求") from exc
    await _complete_director_agent_task(
        task_id,
        result_payload={
            "approved": review.approved,
            "summary": review.summary,
            "review": review.model_dump(mode="json"),
            "runtime_manifest": result.manifest,
        },
        message="分镜审核通过" if review.approved else "分镜审核发现需要确认的问题",
    )


async def execute_director_storyboard_repair_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        script = await session.get(ScriptVersion, str(task.request_payload.get("script_version_id") or ""))
        storyboard = await session.get(
            StoryboardVersion,
            str(task.request_payload.get("storyboard_version_id") or ""),
        )
        if chapter is None or script is None or storyboard is None:
            raise RuntimeError("分镜修复上下文已不存在")
        project = await session.get(Project, chapter.project_id)
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        assets = list(
            (await session.scalars(select(Asset).where(Asset.project_id == chapter.project_id))).all()
        )
        missing_ready_assets = await missing_storyboard_ready_asset_names(assets)
        if missing_ready_assets:
            raise RuntimeError(f"修复分镜前必须先完成资产图片：{'、'.join(missing_ready_assets[:8])}")
        asset_context = [
            {"name": asset.name, "description": asset.description, "media_url": asset.media_url}
            for asset in assets
            if asset.asset_type != AssetType.AUDIO
        ]
        duration_contract = storyboard_duration_contract(video_model)
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        image_model = await project_image_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        image_contract = image_model_execution_contract(
            image_model,
            selected_resolution=str(project.image_resolution or "1K") if project else "1K",
            aspect_ratio=str(project.aspect_ratio or "1:1") if project else "1:1",
        )
        schema_duration = float(duration_contract["schema_example_duration_seconds"])
        prompt = (
            "你是分镜修复子智能体。依据导演 Skills、审核意见和已完成资产修复分镜。"
            "partial 模式只调整问题镜头，"
            "但返回完整分镜；full 模式重新制作完整分镜。asset_names 只能使用资产清单中的精确名称。"
            "duration_seconds 必须从目标视频模型支持时长中选择，并按时长档位与叙事负载重新判断；"
            "不得生成列表外时长，也不得无理由把多数镜头压到最短档。\n"
            f'返回结构：{{"shots":[{{"title":"镜头标题","shot_type":"中景","duration_seconds":{schema_duration:g},'
            '"scene_description":"场景","action_description":"动作与运镜","dialogue":"台词",'
            '"image_prompt":"首帧提示词","video_prompt":"","asset_names":["资产名"]}]}\n'
            "分镜修复阶段不生成最终视频模型提示词，video_prompt 必须返回空字符串。\n"
            "结构示例中的时长只表示数字字段类型，不是默认时长。\n\n"
            f"修复模式：{task.request_payload.get('repair_mode')}\n"
            "目标视频模型时长约束："
            f"{json.dumps(duration_contract, ensure_ascii=False)}\n"
            f"目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}\n"
            f"目标图片模型完整执行契约：{json.dumps(image_contract, ensure_ascii=False)}\n"
            f"用户意见：{task.request_payload.get('feedback') or '无补充'}\n"
            f"审核结果：{json.dumps(task.request_payload.get('review') or {}, ensure_ascii=False)}\n"
            f"资产清单：{json.dumps(asset_context, ensure_ascii=False)}\n"
            f"剧本：\n{script.content}\n原分镜：{json.dumps(storyboard.content, ensure_ascii=False)}"
        )
    request = await runtime_request(task_id, prompt_code="storyboard-repair", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        repaired = StoryboardGenerationPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的修复分镜结构不符合要求") from exc
    repaired.shots = [
        shot.model_copy(
            update={
                "duration_seconds": normalize_storyboard_duration_for_model(
                    shot.duration_seconds,
                    video_model,
                ),
                "video_prompt": "",
            }
        )
        for shot in repaired.shots
    ]

    async with SessionLocal() as session:
        from app.api.routes.storyboards import create_storyboard
        from app.domain.schemas import StoryboardShotCreate

        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        script = await session.get(ScriptVersion, str(task.request_payload.get("script_version_id") or ""))
        user = await session.get(User, task.user_id) if task else None
        if not owns_running_task(task) or chapter is None or script is None or user is None:
            return
        assets = list(
            (await session.scalars(select(Asset).where(Asset.project_id == chapter.project_id))).all()
        )
        by_name = {asset.name: asset for asset in assets}
        unknown = sorted(
            {name for shot in repaired.shots for name in shot.asset_names if name not in by_name}
        )
        if unknown:
            raise RuntimeError(f"修复分镜引用了未知资产：{'、'.join(unknown[:5])}")
        shot_payloads = [
            StoryboardShotCreate(
                **shot.model_dump(exclude={"asset_names"}),
                asset_ids=[by_name[name].id for name in shot.asset_names],
                reference_image_url=next(
                    (by_name[name].media_url for name in shot.asset_names if by_name[name].media_url),
                    None,
                ),
            )
            for shot in repaired.shots
        ]
        version, records = await create_storyboard(
            session,
            user=user,
            chapter=chapter,
            script=script,
            shots=shot_payloads,
        )
        serialized = json.dumps({"shots": version.content}, ensure_ascii=False, indent=2)
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-分镜修复-v{version.version}.json",
                kind=ProjectFileKind.STORYBOARD,
                mime_type="application/json",
                size_bytes=len(serialized.encode("utf-8")),
                content=serialized,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "storyboard_version_id": version.id,
                    "source_task_id": task.id,
                },
            )
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "storyboard_version_id": version.id,
            "shot_ids": [shot.id for shot in records],
            "shot_count": len(records),
            "summary": f"已修复 {len(records)} 个镜头",
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"修复分镜 v{version.version} 已生成，正在重新审核",
            metadata={"storyboard_version_id": version.id},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_asset_extraction_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        script_id = str(task.request_payload.get("script_version_id") or "")
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        if chapter is None or script is None or script.chapter_id != chapter.id:
            raise RuntimeError("资产提取所用章节或剧本已不存在")
        prompt = (
            "分析以下生效剧本，提取所有人物、场景、道具及人物/场景/道具的衍生形态。"
            "基础资产 parent_name 必须为 null；衍生资产的 parent_name 必须精确填写其基础资产名称。"
            "asset_type 只能是 character、scene、prop。不要生成图片提示词。\n"
            '返回结构：{"assets":[{"asset_type":"character","name":"名称",'
            '"description":"可核验的形象与叙事说明","parent_name":null}]}\n\n'
            f"章节：{chapter.title}\n剧本版本：v{script.version}《{script.title}》\n剧本正文：\n{script.content}"
        )
    request = await runtime_request(task_id, prompt_code="script-asset-extraction", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = AssetExtractionPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的资产结构不符合要求") from exc
    await record_progress(task_id, 65, f"已识别 {len(parsed.assets)} 个资产，正在写入项目资产库")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        if not owns_running_task(task) or chapter is None or script is None:
            return
        if chapter.active_script_version_id != script.id:
            raise RuntimeError("生效剧本已切换，本次资产提取结果已作废")
        latest = await session.scalar(
            select(func.max(AssetExtraction.version)).where(AssetExtraction.chapter_id == chapter.id)
        )
        await session.execute(
            update(AssetExtraction)
            .where(AssetExtraction.chapter_id == chapter.id, AssetExtraction.is_active.is_(True))
            .values(is_active=False, invalidated_reason="已生成新的资产提取版本")
        )
        extraction = AssetExtraction(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            script_version_id=script.id,
            version=(latest or 0) + 1,
            is_active=True,
        )
        session.add(extraction)
        await session.flush()
        roots: dict[str, Asset] = {}
        created: list[Asset] = []
        for item in parsed.assets:
            if item.parent_name:
                continue
            asset = Asset(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                scope=AssetScope.PROJECT,
                asset_type=AssetType(item.asset_type),
                name=item.name,
                description=item.description,
                status=AssetStatus.EXTRACTED,
                asset_metadata={"source_task_id": task.id, "extraction_id": extraction.id},
            )
            session.add(asset)
            await session.flush()
            await snapshot_asset_revision(
                session,
                asset,
                change_type="ai_extraction",
                source_task_id=task.id,
            )
            roots[item.name] = asset
            created.append(asset)
        for item in parsed.assets:
            if not item.parent_name:
                continue
            parent = roots.get(item.parent_name)
            if parent is None:
                raise RuntimeError(f"衍生资产“{item.name}”缺少基础资产“{item.parent_name}”")
            if parent.asset_type.value != item.asset_type:
                raise RuntimeError(f"衍生资产“{item.name}”与基础资产“{item.parent_name}”类型不一致")
            asset = Asset(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                scope=AssetScope.PROJECT,
                asset_type=AssetType(item.asset_type),
                parent_asset_id=parent.id,
                name=item.name,
                description=item.description,
                status=AssetStatus.EXTRACTED,
                asset_metadata={"source_task_id": task.id, "extraction_id": extraction.id},
            )
            session.add(asset)
            await session.flush()
            await snapshot_asset_revision(
                session,
                asset,
                change_type="ai_extraction",
                source_task_id=task.id,
            )
            created.append(asset)
        for asset in created:
            session.add(AssetExtractionItem(extraction_id=extraction.id, asset_id=asset.id))
        serialized = json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False, indent=2)
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-资产提取-v{extraction.version}.json",
                kind=ProjectFileKind.ASSET,
                mime_type="application/json",
                size_bytes=len(serialized.encode("utf-8")),
                content=serialized,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "script_version_id": script.id,
                    "extraction_id": extraction.id,
                    "source_task_id": task.id,
                },
            )
        )
        chapter.status = ChapterStatus.ASSETS
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "extraction_id": extraction.id,
            "asset_ids": [asset.id for asset in created],
            "asset_count": len(created),
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"已提取 {len(created)} 个塑造资产",
            metadata={"extraction_id": extraction.id, "asset_count": len(created)},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_asset_prompt_task(
    task_id: str,
    runtime_factory: RuntimeFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        asset_ids = [str(item) for item in task.request_payload.get("asset_ids") or []]
        assets = list((await session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))).all())
        if len(assets) != len(asset_ids):
            raise RuntimeError("部分待生成提示词的资产已不存在")
        project = await session.get(Project, task.project_id)
        image_model = await project_image_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        image_contract = image_model_execution_contract(
            image_model,
            selected_resolution=str(project.image_resolution or "1K") if project else "1K",
            aspect_ratio=str(project.aspect_ratio or "1:1") if project else "1:1",
        )
        asset_rows = [
            {
                "asset_id": asset.id,
                "asset_type": asset.asset_type.value,
                "name": asset.name,
                "description": asset.description,
                "is_derivative": asset.parent_asset_id is not None,
            }
            for asset in assets
        ]
        prompt = (
            "为下列资产分别生成可直接提交给图片模型的中文提示词。提示词必须描述主体、构图、材质、"
            "光线、背景和一致性约束，不得包含资产名称之外的文字、水印或 UI。"
            "所有资产图必须严格使用目标图片模型执行契约中的 selected_aspect_ratio，"
            "不得因为人物、道具或衍生资产类型改成方图或其它画幅。\n"
            '返回结构：{"assets":[{"asset_id":"原 ID","generation_prompt":"完整提示词"}]}\n\n'
            f"目标图片模型完整执行契约：{json.dumps(image_contract, ensure_ascii=False)}\n"
            f"资产数据：\n{json.dumps(asset_rows, ensure_ascii=False)}"
        )
    request = await runtime_request(task_id, prompt_code="asset-prompt-generation", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = PromptGenerationPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的资产提示词结构不符合要求") from exc
    prompts = {item.asset_id: item.generation_prompt.strip() for item in parsed.assets}
    if set(prompts) != set(asset_ids):
        raise RuntimeError("AI 返回的资产提示词与所选资产不匹配")
    await record_progress(task_id, 70, "提示词已生成，正在同步资产版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        assets = list((await session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))).all())
        if not owns_running_task(task) or len(assets) != len(asset_ids):
            return
        expected_versions = task.request_payload.get("asset_versions") or {}
        if any(asset.version != int(expected_versions.get(asset.id, -1)) for asset in assets):
            raise RuntimeError("资产已被编辑，本次提示词结果未覆盖用户的新版本")
        for asset in assets:
            asset.generation_prompt = prompts[asset.id]
            asset.status = AssetStatus.PROMPT_READY
            asset.version += 1
            await snapshot_asset_revision(
                session,
                asset,
                change_type="ai_prompt_generation",
                source_task_id=task.id,
            )
        image_dispatches: list[tuple[AITask, TaskEvent, Asset]] = []
        image_queue_error: str | None = None
        if task.request_payload.get("auto_queue_images_after_prompt"):
            try:
                from app.services.asset_tasks import queue_asset_image_generation_tasks

                project = await session.get(Project, task.project_id)
                user = await session.get(User, task.user_id)
                if project is None or user is None:
                    raise RuntimeError("自动生图所需项目或用户不存在")
                image_dispatches = await queue_asset_image_generation_tasks(
                    session,
                    user=user,
                    project=project,
                    assets=assets,
                    only_missing_image=True,
                )
            except Exception as exc:
                image_queue_error = _safe_error_message(exc)
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "asset_ids": asset_ids,
            "asset_count": len(assets),
            "auto_queue_images_after_prompt": bool(
                task.request_payload.get("auto_queue_images_after_prompt")
            ),
            "queued_image_task_ids": [item[0].id for item in image_dispatches],
            "image_queue_error": image_queue_error,
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"已为 {len(assets)} 个资产生成生图提示词",
            metadata={"asset_count": len(assets)},
        )
        await session.commit()
        await publish_task_event(task, event)
        for image_task, image_event, _asset in image_dispatches:
            await enqueue_task(image_task.id)
            await publish_task_event(image_task, image_event)


async def execute_asset_image_task(task_id: str, gateway_factory: GatewayFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id or not task.model_id:
            return
        asset_id = str(task.request_payload.get("asset_id") or "")
        asset = await session.get(Asset, asset_id)
        model = await session.get(AIModel, task.model_id)
        project = await session.get(Project, task.project_id)
        if (
            asset is None
            or asset.project_id != task.project_id
            or asset.tenant_id != task.tenant_id
            or not asset.generation_prompt.strip()
        ):
            raise RuntimeError("待生成图片的资产不可用")
        if project is None or project.tenant_id != task.tenant_id:
            raise RuntimeError("资产生图所需项目不可用")
        if asset.asset_type == AssetType.AUDIO:
            raise RuntimeError("音频资产不能执行生图任务")
        if model is None or model.model_type != ModelType.IMAGE or not model.enabled:
            raise RuntimeError("资产生图模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("资产生图平台不可用")
        aspect_ratio = str(project.aspect_ratio or task.request_payload.get("aspect_ratio") or "16:9")
        supported_aspect_ratios = model.capabilities.get("aspect_ratios")
        if (
            isinstance(supported_aspect_ratios, list)
            and supported_aspect_ratios
            and aspect_ratio not in supported_aspect_ratios
        ):
            raise RuntimeError(
                f"项目画幅 {aspect_ratio} 不受当前图片模型支持；"
                f"该模型支持：{', '.join(str(item) for item in supported_aspect_ratios)}"
            )
        request = ImageGenerationRequest(
            model=model.model_id,
            prompt=asset.generation_prompt,
            resolution=str(project.image_resolution or task.request_payload.get("image_resolution") or "1K"),
            aspect_ratio=aspect_ratio,
            capabilities=model.capabilities,
            idempotency_key=task.idempotency_key or task.id,
        )
        gateway = gateway_factory(provider)
    image_data = await gateway.generate_image(request)
    await record_progress(task_id, 78, "图片已生成，正在校验并写入资产库")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        asset = await session.get(Asset, asset_id)
        if not owns_running_task(task) or asset is None or not task.project_id:
            return
        expected_version = int(task.request_payload.get("asset_version") or -1)
        if asset.version != expected_version:
            raise RuntimeError("资产已被编辑，本次图片结果未覆盖用户的新版本")
        _local_url, stored_path = await run_in_threadpool(
            save_asset_image,
            image_data,
            uploads_root=get_settings().uploads_root,
            tenant_id=task.tenant_id,
            project_id=task.project_id,
            asset_id=asset.id,
        )
        try:
            storage_key, media_url = await persist_media_file(stored_path, "image/webp")
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        asset.media_url = media_url
        asset.status = AssetStatus.READY
        asset.version += 1
        await snapshot_asset_revision(
            session,
            asset,
            change_type="image_generation",
            source_task_id=task.id,
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "asset_id": asset.id,
            "media_url": media_url,
            "provider_id": provider.id,
            "model_id": task.model_id,
            "image_resolution": project.image_resolution,
            "aspect_ratio": project.aspect_ratio,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"资产“{asset.name}”图片已生成",
            metadata={"asset_id": asset.id, "media_url": media_url},
        )
        try:
            await session.commit()
        except Exception:
            await cleanup_media(storage_key, stored_path)
            await session.rollback()
            raise
        await publish_task_event(task, event)


async def execute_storyboard_task(task_id: str, runtime_factory: RuntimeFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        script_id = str(task.request_payload.get("script_version_id") or "")
        extraction_id = str(task.request_payload.get("asset_extraction_id") or "")
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        extraction = await session.get(AssetExtraction, extraction_id)
        project = await session.get(Project, chapter.project_id) if chapter else None
        video_model = await project_video_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        if (
            chapter is None
            or script is None
            or extraction is None
            or script.chapter_id != chapter.id
            or extraction.chapter_id != chapter.id
            or extraction.script_version_id != script.id
        ):
            raise RuntimeError("分镜所用剧本或资产提取版本已不存在")
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                    .where(AssetExtractionItem.extraction_id == extraction.id)
                    .order_by(Asset.asset_type, Asset.name)
                )
            ).all()
        )
        missing_ready_assets = await missing_storyboard_ready_asset_names(assets)
        if missing_ready_assets:
            raise RuntimeError(f"生成分镜前必须先完成资产图片：{'、'.join(missing_ready_assets[:8])}")
        asset_context = [
            {
                "name": asset.name,
                "type": asset.asset_type.value,
                "description": asset.description,
                "image_prompt": asset.generation_prompt,
                "reference_image_url": asset.media_url,
            }
            for asset in assets
        ]
        duration_contract = storyboard_duration_contract(video_model)
        model_contract = video_model_execution_contract(
            video_model,
            requested_resolution=str(project.video_resolution or "") if project else "",
            aspect_ratio=str(project.aspect_ratio or "") if project else "",
        )
        image_model = await project_image_model_for_storyboard(
            session,
            project=project,
            tenant_id=task.tenant_id,
        )
        image_contract = image_model_execution_contract(
            image_model,
            selected_resolution=str(project.image_resolution or "1K") if project else "1K",
            aspect_ratio=str(project.aspect_ratio or "1:1") if project else "1:1",
        )
        schema_duration = float(duration_contract["schema_example_duration_seconds"])
        prompt = (
            "将以下生效剧本拆解为可独立拍摄和生成视频的连续分镜。每个镜头必须包含镜头标题、"
            "景别、符合目标视频模型能力的时长、场景、动作、台词和完整首帧图片提示词。"
            "asset_names 只能填写资产清单中完全一致的名称，没有引用时返回空数组。"
            "duration_seconds 必须从目标视频模型支持时长中选择，并根据台词、动作、反应、停顿和运镜的"
            "真实负载选择短、中、长档；不得无理由让多数镜头使用最短档。"
            "分镜需保持角色、场景、道具连续性，并给出明确运镜、主体动作和环境动态。\n"
            '返回结构：{"shots":[{"title":"镜头标题","shot_type":"中景",'
            f'"duration_seconds":{schema_duration:g},"scene_description":"场景",'
            '"action_description":"动作与运镜","dialogue":"台词或空字符串",'
            '"image_prompt":"首帧图片提示词","video_prompt":"",'
            '"asset_names":["资产名称"]}]}\n'
            "分镜阶段不得提前生成供应商专用视频提示词，video_prompt 必须返回空字符串；"
            "审核通过后平台会启动独立的视频提示词任务。\n"
            "结构示例中的时长只表示数字字段类型，不是默认时长。\n\n"
            "目标视频模型时长约束："
            f"{json.dumps(duration_contract, ensure_ascii=False)}\n"
            f"目标视频模型完整执行契约：{json.dumps(model_contract, ensure_ascii=False)}\n"
            f"目标图片模型完整执行契约：{json.dumps(image_contract, ensure_ascii=False)}\n"
            f"章节：{chapter.title}\n剧本：v{script.version}《{script.title}》\n"
            f"资产清单：{json.dumps(asset_context, ensure_ascii=False)}\n\n剧本正文：\n{script.content}"
        )
    request = await runtime_request(task_id, prompt_code="storyboard-generation", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = StoryboardGenerationPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的分镜结构不符合要求") from exc
    parsed.shots = [
        shot.model_copy(
            update={
                "duration_seconds": normalize_storyboard_duration_for_model(
                    shot.duration_seconds,
                    video_model,
                ),
                "video_prompt": "",
            }
        )
        for shot in parsed.shots
    ]
    asset_by_name = {asset.name: asset for asset in assets}
    unknown_names = sorted(
        {name for shot in parsed.shots for name in shot.asset_names if name not in asset_by_name}
    )
    if unknown_names:
        raise RuntimeError(f"AI 分镜引用了未知资产：{'、'.join(unknown_names[:5])}")
    await record_progress(task_id, 72, f"已规划 {len(parsed.shots)} 个镜头，正在建立分镜版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        extraction = await session.get(AssetExtraction, extraction_id)
        if not owns_running_task(task) or chapter is None or script is None:
            return
        if (
            chapter.active_script_version_id != script.id
            or extraction is None
            or not extraction.is_active
            or extraction.script_version_id != script.id
        ):
            raise RuntimeError("生效剧本或资产提取版本已切换，本次分镜结果已作废")
        current_assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                    .where(AssetExtractionItem.extraction_id == extraction.id)
                )
            ).all()
        )
        missing_ready_assets = await missing_storyboard_ready_asset_names(current_assets)
        if missing_ready_assets:
            raise RuntimeError(f"资产图片在分镜写入前发生变化，未完成：{'、'.join(missing_ready_assets[:8])}")
        current_by_name = {asset.name: asset for asset in current_assets}
        latest = await session.scalar(
            select(func.max(StoryboardVersion.version)).where(StoryboardVersion.chapter_id == chapter.id)
        )
        dialogue_reason = "已生成新的分镜版本，需要重新提取台词"
        affected_dialogues = select(DialogueVersion.id).where(
            DialogueVersion.chapter_id == chapter.id,
            DialogueVersion.storyboard_version_id.is_not(None),
            DialogueVersion.is_active.is_(True),
        )
        await session.execute(
            update(AudioClip)
            .where(AudioClip.dialogue_version_id.in_(affected_dialogues), AudioClip.is_active.is_(True))
            .values(is_active=False, invalidated_reason=dialogue_reason)
        )
        await session.execute(
            update(DialogueVersion)
            .where(
                DialogueVersion.chapter_id == chapter.id,
                DialogueVersion.storyboard_version_id.is_not(None),
                DialogueVersion.is_active.is_(True),
            )
            .values(is_active=False, invalidated_reason=dialogue_reason)
        )
        await session.execute(
            update(StoryboardVersion)
            .where(StoryboardVersion.chapter_id == chapter.id, StoryboardVersion.is_active.is_(True))
            .values(is_active=False, invalidated_reason="已生成新的分镜版本")
        )
        content: list[dict] = []
        await invalidate_compositions(
            session,
            chapter_id=chapter.id,
            reason="AI 已生成新的分镜版本",
        )
        storyboard = StoryboardVersion(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            script_version_id=script.id,
            version=(latest or 0) + 1,
            content=content,
            is_active=True,
        )
        session.add(storyboard)
        await session.flush()
        shot_ids: list[str] = []
        for index, payload in enumerate(parsed.shots, start=1):
            asset_ids = [current_by_name[name].id for name in payload.asset_names]
            reference_url = next(
                (
                    current_by_name[name].media_url
                    for name in payload.asset_names
                    if current_by_name[name].media_url
                ),
                None,
            )
            values = payload.model_dump(exclude={"asset_names"})
            values.update(asset_ids=asset_ids, reference_image_url=reference_url)
            shot = StoryboardShot(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                chapter_id=chapter.id,
                storyboard_version_id=storyboard.id,
                order_index=index,
                **values,
            )
            session.add(shot)
            await session.flush()
            shot_ids.append(shot.id)
            content.append({"order_index": index, **payload.model_dump(mode="json"), "asset_ids": asset_ids})
        storyboard.content = content
        serialized = json.dumps({"shots": content}, ensure_ascii=False, indent=2)
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-分镜表-v{storyboard.version}.json",
                kind=ProjectFileKind.STORYBOARD,
                mime_type="application/json",
                size_bytes=len(serialized.encode("utf-8")),
                content=serialized,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "script_version_id": script.id,
                    "storyboard_version_id": storyboard.id,
                    "source_task_id": task.id,
                },
            )
        )
        chapter.status = ChapterStatus.STORYBOARD
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "storyboard_version_id": storyboard.id,
            "shot_ids": shot_ids,
            "shot_count": len(shot_ids),
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"分镜 v{storyboard.version} 已生成，共 {len(shot_ids)} 个镜头",
            metadata={"storyboard_version_id": storyboard.id, "shot_count": len(shot_ids)},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_shot_video_prompt_task(task_id: str, runtime_factory: RuntimeFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        storyboard_id = str(task.request_payload.get("storyboard_version_id") or "")
        shot_ids = [str(item) for item in task.request_payload.get("shot_ids") or []]
        shot_versions = {
            str(key): int(value) for key, value in (task.request_payload.get("shot_versions") or {}).items()
        }
        chapter = await session.get(Chapter, chapter_id)
        storyboard = await session.get(StoryboardVersion, storyboard_id)
        shots = list(
            (
                await session.scalars(
                    select(StoryboardShot)
                    .where(StoryboardShot.id.in_(shot_ids))
                    .order_by(StoryboardShot.order_index)
                )
            ).all()
        )
        if chapter is None or storyboard is None or storyboard.chapter_id != chapter.id:
            raise RuntimeError("视频提示词任务所用章节或分镜版本已不存在")
        if not storyboard.is_active:
            raise RuntimeError("只能为当前生效分镜生成视频提示词")
        if len(shots) != len(shot_ids):
            raise RuntimeError("部分待生成提示词的镜头已不存在")
        if any(shot.storyboard_version_id != storyboard.id for shot in shots):
            raise RuntimeError("待生成提示词的镜头不属于当前分镜")
        if any(shot.version != shot_versions.get(shot.id, -1) for shot in shots):
            raise RuntimeError("镜头已被编辑，本次视频提示词任务已失效")
        assets_by_id = await load_shot_assets_with_parents(
            session,
            shots=shots,
            tenant_id=task.tenant_id,
        )
        raw_target_video_model = task.request_payload.get("target_video_model")
        target_video_model = dict(raw_target_video_model) if isinstance(raw_target_video_model, dict) else {}
        raw_capabilities = target_video_model.get("capabilities")
        video_capabilities = dict(raw_capabilities) if isinstance(raw_capabilities, dict) else {}
        audio_reference_map_by_shot = {
            shot.id: build_shot_audio_references(shot, assets_by_id, video_capabilities) for shot in shots
        }
        has_audio_references = any(audio_reference_map_by_shot.values())
        audio_enabled = video_audio_enabled(
            video_capabilities,
            bool(task.request_payload.get("audio_enabled", False)) or has_audio_references,
        )
        execution_contract = {
            **target_video_model,
            **video_model_execution_contract(
                None,
                requested_resolution=str(task.request_payload.get("video_resolution") or ""),
                aspect_ratio=str(task.request_payload.get("aspect_ratio") or ""),
                audio_enabled=audio_enabled,
            ),
            "model_id": target_video_model.get("model_id"),
            "model_name": target_video_model.get("model_name"),
        }
        execution_contract.update(
            {
                "generation_modes": video_capabilities.get("generation_modes", ["text_to_video"]),
                "supported_durations_seconds": supported_video_durations(video_capabilities),
                "duration_resolution_map": video_capabilities.get("duration_resolution_map", []),
                "supported_aspect_ratios": video_capabilities.get("aspect_ratios", []),
                "reference_limits": video_capabilities.get("reference_limits", {}),
                "audio_policy": video_capabilities.get("audio_policy", "optional"),
                "audio_enabled_for_this_task": audio_enabled,
                "prompt_languages": video_capabilities.get("prompt_languages", ["zh-CN"]),
                "preferred_prompt_language": video_capabilities.get("preferred_prompt_language"),
                "negative_prompt_supported": bool(video_capabilities.get("negative_prompt_supported")),
            }
        )
        shot_rows = []
        reference_map_by_shot: dict[str, list[dict[str, str]]] = {}
        for shot in shots:
            image_references = limit_video_reference_media(
                build_shot_image_references(shot, assets_by_id),
                video_capabilities,
            )
            audio_references = audio_reference_map_by_shot[shot.id]
            reference_map_by_shot[shot.id] = image_references
            generation_mode = resolve_video_generation_mode(
                video_capabilities,
                image_reference_count=len(image_references),
            )
            shot_rows.append(
                {
                    "shot_id": shot.id,
                    "shot_order_index": shot.order_index,
                    "order_index": shot.order_index,
                    "title": shot.title,
                    "shot_type": shot.shot_type,
                    "duration_seconds": float(shot.duration_seconds),
                    "scene_description": shot.scene_description,
                    "action_description": shot.action_description,
                    "dialogue": shot.dialogue if audio_enabled else "",
                    "dialogue_locks": dialogue_lock_lines(shot.dialogue) if audio_enabled else [],
                    "silent_performance": bool(shot.dialogue) and not audio_enabled,
                    "audio_enabled": audio_enabled,
                    "resolved_generation_mode": generation_mode,
                    "image_prompt": shot.image_prompt,
                    "current_video_prompt": shot.video_prompt,
                    "reference_image_url": shot.reference_image_url,
                    "reference_map": [
                        {
                            "token": reference["token"],
                            "role": reference["role"],
                            "asset_name": reference.get("asset_name", ""),
                            "asset_names": reference.get("asset_names", ""),
                            "asset_type": reference.get("asset_type", ""),
                            "description": reference.get("asset_description", ""),
                            "url": reference["url"],
                        }
                        for reference in image_references
                    ],
                    "reference_audio_map": [
                        {
                            "token": reference["token"],
                            "role": reference["role"],
                            "character_name": reference.get("character_name", ""),
                            "source_character_name": reference.get("source_character_name", ""),
                            "mime_type": reference.get("mime_type", ""),
                            "url": reference["url"],
                        }
                        for reference in audio_references
                    ],
                    "assets": [
                        {
                            "name": assets_by_id[asset_id].name,
                            "asset_type": assets_by_id[asset_id].asset_type.value,
                            "description": assets_by_id[asset_id].description,
                            "generation_prompt": assets_by_id[asset_id].generation_prompt,
                            "has_image": bool(assets_by_id[asset_id].media_url),
                            "media_url": assets_by_id[asset_id].media_url,
                            "reference_token": next(
                                (
                                    reference["token"]
                                    for reference in image_references
                                    if reference.get("asset_id") == asset_id
                                    or asset_id in reference.get("asset_ids", "").split(",")
                                ),
                                "",
                            ),
                        }
                        for asset_id in shot.asset_ids
                        if asset_id in assets_by_id
                    ],
                }
            )
        protocol = video_prompt_protocol(target_video_model)
        preferred_language = str(protocol["preferred_prompt_language"])
        protocol_appendix = (
            internal_system_prompt_content("video-prompt-generation-h3.md")
            if protocol["name"] == "minimax_h3"
            else ""
        )
        language_instruction = (
            "当前目标是 MiniMax H3，按 H3 专用协议使用英文结构和英文画面描述；"
            "但 dialogue_locks 中的中文台词必须逐字保留。"
            if protocol["name"] == "minimax_h3"
            else (
                "当前目标不是 MiniMax H3，不得使用 H3 的三段式、六段式、英文对齐首行或字段名。"
                f"最终视频提示词默认使用 {preferred_language}；台词始终逐字保留原语言。"
            )
        )
        audio_instruction = (
            "本任务明确启用音频；只允许使用 dialogue_locks 中的原文台词，不得新增说话人、"
            "新增台词、翻译台词、虚构语言或叠加无来源人声。reference_audio_map 中每个 "
            "<Audio N> 只绑定其 character_name，只参考声音身份，不复制样例台词。"
            if audio_enabled
            else (
                "本任务明确关闭音频。dialogue_locks 仅用于理解表演，不得写入可听内容；"
                "最终提示词必须要求完全无声，禁止对白、旁白、歌唱、配乐、环境声、音效和虚构语言，"
                "人物不得开口说话。"
            )
        )
        prompt = (
            "下面是平台传入的视频提示词生成任务快照。请严格使用系统提示词模板"
            "《视频提示词生成》的规则，以及当前项目所选视觉手册/导演手册中与视频提示词相关的 skill。"
            "你要读取每个镜头的首帧、动作、台词、资产图片参考和目标视频模型能力，生成最终可执行的视频提示词。"
            "不得只复述分镜，不得编造未提供的资产或引用。"
            "每个镜头的 reference_map 是平台实际提交给视频供应商的图片顺序，"
            "提示词中必须显式使用其中的 <Picture N> 编号："
            "首帧图用于锚定 0.00 秒，资产参考图用于保持人物、场景、道具外观；"
            "如果 reference_map 为空才允许纯文本视频提示词。"
            "每个镜头的 reference_audio_map 是平台实际提交给视频供应商的人物参考音频顺序；"
            "必须显式写出每个 <Audio N> 与 character_name 的唯一绑定关系。"
            "每个镜头的 dialogue_locks 是必须逐字保留的对白、旁白、歌词或画内文字；"
            f"{language_instruction}"
            f"{audio_instruction}"
            '请优先按系统模板输出契约返回：{"prompts":[{"shot_order_index":1,"mode":"image_to_video",'
            '"prompt":"完整视频提示词","negative_prompt":"","reference_asset_names":["资产名"]}]}。'
            '如无法使用该结构，也兼容返回：{"shots":[{"shot_id":"原 ID","video_prompt":"完整视频提示词"}]}。'
            "完整回复只能是合法 JSON，不要 Markdown、解释或代码围栏。\n\n"
            f"项目规格：分辨率 {task.request_payload.get('video_resolution') or '1080p'}，"
            f"画幅 {task.request_payload.get('aspect_ratio') or '16:9'}。\n"
            f"提示词协议：{json.dumps(protocol, ensure_ascii=False)}\n"
            f"目标视频模型完整执行契约：\n{json.dumps(execution_contract, ensure_ascii=False)}\n"
            f"镜头数据：\n{json.dumps(shot_rows, ensure_ascii=False)}"
        )
    request = await runtime_request(
        task_id,
        prompt_code="video-prompt-generation",
        prompt=prompt,
        prompt_appendix=protocol_appendix,
    )
    result = await runtime_factory().run(request)
    raw_response = parse_json_object(result.final_response)
    try:
        parsed = ShotVideoPromptGenerationPayload.model_validate(raw_response)
        prompts = {item.shot_id: item.video_prompt.strip() for item in parsed.shots}
    except ValidationError:
        try:
            parsed_from_template = VideoPromptTemplateGenerationPayload.model_validate(raw_response)
        except ValidationError as exc:
            raise RuntimeError("AI 返回的视频提示词结构不符合要求") from exc
        shot_id_by_order = {shot.order_index: shot.id for shot in shots}
        prompts = {
            shot_id_by_order[item.shot_order_index]: item.prompt.strip()
            for item in parsed_from_template.prompts
            if item.shot_order_index in shot_id_by_order
        }
    prompts = {
        shot_id: enforce_video_audio_policy(
            ensure_video_prompt_audio_reference_locks(
                ensure_video_prompt_reference_locks(
                    prompt,
                    reference_map_by_shot.get(shot_id, []),
                    language=preferred_language,
                ),
                audio_reference_map_by_shot.get(shot_id, []),
                language=preferred_language,
            ),
            next((shot.dialogue for shot in shots if shot.id == shot_id), ""),
            audio_enabled=audio_enabled,
            protocol_name=str(protocol["name"]),
            language=preferred_language,
        )
        for shot_id, prompt in prompts.items()
    }
    if any(not prompt for prompt in prompts.values()):
        raise RuntimeError("AI 返回的视频提示词结构不符合要求")
    if set(prompts) != set(shot_ids):
        raise RuntimeError("AI 返回的视频提示词与所选镜头不匹配")
    await record_progress(task_id, 72, "视频提示词已生成，正在同步镜头版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        storyboard = await session.get(StoryboardVersion, storyboard_id)
        shots = list(
            (
                await session.scalars(
                    select(StoryboardShot)
                    .where(StoryboardShot.id.in_(shot_ids))
                    .order_by(StoryboardShot.order_index)
                )
            ).all()
        )
        if not owns_running_task(task) or storyboard is None or len(shots) != len(shot_ids):
            return
        if any(shot.version != shot_versions.get(shot.id, -1) for shot in shots):
            raise RuntimeError("镜头已被编辑，本次视频提示词结果未覆盖用户的新版本")
        changed_shot_ids: list[str] = []
        for shot in shots:
            next_prompt = prompts[shot.id]
            if shot.video_prompt != next_prompt:
                shot.video_prompt = next_prompt
                shot.version += 1
                changed_shot_ids.append(shot.id)
        if changed_shot_ids:
            await session.execute(
                update(VideoClip)
                .where(VideoClip.shot_id.in_(changed_shot_ids), VideoClip.is_active.is_(True))
                .values(is_active=False, invalidated_reason="镜头视频提示词已更新")
            )
            await invalidate_compositions(
                session,
                chapter_id=chapter_id,
                reason="镜头视频提示词已更新",
            )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "shot_ids": shot_ids,
            "shot_count": len(shots),
            "changed_shot_ids": changed_shot_ids,
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"已生成 {len(shots)} 个镜头的视频提示词",
            metadata={"shot_count": len(shots), "changed_shot_ids": changed_shot_ids},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_shot_video_task(task_id: str, gateway_factory: GatewayFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.model_id:
            return
        shot_id = str(task.request_payload.get("shot_id") or "")
        storyboard_id = str(task.request_payload.get("storyboard_version_id") or "")
        clip_id = str(task.request_payload.get("video_clip_id") or "")
        shot = await session.get(StoryboardShot, shot_id)
        storyboard = await session.get(StoryboardVersion, storyboard_id)
        clip = await session.get(VideoClip, clip_id)
        model = await session.get(AIModel, task.model_id)
        if shot is None or storyboard is None or clip is None:
            raise RuntimeError("视频任务所用镜头或分镜版本已不存在")
        expected_shot_version = int(task.request_payload.get("shot_version") or -1)
        if (
            shot.version != expected_shot_version
            or shot.storyboard_version_id != storyboard.id
            or not storyboard.is_active
        ):
            raise RuntimeError("镜头或分镜已更新，本次视频任务已失效")
        if model is None or model.model_type != ModelType.VIDEO or not model.enabled:
            raise RuntimeError("视频模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("视频模型平台不可用")
        reference_media = [
            {str(key): str(value) for key, value in item.items() if isinstance(value, str)}
            for item in (task.request_payload.get("reference_media") or [])
            if isinstance(item, dict)
        ]
        assets_by_id: dict[str, Asset] = {}
        if not reference_media and shot.asset_ids:
            assets_by_id = await load_shot_assets_with_parents(
                session,
                shots=[shot],
                tenant_id=task.tenant_id,
            )
        if not reference_media:
            reference_media = [
                *build_shot_image_references(shot, assets_by_id),
                *build_shot_audio_references(shot, assets_by_id, model.capabilities or {}),
            ]
        reference_limits = model.capabilities.get("reference_limits")
        image_limit = reference_limits.get("image") if isinstance(reference_limits, dict) else None
        if isinstance(image_limit, dict):
            image_references = [item for item in reference_media if item.get("type") == "image"]
            non_image_references = [item for item in reference_media if item.get("type") != "image"]
            max_image_references = int(image_limit.get("max_count") or 0)
            image_references = (
                image_references[:max_image_references]
                if image_limit.get("enabled") and max_image_references > 0
                else []
            )
            reference_media = image_references + non_image_references
        generation_mode = str(task.request_payload.get("generation_mode") or "")
        if not generation_mode:
            generation_mode = resolve_video_generation_mode(
                model.capabilities,
                image_reference_count=sum(1 for item in reference_media if item.get("type") == "image"),
            )
        audio_enabled = video_audio_enabled(
            model.capabilities,
            bool(task.request_payload.get("audio_enabled", False)),
        )
        provider_protocol = video_prompt_protocol(
            {
                "provider_code": provider.code,
                "model_id": model.model_id,
                "capabilities": model.capabilities,
            }
        )
        effective_video_prompt = enforce_video_audio_policy(
            shot.video_prompt,
            shot.dialogue,
            audio_enabled=audio_enabled,
            protocol_name=str(provider_protocol["name"]),
            language=str(provider_protocol["preferred_prompt_language"]),
        )
        duration_seconds = float(task.request_payload.get("duration_seconds") or shot.duration_seconds)
        resolution = str(task.request_payload.get("video_resolution") or "1080p")
        aspect_ratio = str(task.request_payload.get("aspect_ratio") or "16:9")
        video_project = await session.get(Project, task.project_id) if task.project_id else None
        if video_project and video_project.creation_mode == "ai":
            aspect_ratio = video_project.aspect_ratio
            resolution = video_project.video_resolution
        if model.capabilities.get("schema_version") == 1:
            resolution = compatible_video_resolution(
                model.capabilities,
                duration_seconds=duration_seconds,
                requested_resolution=resolution,
                aspect_ratio=aspect_ratio,
            )
            validate_video_generation_request(
                model.capabilities,
                generation_mode=generation_mode,
                duration_seconds=duration_seconds,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                reference_media=reference_media,
                audio_enabled=audio_enabled,
            )
        reference_media = await prepare_adapter_reference_media(provider, reference_media)
        request = VideoGenerationRequest(
            model=model.model_id,
            prompt=effective_video_prompt,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            reference_image_url=shot.reference_image_url,
            capabilities=model.capabilities,
            idempotency_key=task.idempotency_key or task.id,
            generation_mode=generation_mode,
            audio_enabled=audio_enabled,
            reference_media=reference_media,
        )
        gateway = gateway_factory(provider)
        provider_job_id = (
            str(task.provider_job_id or (task.result_payload or {}).get("provider_job_id") or "") or None
        )

    if provider_job_id:
        video_result = await gateway.poll_video(request, provider_job_id)
    else:
        video_result = await gateway.submit_video(request)
    if video_result.provider_job_id and video_result.provider_job_id != provider_job_id:
        provider_job_id = video_result.provider_job_id
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, task_id)
            clip = await session.get(VideoClip, clip_id)
            if not owns_running_task(task) or clip is None:
                return
            task.result_payload = {**(task.result_payload or {}), "provider_job_id": provider_job_id}
            task.provider_job_id = provider_job_id
            clip.provider_job_id = provider_job_id
            clip.status = VideoClipStatus.GENERATING
            event = record_task_event(
                session,
                task,
                status=TaskStatus.RUNNING,
                progress=35,
                message="视频平台已受理，正在生成镜头",
                metadata={"provider_job_id": provider_job_id},
            )
            await session.commit()
            await publish_task_event(task, event)

    poll_started = time.monotonic()
    adapter_video = (
        provider.adapter_config.get("video") if isinstance(provider.adapter_config, dict) else None
    )
    adapter_video = adapter_video if isinstance(adapter_video, dict) else {}
    configured_poll_interval = (
        adapter_video.get("poll_interval_seconds") or request.capabilities.get("poll_interval_seconds") or 5
    )
    configured_poll_timeout = (
        adapter_video.get("poll_timeout_seconds") or request.capabilities.get("poll_timeout_seconds") or 1800
    )
    poll_interval = max(
        1.0,
        min(float(configured_poll_interval), 60.0),
    )
    poll_timeout = max(
        30.0,
        min(float(configured_poll_timeout), 7200.0),
    )
    while video_result.status == "pending":
        if not provider_job_id:
            raise RuntimeError("视频平台未返回可恢复的任务 ID")
        if time.monotonic() - poll_started >= poll_timeout:
            raise RuntimeError("视频平台任务等待超时，可稍后重试")
        await asyncio.sleep(poll_interval)
        video_result = await gateway.poll_video(request, provider_job_id)
    if video_result.status == "failed":
        raise ProviderJobTerminalError(video_result.error_message or "视频平台任务失败")
    if video_result.video_data is None:
        raise RuntimeError("视频平台未返回视频文件")
    await record_progress(task_id, 88, "视频已生成，正在校验镜头版本并写入项目文件库")
    _local_url, stored_path, mime_type = await run_in_threadpool(
        save_project_video,
        video_result.video_data,
        uploads_root=get_settings().uploads_root,
        tenant_id=task.tenant_id,
        project_id=str(task.project_id),
        clip_id=clip_id,
        content_type=video_result.content_type,
    )
    try:
        storage_key, media_url = await persist_media_file(stored_path, mime_type)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    try:
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, task_id)
            shot = await session.get(StoryboardShot, shot_id)
            storyboard = await session.get(StoryboardVersion, storyboard_id)
            clip = await session.get(VideoClip, clip_id)
            if not owns_running_task(task) or shot is None or clip is None:
                await cleanup_media(storage_key, stored_path)
                return
            if (
                storyboard is None
                or not storyboard.is_active
                or shot.version != int(task.request_payload.get("shot_version") or -1)
            ):
                raise RuntimeError("镜头或分镜已更新，视频结果未覆盖新版本")
            await session.execute(
                update(VideoClip)
                .where(VideoClip.shot_id == shot.id, VideoClip.is_active.is_(True), VideoClip.id != clip.id)
                .values(is_active=False, invalidated_reason=f"已生成视频 v{clip.version}")
            )
            clip.status = VideoClipStatus.READY
            clip.media_url = media_url
            clip.provider_job_id = provider_job_id
            clip.error_message = None
            clip.is_active = True
            await invalidate_compositions(
                session,
                chapter_id=shot.chapter_id,
                reason=f"镜头 {shot.order_index:02d} 已生成新视频 v{clip.version}",
            )
            session.add(
                ProjectFile(
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    project_id=shot.project_id,
                    name=f"镜头-{shot.order_index:02d}-视频-v{clip.version}{stored_path.suffix}",
                    kind=ProjectFileKind.VIDEO,
                    mime_type=mime_type,
                    size_bytes=len(video_result.video_data),
                    storage_path=storage_key,
                    editable=False,
                    file_metadata={
                        "chapter_id": shot.chapter_id,
                        "storyboard_version_id": storyboard.id,
                        "shot_id": shot.id,
                        "video_clip_id": clip.id,
                        "source_task_id": task.id,
                    },
                )
            )
            task.status = TaskStatus.SUCCEEDED
            task.result_payload = {
                **(task.result_payload or {}),
                "credit_refunded": False,
                "provider_job_id": provider_job_id,
                "video_clip_id": clip.id,
                "media_url": media_url,
            }
            task.provider_job_id = provider_job_id
            event = record_task_event(
                session,
                task,
                status=TaskStatus.SUCCEEDED,
                progress=100,
                message=f"镜头 {shot.order_index:02d} 视频已生成",
                metadata={"video_clip_id": clip.id, "media_url": media_url},
            )
            await session.commit()
            await publish_task_event(task, event)
    except Exception:
        await cleanup_media(storage_key, stored_path)
        raise


async def execute_dialogue_extraction_task(task_id: str, runtime_factory: RuntimeFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        chapter_id = str(task.request_payload.get("chapter_id") or "")
        script_id = str(task.request_payload.get("script_version_id") or "")
        storyboard_id = str(task.request_payload.get("storyboard_version_id") or "") or None
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        storyboard = await session.get(StoryboardVersion, storyboard_id) if storyboard_id else None
        if chapter is None or script is None or script.chapter_id != chapter.id:
            raise RuntimeError("台词提取所用剧本版本已不存在")
        if storyboard_id and (storyboard is None or storyboard.chapter_id != chapter.id):
            raise RuntimeError("台词提取所用分镜版本已不存在")
        shots = (
            list(
                (
                    await session.scalars(
                        select(StoryboardShot)
                        .where(StoryboardShot.storyboard_version_id == storyboard_id)
                        .order_by(StoryboardShot.order_index)
                    )
                ).all()
            )
            if storyboard_id
            else []
        )
        shot_context = [
            {
                "order_index": shot.order_index,
                "title": shot.title,
                "dialogue": shot.dialogue,
                "scene": shot.scene_description,
                "action": shot.action_description,
            }
            for shot in shots
        ]
        prompt = (
            "从生效剧本中提取所有需要演员配音的对白。旁白可使用角色名‘旁白’，不要把场景说明、"
            "镜头说明或纯动作当成台词。保留适合表演的情绪和语气指导；有分镜时将台词匹配到对应"
            "shot_order_index，没有可靠对应时返回 null。角色名称必须与剧本中的人物名一致。\n"
            '返回结构：{"lines":[{"shot_order_index":1,"speaker":"角色名",'
            '"text":"台词正文","emotion":"克制","direction":"压低声音，短暂停顿"}]}\n\n'
            f"章节：{chapter.title}\n剧本 v{script.version}：\n{script.content}\n\n"
            f"当前分镜：{json.dumps(shot_context, ensure_ascii=False)}"
        )
    request = await runtime_request(task_id, prompt_code="dialogue-extraction", prompt=prompt)
    result = await runtime_factory().run(request)
    try:
        parsed = DialogueExtractionPayload.model_validate(parse_json_object(result.final_response))
    except ValidationError as exc:
        raise RuntimeError("AI 返回的台词结构不符合要求") from exc
    await record_progress(task_id, 72, f"已识别 {len(parsed.lines)} 条台词，正在建立配音版本")

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        chapter = await session.get(Chapter, chapter_id)
        script = await session.get(ScriptVersion, script_id)
        storyboard = await session.get(StoryboardVersion, storyboard_id) if storyboard_id else None
        if not owns_running_task(task) or chapter is None or script is None:
            return
        current_script_hash = hashlib.sha256(script.content.encode("utf-8")).hexdigest()
        if (
            chapter.active_script_version_id != script.id
            or current_script_hash != task.request_payload.get("script_hash")
            or (storyboard_id and (storyboard is None or not storyboard.is_active))
        ):
            raise RuntimeError("生效剧本或分镜已切换，本次台词提取结果已作废")
        current_shots = (
            list(
                (
                    await session.scalars(
                        select(StoryboardShot).where(StoryboardShot.storyboard_version_id == storyboard_id)
                    )
                ).all()
            )
            if storyboard_id
            else []
        )
        shot_by_order = {shot.order_index: shot for shot in current_shots}
        latest = await session.scalar(
            select(func.max(DialogueVersion.version)).where(DialogueVersion.chapter_id == chapter.id)
        )
        await session.execute(
            update(DialogueVersion)
            .where(DialogueVersion.chapter_id == chapter.id, DialogueVersion.is_active.is_(True))
            .values(is_active=False, invalidated_reason="已生成新的台词版本")
        )
        await session.execute(
            update(AudioClip)
            .where(AudioClip.chapter_id == chapter.id, AudioClip.is_active.is_(True))
            .values(is_active=False, invalidated_reason="已生成新的台词版本")
        )
        await invalidate_compositions(
            session,
            chapter_id=chapter.id,
            reason="AI 已提取新的台词版本",
        )
        dialogue = DialogueVersion(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            script_version_id=script.id,
            storyboard_version_id=storyboard_id,
            version=(latest or 0) + 1,
            runtime_manifest=result.manifest,
            is_active=True,
        )
        session.add(dialogue)
        await session.flush()
        serialized_lines: list[dict[str, object]] = []
        line_ids: list[str] = []
        for index, payload in enumerate(parsed.lines, start=1):
            source_hash = hashlib.sha256(
                f"{payload.speaker}\n{payload.text}\n{payload.emotion}\n{payload.direction}".encode()
            ).hexdigest()
            line = DialogueLine(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                chapter_id=chapter.id,
                dialogue_version_id=dialogue.id,
                shot_id=(
                    shot_by_order[payload.shot_order_index].id
                    if payload.shot_order_index in shot_by_order
                    else None
                ),
                order_index=index,
                speaker=payload.speaker.strip(),
                text=payload.text.strip(),
                emotion=payload.emotion.strip() or "自然",
                direction=payload.direction.strip(),
                source_hash=source_hash,
            )
            session.add(line)
            await session.flush()
            line_ids.append(line.id)
            serialized_lines.append(
                {
                    "order_index": index,
                    "shot_order_index": payload.shot_order_index,
                    "speaker": line.speaker,
                    "text": line.text,
                    "emotion": line.emotion,
                    "direction": line.direction,
                }
            )
        serialized = json.dumps({"lines": serialized_lines}, ensure_ascii=False, indent=2)
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=chapter.project_id,
                name=f"{chapter.title}-台词表-v{dialogue.version}.json",
                kind=ProjectFileKind.AUDIO,
                mime_type="application/json",
                size_bytes=len(serialized.encode("utf-8")),
                content=serialized,
                editable=True,
                file_metadata={
                    "chapter_id": chapter.id,
                    "script_version_id": script.id,
                    "storyboard_version_id": storyboard_id,
                    "dialogue_version_id": dialogue.id,
                    "source_task_id": task.id,
                    "role": "dialogue_sheet",
                },
            )
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {
            "credit_refunded": False,
            "dialogue_version_id": dialogue.id,
            "dialogue_line_ids": line_ids,
            "line_count": len(line_ids),
            "runtime_manifest": result.manifest,
        }
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message=f"台词 v{dialogue.version} 已提取，共 {len(line_ids)} 条",
            metadata={"dialogue_version_id": dialogue.id, "line_count": len(line_ids)},
        )
        await session.commit()
        await publish_task_event(task, event)


async def execute_dialogue_tts_task(task_id: str, gateway_factory: GatewayFactory) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.model_id:
            return
        dialogue_id = str(task.request_payload.get("dialogue_version_id") or "")
        line_id = str(task.request_payload.get("dialogue_line_id") or "")
        binding_id = str(task.request_payload.get("voice_binding_id") or "")
        clip_id = str(task.request_payload.get("audio_clip_id") or "")
        dialogue = await session.get(DialogueVersion, dialogue_id)
        line = await session.get(DialogueLine, line_id)
        binding = await session.get(VoiceBinding, binding_id)
        clip = await session.get(AudioClip, clip_id)
        model = await session.get(AIModel, task.model_id)
        if dialogue is None or line is None or binding is None or clip is None:
            raise RuntimeError("配音任务所用台词或音色绑定已不存在")
        if (
            not dialogue.is_active
            or line.dialogue_version_id != dialogue.id
            or line.version != int(task.request_payload.get("line_version") or -1)
            or binding.version != int(task.request_payload.get("binding_version") or -1)
            or not binding.enabled
            or clip.line_version != line.version
            or clip.binding_version != binding.version
        ):
            raise RuntimeError("台词版本或角色音色已更新，本次配音任务已失效")
        if model is None or model.model_type != ModelType.TTS or not model.enabled:
            raise RuntimeError("TTS 模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or provider.tenant_id != task.tenant_id or not provider.enabled:
            raise RuntimeError("TTS 模型平台不可用")
        request = SpeechGenerationRequest(
            model=model.model_id,
            text=line.text,
            voice=binding.provider_voice_id,
            style=binding.style,
            instructions="\n".join(
                value for value in (binding.instructions, line.direction, line.emotion) if value
            ),
            capabilities=model.capabilities,
            idempotency_key=task.idempotency_key or task.id,
        )
        gateway = gateway_factory(provider)
        provider_job_id = (
            str(task.provider_job_id or (task.result_payload or {}).get("provider_job_id") or "") or None
        )

    speech_result = (
        await gateway.poll_speech(request, provider_job_id)
        if provider_job_id
        else await gateway.submit_speech(request)
    )
    if speech_result.provider_job_id and speech_result.provider_job_id != provider_job_id:
        provider_job_id = speech_result.provider_job_id
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, task_id)
            clip = await session.get(AudioClip, clip_id)
            if not owns_running_task(task) or clip is None:
                return
            task.result_payload = {**(task.result_payload or {}), "provider_job_id": provider_job_id}
            task.provider_job_id = provider_job_id
            clip.provider_job_id = provider_job_id
            clip.status = AudioClipStatus.GENERATING
            event = record_task_event(
                session,
                task,
                status=TaskStatus.RUNNING,
                progress=35,
                message="TTS 平台已受理，正在合成语音",
                metadata={"provider_job_id": provider_job_id},
            )
            await session.commit()
            await publish_task_event(task, event)
    poll_started = time.monotonic()
    poll_interval = max(1.0, min(float(request.capabilities.get("poll_interval_seconds") or 3), 60.0))
    poll_timeout = max(30.0, min(float(request.capabilities.get("poll_timeout_seconds") or 900), 3600.0))
    while speech_result.status == "pending":
        if not provider_job_id:
            raise RuntimeError("TTS 平台未返回可恢复的任务 ID")
        if time.monotonic() - poll_started >= poll_timeout:
            raise RuntimeError("TTS 平台任务等待超时，可稍后重试")
        await asyncio.sleep(poll_interval)
        speech_result = await gateway.poll_speech(request, provider_job_id)
    if speech_result.status == "failed":
        raise ProviderJobTerminalError(speech_result.error_message or "TTS 平台任务失败")
    if speech_result.audio_data is None:
        raise RuntimeError("TTS 平台未返回音频文件")
    await record_progress(task_id, 88, "语音已合成，正在校验台词版本并写入项目文件库")
    _local_url, stored_path, mime_type = await run_in_threadpool(
        save_project_audio,
        speech_result.audio_data,
        uploads_root=get_settings().uploads_root,
        tenant_id=task.tenant_id,
        project_id=str(task.project_id),
        clip_id=clip_id,
        content_type=speech_result.content_type,
    )
    try:
        storage_key, media_url = await persist_media_file(stored_path, mime_type)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    try:
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, task_id)
            dialogue = await session.get(DialogueVersion, dialogue_id)
            line = await session.get(DialogueLine, line_id)
            binding = await session.get(VoiceBinding, binding_id)
            clip = await session.get(AudioClip, clip_id)
            if not owns_running_task(task) or line is None or clip is None:
                await cleanup_media(storage_key, stored_path)
                return
            if (
                dialogue is None
                or not dialogue.is_active
                or binding is None
                or not binding.enabled
                or line.version != int(task.request_payload.get("line_version") or -1)
                or binding.version != int(task.request_payload.get("binding_version") or -1)
            ):
                raise RuntimeError("台词或音色已更新，配音结果未覆盖新版本")
            await session.execute(
                update(AudioClip)
                .where(
                    AudioClip.dialogue_line_id == line.id,
                    AudioClip.is_active.is_(True),
                    AudioClip.id != clip.id,
                )
                .values(is_active=False, invalidated_reason=f"已生成配音 v{clip.version}")
            )
            clip.status = AudioClipStatus.READY
            clip.media_url = media_url
            clip.provider_job_id = provider_job_id
            clip.duration_seconds = (
                Decimal(str(speech_result.duration_seconds))
                if speech_result.duration_seconds is not None
                else None
            )
            clip.error_message = None
            clip.is_active = True
            await invalidate_compositions(
                session,
                chapter_id=line.chapter_id,
                reason=f"台词 {line.order_index:02d} 已生成新配音 v{clip.version}",
            )
            session.add(
                ProjectFile(
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    project_id=line.project_id,
                    name=f"台词-{line.order_index:03d}-{line.speaker}-配音-v{clip.version}{stored_path.suffix}",
                    kind=ProjectFileKind.AUDIO,
                    mime_type=mime_type,
                    size_bytes=len(speech_result.audio_data),
                    storage_path=storage_key,
                    editable=False,
                    file_metadata={
                        "chapter_id": line.chapter_id,
                        "dialogue_version_id": dialogue.id,
                        "dialogue_line_id": line.id,
                        "voice_binding_id": binding.id,
                        "audio_clip_id": clip.id,
                        "source_task_id": task.id,
                        "role": "dialogue_audio",
                    },
                )
            )
            task.status = TaskStatus.SUCCEEDED
            task.result_payload = {
                **(task.result_payload or {}),
                "credit_refunded": False,
                "provider_job_id": provider_job_id,
                "audio_clip_id": clip.id,
                "media_url": media_url,
            }
            task.provider_job_id = provider_job_id
            event = record_task_event(
                session,
                task,
                status=TaskStatus.SUCCEEDED,
                progress=100,
                message=f"{line.speaker} · 台词 {line.order_index:02d} 配音已生成",
                metadata={"audio_clip_id": clip.id, "media_url": media_url},
            )
            await session.commit()
            await publish_task_event(task, event)
    except Exception:
        await cleanup_media(storage_key, stored_path)
        raise


async def execute_composition_render_task(
    task_id: str,
    renderer_factory: RendererFactory,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task) or not task.project_id:
            return
        composition_id = str(task.request_payload.get("composition_id") or "")
        composition = await session.get(CompositionVersion, composition_id)
        if (
            composition is None
            or composition.project_id != task.project_id
            or composition.version != int(task.request_payload.get("composition_version") or -1)
            or composition.status != CompositionStatus.RENDERING
        ):
            raise RuntimeError("成片版本已更新或不可用")
        storyboard = await session.get(StoryboardVersion, composition.storyboard_version_id)
        if storyboard is None or not storyboard.is_active:
            raise RuntimeError("生效分镜已切换，旧成片清单不能继续渲染")
        manifest = dict(composition.timeline_manifest)
        for shot in manifest.get("shots") or []:
            clip = await session.get(VideoClip, str(shot.get("video_clip_id") or ""))
            if clip is None or not clip.is_active or clip.status != VideoClipStatus.READY:
                raise RuntimeError(f"镜头 {int(shot.get('order_index') or 0):02d} 视频已失效")
            for item in shot.get("dialogue_clips") or []:
                audio = await session.get(AudioClip, str(item.get("audio_clip_id") or ""))
                if audio is None or not audio.is_active or audio.status != AudioClipStatus.READY:
                    raise RuntimeError("成片清单中的台词音频已失效")
        uploads_root = get_settings().uploads_root.resolve()
        output_dir = uploads_root / task.tenant_id / "projects" / task.project_id / "renders"
        output_path = output_dir / f"{composition.id}-v{composition.version}.mp4"
        resolution = composition.resolution
        aspect_ratio = composition.aspect_ratio
        fps = composition.fps

    try:
        render_manifest = await materialize_composition_manifest(manifest)
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("成片清单中的媒体对象不存在或路径无效") from error
    await record_progress(task_id, 24, "已锁定镜头与音轨版本，正在启动渲染")
    await renderer_factory().render(
        render_manifest,
        output_path=output_path,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        fps=fps,
    )
    await record_progress(task_id, 92, "成片已编码，正在写入项目文件库")
    try:
        storage_key, media_url = await persist_media_file(output_path, "video/mp4")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    try:
        async with SessionLocal() as session:
            task = await owned_task_for_update(session, task_id)
            composition = await session.get(CompositionVersion, composition_id)
            storyboard = (
                await session.get(StoryboardVersion, composition.storyboard_version_id)
                if composition
                else None
            )
            if not owns_running_task(task) or composition is None:
                await cleanup_media(storage_key, output_path)
                return
            if (
                composition.status != CompositionStatus.RENDERING
                or storyboard is None
                or not storyboard.is_active
            ):
                raise RuntimeError("渲染期间生产版本已切换，结果未设为生效")
            await session.execute(
                update(CompositionVersion)
                .where(
                    CompositionVersion.chapter_id == composition.chapter_id,
                    CompositionVersion.id != composition.id,
                )
                .values(is_active=False)
            )
            composition.status = CompositionStatus.READY
            composition.output_url = media_url
            composition.error_message = None
            composition.is_active = True
            composition.invalidated_reason = None
            chapter = await session.get(Chapter, composition.chapter_id)
            if chapter is not None:
                chapter.status = ChapterStatus.COMPLETED
            session.add(
                ProjectFile(
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    project_id=composition.project_id,
                    name=f"{composition.title}-v{composition.version}.mp4",
                    kind=ProjectFileKind.VIDEO,
                    mime_type="video/mp4",
                    size_bytes=output_path.stat().st_size,
                    storage_path=storage_key,
                    editable=False,
                    file_metadata={
                        "chapter_id": composition.chapter_id,
                        "composition_id": composition.id,
                        "source_task_id": task.id,
                        "role": "chapter_render",
                        "manifest": composition.timeline_manifest,
                    },
                )
            )
            task.status = TaskStatus.SUCCEEDED
            task.result_payload = {
                **(task.result_payload or {}),
                "credit_refunded": False,
                "composition_id": composition.id,
                "media_url": media_url,
            }
            event = record_task_event(
                session,
                task,
                status=TaskStatus.SUCCEEDED,
                progress=100,
                message=f"{composition.title} 已完成渲染",
                metadata={"composition_id": composition.id, "media_url": media_url},
            )
            await session.commit()
            await publish_task_event(task, event)
    except Exception:
        await cleanup_media(storage_key, output_path)
        raise


def composition_source_paths(manifest: dict) -> list[str]:
    paths: list[str] = []
    for shot in manifest.get("shots") or []:
        paths.append(str(shot.get("video_storage_path") or ""))
        paths.extend(str(item.get("audio_storage_path") or "") for item in shot.get("dialogue_clips") or [])
    background = manifest.get("background_music")
    if background:
        paths.append(str(background.get("storage_path") or ""))
    paths.extend(str(item.get("storage_path") or "") for item in manifest.get("environment_audio") or [])
    return [path for path in paths if path]


async def materialize_composition_manifest(manifest: dict) -> dict:
    materialized = deepcopy(manifest)
    for shot in materialized.get("shots") or []:
        shot["video_storage_path"] = str(
            await materialize_media_file(str(shot.get("video_storage_path") or ""))
        )
        for item in shot.get("dialogue_clips") or []:
            item["audio_storage_path"] = str(
                await materialize_media_file(str(item.get("audio_storage_path") or ""))
            )
    background = materialized.get("background_music")
    if background:
        background["storage_path"] = str(
            await materialize_media_file(str(background.get("storage_path") or ""))
        )
    for item in materialized.get("environment_audio") or []:
        item["storage_path"] = str(await materialize_media_file(str(item.get("storage_path") or "")))
    return materialized


async def fail_task(
    task_id: str,
    message: str,
    *,
    provider_job_terminal: bool = False,
) -> None:
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        result = dict(task.result_payload or {})
        history = list(result.get("attempt_history") or [])
        history.append({"status": "failed", "message": message})
        result["attempt_history"] = history
        provider_job_id = str(task.provider_job_id or result.get("provider_job_id") or "") or None
        personal_video = (
            task.task_type == "agent_chat_run"
            and task.request_payload.get("scope") == "personal"
            and task.request_payload.get("mode") == "video"
        )
        if (task.task_type in PROVIDER_JOB_TASKS or personal_video) and provider_job_id:
            result["resume_provider_job"] = not provider_job_terminal
            result["provider_job_terminal"] = provider_job_terminal
        task.result_payload = result
        task.status = TaskStatus.FAILED
        task.error_message = message
        if task.task_type == "agent_chat_run":
            chat_session_id = str(task.request_payload.get("agent_chat_session_id") or "")
            chat_session = await session.get(AgentChatSession, chat_session_id) if chat_session_id else None
            existing_message = (
                await session.scalar(
                    select(AgentChatMessage).where(
                        AgentChatMessage.session_id == chat_session_id,
                        AgentChatMessage.run_id == task.id,
                        AgentChatMessage.role == AgentMessageRole.ASSISTANT,
                    )
                )
                if chat_session_id
                else None
            )
            if chat_session is not None and existing_message is None:
                failure_label = {
                    "image": "图片生成",
                    "video": "视频生成",
                    "skill": "Skill 创作",
                }.get(str(task.request_payload.get("mode") or ""), " Agent 创作")
                assistant_message = AgentChatMessage(
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    session_id=chat_session.id,
                    role=AgentMessageRole.ASSISTANT,
                    content=(
                        f"这轮{failure_label}失败了：{message}\n\n"
                        "我已经把任务标记为失败并退回本次失败任务的积分。"
                        "你可以稍后重试；如果连续失败，请让管理员检查模型平台、API Key、"
                        "供应商并发限制或 Agent Runtime 日志。"
                    ),
                    run_id=task.id,
                    finish_reason="failed",
                    runtime_events=[],
                    runtime_manifest={
                        "runtime_type": "platform_error",
                        "agent_chat_session_id": chat_session.id,
                        "error_message": message,
                    },
                )
                session.add(assistant_message)
                chat_session.runtime_manifest = {
                    **(chat_session.runtime_manifest or {}),
                    "last_error": message,
                }
                chat_session.last_message_at = datetime.now(UTC)
                result["assistant_message_id"] = assistant_message.id
                task.result_payload = result
        if task.task_type == "asset_image_generation":
            asset_id = str(task.request_payload.get("asset_id") or "")
            asset = await session.get(Asset, asset_id)
            expected_version = int(task.request_payload.get("asset_version") or -1)
            if (
                asset is not None
                and asset.version == expected_version
                and asset.status == AssetStatus.GENERATING
            ):
                asset.status = AssetStatus.READY if asset.media_url else AssetStatus.FAILED
        elif task.task_type == "shot_video_generation":
            clip = await session.get(
                VideoClip,
                str(task.request_payload.get("video_clip_id") or ""),
            )
            if clip is not None and clip.status in {
                VideoClipStatus.QUEUED,
                VideoClipStatus.GENERATING,
            }:
                clip.status = VideoClipStatus.FAILED
                clip.error_message = message
        elif task.task_type == "dialogue_tts_generation":
            clip = await session.get(
                AudioClip,
                str(task.request_payload.get("audio_clip_id") or ""),
            )
            if clip is not None and clip.status in {
                AudioClipStatus.QUEUED,
                AudioClipStatus.GENERATING,
            }:
                clip.status = AudioClipStatus.FAILED
                clip.error_message = message
        elif task.task_type == "chapter_composition_render":
            composition = await session.get(
                CompositionVersion,
                str(task.request_payload.get("composition_id") or ""),
            )
            if composition is not None and composition.status == CompositionStatus.RENDERING:
                composition.status = CompositionStatus.FAILED
                composition.error_message = message
        elif task.task_type in {"chapter_analysis_generation", "chapter_script_generation"}:
            chapter = await session.get(
                Chapter,
                str(task.request_payload.get("chapter_id") or ""),
            )
            if chapter is not None:
                script_count = await session.scalar(
                    select(func.count(ScriptVersion.id)).where(ScriptVersion.chapter_id == chapter.id)
                )
                analysis_count = await session.scalar(
                    select(func.count(ChapterAnalysis.id)).where(ChapterAnalysis.chapter_id == chapter.id)
                )
                if script_count:
                    chapter.status = ChapterStatus.REVIEWING
                elif analysis_count:
                    chapter.status = ChapterStatus.ANALYZED
                else:
                    chapter.status = ChapterStatus.UNINITIALIZED
        event = record_task_event(
            session,
            task,
            status=TaskStatus.FAILED,
            progress=100,
            message=message,
        )
        await refund_task_cost(session, task, reason="AI 任务失败退款")
        await session.commit()
        await publish_task_event(task, event)


def build_cover_prompt(project: Project, handbook: Handbook | None) -> str:
    style = ""
    if handbook is not None:
        style = f"\n视觉手册：{handbook.name}\n视觉约束：{handbook.description}"
        skill_text = read_skill_bundle(handbook.skill_path)
        if skill_text:
            style += f"\n画风技能约束：\n{skill_text}"
    return (
        "为一部 AI 短剧创作高质量项目封面。画面必须直接表现故事主体，不要出现文字、水印或品牌标志。"
        f"\n项目名称：{project.name}"
        f"\n项目简介：{project.description}"
        f"\n目标画幅：{project.aspect_ratio}"
        f"{style}"
    )


def read_skill_bundle(relative_path: str) -> str:
    root = get_settings().skills_root.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return ""
    if not target.is_dir():
        return ""
    chunks: list[str] = []
    total = 0
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
            continue
        content = path.read_text(encoding="utf-8")
        remaining = 50_000 - total
        if remaining <= 0:
            break
        content = content[:remaining]
        chunks.append(f"[{path.relative_to(target).as_posix()}]\n{content}")
        total += len(content)
    return "\n\n".join(chunks)


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, AgentRuntimeRequestError):
        return error.public_message
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in {401, 403}:
            return "AI 服务鉴权失败，请联系管理员检查平台凭据或内部配置"
        if status_code == 429:
            return "AI 服务请求频率受限，请稍后重试"
        if status_code >= 500:
            return "AI 服务暂时不可用，请稍后重试"
        return "AI 服务请求失败，请检查模型平台配置"
    if isinstance(error, httpx.RequestError):
        return "暂时无法连接 AI 服务，请稍后重试"
    message = str(error).strip()
    return (message or error.__class__.__name__)[:1000]
