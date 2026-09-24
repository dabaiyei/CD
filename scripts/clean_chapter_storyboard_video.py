"""清除单个章节的分镜与视频产物，保留剧本、资产和章节本身。

平台自带的章节清理（``purge_chapter_production``）会把剧本和资产一起删掉，
而这里的场景是「分镜做坏了，只想重做分镜与视频，不要重做剧本和资产图」。
因此本脚本只删：

* storyboard_versions / storyboard_shots
* video_clips
* dialogue_versions / dialogue_lines / audio_clips / composition_versions
  （它们全部由分镜派生；留着会变成指向已删分镜的悬空数据）
* 分镜与视频相关的 project_files，以及磁盘上的视频/首帧文件
* 记录这些阶段导演子任务（分镜生成/审核/修复、视频提示词/视频/拼接）
  及其通知；剧本与资产阶段的任务与通知保留

章节状态回退到 ASSETS（资产已就绪、等待重新生成分镜）。导演工作流里
``storyboard_version_id`` 指向已删分镜，必须清掉，否则「继续」会误判为
已完成；已 COMPLETED 的流程改回 WAITING_USER，让用户能重新触发分镜。

默认只演练（dry-run）。加 --apply 才会真正写库并删除文件。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import delete, select, update

from app.db.models import (
    AudioClip,
    AITask,
    Chapter,
    ChapterStatus,
    CompositionVersion,
    DialogueLine,
    DialogueVersion,
    DirectorChildRun,
    DirectorWorkflowRun,
    DirectorWorkflowStage,
    DirectorWorkflowStatus,
    Notification,
    ProjectFile,
    ProjectFileKind,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    VideoClip,
)
from app.db.session import SessionLocal
from app.services.object_storage import (
    delete_media_file,
    delete_media_prefix,
    object_key_from_media_url,
)


# 只清这些阶段的任务：分镜本身，以及从分镜派生的视频产物。
STORYBOARD_VIDEO_TASK_TYPES = {
    "chapter_storyboard_generation",
    "director_storyboard_review",
    "director_storyboard_repair",
    "shot_first_frame_generation",
    "shot_video_prompt_generation",
    "shot_video_generation",
    "storyboard_video_concat",
    "chapter_dialogue_extraction",
    "dialogue_tts_generation",
    "chapter_composition_render",
}
# 剧本、资产、原文、分析、连续性记忆都属于上游，绝不能随分镜一起清掉。
KEEP_FILE_KINDS = {
    ProjectFileKind.SOURCE,
    ProjectFileKind.ANALYSIS,
    ProjectFileKind.SCRIPT,
    ProjectFileKind.ASSET,
    ProjectFileKind.MEMORY,
}


async def _chapter(session, chapter_id: str) -> Chapter:
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None:
        raise SystemExit(f"章节不存在：{chapter_id}")
    return chapter


async def collect(session, chapter: Chapter) -> dict:
    boards = list(
        (await session.scalars(select(StoryboardVersion).where(StoryboardVersion.chapter_id == chapter.id))).all()
    )
    board_ids = [board.id for board in boards]
    shots = list(
        (await session.scalars(select(StoryboardShot).where(StoryboardShot.chapter_id == chapter.id))).all()
    )
    clips = list(
        (await session.scalars(select(VideoClip).where(VideoClip.chapter_id == chapter.id))).all()
    )
    dialogues = list(
        (await session.scalars(select(DialogueVersion).where(DialogueVersion.chapter_id == chapter.id))).all()
    )
    lines = list(
        (await session.scalars(select(DialogueLine).where(DialogueLine.chapter_id == chapter.id))).all()
    )
    audio = list(
        (await session.scalars(select(AudioClip).where(AudioClip.chapter_id == chapter.id))).all()
    )
    compositions = list(
        (
            await session.scalars(
                select(CompositionVersion).where(CompositionVersion.chapter_id == chapter.id)
            )
        ).all()
    )
    project_files = list(
        (
            await session.scalars(
                select(ProjectFile).where(ProjectFile.project_id == chapter.project_id)
            )
        ).all()
    )
    files = [
        item
        for item in project_files
        if (item.file_metadata or {}).get("chapter_id") == chapter.id
        and item.kind not in KEEP_FILE_KINDS
    ]
    tasks = [
        task
        for task in (
            await session.scalars(select(AITask).where(AITask.project_id == chapter.project_id))
        ).all()
        if task.task_type in STORYBOARD_VIDEO_TASK_TYPES and _mentions(task, chapter.id, board_ids, clips)
    ]
    workflows = list(
        (
            await session.scalars(
                select(DirectorWorkflowRun).where(DirectorWorkflowRun.chapter_id == chapter.id)
            )
        ).all()
    )
    children = list(
        (
            await session.scalars(
                select(DirectorChildRun).where(
                    DirectorChildRun.workflow_id.in_([workflow.id for workflow in workflows])
                )
            )
        ).all()
    )
    stage_children = [
        child
        for child in children
        if child.kind
        in {
            "storyboard_generation",
            "storyboard_review",
            "storyboard_repair",
            "video_prompt",
            "video_generation",
        }
    ]
    return {
        "boards": boards,
        "shots": shots,
        "clips": clips,
        "dialogues": dialogues,
        "lines": lines,
        "audio": audio,
        "compositions": compositions,
        "files": files,
        "tasks": tasks,
        "workflows": workflows,
        "stage_children": stage_children,
    }


def _mentions(task: AITask, chapter_id: str, board_ids: list[str], clips: list) -> bool:
    """任务是否属于本章的分镜/视频阶段。"""
    if task.request_payload and task.request_payload.get("chapter_id") == chapter_id:
        return True
    clip_ids = {clip.id for clip in clips}
    for payload in (task.request_payload or {}, task.result_payload or {}):
        for value in _walk(payload):
            if value in board_ids or value in clip_ids:
                return True
    return False


def _walk(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


async def _media_paths(found: dict) -> set[str]:
    """媒体文件路径：片段、项目文件、合成、配音，以及视频任务的连续性尾帧。"""
    media: set[str] = set()
    # 视频任务会把上一镜尾帧写到 continuity/<task_id>/，用于下一镜接续。
    # 任务记录删除后这些文件再无人引用，必须一起清掉。
    for task in found["tasks"]:
        if task.task_type == "shot_video_generation":
            media.add(f"continuity/{task.id}")
    for clip in found["clips"]:
        path = object_key_from_media_url(clip.media_url)
        if path:
            media.add(path)
    for item in found["files"]:
        if item.storage_path:
            media.add(str(item.storage_path))
    for composition in found["compositions"]:
        path = object_key_from_media_url(composition.output_url)
        if path:
            media.add(path)
    for clip in found["audio"]:
        path = object_key_from_media_url(clip.media_url)
        if path:
            media.add(path)
    return media


async def apply_cleanup(session, chapter: Chapter, found: dict) -> None:
    task_ids = [task.id for task in found["tasks"]]
    if task_ids:
        await session.execute(delete(Notification).where(Notification.task_id.in_(task_ids)))
        await session.execute(delete(AITask).where(AITask.id.in_(task_ids)))
    for child in found["stage_children"]:
        child.task_id = None
    for workflow in found["workflows"]:
        workflow.storyboard_version_id = None
        if workflow.status == DirectorWorkflowStatus.COMPLETED:
            # 分镜已被清掉，保留 COMPLETED 会让「继续」直接判定为已完成。
            workflow.status = DirectorWorkflowStatus.WAITING_USER
            workflow.stage = DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
            workflow.current_task_id = None
            workflow.last_message = "分镜与视频已清除，可从资产阶段重新生成分镜"

    if found["lines"]:
        await session.execute(
            delete(DialogueLine).where(DialogueLine.id.in_([line.id for line in found["lines"]]))
        )
    if found["audio"]:
        await session.execute(delete(AudioClip).where(AudioClip.id.in_([item.id for item in found["audio"]])))
    if found["compositions"]:
        await session.execute(
            delete(CompositionVersion).where(
                CompositionVersion.id.in_([item.id for item in found["compositions"]])
            )
        )
    if found["dialogues"]:
        await session.execute(
            delete(DialogueVersion).where(
                DialogueVersion.id.in_([item.id for item in found["dialogues"]])
            )
        )
    if found["clips"]:
        await session.execute(delete(VideoClip).where(VideoClip.id.in_([clip.id for clip in found["clips"]])))
    if found["shots"]:
        await session.execute(
            delete(StoryboardShot).where(StoryboardShot.id.in_([shot.id for shot in found["shots"]]))
        )
    if found["boards"]:
        await session.execute(
            delete(StoryboardVersion).where(
                StoryboardVersion.id.in_([board.id for board in found["boards"]])
            )
        )
    if found["files"]:
        await session.execute(
            delete(ProjectFile).where(ProjectFile.id.in_([item.id for item in found["files"]]))
        )
    chapter.status = ChapterStatus.ASSETS


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter-id", required=True, help="要清理的章节 ID")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只演练）")
    parser.add_argument("--show-media", action="store_true", help="列出将要删除的媒体文件")
    args = parser.parse_args()

    async with SessionLocal() as session:
        chapter = await _chapter(session, args.chapter_id)
        found = await collect(session, chapter)
        active = [task for task in found["tasks"] if task.status in {"QUEUED", "RUNNING"}]
        if active:
            raise SystemExit(f"章节仍有 {len(active)} 个进行中的分镜/视频任务，请先停止后再清理")

        print(f"章节：{chapter.title}")
        print(f"  分镜版本：{len(found['boards'])}（镜头 {len(found['shots'])}）")
        print(f"  视频片段：{len(found['clips'])}")
        print(f"  台词/配音/合成：{len(found['dialogues'])}/{len(found['audio'])}/{len(found['compositions'])}")
        print(f"  项目文件：{len(found['files'])}")
        print(f"  任务：{len(found['tasks'])}；导演子任务：{len(found['stage_children'])}")
        print(f"  导演流程：{len(found['workflows'])}")
        print(f"  章节状态：{chapter.status} → {ChapterStatus.ASSETS}")
        if args.show_media:
            for path in sorted(await _media_paths(found)):
                print(f"    media: {path}")
        if not args.apply:
            print("\n演练模式：未做任何修改。加 --apply 执行。")
            return

        media = await _media_paths(found)
        await apply_cleanup(session, chapter, found)
        await session.commit()
        unique = sorted(set(media))
        failures = 0
        for path in unique:
            try:
                if path.startswith("continuity/"):
                    await delete_media_prefix(path)
                else:
                    await delete_media_file(path)
            except Exception as error:  # noqa: BLE001 - 报告而不是中断清理
                failures += 1
                print(f"  文件删除失败 {path}：{error}")
        print(f"\n已清理。媒体文件 {len(unique) - failures}/{len(unique)} 个删除成功。")


if __name__ == "__main__":
    asyncio.run(main())
