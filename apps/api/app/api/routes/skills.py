from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_admin
from app.db.models import Handbook, PromptTemplate, User
from app.db.session import get_session
from app.domain.schemas import SkillFilePublic, SkillFileSave, SkillTreeNode
from app.services.managed_skills import (
    ALLOWED_SKILL_EXTENSIONS,
    SYSTEM_PROMPT_CODES,
    ensure_handbook_package,
    ensure_prompt_file,
    handbook_manifest,
    prompt_public_path,
    resolve_tenant_skill_path,
    stored_to_public_path,
    tenant_skills_root,
)

router = APIRouter(prefix="/admin/skills", tags=["admin-skills"])


def safe_skill_path(tenant_id: str, relative_path: str) -> Path:
    try:
        return resolve_tenant_skill_path(tenant_id, relative_path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Skills 文件不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def build_tree(directory: Path, root: Path) -> list[SkillTreeNode]:
    nodes: list[SkillTreeNode] = []
    preferred_root_order = {
        "visual-handbooks": 0,
        "director-handbooks": 1,
        "system-prompts": 2,
    }
    for entry in sorted(
        directory.iterdir(),
        key=lambda item: (
            item.is_file(),
            preferred_root_order.get(item.name, 99) if directory == root else 0,
            item.name.lower(),
        ),
    ):
        if entry.is_symlink():
            continue
        relative = entry.relative_to(root).as_posix()
        if entry.is_dir():
            children = build_tree(entry, root)
            if children:
                nodes.append(
                    SkillTreeNode(
                        name=entry.name,
                        path=relative,
                        kind="directory",
                        children=children,
                    )
                )
        elif entry.suffix.lower() in ALLOWED_SKILL_EXTENSIONS:
            if directory.name == "system-prompts" and entry.stem not in SYSTEM_PROMPT_CODES:
                continue
            nodes.append(SkillTreeNode(name=entry.name, path=relative, kind="file"))
    return nodes


async def ensure_tenant_managed_files(session: AsyncSession, tenant_id: str) -> None:
    handbooks = (
        await session.scalars(select(Handbook).where(Handbook.tenant_id == tenant_id))
    ).all()
    prompts = (
        await session.scalars(select(PromptTemplate).where(PromptTemplate.tenant_id == tenant_id))
    ).all()
    for handbook in handbooks:
        await run_in_threadpool(ensure_handbook_package, handbook)
    for prompt in prompts:
        if prompt.code not in SYSTEM_PROMPT_CODES:
            continue
        await run_in_threadpool(ensure_prompt_file, prompt)
    await session.commit()


@router.get("/tree", response_model=list[SkillTreeNode])
async def skill_tree(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[SkillTreeNode]:
    await ensure_tenant_managed_files(session, admin.tenant_id)
    root = tenant_skills_root(admin.tenant_id)
    return await run_in_threadpool(build_tree, root, root)


@router.get("/file", response_model=SkillFilePublic)
async def read_skill_file(
    path: str,
    admin: User = Depends(require_admin),
) -> SkillFilePublic:
    file_path = safe_skill_path(admin.tenant_id, path)
    try:
        content = await run_in_threadpool(file_path.read_text, "utf-8")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=400, detail="文件不是有效的 UTF-8 文本") from error
    return SkillFilePublic(path=path, content=content)


def write_skill_file(file_path: Path, content: str) -> None:
    temporary = file_path.parent / f".{file_path.name}.tmp"
    temporary.write_text(f"{content.rstrip()}\n", encoding="utf-8", newline="\n")
    temporary.replace(file_path)


@router.put("/file", response_model=SkillFilePublic)
async def save_skill_file(
    path: str,
    payload: SkillFileSave,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SkillFilePublic:
    file_path = safe_skill_path(admin.tenant_id, path)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="受管 Skills 文件不能为空")

    prompt_code = Path(path).stem if path.startswith("system-prompts/") else None
    prompt = None
    if (
        prompt_code is not None
        and prompt_code in SYSTEM_PROMPT_CODES
        and prompt_public_path(prompt_code) == path
    ):
        prompt = await session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.tenant_id == admin.tenant_id,
                PromptTemplate.code == prompt_code,
            )
        )
    if path.startswith("system-prompts/") and prompt is None:
        raise HTTPException(status_code=409, detail="系统提示词目录不允许新增或重命名文件")

    handbook = None
    if path.startswith(("visual-handbooks/", "director-handbooks/")):
        package_path = Path(path).parent.as_posix()
        handbooks = (
            await session.scalars(select(Handbook).where(Handbook.tenant_id == admin.tenant_id))
        ).all()
        handbook = next(
            (
                item
                for item in handbooks
                if stored_to_public_path(admin.tenant_id, item.skill_path) == package_path
            ),
            None,
        )
        if handbook is None:
            raise HTTPException(status_code=409, detail="创作手册目录不允许新增或重命名文件")
        allowed_names = {item.filename for item in handbook_manifest(handbook.handbook_type)}
        if Path(path).name not in allowed_names:
            raise HTTPException(status_code=409, detail="创作手册只能编辑固定文件")

    await run_in_threadpool(write_skill_file, file_path, content)
    if prompt is not None:
        prompt.content = content
        prompt.version += 1
    if handbook is not None:
        handbook.version += 1
    await session.commit()
    return SkillFilePublic(path=path, content=f"{content}\n")
