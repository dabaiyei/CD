from __future__ import annotations

import json

from app.db.models import Chapter, Project, ProjectFileKind, SourceMode, TaskStatus
from app.db.session import SessionLocal
from app.services.ai_creation import (
    ChapterPremise,
    OutlinePlan,
    OutlineResult,
    ProposalResult,
    save_creation_file,
)
from app.services.task_events import publish_task_event, record_task_event


def creation_inputs(payload: dict) -> dict:
    """Keep persistence/checkpoints and old proposals out of model input."""
    preferences = dict(payload["preferences"])
    preferences["feedback"] = str(preferences.get("feedback") or "")[:2000]
    selected = payload.get("selected") or {}
    return {
        "preferences": preferences,
        "selected": {
            key: str(selected.get(key) or "")[:2000] for key in ("title", "introduction", "premise")
        },
        "feedback_history": [str(value)[:500] for value in payload.get("feedback_history", [])[-4:]],
    }


async def execute_ai_creation_task(task_id: str, runtime_factory) -> None:
    # Imported at execution time to keep the worker dispatcher acyclic.
    from app.services.task_worker import (
        owned_task_for_update,
        owns_running_task,
        parse_json_object,
        record_progress,
        runtime_request,
    )

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        project = await session.get(Project, task.project_id)
        if project is None or project.creation_state.get("task_id") != task.id:
            raise RuntimeError("此创作任务已被新的创作选择替代")
        payload = dict(task.request_payload)
        phase = payload["phase"]
        preferences = payload["preferences"]
        checkpoint = dict(
            (task.result_payload or {}).get("creation_checkpoint") or payload.get("creation_checkpoint") or {}
        )
    if phase == "proposals":
        schema = ProposalResult.model_json_schema()
        instruction = (
            "你正在协助用户从零创作原创短剧。依据用户类型、章节数量及成片时长，"
            "给出3至5个差异明显、能够持续展开的剧名、简介及故事梗概。"
            "认真吸收用户的反馈，避免重复被否决的提案。现在不要生成正式剧本。"
            "历史意见继续适用，若与最新明确意见矛盾，以最新意见为准。"
        )
    else:
        schema = OutlinePlan.model_json_schema()
        instruction = (
            "用户已经选定故事，现在制定完整剧本大纲及世界观人物设定，"
            f"按叙事顺序在 chapter_titles 中生成恰好 {preferences['chapter_count']} 章的标题。"
            "只规划精简大纲和章名目录，不逐章生成内容。大纲控制在3000字内，设定控制在3000字内。"
            "story_bible 作为后续所有创作的记忆与设定文档，明确人物身份关系、"
            "世界规则、时间线、伏笔、禁忌与连续性事项。此阶段只规划，不生成正式剧本。"
        )
    await record_progress(
        task_id, 15, "正在构思剧集提案" if phase == "proposals" else "正在规划故事与全部章节"
    )

    async def generate(instruction_text: str, schema_value: dict, input_value: dict):
        request = await runtime_request(
            task_id,
            prompt_code="project-ai-creation",
            prompt=instruction_text
            + "\n输入："
            + json.dumps(input_value, ensure_ascii=False)
            + "\n严格按此 JSON Schema 返回："
            + json.dumps(schema_value, ensure_ascii=False),
        )
        return await runtime_factory().run(request)

    async def persist_checkpoint(value: dict) -> None:
        async with SessionLocal() as session:
            current = await owned_task_for_update(session, task_id)
            if not owns_running_task(current):
                raise RuntimeError("创作任务已停止")
            current.result_payload = {**(current.result_payload or {}), "creation_checkpoint": value}
            project = await session.get(Project, current.project_id)
            if project is None or project.creation_state.get("task_id") != current.id:
                raise RuntimeError("创作状态已更新")
            project.creation_state = {**project.creation_state, "checkpoint": value}
            await session.commit()

    manifest = checkpoint.get("manifest", {})
    if phase == "proposals":
        result = await generate(instruction, schema, creation_inputs(payload))
        parsed = ProposalResult.model_validate(parse_json_object(result.final_response))
        manifest = result.manifest
    else:
        if not checkpoint.get("plan"):
            result = await generate(instruction, schema, creation_inputs(payload))
            plan = OutlinePlan.model_validate(parse_json_object(result.final_response))
            if len(plan.chapter_titles) != preferences["chapter_count"]:
                raise RuntimeError("AI 返回的章节数量与已确认数量不一致，请重试章节规划")
            if any(not title.strip() or len(title) > 255 for title in plan.chapter_titles):
                raise RuntimeError("AI 返回了空章节标题或过长标题")
            checkpoint = {"plan": plan.model_dump(), "chapters": [], "manifest": result.manifest}
            await persist_checkpoint(checkpoint)
        plan = OutlinePlan.model_validate(checkpoint["plan"])
        # Preserve old checkpoints without eagerly expanding the remaining chapters.
        saved = {item["title"]: item for item in checkpoint.get("chapters", [])}
        chapters = [
            saved.get(title)
            or ChapterPremise(
                title=title,
                description=f"第{index}章《{title}》：尚未创作。请依据项目大纲、设定和前章记忆，仅创作本章。",
            ).model_dump()
            for index, title in enumerate(plan.chapter_titles, 1)
        ]
        parsed = OutlineResult(outline=plan.outline, story_bible=plan.story_bible, chapters=chapters)
    await record_progress(task_id, 85, "正在保存创作成果")
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        project = await session.get(Project, task.project_id)
        if project is None or project.creation_state.get("task_id") != task.id:
            raise RuntimeError("项目创作状态已变化，旧结果未覆盖当前项目")
        state = dict(project.creation_state)
        history = list(state.get("messages", []))
        history.append({"role": "assistant", "content": parsed.model_dump()})
        state.update(messages=history, revision=state.get("revision", 0) + 1)
        if isinstance(parsed, ProposalResult):
            state.update(phase="choosing", proposals=[item.model_dump() for item in parsed.proposals])
        else:
            selected = payload["selected"]
            project.name = selected["title"]
            project.description = selected["introduction"]
            await save_creation_file(
                session, project, "故事大纲.md", parsed.outline, ProjectFileKind.ANALYSIS
            )
            await save_creation_file(
                session, project, "创作记忆与设定.md", parsed.story_bible, ProjectFileKind.MEMORY
            )
            for index, chapter in enumerate(parsed.chapters, start=1):
                # One source file per chapter preserves chapter deletion/redo boundaries.
                source = await save_creation_file(
                    session,
                    project,
                    f"第{index:03d}章-创作基础.md",
                    f"# {chapter.title}\n\n{chapter.description}",
                    ProjectFileKind.SOURCE,
                )
                session.add(
                    Chapter(
                        tenant_id=project.tenant_id,
                        user_id=project.owner_id,
                        project_id=project.id,
                        source_file_id=source.id,
                        source_mode=SourceMode.SCRIPT,
                        order_index=index,
                        title=chapter.title,
                        original_content=chapter.description,
                    )
                )
            await save_creation_file(
                session,
                project,
                "章节列表.json",
                json.dumps(
                    [item.model_dump() for item in parsed.chapters],
                    ensure_ascii=False,
                    indent=2,
                ),
                ProjectFileKind.ANALYSIS,
            )
            state.update(phase="ready", chapter_count=len(parsed.chapters))
            state.pop("checkpoint", None)
        project.creation_state = state
        await save_creation_file(
            session, project, "AI创作对话记录.json", json.dumps(state, ensure_ascii=False, indent=2)
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {"phase": state["phase"], "runtime_manifest": manifest}
        event = record_task_event(
            session,
            task,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            message="剧集提案已生成，请选择" if phase == "proposals" else "故事大纲与章节已创建",
        )
        await session.commit()
        await publish_task_event(task, event)
