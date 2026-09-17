"""将扩充画风手册的人物四视图补丁同步到已安装手册；先核对旧版并备份。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from sqlalchemy import select
from app.db.models import Handbook, HandbookType
from app.db.session import SessionLocal
from app.services.managed_skills import read_handbook_files, replace_handbook_files, validate_handbook_files, handbook_usage_instructions

OLD_BASE = "按项目画幅呈现完整身体、头脚与必要衣饰，透视自然，双手与肢体结构清楚。背景简洁且保留接地关系，不自动加文字、多视图排版或水印。需要多角度时按平台任务分别生成。"
OLD_DERIVATIVE = "先声明引用父资产与保持项，再说明本次改变项、姿态和镜头可见细节。画风统一但不复制基础图的固定姿势。生成图用于当前衍生资产，不另建同名基础人物。"


def previous_content(filename: str, content: str) -> str:
    if filename == "character.md":
        start = content.index("## 基础资产\n") + len("## 基础资产\n")
        end = content.index("\n## 输出\n", start)
        return (content[:start] + OLD_BASE + "\n" + content[end:]).strip()
    start = content.index("## 输出方式\n") + len("## 输出方式\n")
    return (content[:start] + OLD_DERIVATIVE).strip()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    pack = ROOT / "resources/visual-handbooks/expanded-2026-09"
    entries = json.loads((pack / "catalog.json").read_text(encoding="utf-8"))
    changes = []
    async with SessionLocal() as session:
        for entry in entries:
            handbook = await session.scalar(select(Handbook).where(
                Handbook.tenant_id == args.tenant_id,
                Handbook.handbook_type == HandbookType.VISUAL,
                Handbook.name == entry["name"],
            ))
            if not handbook:
                raise RuntimeError(f"未安装：{entry['name']}")
            current = {f["filename"]: f["content"].strip() for f in read_handbook_files(handbook)}
            updated = dict(current)
            for filename in ("character.md", "character-derivative.md"):
                desired = (pack / entry["slug"] / filename).read_text(encoding="utf-8").strip()
                if current[filename] == desired:
                    continue
                # Ignore blank-line spacing only; any user-authored text needs an explicit merge.
                normalize = lambda text: "\n".join(line.strip() for line in text.splitlines() if line.strip())
                if normalize(current[filename]) != normalize(previous_content(filename, desired)):
                    raise RuntimeError(f"检测到额外编辑，需合并后再更新：{entry['name']}/{filename}")
                updated[filename] = desired
            validate_handbook_files(HandbookType.VISUAL, updated)
            route = handbook_usage_instructions([handbook], prompt_code="asset-prompt-generation")
            assert "character.md" in route and "character-derivative.md" in route
            if updated != current:
                changes.append((handbook, current, updated))
        print(f"校验通过：8套手册、人物与衍生阶段引用；待更新 {len(changes)} 套")
        if not args.apply or not changes:
            return
        backup = ROOT / "output/handbook-backups" / datetime.now().strftime("four-views-%Y%m%d-%H%M%S-%f")
        backup.mkdir(parents=True)
        for handbook, current, _ in changes:
            (backup / f"{handbook.id}.json").write_text(json.dumps({
                "name": handbook.name, "version": handbook.version,
                "skill_path": handbook.skill_path, "files": current,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            for handbook, _, updated in changes:
                replace_handbook_files(handbook, updated)
                handbook.version += 1
                actual = {f["filename"]: f["content"].strip() for f in read_handbook_files(handbook)}
                assert actual == updated
                print(f"已更新并回读核验：{handbook.name} v{handbook.version}")
            await session.commit()
        except Exception:
            await session.rollback()
            for path in backup.glob("*.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                replace_handbook_files(SimpleNamespace(
                    handbook_type=HandbookType.VISUAL, skill_path=saved["skill_path"],
                ), saved["files"])
            raise
        print(f"备份：{backup}")


if __name__ == "__main__":
    asyncio.run(main())
