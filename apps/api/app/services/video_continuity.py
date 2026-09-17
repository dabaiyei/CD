"""Persisted, owner-scoped video continuation without holding a provider slot."""
from __future__ import annotations

import re
import json
import math
import tempfile
from pathlib import Path

from PIL import Image, ImageStat, ImageChops
from sqlalchemy import select

from app.db.models import AITask, Asset, AssetType, StoryboardShot, StoryboardVersion, TaskStatus, VideoClip, VideoClipStatus
from app.services.video_concat import run_media_command


async def predecessor(session, shot):
    previous = await session.scalar(select(StoryboardShot).where(
        StoryboardShot.storyboard_version_id == shot.storyboard_version_id,
        StoryboardShot.user_id == shot.user_id, StoryboardShot.project_id == shot.project_id,
        StoryboardShot.order_index < shot.order_index,
    ).order_by(StoryboardShot.order_index.desc()).limit(1))
    if previous is None:
        return None
    board = await session.get(StoryboardVersion, shot.storyboard_version_id)
    groups = {item.get("order_index"): item.get("continuity_group", "") for item in (board.content or [])}
    current_group, prior_group = groups.get(shot.order_index), groups.get(previous.order_index)
    if current_group or prior_group:
        return previous if current_group and current_group == prior_group else None
    # Legacy boards: only explicit same scene evidence; never infer from character identity alone.
    if re.search(r"次日|翌日|多年后|数日后|时间跳跃|切至另一|转场至|闪回", shot.scene_description + shot.action_description):
        return None
    scene_ids = set(await session.scalars(select(Asset.id).where(
        Asset.id.in_(set(previous.asset_ids) & set(shot.asset_ids)), Asset.asset_type == AssetType.SCENE,
        Asset.project_id == shot.project_id, Asset.user_id == shot.user_id,
    )))
    same_description = bool(shot.scene_description.strip() and shot.scene_description.strip() == previous.scene_description.strip())
    return previous if scene_ids or same_description else None


async def wake_continuations(session):
    waiting = (await session.scalars(select(AITask).where(
        AITask.status == TaskStatus.QUEUED,
        AITask.request_payload["video_continuity_waiting"].as_boolean().is_(True),
    ).with_for_update(skip_locked=True))).all()
    for task in waiting:
        dependency = await session.get(AITask, task.request_payload.get("video_continuity_task_id"))
        if not dependency or dependency.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            task.request_payload = {**task.request_payload, "video_continuity_waiting": False}


async def extract_boundary(source: Path, directory: Path, *, video_reference: bool):
    """Decode the last half second; reject black/white tails instead of anchoring on them."""
    info = json.loads(await run_media_command("ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration", "-of", "json", str(source)))
    try:
        duration = float(info["streams"][0]["duration"])
    except (KeyError, IndexError, TypeError, ValueError):
        packets = json.loads(await run_media_command("ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(source)))
        ends = [float(p["pts_time"]) + float(p.get("duration_time") or 0)
            for p in packets.get("packets", []) if "pts_time" in p]
        duration = max(ends, default=0)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("上一镜没有可用的视频时长，无法提取尾帧")
    await run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(max(0, duration - 0.5)), "-i", str(source), "-an", "-vf", "fps=20", str(directory / "tail-%03d.png"))
    frames = sorted(directory.glob("tail-*.png"))
    selected = None
    for frame in reversed(frames):
        with Image.open(frame) as image:
            stats = ImageStat.Stat(image.convert("L").resize((64, 64)))
            if 3 < stats.mean[0] < 252 and stats.stddev[0] > 1:
                selected = frame
                break
    if selected is None:
        raise RuntimeError("上一镜结尾无有效画面，无法接续；请重新生成上一镜")
    # Avoid silently skipping a long fade and then concatenating its black tail.
    rejected = len(frames) - 1 - frames.index(selected)
    if rejected > 2:
        raise RuntimeError("上一镜尾部存在明显黑场或白场，请修复后再接续")
    tail = directory / "tail.mp4"
    if video_reference:
        await run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(max(0, duration - 1)), "-i", str(source), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(tail))
    return selected, tail if video_reference else None


async def validate_source(session, task):
    binding = task.request_payload.get("video_continuity")
    if not binding:
        return
    clip = await session.get(VideoClip, binding["clip_id"])
    shot = await session.get(StoryboardShot, binding["shot_id"])
    if (not clip or not clip.is_active or clip.status != VideoClipStatus.READY or not shot
        or shot.version != binding["shot_version"] or clip.media_url != binding["media_url"]
        or clip.user_id != task.user_id or clip.project_id != task.project_id):
        raise RuntimeError("上一镜视频或分镜已改变，本次接续结果失效，请基于新视频重新生成")


