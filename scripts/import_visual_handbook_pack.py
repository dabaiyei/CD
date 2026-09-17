"""Validate and explicitly install the curated visual handbook pack (no API credentials)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Handbook, HandbookType, Tenant, new_id
from app.db.session import SessionLocal
from app.services.managed_skills import create_handbook_package, read_handbook_files, validate_handbook_files
from app.services.media import save_handbook_cover
from app.services.object_storage import persist_media_file


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Install; otherwise only validate source files")
    parser.add_argument("--allow-missing-covers", action="store_true", help="Install content while the image API is unavailable")
    args = parser.parse_args()
    pack = ROOT / "resources/visual-handbooks/expanded-2026-09"
    covers = ROOT / "output/imagegen/visual-handbooks-2026-09"
    catalog = json.loads((pack / "catalog.json").read_text(encoding="utf-8"))
    validated = []
    for entry in catalog:
        folder = pack / entry["slug"]
        files = validate_handbook_files(HandbookType.VISUAL, {
            path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.md")
        })
        cover = covers / f"{entry['slug']}.png"
        if args.apply and not args.allow_missing_covers and not cover.is_file():
            raise RuntimeError(f"Missing generated cover: {cover.name}")
        validated.append((entry, files, cover))
    print(f"Validated {len(validated)} handbooks / {sum(len(files) for _, files, _ in validated)} files")
    if not args.apply:
        return
    async with SessionLocal() as session:
        if await session.get(Tenant, args.tenant_id) is None:
            raise RuntimeError("Tenant not found")
        created = 0
        for entry, files, cover in validated:
            existing = await session.scalar(select(Handbook).where(
                Handbook.tenant_id == args.tenant_id,
                Handbook.handbook_type == HandbookType.VISUAL,
                Handbook.name == entry["name"],
            ))
            if existing:
                if not existing.cover_url and cover.is_file():
                    _, path = save_handbook_cover(
                        cover.read_bytes(), uploads_root=get_settings().uploads_root,
                        tenant_id=args.tenant_id, handbook_id=existing.id,
                    )
                    _, existing.cover_url = await persist_media_file(path, "image/webp")
                    existing.version += 1
                print(f"Preserved existing handbook: {entry['name']}")
                continue
            handbook = Handbook(
                id=new_id(), tenant_id=args.tenant_id, handbook_type=HandbookType.VISUAL,
                name=entry["name"], description=f"{entry['medium']}。{entry['palette']}。适合{entry['kind']}短剧；角色妆造、材质、空间与连续动作配套。",
                skill_path="", enabled=True, version=1,
            )
            create_handbook_package(handbook, files)
            if cover.is_file():
                _, path = save_handbook_cover(
                    cover.read_bytes(), uploads_root=get_settings().uploads_root,
                    tenant_id=args.tenant_id, handbook_id=handbook.id,
                )
                _, handbook.cover_url = await persist_media_file(path, "image/webp")
            session.add(handbook)
            await session.flush()
            assert len(read_handbook_files(handbook)) == 12
            created += 1
            print(f"Installed: {entry['name']} / {handbook.id}")
        await session.commit()
        print(f"Installed {created}; preserved {len(validated) - created}")


if __name__ == "__main__":
    asyncio.run(main())
