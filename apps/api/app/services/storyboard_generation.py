"""Bounded storyboard generation with exact timing and durable per-shot repairs."""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from functools import reduce

from app.services.source_timeline import extract


SCENE_MARKER = re.compile(r"^\s*场\s*\d+\s*[｜|]")
# One scene's prose is small enough for the model to describe completely. The
# budget only subdivides scenes long enough to risk a truncated reply.
SEGMENT_BUDGET = 6000
# Imported novels often have no scene markers at all, and the model answered a
# whole 4000-character chapter with the opening two shots. Unmarked prose is
# therefore cut into smaller units so every request has a body small enough to
# describe in full.
PROSE_BUDGET = 2000
# The full chapter body is appended to the request prompt as "剧本正文：". When a
# chapter is generated scene by scene that body would invite the model to return
# shots for the whole chapter at once, so it is dropped and the scene brief
# supplies the text instead.
SCRIPT_BODY_MARKER = "\n剧本正文："


def _without_script_body(prompt: str) -> str:
    """Drop the whole-chapter body from a prompt that will carry one scene."""
    head, marker, _ = prompt.partition(SCRIPT_BODY_MARKER)
    return head if marker else prompt


def _scene_prompt_base(prompt: str, script: str) -> str:
    """The prompt a scene request starts from, without the whole chapter body.

    The pipeline hands the exact chapter text to the generator, so removing that
    text is precise; the marker fallback covers callers that only appended a
    body without passing it separately.
    """
    if script and script in prompt:
        head = prompt.replace(script, "", 1).rstrip()
        if head.endswith(SCRIPT_BODY_MARKER):
            head = head[: -len(SCRIPT_BODY_MARKER)].rstrip()
        return head
    return _without_script_body(prompt)


def segment_script(content: str, budget: int | None = None) -> list[dict]:
    """Divide a chapter into the units a storyboard is generated one at a time.

    A source carrying an explicit timeline is already divided by its time slots.
    A plain prose chapter has no boundaries at all, so the platform used to ask
    for the entire chapter in a single request and accept whatever came back --
    a board covering one scene out of five was indistinguishable from a complete
    one. Scene markers are the natural replacement when the script has them;
    otherwise the prose is chunked by paragraph. Either way the board cannot be
    published until every unit has produced shots.
    """
    lines = content.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in {"---", ""} and not current:
            continue
        if SCENE_MARKER.match(line) and any(part.strip() for part in current):
            blocks.append(current)
            current = [line]
        elif stripped == "---":
            continue
        else:
            current.append(line)
    if any(part.strip() for part in current):
        blocks.append(current)
    # A chapter with scene markers is already divided by them; a markerless
    # chapter has no boundaries, so its paragraphs are chunked instead.
    if budget is None:
        marked = any(SCENE_MARKER.match(line) for line in lines)
        budget = SEGMENT_BUDGET if marked else PROSE_BUDGET
    budget = max(1, budget)  # A non-positive budget would never advance the cutter.
    segments: list[dict] = []
    for block in blocks:
        text = "\n".join(block).strip()
        if not text:
            continue
        name = block[0].strip() if SCENE_MARKER.match(block[0]) else ""
        pieces = _split_oversized(text, budget)
        for part, piece in enumerate(pieces, start=1):
            segments.append({
                "key": str(len(segments) + 1),
                "label": name or f"第{len(segments) + 1}段",
                "content": piece,
                "part": part,
                "parts": len(pieces),
            })
    return segments


def _split_oversized(text: str, budget: int) -> list[str]:
    """Cut a block that would not fit one request, keeping paragraph boundaries.

    A single paragraph can still exceed the budget, so it is hard-wrapped; the
    chunks keep every character, which is what makes the coverage check work.
    """
    if len(text) <= budget:
        return [text]
    pieces, current = [], ""
    for paragraph in text.splitlines():
        candidate = paragraph if not current else f"{current}\n{paragraph}"
        if len(candidate) > budget and current:
            pieces.append(current)
            current = paragraph
        else:
            current = candidate
        while len(current) > budget:
            pieces.append(current[:budget])
            current = current[budget:]
    if current:
        pieces.append(current)
    return pieces or [text]


