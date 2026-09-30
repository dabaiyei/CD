"""Reference-aware generation units, independent of observation/analysis batches."""

from __future__ import annotations

import math

from app.services.provider_adapters import (
    compatible_video_resolution,
    supported_video_durations,
    validate_video_generation_request,
)


def reference_indices(references, shot_index):
    return [
        i
        for i, ref in enumerate(references)
        if not ref.get("shot_indices") or shot_index in ref["shot_indices"]
    ]


def generation_plan(options, caps, references):
    durations = supported_video_durations(caps)
    if not durations:
        raise ValueError("请先配置视频模型支持的时长")
    durations = sorted(durations)
    maximum = max(durations)
    shots = options.plan.shots
    for reference in references:
        if any(i > len(shots) for i in reference.get("shot_indices", [])):
            raise ValueError("参考图绑定了不存在的镜号")
        if reference.get("role", "character") == "character" and not reference.get("target", "").strip():
            raise ValueError("请为人物参考图填写要替换的原片角色，如：左侧白衣女子")
    units = []
    # A camera cut need not be a separate provider request. Explicit scene changes,
    # reference changes and user-marked cuts form boundaries; capability limits split long actions.
    for number, shot in enumerate(shots, 1):
        refs = reference_indices(references, number)
        count = max(1, math.ceil((shot.end - shot.start) / maximum))
        for part in range(count):
            start = shot.start + (shot.end - shot.start) * part / count
            end = shot.start + (shot.end - shot.start) * (part + 1) / count
            event = {
                "shot_index": number,
                "start": start,
                "end": end,
                "source_start": shot.start,
                "source_end": shot.end,
                "observation": shot.observation,
                "prompt": shot.prompt,
            }
            previous = units[-1] if units else None
            same_scene = previous and (
                not shot.scene_id or not previous["scene_id"] or shot.scene_id == previous["scene_id"]
            )
            merge = (
                previous
                and part == 0
                and shot.boundary != "cut"
                and same_scene
                and previous["reference_indices"] == refs
                and end - previous["start"] <= maximum + 0.001
                and sum(len(e["prompt"]) + len(e["observation"]) for e in previous["events"])
                + len(shot.prompt)
                + len(shot.observation)
                <= 15000
            )
            if merge:
                previous["end"] = end
                previous["events"].append(event)
                previous["shot_indices"].append(number)
                if not previous["scene_id"]:
                    previous["scene_id"] = shot.scene_id
            else:
                continuity = bool(previous and (part > 0 or shot.boundary == "continuous"))
                units.append(
                    {
                        "start": start,
                        "end": end,
                        "events": [event],
                        "shot_indices": [number],
                        "reference_indices": refs,
                        "scene_id": shot.scene_id,
                        "continues_previous": continuity,
                    }
                )
    warnings = []
    modes = caps.get("generation_modes") or ["text_to_video"]
    limits = caps.get("reference_limits") or {}
    image_limit = limits.get("image") or {}
    with_video = (
        options.use_reference_video
        and "full_reference" in modes
        and bool((limits.get("video") or {}).get("enabled"))
    )
    if options.use_reference_video and not with_video:
        warnings.append("模型不支持原视频动作参考，将按场景图和动作描述重建，不能保证原片动作精确复现。")
    for index, unit in enumerate(units):
        duration = unit["end"] - unit["start"]
        unit["duration"] = next(d for d in durations if d + 0.001 >= duration)
        unit["use_motion_reference"] = with_video
        bound = [references[i] for i in unit["reference_indices"]]
        # Character references must not be silently interpreted as literal first frames.
        first_only = "first_frame" in modes and not any(m in modes for m in ["full_reference", "multi_shot"])
        unit["prepare_frame"] = bool(
            first_only and bound and (len(bound) != 1 or bound[0].get("role") != "composition")
        )
        needs_tail = unit["continues_previous"]
        if needs_tail and not any(m in modes for m in ["first_frame", "full_reference", "multi_shot"]):
            raise ValueError(
                f"第 {index + 1} 段需要连续动作衔接，但模型不支持图像参考，请更换模型或改为自然切镜"
            )
        if needs_tail and first_only and unit["reference_indices"] == units[index - 1]["reference_indices"]:
            unit["prepare_frame"] = False  # The accepted predecessor already establishes target identity.
        count = (
            1
            if first_only and (bound or needs_tail or options.use_reference_frame)
            else len(bound)
            + int(options.use_reference_frame and not bound and not needs_tail)
            + int(needs_tail)
        )
        if count and image_limit.get("enabled") is False:
            raise ValueError("当前视频模型未启用图片参考")
        if count and not any(m in modes for m in ["first_frame", "full_reference", "multi_shot"]):
            raise ValueError("当前模型不支持参考帧，请关闭参考帧或选择图生视频模型")
        if count > (image_limit.get("max_count") or (1 if first_only else 8)):
            raise ValueError(f"第 {index + 1} 段参考图超出模型上限（连续段还需一张前段尾帧）")
        if unit["prepare_frame"] and not options.image_model_id:
            raise ValueError("首帧模型替换人物需要先生成场景图，请选择场景参考图模型")
        if bound and not any(m in modes for m in ["first_frame", "full_reference", "multi_shot"]):
            raise ValueError("当前视频模型不支持图片参考")
        if not count and not with_video and "text_to_video" not in modes:
            raise ValueError(f"第 {index + 1} 段缺少参考图，请绑定场景图或开启原片参考帧")
        unit["image_count"] = count
        if caps.get("schema_version") == 1:
            unit["resolution"] = compatible_video_resolution(
                caps,
                duration_seconds=unit["duration"],
                requested_resolution=options.resolution,
                aspect_ratio=options.aspect_ratio,
            )
            mode = (
                "full_reference"
                if "full_reference" in modes and (count or with_video)
                else "multi_shot"
                if "multi_shot" in modes and count
                else "first_frame"
                if count
                else "text_to_video"
            )
            accepted = image_limit.get("accepted_mime_types") or ["image/webp"]
            mime = next((m for m in ["image/webp", "image/png", "image/jpeg"] if m in accepted), None)
            if count and not mime:
                raise ValueError("模型不支持可用的参考图片格式")
            media = [
                {"type": "image", "mime_type": mime, "url": "https://example.com/reference"}
                for _ in range(count)
            ]
            if with_video:
                media.append({"type": "video", "mime_type": "video/mp4", "url": "https://example.com/source"})
            validate_video_generation_request(
                caps,
                generation_mode=mode,
                duration_seconds=unit["duration"],
                resolution=unit["resolution"],
                aspect_ratio=options.aspect_ratio,
                reference_media=media,
                audio_enabled=options.audio == "generated" or caps.get("audio_policy") == "required",
            )
        elif options.audio == "generated" and caps.get("audio_policy") == "disabled":
            raise ValueError("当前视频模型不支持生成音频")
        if unit["duration"] > duration + 0.05:
            warnings.append(
                f"第 {index + 1} 段原片 {duration:g} 秒，生成 {unit['duration']:g} 秒完整动作；不截掉结尾。"
            )
            if options.audio == "source":
                warnings.append(
                    "保留原音时，将只对本段音频做节奏适配；不借用后段对白。若希望重新表演台词可选生成新的声音。"
                )
        if needs_tail:
            warnings.append(f"第 {index + 1} 段将等待前段成功，并用其实际尾帧衔接。")
    return {
        "version": 2,
        "units": units,
        "warnings": list(dict.fromkeys(warnings)),
        "source_seconds": shots[-1].end,
        "output_seconds": sum(u["duration"] for u in units),
    }


def direction_context(unit, references):
    scale = unit["duration"] / (unit["end"] - unit["start"])
    bindings = []
    for order, i in enumerate(unit["reference_indices"], 1):
        ref = references[i]
        bindings.append(
            f"图片{order}：用途={ref.get('role', 'character')}；"
            f"替换对象={ref.get('target', '')}；{ref['purpose']}。"
            + (
                "人物身份、脸、服装取自此图，不保留被替换角色的旧外貌。"
                if ref.get("role", "character") == "character"
                else "仅按指定用途参考，不更改其他人物身份。"
            )
        )
    timeline = []
    for event in unit["events"]:
        start = (event["start"] - unit["start"]) * scale
        end = (event["end"] - unit["start"]) * scale
        timeline.append({**event, "target_start": round(start, 3), "target_end": round(end, 3)})
    return {"bindings": bindings, "timeline": timeline}
