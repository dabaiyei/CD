"""校验并安装导演手册扩充包，保留已有同名内容和项目选择。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import select

from app.db.models import Handbook, HandbookType, Tenant, new_id
from app.db.session import SessionLocal
from app.core.config import get_settings
from app.services.media import save_handbook_cover
from app.services.object_storage import persist_media_file
from app.services.managed_skills import (
    HANDBOOK_TASK_FILES,
    create_handbook_package,
    read_handbook_files,
    validate_handbook_files,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--attach-covers", action="store_true", help="绑定已生成封面，保留已有封面")
    args = parser.parse_args()
    pack = ROOT / "resources/director-handbooks/expanded-2026-09"
    catalog = json.loads((pack / "catalog.json").read_text(encoding="utf-8"))
    validated = []
    for entry in catalog:
        files = validate_handbook_files(HandbookType.DIRECTOR, {
            path.name: path.read_text(encoding="utf-8")
            for path in (pack / entry["slug"]).glob("*.md")
        })
        for mapping in HANDBOOK_TASK_FILES.values():
            for filename in mapping.get(HandbookType.DIRECTOR, ()):
                if filename not in files:
                    raise ValueError(f"阶段引用缺失：{entry['name']}/{filename}")
        validated.append((entry, files))
    print(f"校验通过：{len(validated)} 套 / {sum(len(f) for _, f in validated)} 文件")
    if not args.apply:
        return
    async with SessionLocal() as session:
        if await session.get(Tenant, args.tenant_id) is None:
            raise ValueError("租户不存在")
        created = 0
        for entry, files in validated:
            existing = await session.scalar(select(Handbook).where(
                Handbook.tenant_id == args.tenant_id,
                Handbook.handbook_type == HandbookType.DIRECTOR,
                Handbook.name == entry["name"],
            ))
            if existing:
                if args.attach_covers and not existing.cover_url:
                    cover = ROOT / f"output/imagegen/director-handbooks-2026-09/{entry['slug']}.png"
                    if not cover.is_file():
                        raise RuntimeError(f"封面缺失：{entry['name']}")
                    _, path = save_handbook_cover(
                        cover.read_bytes(), uploads_root=get_settings().uploads_root,
                        tenant_id=args.tenant_id, handbook_id=existing.id,
                    )
                    _, existing.cover_url = await persist_media_file(path, "image/webp")
                    existing.version += 1
                    print(f"绑定封面：{entry['name']}")
                print(f"保留：{entry['name']}")
                continue
            handbook = Handbook(
                id=new_id(), tenant_id=args.tenant_id,
                handbook_type=HandbookType.DIRECTOR,
                name=entry["name"], description=entry["description"],
                skill_path="", enabled=True, version=1,
            )
            create_handbook_package(handbook, files)
            session.add(handbook)
            await session.flush()
            actual = {f["filename"]: f["content"].strip() for f in read_handbook_files(handbook)}
            if actual != files:
                raise RuntimeError(f"写入核验失败：{entry['name']}")
            created += 1
            print(f"安装：{entry['name']}")
        await session.commit()
        all_directors = (await session.scalars(select(Handbook).where(
            Handbook.tenant_id == args.tenant_id,
            Handbook.handbook_type == HandbookType.DIRECTOR,
        ))).all()
        print(f"新增 {created} 套，当前租户共有 {len(all_directors)} 套导演手册")


if __name__ == "__main__":
    asyncio.run(main())