def _segment_brief(segment: dict, segments: list[dict], state: dict,
                   *, budget: int | None = None) -> str:
    """Tell the model which scene to shoot now and to number shots continuously.

    Each scene is generated in its own request, so the model never sees the rest
    of the chapter. It is given the position of this scene in the chapter, the
    last shot of the previous scene for continuity, and the instruction to keep
    numbering across the whole chapter rather than restarting at one.

    A chapter budget is split across scenes by their length, because telling a
    scene it may use the whole chapter's seconds would multiply the budget by
    the number of scenes.
    """
    number = next(i for i, item in enumerate(segments, start=1) if item is segment)
    within = ""
    if segment.get("parts", 1) > 1:
        within = f"（该场第 {segment['part']}/{segment['parts']} 部分，只需覆盖这部分正文）"
    brief = (
        f"\n\n本次只生成全章第 {number}/{len(segments)} 个场景（{segment['label']}）{within}，"
        "只拆解下面的场景正文，不要生成其它场景的镜头，也不要补写剧本之外的内容。"
        "order_index 必须接续上一场继续编号，而不是从 1 重新开始；"
        "上一场的最后一个镜头如下，用于衔接站位、朝向、伤势、服装和道具状态："
    )
    previous = list(state["valid"].values())[-1:]
    brief += json.dumps(previous, ensure_ascii=False)
    if budget:
        share = _scene_budget(segment, segments, budget)
        brief += (
            f"\n本场的目标成片时长约 {share} 秒（全章 {budget} 秒按场景长短分配），"
            "本场所有镜头 duration_seconds 之和应接近该值，不得明显超出；"
            "镜头数量按本场内容决定，内容不足就不要为了凑时长增加镜头。"
        )
    brief += f"\n本场正文：\n{segment['content']}"
    return brief


def _scene_budget(segment: dict, segments: list[dict], budget: int) -> int:
    """Split the chapter budget across scenes in proportion to their length."""
    total = sum(len(item["content"]) for item in segments) or 1
    share = round(budget * len(segment["content"]) / total)
    return max(1, share)


def segment_offset(segments: list[dict], state: dict, segment: dict) -> int:
    """How many shots the scenes before this one contributed, for numbering."""
    offset = 0
    for other in segments:
        if other is segment:
            break
        offset += len(state["segment_shots"].get(other["key"], []))
    return offset


def allocate_timeline(source: str, durations: list[float]) -> list[dict]:
    spans = sorted(extract(source), key=lambda s: (s.start, s.end))
    if not spans or spans[0].start != 0:
        return []
    covered = 0.0
    for span in spans:
        if span.start > covered + .001:
            return []  # Partial timestamps do not define a complete chapter.
        covered = max(covered, span.end)
    # Model duration constrains the output file, not each editorial cut. Pack the
    # complete chapter into legal clips and retain short shots inside each clip.
    ends = [covered]
    allowed = sorted({Decimal(str(d)) for d in durations if d > 0})
    if not allowed:
        raise RuntimeError("视频模型未提供合法时长，无法分配分镜时间轴")
    plan, start = [], Decimal(0)
    for end in ends:
        length = Decimal(str(end)) - start
        precision = max(-v.as_tuple().exponent for v in [length, *allowed])
        if precision > 3:
            raise RuntimeError("时间轴精度超过毫秒，请检查原文时间标记")
        scale = 10 ** precision
        values = [int(d * scale) for d in allowed]
        divisor = reduce(math.gcd, values)
        target = int(length * scale)
        if target % divisor or length < allowed[0] or length > allowed[-1] * 200:
            raise RuntimeError(f"原文 {start}—{end:g} 秒无法用模型合法时长 {durations} 精确组合；请调整时间轴或模型")
        target //= divisor
        values = [v // divisor for v in values]
        if target > 500_000:
            raise RuntimeError("时间轴分配规模过大，请拆分章节")
        # Minimize independently generated clips (continuity seams), then balance.
        best = {0: (0, Decimal(0), 0, Decimal(0))}
        for total in range(1, target + 1):
            candidates = []
            for ticks, duration in zip(values, allowed):
                previous = best.get(total - ticks)
                if previous and previous[0] < 200:
                    candidates.append((previous[0] + 1, previous[1] + duration ** 2,
                                       total - ticks, duration))
            if candidates:
                best[total] = min(candidates, key=lambda item: item[:2])
        if target not in best:
            raise RuntimeError(f"原文 {start}—{end:g} 秒无法用模型合法时长 {durations} 精确组合；请调整时间轴或模型")
        parts, cursor = [], target
        while cursor:
            parts.append(best[cursor][3])
            cursor = best[cursor][2]
        for duration in reversed(parts):
            finish = start + duration
            cues = [s.cue for s in spans if Decimal(str(s.start)) < finish and Decimal(str(s.end)) > start]
            internal_shots = []
            for span in spans:
                a, b = max(start, Decimal(str(span.start))), min(finish, Decimal(str(span.end)))
                if b > a:
                    internal_shots.append({"start_seconds": float(a - start), "end_seconds": float(b - start),
                        "source_start_seconds": float(a), "source_end_seconds": float(b),
                        "description": span.cue})
            plan.append({"order_index": len(plan) + 1, "start_seconds": float(start),
                         "end_seconds": float(finish), "duration_seconds": float(duration),
                         "source_cues": cues, "internal_shots": internal_shots})
            start = finish
    if len(plan) > 200:
        raise RuntimeError("时间轴需要超过200个分镜，请拆分章节")
    return plan


def decode_shots(text: str) -> list:
    """Accept fenced JSON or recover complete rows from a truncated shots array."""
    decoder = json.JSONDecoder()
    clean = text.strip().removeprefix('```json').removeprefix('```').strip()
    if clean.startswith('['):
        try:
            value, _ = decoder.raw_decode(clean)
            if isinstance(value, list):
                return value
        except ValueError:
            pass
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("shots"), list):
            return value["shots"]
    match = re.search(r'"shots"\s*:\s*\[', text)
    rows = []
    if match:
        offset = match.end()
        while offset < len(text):
            while offset < len(text) and text[offset] in ' \n\r\t,':
                offset += 1
            try:
                row, length = decoder.raw_decode(text[offset:])
            except ValueError:
                break
            if not isinstance(row, dict):
                break
            rows.append(row)
            offset += length
    return rows


