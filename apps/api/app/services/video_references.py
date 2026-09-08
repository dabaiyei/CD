from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, AssetType, StoryboardShot


async def load_shot_assets_with_parents(
    session: AsyncSession,
    *,
    shots: Iterable[StoryboardShot],
    tenant_id: str,
) -> dict[str, Asset]:
    asset_ids = {asset_id for shot in shots for asset_id in shot.asset_ids}
    if not asset_ids:
        return {}
    assets = list(
        (
            await session.scalars(
                select(Asset).where(Asset.id.in_(asset_ids), Asset.tenant_id == tenant_id)
            )
        ).all()
    )
    parent_ids = {asset.parent_asset_id for asset in assets if asset.parent_asset_id}
    if parent_ids:
        assets.extend(
            list(
                (
                    await session.scalars(
                        select(Asset).where(
                            Asset.id.in_(parent_ids),
                            Asset.tenant_id == tenant_id,
                        )
                    )
                ).all()
            )
        )
    return {asset.id: asset for asset in assets}


def build_shot_image_references(
    shot: StoryboardShot,
    assets_by_id: dict[str, Asset],
) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    by_url: dict[str, dict[str, str]] = {}

    def add_reference(url: str | None, *, role: str, asset: Asset | None = None) -> None:
        if not url:
            return
        existing = by_url.get(url)
        if existing is not None:
            if asset is not None:
                names = [item for item in existing.get("asset_names", "").split("、") if item]
                if asset.name not in names:
                    names.append(asset.name)
                    existing["asset_names"] = "、".join(names)
                asset_ids = [item for item in existing.get("asset_ids", "").split(",") if item]
                if asset.id not in asset_ids:
                    asset_ids.append(asset.id)
                    existing["asset_ids"] = ",".join(asset_ids)
                if not existing.get("asset_id"):
                    existing["asset_id"] = asset.id
                    existing["asset_name"] = asset.name
                    existing["asset_type"] = asset.asset_type.value
                    existing["asset_description"] = asset.description
            return
        reference = {
            "type": "image",
            "url": url,
            "mime_type": mimetypes.guess_type(url.split("?", 1)[0])[0] or "",
            "token": f"<Picture {len(references) + 1}>",
            "role": role,
        }
        if asset is not None:
            reference.update(
                {
                    "asset_id": asset.id,
                    "asset_ids": asset.id,
                    "asset_name": asset.name,
                    "asset_names": asset.name,
                    "asset_type": asset.asset_type.value,
                    "asset_description": asset.description,
                }
            )
        references.append(reference)
        by_url[url] = reference

    add_reference(shot.reference_image_url, role="first_frame")
    for asset_id in shot.asset_ids:
        asset = assets_by_id.get(asset_id)
        if asset is not None and asset.media_url:
            add_reference(asset.media_url, role="asset_reference", asset=asset)
    return references


def build_shot_audio_references(
    shot: StoryboardShot,
    assets_by_id: dict[str, Asset],
    capabilities: dict[str, Any],
) -> list[dict[str, str]]:
    if capabilities.get("audio_policy") == "disabled":
        return []
    limits = capabilities.get("reference_limits")
    audio_limit = limits.get("audio") if isinstance(limits, dict) else None
    if not isinstance(audio_limit, dict) or not audio_limit.get("enabled"):
        return []
    maximum = int(audio_limit.get("max_count") or 0)
    if maximum <= 0:
        return []
    accepted = {
        str(value).lower()
        for value in audio_limit.get("accepted_mime_types", [])
        if isinstance(value, str) and value
    }
    references: list[dict[str, str]] = []
    seen_assets: set[str] = set()
    for asset_id in shot.asset_ids:
        asset = assets_by_id.get(asset_id)
        if asset is None or asset.asset_type != AssetType.CHARACTER:
            continue
        source = asset
        metadata = dict(source.asset_metadata or {})
        if not metadata.get("reference_audio_url") and source.parent_asset_id:
            source = assets_by_id.get(source.parent_asset_id) or source
            metadata = dict(source.asset_metadata or {})
        url = metadata.get("reference_audio_url")
        if not isinstance(url, str) or not url or asset.id in seen_assets:
            continue
        mime_type = str(
            metadata.get("reference_audio_mime_type")
            or mimetypes.guess_type(url.split("?", 1)[0])[0]
            or ""
        ).lower()
        if accepted and mime_type not in accepted:
            continue
        seen_assets.add(asset.id)
        references.append(
            {
                "type": "audio",
                "url": url,
                "mime_type": mime_type,
                "token": f"<Audio {len(references) + 1}>",
                "role": "character_voice_reference",
                "asset_id": asset.id,
                "source_asset_id": source.id,
                "character_name": asset.name,
                "source_character_name": source.name,
            }
        )
        if len(references) >= maximum:
            break
    return references


def ensure_video_prompt_audio_reference_locks(
    prompt: str,
    references: list[dict[str, str]],
    *,
    language: str,
) -> str:
    if not references:
        return prompt
    missing = [
        item
        for item in references
        if item.get("token") not in prompt or item.get("character_name") not in prompt
    ]
    if not missing:
        return prompt
    if language.lower().startswith("zh"):
        lines = [
            (
                f"{item['token']} 仅对应人物“{item.get('character_name') or '未命名人物'}”的声音身份参考；"
                "只参考音色、音域、口音、语速与发声质感，不得复制参考音频中的台词或把声音分配给其他人物。"
            )
            for item in missing
        ]
        return f"{prompt.strip()}\n\n人物参考音频约束：\n" + "\n".join(lines)
    lines = [
        (
            f"{item['token']} belongs exclusively to character "
            f'"{item.get("character_name") or "unnamed character"}"; use only voice identity, '
            "timbre, range, accent, pacing, and vocal texture. Do not copy its spoken words or "
            "assign the voice to another character."
        )
        for item in missing
    ]
    return f"{prompt.strip()}\n\nCharacter audio reference locks:\n" + "\n".join(lines)
