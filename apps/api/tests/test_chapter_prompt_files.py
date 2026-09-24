import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import (
    Chapter,
    Project,
    ProjectFile,
    ProjectFileKind,
    ScriptVersion,
    SourceMode,
    StoryboardShot,
    StoryboardVersion,
    User,
)
from app.db.session import SessionLocal
from app.services.chapter_prompt_files import sync_chapter_prompt_files, visible_prompt_files


@pytest.fixture
def chapter_docs(client, creator_headers):
    base_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]

    async def create():
        async with SessionLocal() as session:
            base = await session.get(Project, base_id)
            project = Project(
                tenant_id=base.tenant_id,
                owner_id=base.owner_id,
                name="固定文件测试",
                aspect_ratio=base.aspect_ratio,
                image_resolution=base.image_resolution,
                image_model_id=base.image_model_id,
                video_model_id=base.video_model_id,
                video_resolution=base.video_resolution,
                visual_handbook_id=base.visual_handbook_id,
                director_handbook_id=base.director_handbook_id,
            )
            session.add(project)
            await session.flush()
            scope = dict(tenant_id=base.tenant_id, user_id=base.owner_id, project_id=project.id)
            source = ProjectFile(**scope, name="原文.txt", kind=ProjectFileKind.SOURCE, content="原文")
            session.add(source)
            await session.flush()
            chapter = Chapter(
                **scope,
                source_file_id=source.id,
                source_mode=SourceMode.SCRIPT,
                order_index=1,
                title="第一章",
                original_content="两人交锋",
            )
            session.add(chapter)
            await session.flush()
            script = ScriptVersion(
                **scope, chapter_id=chapter.id, version=1, title="剧本", content="两人交锋", is_active=True
            )
            session.add(script)
            await session.flush()
            chapter.active_script_version_id = script.id
            board = StoryboardVersion(
                **scope,
                chapter_id=chapter.id,
                script_version_id=script.id,
                version=1,
                is_active=True,
                content=[],
            )
            session.add(board)
            await session.flush()
            shots = [
                StoryboardShot(
                    **scope,
                    chapter_id=chapter.id,
                    storyboard_version_id=board.id,
                    order_index=index,
                    title=f"镜头{index}",
                    duration_seconds=5,
                    image_prompt=f"图片{index}",
                    video_prompt=f"视频{index}",
                )
                for index in (1, 2)
            ]
            session.add_all(shots)
            await session.flush()
            files = await sync_chapter_prompt_files(session, board)
            await session.commit()
            return dict(
                project=project.id,
                chapter=chapter.id,
                board=board.id,
                user=base.owner_id,
                shots=[shot.id for shot in shots],
                files=[item.id for item in files],
            )

    identity = asyncio.run(create())
    yield identity
    assert (
        client.delete(f"/api/v1/projects/{identity['project']}", headers=creator_headers).status_code == 204
    )


def test_repeated_saves_reuse_both_ids_and_preserve_sibling_prompts(client, chapter_docs):
    async def exercise():
        async with SessionLocal() as session:
            board = await session.get(StoryboardVersion, chapter_docs["board"])
            for index in range(3):
                shot = await session.get(StoryboardShot, chapter_docs["shots"][0])
                shot.video_prompt = f"修正{index}"
                files = await sync_chapter_prompt_files(session, board)
                assert [item.id for item in files] == chapter_docs["files"]
                await session.commit()
            prompts = json.loads(files[1].content)["shots"]
            assert [row["video_prompt"] for row in prompts] == ["修正2", "视频2"]
            assert "video_prompt" not in json.loads(files[0].content)["shots"][0]
            assert board.content[0]["video_prompt"] == "修正2"
            assert (
                len(
                    (
                        await session.scalars(
                            select(ProjectFile).where(
                                ProjectFile.project_id == chapter_docs["project"],
                                visible_prompt_files(),
                                ProjectFile.kind != ProjectFileKind.SOURCE,
                            )
                        )
                    ).all()
                )
                == 2
            )

    asyncio.run(exercise())


