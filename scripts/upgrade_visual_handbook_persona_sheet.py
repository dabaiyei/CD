"""把“左一右三”人物设定图排版同步到已安装的内置画风手册；先核对旧版并备份。

与 `upgrade_visual_handbook_four_views.py` 同样的安全策略：只更新与仓库模板逐字一致的
已安装快照。检测到任何用户编辑就报错中止，不静默覆盖；AI 生成的自定义手册不在范围内。
"""
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
from app.services.managed_skills import (
    handbook_usage_instructions,
    read_handbook_files,
    replace_handbook_files,
    validate_handbook_files,
)

TARGET_FILES = ("character.md", "character-derivative.md")


def normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    pack = ROOT / "resources/visual-handbooks/expanded-2026-09"
    entries = json.loads((pack / "catalog.json").read_text(encoding="utf-8"))
    changes = []
    async with SessionLocal() as session:
        matched = 0
        for entry in entries:
            handbook = await session.scalar(select(Handbook).where(
                Handbook.tenant_id == args.tenant_id,
                Handbook.handbook_type == HandbookType.VISUAL,
                Handbook.name == entry["name"],
            ))
            if handbook is None:
                print(f"未安装，跳过：{entry['name']}")
                continue
            matched += 1
            current = {item["filename"]: item["content"].strip() for item in read_handbook_files(handbook)}
            updated = dict(current)
            for filename in TARGET_FILES:
                desired = (pack / entry["slug"] / filename).read_text(encoding="utf-8").strip()
                if current[filename] == desired:
                    continue
                # 只接受“旧版模板”这一种差异；用户改过的内容需要人工合并。
                if normalize(current[filename]) not in _accepted_previous(entry["slug"], filename):
                    raise RuntimeError(f"检测到额外编辑，需合并后再更新：{entry['name']}/{filename}")
                updated[filename] = desired
            validate_handbook_files(HandbookType.VISUAL, updated)
            route = handbook_usage_instructions([handbook], prompt_code="asset-prompt-generation")
            assert "character.md" in route and "character-derivative.md" in route
            if updated != current:
                changes.append((handbook, current, updated))
        print(f"匹配内置手册 {matched} 套；待更新 {len(changes)} 套")
        if not args.apply or not changes:
            return

        backup = ROOT / "output/handbook-backups" / datetime.now().strftime("persona-sheet-%Y%m%d-%H%M%S-%f")
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
                actual = {item["filename"]: item["content"].strip() for item in read_handbook_files(handbook)}
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


def _accepted_previous(slug: str, filename: str) -> set[str]:
    """Return the accepted predecessor snapshots for a file.

    Imported lazily so the module stays importable without git history.
    """
    import subprocess

    revisions = ("HEAD",)
    snapshots: set[str] = set()
    for revision in revisions:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "show",
             f"{revision}:resources/visual-handbooks/expanded-2026-09/{slug}/{filename}"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout.strip():
            snapshots.add(normalize(result.stdout))
    return snapshots


if __name__ == "__main__":
    asyncio.run(main())
