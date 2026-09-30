"""Repair earlier heuristic substitutions using a pre-sync backup and named history.

Dry-run by default. Does not restore whole database rows or media.
"""

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from app.db.models import AITask, Asset, StoryboardShot, StoryboardVersion, TaskStatus
from app.db.session import SessionLocal
from app.services.asset_identity import asset_name_key
from app.services.chapter_prompt_files import sync_chapter_prompt_files
from app.services.director_orchestration import ensure_chapter_not_automating
from app.services.storyboard_reference_sync import (
    historical_asset_names,
    sync_asset_images,
)
from sqlalchemy import select


async def main(args):
    with sqlite3.connect(
        f"file:{args.before.resolve().as_posix()}?mode=ro", uri=True
    ) as backup:
        backup.row_factory = sqlite3.Row
        originals = backup.execute(
            "select * from storyboard_shots where storyboard_version_id=?",
            (args.board,),
        ).fetchall()
    async with SessionLocal() as session:
        board = await session.get(StoryboardVersion, args.board)
        assert board and board.is_active
        await ensure_chapter_not_automating(
            session, chapter_id=board.chapter_id, user_id=board.user_id
        )
        pending = (
            await session.scalars(
                select(AITask).where(
                    AITask.project_id == board.project_id,
                    AITask.user_id == board.user_id,
                    AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                )
            )
        ).all()
        assert not any(
            (t.request_payload or {}).get("chapter_id") == board.chapter_id
            for t in pending
        )
        history = await historical_asset_names(session, board)
        assets = (
            await session.scalars(
                select(Asset).where(
                    Asset.project_id == board.project_id,
                    Asset.user_id == board.user_id,
                    Asset.tenant_id == board.tenant_id,
                )
            )
        ).all()
        names = {}
        for asset in assets:
            names.setdefault(asset_name_key(asset.name), []).append(asset.id)
        corrected = []
        for original in originals:
            shot = await session.get(StoryboardShot, original["id"])
            if not shot or shot.version != original["version"] + 1:
                continue
            if any(
                getattr(shot, field) != original[field]
                for field in (
                    "title",
                    "scene_description",
                    "action_description",
                    "dialogue",
                    "image_prompt",
                    "video_prompt",
                )
            ):
                continue
            target = []
            for aid in json.loads(original["asset_ids"]):
                labels = history.get(aid, set())
                matches = names.get(next(iter(labels)), []) if len(labels) == 1 else []
                target.append(matches[0] if len(matches) == 1 else aid)
            if shot.asset_ids != target:
                shot.asset_ids = target
                shot.version += 1
                corrected.append(shot.order_index)
        result = await sync_asset_images(session, board)
        await sync_chapter_prompt_files(session, board)
        print(
            json.dumps(
                {"corrected_previous_sync": corrected, **result}, ensure_ascii=False
            )
        )
        if args.apply:
            await session.commit()
        else:
            await session.rollback()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args()))