def test_legacy_copies_disappear_from_listing_and_ai_context(client, creator_headers, chapter_docs):
    async def legacy():
        async with SessionLocal() as session:
            board = await session.get(StoryboardVersion, chapter_docs["board"])
            copy = ProjectFile(
                tenant_id=board.tenant_id,
                user_id=board.user_id,
                project_id=board.project_id,
                name="第一章-分镜修复-v7.json",
                kind=ProjectFileKind.STORYBOARD,
                content='{"old":true}',
                file_metadata={"chapter_id": board.chapter_id, "storyboard_version_id": board.id},
            )
            session.add(copy)
            await session.commit()
            return copy.id

    obsolete = asyncio.run(legacy())
    prefix = f"/api/v1/projects/{chapter_docs['project']}/files"
    listing = client.get(prefix, headers=creator_headers)
    assert listing.status_code == 200
    assert obsolete not in [item["id"] for item in listing.json()]
    assert len(listing.json()) == 3

    async def snapshots():
        from app.api.routes.agent_chat import project_file_snapshots

        async with SessionLocal() as session:
            user = await session.get(User, chapter_docs["user"])
            result = await project_file_snapshots(
                session, user, chapter_docs["project"], chapter_id=chapter_docs["chapter"]
            )
            assert {item.id for item in result} == set(chapter_docs["files"])
            archived = await session.get(ProjectFile, obsolete)
            assert archived.content == '{"old":true}'
            assert not archived.editable

    asyncio.run(snapshots())


def test_manual_file_edit_updates_one_real_shot_and_round_trips(client, creator_headers, chapter_docs):
    prefix = f"/api/v1/projects/{chapter_docs['project']}/files"
    document = client.get(f"{prefix}/{chapter_docs['files'][0]}", headers=creator_headers).json()
    row = json.loads(document["content"])["shots"][0]
    patch = json.dumps({"shots": [{"shot_id": row["shot_id"], "image_prompt": "新的构图"}]})
    saved = client.put(f"{prefix}/{document['id']}", headers=creator_headers, json={"content": patch})
    assert saved.status_code == 200, saved.text
    rows = json.loads(saved.json()["content"])["shots"]
    assert len(rows) == 2
    assert rows[0]["image_prompt"] == "新的构图"
    assert rows[1]["image_prompt"] == "图片2"
    assert saved.json()["size_bytes"] == len(saved.json()["content"].encode())

    async def check():
        async with SessionLocal() as session:
            shots = [await session.get(StoryboardShot, identity) for identity in chapter_docs["shots"]]
            assert [shot.version for shot in shots] == [2, 1]
            assert [shot.video_prompt for shot in shots] == ["视频1", "视频2"]

    asyncio.run(check())


def test_ai_edit_updates_prompt_and_stale_edit_is_rejected(client, chapter_docs):
    from app.api.routes.agent_chat import apply_project_file_changes, project_file_snapshots
    from app.services.agent_runtime import AgentRuntimeProjectFileChange

    async def exercise():
        async with SessionLocal() as session:
            user = await session.get(User, chapter_docs["user"])
            project = await session.get(Project, chapter_docs["project"])
            snapshots = await project_file_snapshots(session, user, project.id)
            snapshot = next(item for item in snapshots if item.id == chapter_docs["files"][1])
            change = AgentRuntimeProjectFileChange(
                operation="update",
                file_id=snapshot.id,
                base_sha256=snapshot.sha256,
                content=json.dumps(
                    {"shots": [{"shot_id": chapter_docs["shots"][1], "video_prompt": "高速跟拍"}]}
                ),
            )
            args = dict(
                db=session,
                user=user,
                project=project,
                chat_session=SimpleNamespace(id="test"),
                run_id="test",
                snapshots=snapshots,
                changes=[change],
            )
            result = await apply_project_file_changes(**args)
            assert result[0]["status"] == "applied", result
            await session.commit()
            shot = await session.get(StoryboardShot, chapter_docs["shots"][1])
            assert shot.video_prompt == "高速跟拍"
            result = await apply_project_file_changes(**args)
            assert result[0]["status"] == "conflict"
            assert shot.version == 2

    asyncio.run(exercise())


def test_invalid_or_foreign_shot_edit_is_atomic(client, creator_headers, admin_headers, chapter_docs):
    url = f"/api/v1/projects/{chapter_docs['project']}/files/{chapter_docs['files'][1]}"
    before = client.get(url, headers=creator_headers).json()["content"]
    for bad in (
        "not-json",
        json.dumps(
            {
                "shots": [
                    {"shot_id": chapter_docs["shots"][0], "video_prompt": "不能保存"},
                    {"shot_id": "another-users-shot", "video_prompt": "不能覆盖"},
                ]
            }
        ),
    ):
        result = client.put(url, headers=creator_headers, json={"content": bad})
        assert result.status_code == 422, result.text
        assert client.get(url, headers=creator_headers).json()["content"] == before
    assert client.put(url, headers=admin_headers, json={"content": before}).status_code == 404