def complete_output(text: str) -> bool:
    decoder = json.JSONDecoder()
    clean = text.strip().removeprefix('```json').removeprefix('```').strip()
    starts = [0] if clean.startswith('[') else [m.start() for m in re.finditer(r'\{', clean)]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(clean[start:])
        except ValueError:
            continue
        if isinstance(value, list) or (isinstance(value, dict) and isinstance(value.get('shots'), list)):
            return True
    return False


async def generate(request, runtime_factory, *, plan, durations, state, save, progress,
                   script: str = "", budget: int | None = None):
    from app.services.task_worker import GeneratedStoryboardShotPayload, StoryboardGenerationPayload
    from app.services.creation_context import contains_combat
    from app.services.clip_timeline import CLIP_RULES
    schema = ('只返回 JSON 对象 {"shots":[...]}，禁止解释、Markdown、文件保存报告。'
              'shots的每一项是一次视频生成片段，可包含多个短分镜；模型时长限制只约束片段总长，'
              '不约束片段内部每次切镜。action_description按片段内相对秒数展开全部短分镜，'
              '不能把0–2秒短镜头延长到模型最短时长。image_prompt仅描绘片段开头，不拼贴所有机位。'
              '每镜必须含 order_index（从1开始的全章镜号）、title、shot_type、duration_seconds、'
              'scene_description、action_description、dialogue、image_prompt、asset_names；'
              'video_prompt 必须为空字符串。title 和 image_prompt 必须为非空文本。'
              '中文台词原样保留，不能改为英文。')
    properties = GeneratedStoryboardShotPayload.model_json_schema()["properties"]
    basic_fields = ("title", "shot_type", "duration_seconds", "scene_description",
                    "action_description", "dialogue", "image_prompt", "video_prompt", "asset_names")
    schema += '\n输出JSON Schema：' + json.dumps({"type": "object", "required": ["shots"],
        "properties": {"shots": {"type": "array", "minItems": 1, "maxItems": 200,
            "items": {"type": "object", "required": ["title", "duration_seconds", "image_prompt"],
                "properties": {**{k: properties[k] for k in basic_fields},
                    "duration_seconds": {"type": "number", "enum": durations},
                    "order_index": {"type": "integer", "minimum": 1}}}}}}, ensure_ascii=False)
    allowed = {Decimal(str(d)) for d in durations}
    state.setdefault("valid", {})
    state.setdefault("raw", {})
    manifest = state.get("manifest", {})
    # Without a source timeline every scene is its own unit; the board is only
    # complete once all of them have shots. With a timeline the plan already
    # fixes the slots, so batching stays as it was.
    segments = segment_script(script) if script and not plan else []
    state.setdefault("segment_shots", {})

    def validate(row, index):
        if not isinstance(row, dict) or "duration_seconds" not in row:
            raise ValueError("缺少明确的duration_seconds，不允许使用默认时长")
        shot = GeneratedStoryboardShotPayload.model_validate(row)
        if contains_combat(request.prompt) and not shot.action_description.strip() and shot.combat_plan is None:
            raise ValueError("战斗分镜缺少基本动作或战斗时间轴，请补充本镜动作信息")
        if "order_index" in row and row["order_index"] != index:
            raise ValueError("返回镜号与请求镜号不一致")
        if shot.duration_seconds not in allowed:
            raise ValueError(f"时长必须取自 {durations}，不得向上取整")
        if plan and shot.duration_seconds != Decimal(str(plan[index - 1]["duration_seconds"])):
            raise ValueError(f"本镜时长必须为 {plan[index - 1]['duration_seconds']} 秒")
        if plan:
            from app.services.clip_timeline import InternalShot
            shot.internal_shots = [InternalShot.model_validate(s) for s in plan[index - 1]["internal_shots"]]
        return shot.model_copy(update={"video_prompt": ""}).model_dump(mode="json")

    async def call(prompt, label):
        await progress(label)
        current = request.model_copy(update={"prompt": prompt, "tool_mode": "none",
            "system_prompt": request.system_prompt + CLIP_RULES,
            "state_mode": "ephemeral", "recent_messages": [], "conversation_summary": None})
        result = await runtime_factory().run(current)
        state["manifest"] = result.manifest
        return result.final_response

    # Fixed timing allows small batches; un-timed scripts retain model-directed pacing.
    if plan:
        units = [(str(number), plan[i:i + 4], None)
                 for number, i in enumerate(range(0, len(plan), 4))]
    elif segments:
        units = [(segment["key"], [], segment) for segment in segments]
    else:
        units = [("0", [], None)]
    total_units = len(units)
    for unit_no, (key, group, segment) in enumerate(units, start=1):
        indices = [slot["order_index"] for slot in group]
        # A scene-by-scene chapter must not carry the full chapter body, or the
        # model returns shots for scenes that belong to a later unit.
        base = _scene_prompt_base(request.prompt, script) if segment is not None else request.prompt
        if indices and all(str(i) in state["valid"] for i in indices):
            for i in indices:
                validate(state["valid"][str(i)], i)
            continue
        if segment is not None and segment["key"] in state["segment_shots"]:
            # This scene was already generated in an earlier attempt; its shots
            # are checkpointed, so no paid request is repeated for it.
            owned = state["segment_shots"][segment["key"]]
            if owned and all(str(i) in state["valid"] for i in owned):
                continue
            # A checkpoint named this scene but its shots are not there. Treating
            # that as "done" would publish a board that silently skips a scene,
            # so the scene is generated again instead.
            state["segment_shots"].pop(segment["key"], None)
            state["raw"].pop(key, None)
        if key not in state["raw"]:
            previous = list(state["valid"].values())[-1:]
            prompt = base + '\n' + schema
            if group:
                prompt += ('\n本次仅生成以下时间槽，不能生成其它镜头，不能改变起止时间和时长：'
                           + json.dumps(group, ensure_ascii=False)
                           + '\n衔接上一镜：' + json.dumps(previous, ensure_ascii=False))
            elif segment is not None:
                prompt += _segment_brief(segment, segments, state, budget=budget)
            state["raw"][key] = await call(prompt, f"正在生成第 {unit_no}/{total_units} 批分镜")
            await save(state)
        raw = state["raw"][key]
        rows = decode_shots(raw)
        # A wholly unreadable reply receives a formatting repair, not a fresh storyboard.
        if not rows or (group and len(rows) > len(group)) or (not group and not complete_output(raw)):
            if len(raw) > 40_000:
                raise RuntimeError("分镜返回格式错误且内容过长，已保留结果；请缩小生成范围")
            fixed = await call(schema + '\n仅修复下列已有输出的JSON结构，不重写故事。'
                + '\n本批时间槽：' + json.dumps(group, ensure_ascii=False)
                + '\n已有输出：\n' + raw, f"正在修复第 {unit_no} 批返回格式")
            rows = decode_shots(fixed)
            state["raw"][key] = fixed
            await save(state)
            if not group and not complete_output(fixed):
                raise RuntimeError("分镜输出仍被截断，无法确定完整镜头数量；已保存原始结果，未发布残缺分镜")
        if group and len(rows) > len(group):
            raise RuntimeError("本批分镜数超过时间槽数量，格式修复后仍不一致；已保留此前批次")
        if not group:
            if not rows or len(rows) > 200:
                raise RuntimeError("分镜结构修复失败：缺少有效shots数组；已保存原始回复")
            rows = [row for row in rows if isinstance(row, dict)]
            offset = segment_offset(segments, state, segment)
            indices = list(range(offset + 1, offset + len(rows) + 1))
            if segment is not None:
                # Numbering across the chapter is the platform's, not the
                # model's: each scene is generated blind to the others, so a
                # scene-local 1 would collide with the previous scene's shots.
                rows = [{**row, "order_index": indices[pos]} for pos, row in enumerate(rows)]
        indexed = {}
        duplicates = set()
        for pos, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            index = row.get("order_index", indices[pos] if pos < len(indices) else -1)
            if type(index) is not int or index not in indices:
                continue
            if index in indexed:
                duplicates.add(index)
            indexed[index] = row
        for index in duplicates:
            indexed.pop(index)  # Ambiguous rows must be regenerated for this slot only.
        # Preserve every valid row before attempting a broken one, including later rows.
        for index in indices:
            if str(index) not in state["valid"] and index in indexed:
                try:
                    state["valid"][str(index)] = validate(indexed[index], index)
                except (ValueError, TypeError):
                    pass
        await save(state)
        for index in indices:
            if str(index) in state["valid"]:
                validate(state["valid"][str(index)], index)
                continue
            row = indexed.get(index, {})
            for attempt in range(3):
                try:
                    valid = validate(row, index)
                    break
                except (ValueError, TypeError) as error:
                    if attempt == 2:
                        raise RuntimeError(f"第{index}镜修复失败，已保留其它完成镜头：{str(error)[:600]}") from error
                    neighbors = {str(i): state["valid"].get(str(i), indexed.get(i))
                                 for i in (index - 1, index + 1)}
                    prompt = (schema + f'\n仅返回第{index}镜，shots内只能有一项；不要返回整份分镜。'
                        + '\n错误：' + str(error)[:1500]
                        + '\n合法时长：' + json.dumps(durations)
                        + '\n时间槽：' + json.dumps(plan[index - 1] if plan else {}, ensure_ascii=False)
                        + '\n原镜头：' + json.dumps(row, ensure_ascii=False)
                        + '\n相邻镜头仅供衔接：' + json.dumps(neighbors, ensure_ascii=False))
                    # Missing rows need source context; existing rows need only a targeted repair.
                    if not row:
                        source = base
                        if segment is not None:
                            source += f"\n本镜所属场景正文：\n{segment['content']}"
                        prompt = source + '\n' + prompt
                    fixed = decode_shots(await call(prompt, f"正在修复第 {index} 镜（{attempt + 1}/2）"))
                    row = fixed[0] if len(fixed) == 1 else {}
            state["valid"][str(index)] = valid
            await save(state)
            await progress(f"已完成 {len(state['valid'])}/{len(plan) or len(indices)} 镜，结果已保存")
        await progress(f"第 {unit_no}/{total_units} 批已校验并保存，共 {len(state['valid'])} 镜")
        if segment is not None:
            owned = state["segment_shots"].get(segment["key"], [])
            state["segment_shots"][segment["key"]] = sorted(
                set(owned) | {str(i) for i in indices if str(i) in state["valid"]},
                key=int,
            )
            await save(state)
    if segments:
        missing = [s["label"] for s in segments if not state["segment_shots"].get(s["key"])]
        if missing:
            # A scene that produced no shots means the board silently covers only
            # part of the chapter, which is the bug this segmentation prevents.
            raise RuntimeError(
                f"分镜未覆盖全部场景，缺少：{'、'.join(missing[:6])}；已保留已完成场景，可重试补齐"
            )
    rows = [state["valid"][key] for key in sorted(state["valid"], key=int)]
    board = StoryboardGenerationPayload.model_validate({"shots": rows})
    return board.model_dump_json(), state.get("manifest", manifest)
