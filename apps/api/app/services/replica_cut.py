"""Deterministic simple cuts over the native replica picture track."""

import re

from pydantic import BaseModel, ConfigDict, Field

from app.services.object_storage import public_media_url


class Cut(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    asset: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    volume: float = Field(default=1, ge=0, le=1)


class CutInput(BaseModel):
    close_gaps: bool = False
    revision: str = Field(min_length=64, max_length=64)
    clips: list[Cut] = Field(min_length=1, max_length=200)


def read(production):
    source = production["files"]["composition.svml"]
    imports = set(re.findall(r'from="@hypit/([^@]+)@1"', source))
    if imports - {
        "media",
        "media-pipeline",
        "timeline-author",
        "spatial",
        "media-track",
        "film",
        "render-hyperframes",
    }:
        raise ValueError("此工程含字幕或高级轨道，请使用 AI 后期或高级编辑器，避免剪辑破坏同步关系")
    if '<time:Clock id="clock" frame-rate="30"/>' not in source or source.count("<track:Track") != 1:
        raise ValueError("此工程使用自定义时间轴，请使用高级编辑器")
    track = re.search(r'<track:Track id="picture"[^>]*>(.*?)</track:Track>', source, re.S)
    if not track or re.sub(r"<track:Item\b[^>]+/>", "", track[1]).strip():
        raise ValueError("此工程包含高级画面编排，请使用高级编辑器")
    assets = dict(re.findall(r'<asset:Video id="clip-(\d+)" src="\./([^"]+)"/>', source))
    clips = []
    cursor = 0
    for tag in re.findall(r"<track:Item\b[^>]+/>", track[1]):
        media = re.search(r"media=\{media-(\d+)\.media\}", tag)
        at, duration = re.search(r' at="(\d+)f"', tag), re.search(r' for="(\d+)f"', tag)
        if not media or not at or not duration:
            raise ValueError("时间轴含重叠或间隔，请使用高级编辑器")
        asset = assets.get(media[1])
        if asset not in production["assets"]:
            raise ValueError("片段素材不存在")
        appearance = re.search(r"appearance=\{look\.(cut\.item\d+|media\.full)\}", tag)
        if not appearance:
            raise ValueError("片段使用自定义效果，请使用高级编辑器")
        start = 0
        if appearance[1] != "media.full":
            recipe = re.search(re.escape(appearance[1]) + r"\s*\{([^}]+)\}", production["files"]["look.svs"])
            trim = re.search(r"trim-start:\s*(\d+)", recipe[1]) if recipe else None
            if not trim:
                raise ValueError("裁剪数据不完整")
            start = int(trim[1])
        gain = re.search(r'audio-gain="([\d.]+)"', tag)
        clips.append(
            dict(
                asset=asset,
                start=start / 30,
                end=(start + int(duration[1])) / 30,
                volume=float(gain[1]) if gain else 1,
            )
        )
        cursor += int(duration[1])
    if not clips:
        raise ValueError("工程没有可剪辑片段")
    return clips


def view(production):
    try:
        clips, reason = read(production), ""
    except ValueError as exc:
        clips, reason = [], str(exc)
    return dict(
        timing_warning=has_gaps(production),
        revision=production["revision"],
        clips=clips,
        reason=reason,
        assets={
            name: {
                "name": a.get("name") or f"片段 {i + 1}",
                "duration": a["duration"],
                "url": public_media_url(a["key"]),
            }
            for i, (name, a) in enumerate(production["assets"].items())
            if a.get("type") == "video" and a.get("duration")
        },
    )


def has_gaps(production):
    tags = re.findall(r"<track:Item\b[^>]+/>", production["files"]["composition.svml"])
    cursor = 0
    for tag in tags:
        at, duration = re.search(r' at="(\d+)f"', tag), re.search(r' for="(\d+)f"', tag)
        if at and duration:
            if int(at[1]) != cursor:
                return True
            cursor += int(duration[1])
    return False


def apply(production, clips, *, close_gaps=False):
    read(production)  # Refuse to silently flatten an advanced edit.
    if has_gaps(production) and not close_gaps:
        raise ValueError("原工程存在片段间隔或重叠，请确认按当前顺序紧密拼接后保存")
    source = production["files"]["composition.svml"]
    assets = {
        name: index for index, name in re.findall(r'<asset:Video id="clip-(\d+)" src="\./([^"]+)"/>', source)
    }
    lines, recipes, cursor = [], [], 0
    for i, clip in enumerate(clips):
        if clip.asset not in assets:
            raise ValueError("只能使用当前时间轴已绑定的素材")
        start, end = round(clip.start * 30), round(clip.end * 30)
        maximum = round(float(production["assets"][clip.asset]["duration"]) * 30)
        if end > maximum or end <= start:
            raise ValueError("保留区间必须位于原片段内，且至少保留一帧")
        lines.append(
            f'<track:Item id="shot-{i}" media={{media-{assets[clip.asset]}.media}} frame={{full}} '
            f'at="{cursor}f" for="{end - start}f" source-audio="content" audio-gain="{clip.volume:g}" '
            f"appearance={{look.cut.item{i}}}/>"
        )
        recipes.append(
            f"cut.item{i} {{ stack-order: 0; fit: contain; trim-start: {start}; trim-end: {end}; }}"
        )
        cursor += end - start
    source = re.sub(
        r'(<track:Track id="picture"[^>]*>).*?(</track:Track>)',
        lambda m: m[1] + "\n" + "\n".join(lines) + "\n" + m[2],
        source,
        flags=re.S,
    )
    source, count = re.subn(
        r'(<time:Timeline id="timeline" clock=\{clock\} end=")\d+f("/>)',
        lambda m: m[1] + str(cursor) + "f" + m[2],
        source,
    )
    if count != 1:
        raise ValueError("时间轴使用高级编排，不能直接裁剪")
    style = re.sub(r"\n?cut\.item\d+\s*\{[^}]*\}", "", production["files"]["look.svs"])
    style = style.replace("</sheet>", "\n".join(recipes) + "\n</sheet>")
    return {**production, "files": {**production["files"], "composition.svml": source, "look.svs": style}}