async def invalidate_descendants(session, shot_id, replacement_clip_id):
    shot = await session.get(StoryboardShot, shot_id)
    tasks = (await session.scalars(select(AITask).where(
        AITask.project_id == shot.project_id, AITask.user_id == shot.user_id,
        AITask.task_type == "shot_video_generation", AITask.status == TaskStatus.SUCCEEDED,
    ))).all()
    invalid_sources = {c.id for c in (await session.scalars(select(VideoClip).where(
        VideoClip.shot_id == shot_id, VideoClip.id != replacement_clip_id,
    ))).all()}
    for _ in range(len(tasks)):
        added = False
        for task in tasks:
            binding = task.request_payload.get("video_continuity") or {}
            clip_id = task.request_payload.get("video_clip_id")
            if binding.get("clip_id") not in invalid_sources or clip_id in invalid_sources:
                continue
            clip = await session.get(VideoClip, clip_id)
            if clip:
                clip.is_active = False
                clip.invalidated_reason = "上游视频已重做，请按新尾帧重新生成接续镜头"
                invalid_sources.add(clip.id)
                added = True
        if not added:
            break


async def prepare(task_id: str) -> bool:
    from app.services import task_worker as w
    async with w.SessionLocal() as session:
        task = await w.owned_task_for_update(session, task_id)
        if not w.owns_running_task(task):
            return False
        if task.request_payload.get("video_continuity"):
            await validate_source(session, task)
            return True
        # An already submitted legacy job must be polled with its original request.
        if task.provider_job_id:
            return True
        shot = await session.get(StoryboardShot, task.request_payload.get("shot_id"))
        if not shot or shot.user_id != task.user_id or shot.project_id != task.project_id:
            raise RuntimeError("接续镜头不可用")
        previous = await predecessor(session, shot)
        if previous is None:
            return True
        pending = await session.scalar(select(AITask).where(
            AITask.project_id == task.project_id, AITask.user_id == task.user_id,
            AITask.task_type == "shot_video_generation",
            AITask.request_payload["shot_id"].as_string() == previous.id,
            AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
        ).order_by(AITask.created_at.desc()).limit(1))
        if pending:
            task.request_payload = {**task.request_payload, "video_continuity_waiting": True,
                "video_continuity_task_id": pending.id}
            task.status = TaskStatus.QUEUED
            event = w.record_task_event(session, task, status=TaskStatus.QUEUED, progress=5,
                message=f"等待镜头 {previous.order_index} 完成，随后使用尾帧接续")
            await session.commit()
            await w.publish_task_event(task, event)
            return False
        dependency_id = task.request_payload.get("video_continuity_task_id")
        if dependency_id:
            dependency = await session.get(AITask, dependency_id)
            if not dependency or dependency.status != TaskStatus.SUCCEEDED:
                raise RuntimeError("上一镜视频失败或已取消，请完成上一镜后重试接续")
        clip = await session.scalar(select(VideoClip).where(
            VideoClip.shot_id == previous.id, VideoClip.is_active.is_(True),
            VideoClip.status == VideoClipStatus.READY, VideoClip.user_id == task.user_id,
        ).order_by(VideoClip.version.desc()).limit(1))
        if not clip or not clip.media_url:
            raise RuntimeError(f"连续场景需先生成镜头 {previous.order_index}，或一并选择前后镜头生成")
        model = await session.get(w.AIModel, task.model_id)
        caps = model.capabilities if model else {}
        limits = caps.get("reference_limits", {})
        images, videos = limits.get("image", {}), limits.get("video", {})
        if not images.get("enabled") or int(images.get("max_count") or 0) < 1:
            raise RuntimeError("当前模型不支持首帧参考，不能执行连续场景接力；请选择支持图生视频的模型")
        use_video = bool("full_reference" in caps.get("generation_modes", []) and videos.get("enabled")
            and int(videos.get("max_count") or 0) > 0
            and (not videos.get("accepted_mime_types") or "video/mp4" in videos["accepted_mime_types"]))
        provider = await session.get(w.Provider, model.provider_id) if model else None
        adapter = w.ProviderAdapterConfig.model_validate(provider.adapter_config) if provider and provider.adapter_config else None
        use_video = bool(use_video and adapter and adapter.video and
            any(mapping.media_type == "video" for mapping in adapter.video.references))
        if not use_video and not set(caps.get("generation_modes", [])).intersection({"first_frame", "multi_shot"}):
            raise RuntimeError("当前模型没有可执行的首帧接续模式")
        binding = {"clip_id": clip.id, "shot_id": previous.id, "shot_version": previous.version,
            "media_url": clip.media_url, "previous_action": previous.action_description[-1800:],
            "current_action": shot.action_description, "generation_mode": "full_reference" if use_video else "first_frame"}
        accepted_images = images.get("accepted_mime_types") or ["image/png"]
        binding["first_frame_mime"] = next((mime for mime in ["image/png", "image/jpeg", "image/webp"] if mime in accepted_images), "")
        if not binding["first_frame_mime"]:
            raise RuntimeError("当前视频模型不支持可用的尾帧图片格式")
        if not use_video and "first_frame" not in caps.get("generation_modes", []):
            binding["generation_mode"] = "multi_shot"
        source_key = w.object_key_from_media_url(clip.media_url)
        if not source_key:
            raise RuntimeError("上一镜视频不是可读取的项目文件")
    await w.record_progress(task_id, 12, "提取上一镜尾帧与运动参考，准备连续动作接力")
    source = await w.materialize_media_file(source_key)
    stored_keys = []
    try:
        with tempfile.TemporaryDirectory(prefix="cineforge-continuity-") as temp:
            frame, tail = await extract_boundary(source, Path(temp), video_reference=use_video)
            if binding["first_frame_mime"] != "image/png":
                converted = Path(temp) / ("frame.jpg" if binding["first_frame_mime"] == "image/jpeg" else "frame.webp")
                with Image.open(frame) as image:
                    image.convert("RGB").save(converted)
                frame = converted
            # Persist in the normal uploads store so references survive worker restarts.
            for field, path, mime in [("first_frame_url", frame, binding["first_frame_mime"]), ("video_url", tail, "video/mp4")]:
                if path is None:
                    continue
                target = w.get_settings().uploads_root / "continuity" / task_id / path.name
                await w.run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
                import shutil
                await w.run_in_threadpool(shutil.copyfile, path, target)
                key, url = await w.persist_media_file(target, mime)
                stored_keys.append((key, target))
                binding[field] = url
        async with w.SessionLocal() as session:
            task = await w.owned_task_for_update(session, task_id)
            if not w.owns_running_task(task):
                raise RuntimeError("接续任务已停止")
            task.request_payload = {**task.request_payload, "video_continuity": binding}
            await validate_source(session, task)
            await session.commit()
    except BaseException:
        for key, path in stored_keys:
            await w.cleanup_media(key, path)
        raise
    return True


