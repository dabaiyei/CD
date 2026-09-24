import asyncio
import hashlib
from types import SimpleNamespace

from sqlalchemy import select

from app.api.routes.agent_chat import project_file_snapshots
from app.db.models import ChapterStatus, Project, ProjectFile, ProjectFileKind, SourceMode, User
from app.db.session import SessionLocal
from app.services.agent_file_context import SNAPSHOT_PART_CHARS, paged_snapshot
from app.services.agent_runtime import AgentRuntimeProjectFileSnapshot
from app.services.task_worker import director_chapter_snapshots


def test_long_chapter_is_lossless_readonly_parts_not_a_size_error():
    original = "前半段人物与场景。" * 150_000 + "末尾关键反转。"
    chapter = SimpleNamespace(
        id="chapter-1",
        title="长章节",
        source_mode=SourceMode.NOVEL,
        status=ChapterStatus.UNINITIALIZED,
        original_content=original,
    )
    snapshots = director_chapter_snapshots(chapter, None)
    parts = [item for item in snapshots if "-part-" in item.id]
    assert "".join(item.content for item in parts) == f"# 长章节 · 原文\n\n{original}\n"
    assert len(snapshots) < 200
    assert all(len(item.content) <= SNAPSHOT_PART_CHARS and not item.editable for item in snapshots)
    assert all(item.sha256 == hashlib.sha256(item.content.encode()).hexdigest() for item in snapshots)
    assert all(item.path.split("/")[1] == item.directory_id for item in parts)
    index = next(item for item in snapshots if item.id == "current-chapter-original")
    assert parts[-1].path in index.content
    assert original[-8:] not in index.content


def test_short_editable_file_keeps_identity_and_edit_authority():
    snapshot = AgentRuntimeProjectFileSnapshot(
        id="file-1",
        name="memory.md",
        kind="memory",
        path="project-files/file-1/memory.md",
        content="角色记忆",
        sha256=hashlib.sha256("角色记忆".encode()).hexdigest(),
        editable=True,
    )
    assert paged_snapshot(snapshot) == [snapshot]
    assert paged_snapshot(snapshot)[0].editable


def test_large_editable_source_becomes_readonly_index_not_an_overwrite():
    snapshot = AgentRuntimeProjectFileSnapshot(
        id="file-1",
        name="novel.txt",
        kind="source",
        path="project-files/file-1/novel.txt",
        content="x" * (SNAPSHOT_PART_CHARS + 1),
        sha256="a" * 64,
        editable=True,
    )
    parts = paged_snapshot(snapshot)
    assert len(parts) == 3
    assert all(not item.editable for item in parts)
    assert parts[0].id == snapshot.id


def test_project_context_scopes_chapter_and_bounds_history_without_rejection(client, creator_headers):
    project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def verify():
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            user = await session.get(User, project.owner_id)
            shared = dict(
                tenant_id=user.tenant_id,
                user_id=user.id,
                project_id=project_id,
                mime_type="text/plain",
                editable=True,
            )
            source = ProjectFile(
                **shared,
                name="very-long-source.txt",
                kind=ProjectFileKind.SOURCE,
                content="整本小说" * 600_000,
                size_bytes=7_200_000,
            )
            current = ProjectFile(
                **shared,
                name="current-analysis.md",
                kind=ProjectFileKind.ANALYSIS,
                content="本章分析",
                size_bytes=12,
                file_metadata={"chapter_id": "current"},
            )
            other = ProjectFile(
                **shared,
                name="other-analysis.md",
                kind=ProjectFileKind.ANALYSIS,
                content="其他章",
                size_bytes=9,
                file_metadata={"chapter_id": "other"},
            )
            session.add_all([source, current, other])
            await session.flush()
            snapshots = await project_file_snapshots(session, user, project_id, chapter_id="current")
            ids = {item.id for item in snapshots}
            assert current.id in ids and source.id not in ids and other.id not in ids
            for index in range(205):
                session.add(
                    ProjectFile(
                        **shared,
                        name=f"history-{index}.md",
                        kind=ProjectFileKind.OTHER,
                        content="history",
                        size_bytes=7,
                    )
                )
            await session.flush()
            bounded = await project_file_snapshots(
                session, user, project_id, chapter_id="current", max_files=8
            )
            assert len(bounded) <= 8
            assert any(item.id == "project-context-index" for item in bounded)
            # Cross-account access must remain empty even when selecting a chapter.
            another_user = await session.scalar(select(User).where(User.id != user.id))
            assert await project_file_snapshots(session, another_user, project_id, chapter_id="current") == []
            await session.rollback()

    asyncio.run(verify())