def test_new_board_replaces_same_files_without_old_shot_leakage(client, chapter_docs):
    async def exercise():
        from app.api.routes.storyboards import create_storyboard
        from app.domain.schemas import StoryboardShotCreate

        async with SessionLocal() as session:
            chapter = await session.get(Chapter, chapter_docs["chapter"])
            script = await session.get(ScriptVersion, chapter.active_script_version_id)
            user = await session.get(User, chapter_docs["user"])
            board, shots = await create_storyboard(
                session,
                user=user,
                chapter=chapter,
                script=script,
                shots=[StoryboardShotCreate(title="新的分镜", image_prompt="新画面")],
            )
            files = await sync_chapter_prompt_files(session, board)
            assert [item.id for item in files] == chapter_docs["files"]
            assert json.loads(files[0].content)["shots"][0]["shot_id"] == shots[0].id
            assert len(json.loads(files[1].content)["shots"]) == 1
            assert json.loads(files[1].content)["shots"][0]["video_prompt"] == ""
            assert all("-v" not in item.name for item in files)
            await session.commit()

    asyncio.run(exercise())


def test_ai_failure_after_first_shot_flush_rolls_back_all_edits(client, chapter_docs):
    from app.api.routes.agent_chat import apply_project_file_changes, project_file_snapshots
    from app.services.agent_runtime import AgentRuntimeProjectFileChange

    async def exercise():
        async with SessionLocal() as session:
            user = await session.get(User, chapter_docs["user"])
            project = await session.get(Project, chapter_docs["project"])
            snapshots = await project_file_snapshots(session, user, project.id)
            snapshot = next(item for item in snapshots if item.id == chapter_docs["files"][0])
            changes = [
                AgentRuntimeProjectFileChange(
                    operation="update",
                    file_id=snapshot.id,
                    base_sha256=snapshot.sha256,
                    content=json.dumps(
                        {
                            "shots": [
                                {"shot_id": chapter_docs["shots"][0], "image_prompt": "第一条先写入"},
                                {
                                    "shot_id": chapter_docs["shots"][1],
                                    "asset_ids": ["not-an-authorized-asset"],
                                },
                            ]
                        }
                    ),
                )
            ]
            result = await apply_project_file_changes(
                db=session,
                user=user,
                project=project,
                chat_session=SimpleNamespace(id="test"),
                run_id="test",
                snapshots=snapshots,
                changes=changes,
            )
            assert result[0]["status"] == "rejected", result
            await session.commit()
        async with SessionLocal() as session:
            first = await session.get(StoryboardShot, chapter_docs["shots"][0])
            assert first.image_prompt == "图片1"
            assert first.version == 1
            document = await session.get(ProjectFile, snapshot.id)
            assert document.content == snapshot.content

    asyncio.run(exercise())


def test_ai_cannot_create_numbered_copies_of_existing_documents(client, chapter_docs):
    from app.api.routes.agent_chat import apply_project_file_changes, project_file_snapshots
    from app.services.agent_runtime import AgentRuntimeProjectFileChange

    async def exercise():
        async with SessionLocal() as session:
            user = await session.get(User, chapter_docs["user"])
            project = await session.get(Project, chapter_docs["project"])
            chapter = await session.get(Chapter, chapter_docs["chapter"])
            snapshots = await project_file_snapshots(session, user, project.id)
            copies = [
                AgentRuntimeProjectFileChange(operation="create", name=name, content="{}")
                for name in ("第一章-分镜修复-v8.json", "video-prompts-v2.json")
            ]
            result = await apply_project_file_changes(
                db=session,
                user=user,
                project=project,
                chapter=chapter,
                chat_session=SimpleNamespace(id="test"),
                run_id="test",
                snapshots=snapshots,
                changes=copies,
            )
            assert all(item["status"] == "rejected" for item in result)
            assert len(result) == 2

    asyncio.run(exercise())


def test_migrate_legacy_only_project_uses_current_board_not_old_file_text(
    client, creator_headers, chapter_docs
):
    async def legacy_only():
        async with SessionLocal() as session:
            for identity in chapter_docs["files"]:
                await session.delete(await session.get(ProjectFile, identity))
            board = await session.get(StoryboardVersion, chapter_docs["board"])
            old = ProjectFile(
                tenant_id=board.tenant_id,
                user_id=board.user_id,
                project_id=board.project_id,
                name="第一章-分镜表-v1.json",
                kind=ProjectFileKind.STORYBOARD,
                content='{"shots":[]}',
                file_metadata={"chapter_id": board.chapter_id, "storyboard_version_id": board.id},
            )
            session.add(old)
            await session.commit()
            return old.id

    old_id = asyncio.run(legacy_only())
    prefix = f"/api/v1/projects/{chapter_docs['project']}/files"
    listing = client.get(prefix, headers=creator_headers).json()
    assert len(listing) == 3
    assert any(item["id"] == old_id and item["name"] == "第一章-分镜表.json" for item in listing)
    document = client.get(f"{prefix}/{old_id}", headers=creator_headers).json()
    assert len(json.loads(document["content"])["shots"]) == 2
    assert {item["id"] for item in client.get(prefix, headers=creator_headers).json()} == {
        item["id"] for item in listing
    }