def apply_reference(binding, references):
    if not binding:
        return references
    result = [dict(r) for r in references]
    image_index = next((i for i, r in enumerate(result) if r.get("type") == "image"), None)
    frame = {"type": "image", "url": binding["first_frame_url"], "mime_type": binding.get("first_frame_mime", "image/png"),
        "role": "first_frame", "token": "<Picture 1>"}
    if image_index is None:
        result.insert(0, frame)
    else:
        result[image_index] = frame
    if binding.get("video_url"):
        result = [r for r in result if r.get("type") != "video"]
        result.append({"type": "video", "url": binding["video_url"], "mime_type": "video/mp4",
            "role": "motion_continuity", "token": "<Video 1>"})
    return result


def continuation_prompt(binding):
    return ("\n连续场景接续约束（制作指令，不作为字幕或台词）：<Picture 1> 已替换为上一镜实际末尾画面，"
        "本镜0秒从该画面继续，优先于原提示词中不同的起始姿态和构图；保持身份、站位、持物、"
        "光线、轴线、视线和运动方向，不重复上一镜动作，不擅自站定复位，不淡入黑场。"
        "本镜明确要求静止、停下或固定机位时以本镜为准，不强制延续前镜速度。"
        + ("<Video 1> 为上一镜末尾运动参考，仅承接其速度和方向，不复播或复制音轨。" if binding.get("video_url") else "")
        + "前镜计划动作（用于理解动机，实际姿态以尾帧为准）：" + binding["previous_action"]
        + "\n本镜只继续以下动作：" + binding["current_action"])


async def inspect_seam(video: Path, binding):
    """Conservative technical gate; not a semantic action or identity evaluator."""
    from app.services.object_storage import materialize_media_file, object_key_from_media_url
    reference = await materialize_media_file(object_key_from_media_url(binding["first_frame_url"]))
    with tempfile.TemporaryDirectory(prefix="cineforge-seam-") as temp:
        opening = Path(temp) / "opening.png"
        await run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-frames:v", "1", str(opening))
        with Image.open(reference) as left, Image.open(opening) as right:
            left = left.convert("RGB").resize((64, 36))
            right = right.convert("RGB").resize((64, 36))
            difference = sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / (3 * 255)
            brightness = ImageStat.Stat(right.convert("L")).mean[0]
        if brightness < 2 or brightness > 253 or difference > 0.38:
            raise RuntimeError("接续检查发现开头黑白场或明显画面跳变，本次结果未启用，请重新生成此镜头")
        return {"check": "opening_frame_difference", "difference": round(difference, 4),
            "semantic_continuity_verified": False}
