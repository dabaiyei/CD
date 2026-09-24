"""Bounded storyboard generation with exact timing and durable per-shot repairs."""
from __future__ import annotations

import asyncio
import json
import math
import re
from decimal import Decimal
from functools import reduce

import httpx
from pydantic import ValidationError

from app.services.source_timeline import extract


SCENE_MARKER = re.compile(r"^\s*场\s*\d+\s*[｜|]")
# One scene's prose is small enough for the model to describe completely. The
# budget only subdivides scenes long enough to risk a truncated reply.
SEGMENT_BUDGET = 2400
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


class ShotRepairExhausted(RuntimeError):
    """A shot used its retry budget; the batch loop must not multiply it."""


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
                   *, budget: int | None = None, repair: bool = False,
                   existing: list[dict] | None = None) -> str:
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
        f"\n\n本次只{'修复' if repair else '生成'}全章第 {number}/{len(segments)} 个场景"
        f"（{segment['label']}）{within}，"
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
    if repair:
        if existing:
            brief += (
                f"\n原分镜中本场的 {len(existing)} 个镜头如下。逐一检查、按审核意见和用户意见修改，"
                "需要时拆成更多镜头，但不得合并、删除或漏掉任何一个；全部原镜头都必须出现在返回结果中，"
                f"返回数量不得少于 {len(existing)}。保持原镜头的叙事顺序与已确定的剧情，"
                "只修复被指出的问题，不要重写没有问题的镜头：\n"
                + json.dumps(existing, ensure_ascii=False)
            )
        else:
            brief += (
                "\n原分镜完全没有覆盖本场，这是必须补齐的缺口。请依据本场正文补出新镜头，"
                "不要复述其它场景已经拍过的内容。"
            )
    return brief


def _scene_budget(segment: dict, segments: list[dict], budget: int) -> int:
    """Split the chapter budget across scenes in proportion to their length."""
    total = sum(len(item["content"]) for item in segments) or 1
    share = round(budget * len(segment["content"]) / total)
    return max(1, share)


def _repair_brief(segment: dict, segments: list[dict], state: dict,
                  existing: list[dict], *, budget: int | None = None) -> str:
    """The scene brief used when an existing scene is being repaired."""
    return _segment_brief(segment, segments, state, budget=budget, repair=True,
                          existing=existing)


def segment_offset(segments: list[dict], state: dict, segment: dict) -> int:
    """How many shots the scenes before this one contributed, for numbering."""
    offset = 0
    for other in segments:
        if other is segment:
            break
        offset += len(state["segment_shots"].get(other["key"], []))
    return offset


def _scene_affinity(shot: dict, content: str) -> int:
    """How strongly one stored shot reads as belonging to this scene.

    Dialogue is copied verbatim out of the script, so it is the strongest
    signal; the paraphrased scene line and the shot title only break ties.
    """
    body = re.sub(r"\s+", "", content)
    if not body:
        return 0
    score = 0
    for field, weight in (("dialogue", 3), ("scene_description", 1), ("title", 1)):
        text = re.sub(r"\s+", "", str(shot.get(field) or ""))
        if len(text) < 6:
            continue
        grams = {text[index:index + 6] for index in range(len(text) - 5)}
        score += weight * sum(1 for gram in grams if gram in body)
    return score


def match_existing_shots(segments: list[dict], shots) -> dict[str, list[dict]]:
    """Trace a stored board's shots back to the scenes they were written from.

    A board under repair may cover only the opening scenes -- that is the defect
    a repair is supposed to fix -- so shots are matched by what they say rather
    than by position. Assigning a stored tail to later scenes would hide exactly
    the missing coverage the repair has to notice. Matching only walks forward,
    so a shot can belong to its own scene or a later one, never an earlier one.
    """
    result = {segment["key"]: [] for segment in segments}
    if not segments or not shots:
        return result
    cursor = 0
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        scores = [_scene_affinity(shot, segment["content"]) for segment in segments[cursor:]]
        best = max(range(len(scores)), key=lambda index: scores[index]) if scores else 0
        if scores and scores[best] > 0:
            cursor += best
        result[segments[cursor]["key"]].append(shot)
    return result


def coverage_finding(shots: list[dict], segments: list[dict]) -> dict | None:
    """Report a board that cannot physically cover every scene of the chapter.

    Each scene needs at least one clip, so a board with fewer shots than scenes
    is provably incomplete no matter how good the individual shots are. This is
    the deterministic counterpart to the hard coverage check in ``generate``:
    it stops the review stage from approving a truncated board.

    Callers pass an empty ``segments`` for a chapter whose source defines its own
    timeline. Those chapters pack several editorial cuts into one long clip, so a
    shot count below the scene count is normal rather than a coverage gap.
    """
    if not segments or len(shots) >= len(segments):
        return None
    return {
        "severity": "blocking",
        "location": "整批镜头",
        "issue": (
            f"本章共 {len(segments)} 个场景，分镜表只有 {len(shots)} 个镜头，"
            "少于场景数量，不可能覆盖全部场景。"
        ),
        "suggestion": "逐场补齐缺失场景的镜头后重新提交审核，不要减少场景或合并剧情。",
    }


def scenes_for_coverage(content: str, durations: list[float]) -> list[dict]:
    """The scenes a board must cover, or none when a source timeline rules.

    ``generate`` only segments a chapter when the source has no complete
    timeline, so the coverage gate has to use exactly the same condition or it
    would demand more shots than a timed chapter's long clips are meant to hold.
    """
    if not content:
        return []
    try:
        if allocate_timeline(content, durations):
            return []
    except RuntimeError:
        # A source timeline that cannot be expressed with the model's durations
        # is a timing problem the generation path already reports; the coverage
        # gate must not turn it into a second, unrelated failure.
        return []
    return segment_script(content)


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
            value, _ = decoder.raw_decode(text, match.start())
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
                row, end = decoder.raw_decode(text, offset)
            except ValueError:
                break
            if not isinstance(row, dict):
                break
            rows.append(row)
            offset = end
    return rows


def complete_output(text: str) -> bool:
    decoder = json.JSONDecoder()
    clean = text.strip().removeprefix('```json').removeprefix('```').strip()
    starts = [0] if clean.startswith('[') else [m.start() for m in re.finditer(r'\{', clean)]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(clean, start)
        except ValueError:
            continue
        if isinstance(value, list) or (isinstance(value, dict) and isinstance(value.get('shots'), list)):
            return True
    return False


# A review writes its location as "镜12", "镜头 20-30", "镜03-05、10" or
# "镜头 01-33（重点 09→10、18→19）". The emphasis form is the common one for a
# board-wide finding: it names the whole range but only means the shots after
# the marker, and expanding the full range would rewrite the very shots the
# finding asked to leave alone.
FINDING_EMPHASIS = re.compile(r"(?:重点|尤其|特别是|主要集中在|主要是)[：:\s]*")
FINDING_RANGE = re.compile(r"(\d{1,3})\s*[-–—~至]\s*(\d{1,3})")
FINDING_INDEX = re.compile(r"\d{1,3}")
# Free-form user feedback is not a clean location string, so its numbers only
# count when attached to an explicit shot marker ("第3镜", "镜12", "镜头 20-30").
USER_SHOT_REFERENCE = re.compile(
    r"镜(?:头)?\s*(\d{1,3})(?:\s*[-–—~至]\s*(\d{1,3}))?|(\d{1,3})\s*镜(?:头)?"
)


def parse_finding_targets(findings, shot_count: int) -> dict[int, list[dict]]:
    """Map the shot indices a review named to the findings about that shot.

    Findings that point at no particular shot are dropped here rather than
    widened to the whole board: they are handled by the batch path, which is the
    only place whole-board advice can be applied.
    """
    targets: dict[int, list[dict]] = {}
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        explicit = finding.get("shot_indices")
        if isinstance(explicit, list) and explicit:
            for index in sorted({i for i in explicit if type(i) is int and 1 <= i <= shot_count}):
                targets.setdefault(index, []).append(finding)
            continue
        location = str(finding.get("location") or "")
        # Parenthesized comparison/context shots are not repair targets.
        location = re.sub(r"[（(]\s*(?:对照|参考|关联|其余|其馀|比较|原本)[^）)]*[）)]", "", location)
        emphasis = FINDING_EMPHASIS.search(location)
        scope = location[emphasis.end():] if emphasis else location
        indices: set[int] = set()
        for start, end in FINDING_RANGE.findall(scope):
            low, high = sorted((int(start), int(end)))
            indices.update(range(max(1, low), min(shot_count, high) + 1))
        indices.update(int(number) for number in FINDING_INDEX.findall(scope))
        for index in sorted(indices):
            if 1 <= index <= shot_count:
                targets.setdefault(index, []).append(finding)
    return targets


def parse_user_targets(feedback: str, shot_count: int) -> set[int]:
    """Shot indices named in a user's own repair instructions."""
    indices: set[int] = set()
    for start, end, single in USER_SHOT_REFERENCE.findall(feedback or ""):
        if single:
            indices.add(int(single))
        elif start and end:
            low, high = sorted((int(start), int(end)))
            indices.update(range(max(1, low), min(shot_count, high) + 1))
        elif start:
            indices.add(int(start))
    return {index for index in indices if 1 <= index <= shot_count}


# Which fields a finding is actually asking to change. Repair used to rewrite
# the entire shot, which regenerated fields nobody complained about and made the
# reviewer's next pass chase fresh drift.
FIELD_HINTS = (
    ("frame_layout", ("frame_layout", "机位", "相机", "轴线", "越轴", "构图", "视点")),
    ("image_prompt", ("image_prompt", "首帧", "入镜画面")),
    ("action_description", ("action_description", "动作", "运镜", "节拍", "蒙太奇")),
    ("dialogue", ("dialogue", "台词", "对白", "语速", "画外音")),
    ("asset_names", ("asset", "资产", "服装", "道具", "造型")),
    ("duration_seconds", ("时长", "duration")),
    ("scene_description", ("scene_description", "场景描述")),
    ("continuity_group", ("continuity_group", "承接", "接续", "分组")),
    ("emotion_plan", ("情绪", "emotion")),
    ("combat_plan", ("战斗", "combat")),
    ("title", ("标题", "title")),
)


def finding_fields(findings) -> list[str]:
    explicit = {field for item in (findings or []) if isinstance(item, dict)
                for field in (item.get("fields") or []) if isinstance(field, str)}
    if explicit:
        return sorted(explicit & REPAIR_FIELDS)
    text = " ".join(
        f"{item.get('issue', '')} {item.get('suggestion', '')}"
        for item in (findings or [])
        if isinstance(item, dict)
    ).lower()
    return [name for name, hints in FIELD_HINTS if any(hint in text for hint in hints)]


def decode_repair_reply(text: str) -> list[dict]:
    """Accept a partial patch (``fields``) or a whole repaired shot."""
    clean = text.strip().removeprefix("```json").removeprefix("```").strip()
    try:
        value = json.loads(clean)
    except ValueError:
        return [row for row in decode_shots(text) if isinstance(row, dict)]
    if isinstance(value, dict):
        if isinstance(value.get("fields"), dict):
            return [value]
        if isinstance(value.get("shots"), list):
            return [row for row in value["shots"] if isinstance(row, dict)]
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return [row for row in decode_shots(text) if isinstance(row, dict)]


def missing_shot_finding(item):
    text = json.dumps(item, ensure_ascii=False)
    return bool(re.search(r"(?:补充|补|新增|增加|插入)\s*[一二三四五六七八九十\d]*\s*个?镜", text)
                or "缺镜" in text)


REPAIR_FIELDS = {name for name, _ in FIELD_HINTS} | {"shot_type", "internal_shots"}


def invalid_shot_fields(error: Exception) -> list[str]:
    """Restrict schema repairs to the fields the validator actually rejected."""
    if isinstance(error, ValidationError):
        return sorted({str(item["loc"][0]) for item in error.errors()
                       if item["loc"] and item["loc"][0] in REPAIR_FIELDS})
    message = str(error)
    for needle, field in (("表演时间轴", "emotion_plan"), ("打斗时间轴", "combat_plan"),
                          ("内部切镜", "internal_shots"), ("时长", "duration_seconds"),
                          ("duration_seconds", "duration_seconds")):
        if needle in message:
            return [field]
    return []


def merge_shot_patch(stored: dict, rows: list[dict], index: int, fields: list[str]) -> dict:
    """Apply a partial repair reply onto the stored shot.

    A reply that names the fields it changed is merged onto the stored shot so
    everything the review did not mention survives byte for byte. Anything else
    is treated as a full replacement, but only fields the review actually named
    are taken from it -- a repair must not silently rewrite unrelated content.
    """
    if len(rows) != 1 or rows[0].get("order_index", index) != index:
        raise ValueError("补丁必须且只能对应本镜")
    patch = rows[0].get("fields", rows[0])
    if not isinstance(patch, dict):
        raise ValueError("fields 必须是字段补丁对象")
    allowed = {key: value for key, value in patch.items()
               if key in (set(fields) if fields else REPAIR_FIELDS)}
    if not allowed:
        raise ValueError("补丁没有可应用的目标字段")
    return {**stored, **allowed, "order_index": index}


async def generate(request, runtime_factory, *, plan, durations, state, save, progress,
                   script: str = "", budget: int | None = None,
                   repair_shots: list[dict] | None = None,
                   findings: list[dict] | None = None,
                   feedback: str = "", batch_attempts: int = 3, partial: bool = True,
                   concurrency: int = 1, isolate_failures: bool = False,
                   first_frame_mode: bool | None = None):
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
    # The two timelines are first-class storyboard fields: naming them in the
    # reply schema is what makes the model return them instead of dropping them.
    # The full schemas ride in the system prompt, so this stays a compact pointer
    # rather than a nested $defs block repeated in every request.
    properties = {
        **properties,
        "combat_plan": {"type": ["object", "null"], "description": "战斗时间轴；非战斗为 null"},
        "emotion_plan": {"type": ["object", "null"], "description": "人物情绪时间轴；无人物为 null"},
    }
    basic_fields = ("title", "shot_type", "duration_seconds", "scene_description",
                    "action_description", "dialogue", "image_prompt", "video_prompt", "asset_names",
                    "combat_plan", "emotion_plan")
    schema += '\n输出JSON Schema：' + json.dumps({"type": "object", "required": ["shots"],
        "properties": {"shots": {"type": "array", "minItems": 1, "maxItems": 200,
            "items": {"type": "object", "required": ["title", "duration_seconds", "image_prompt"],
                "properties": {**{k: properties[k] for k in basic_fields},
                    "duration_seconds": {"type": "number", "enum": durations},
                    "order_index": {"type": "integer", "minimum": 1}}}}}}, ensure_ascii=False)
    allowed = {Decimal(str(d)) for d in durations}
    state.setdefault("valid", {})
    state.setdefault("raw", {})
    candidates = state.setdefault("repair_candidates", {})
    manifest = state.get("manifest", {})
    # Without a source timeline every scene is its own unit; the board is only
    # complete once all of them have shots. With a timeline the plan already
    # fixes the slots, so batching stays as it was.
    # Pin boundaries in the checkpoint, including after an application upgrade.
    segments = state.setdefault("segments", segment_script(script) if script and not plan else [])
    state.setdefault("segment_shots", {})
    # Repairing an existing board runs scene by scene exactly like generation.
    # Sending the whole board in one request is what let a truncated reply
    # overwrite a complete board with only its opening scenes.
    repair = bool(repair_shots is not None and segments)
    existing_by_segment = match_existing_shots(segments, repair_shots or []) if repair else {}
    if repair:
        state.setdefault("repair_floor", sum(len(items) for items in existing_by_segment.values()))

    # Timed chapters repair each batch of sliding windows. The whole board must
    # not travel with every request -- a long chapter's board is hundreds of
    # kilobytes, and repeating it per batch is what exhausted the context. Only
    # the shots inside the current window are attached, by order index.
    existing_by_index = {
        index: {**row, "order_index": index}
        for index, row in enumerate(repair_shots or [], 1)
        if isinstance(row, dict)
    }
    if repair:
        existing_by_segment = match_existing_shots(segments, list(existing_by_index.values()))

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
        if shot.combat_plan:
            shot.combat_plan.validate_duration(float(shot.duration_seconds))
        if shot.emotion_plan:
            shot.emotion_plan.validate_duration(float(shot.duration_seconds))
        if any(part.end_seconds > float(shot.duration_seconds) + .01 for part in shot.internal_shots):
            raise ValueError("内部切镜时间不得超过本视频片段时长")
        updates = {"video_prompt": ""}
        if first_frame_mode is False:
            from app.services.first_frame_policy import validate_independent_text
            for field in ("scene_description", "action_description", "image_prompt"):
                validate_independent_text(getattr(shot, field), field)
            updates["continuity_group"] = ""
        return shot.model_copy(update=updates).model_dump(mode="json")

    async def call(prompt, label, evidence_files=()):
        await progress(label)
        scoped_request = request
        if evidence_files:
            from app.services.retrieval_context import mount
            scoped_request = mount(request, *evidence_files)
        if request.tool_mode == "retrieval" and len(prompt) > 12000:
            from app.services.retrieval_context import evidence_file, mount
            source = evidence_file("local-operation", prompt)
            scoped_request = mount(scoped_request, source)
            prompt = (f"{label}。本次局部操作要求与完整证据在 {source.path}。"
                      "先 Read 开头的操作与输出要求，再通过 Ripgrep/AstGrep/Read 获取本镜字段、审核问题和相关原文。"
                      "只返回本次操作要求的 JSON，不读取整章或其他无关镜头。")
        current = scoped_request.model_copy(update={"prompt": prompt, "tool_mode": "retrieval" if request.tool_mode == "retrieval" else "none",
            "system_prompt": request.system_prompt + CLIP_RULES +
                '\n本次局部输出协议优先于模板的整章输出要求：仅返回当前请求指定的批次或镜头；'
                '当请求 fields 补丁时，只返回该镜头待修改字段，平台合并其余内容。'
                '仅在明确要求 insert_after 时允许返回待插入的新镜头。',
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

    async def discard_batch(batch_key: str) -> None:
        """Forget a batch reply that can never validate.

        The checkpoint is what lets a retry skip paid work. Keeping an
        unparseable reply in it has the opposite effect: every retry decodes the
        same broken bytes, fails within seconds, and burns the whole attempt
        budget without ever asking the model again. Dropping only the failed
        batch makes the retry regenerate that batch while every finished batch
        stays checkpointed.
        """
        state["raw"].pop(batch_key, None)
        state["segment_shots"].pop(batch_key, None)
        candidates.pop(batch_key, None)
        await save(state)

    async def targeted_repair() -> bool:
        """Patch only the shots a review named, and only the fields it named.

        Regenerating a whole scene because one of its shots was flagged rebuilt
        the shots nobody complained about, which made repair slow, expensive and
        prone to fresh drift on the next review. Everything the review did not
        mention is seeded as already valid and stays byte for byte. Invalid
        patches retry only that shot and retain previously completed patches.
        """
        if repair_shots is None or not partial:
            return False
        named = parse_finding_targets(findings, len(existing_by_index))
        user_targets = parse_user_targets(feedback, len(existing_by_index))
        for index in user_targets:
            named.setdefault(index, [])
        if not named:
            return False
        # Severity and target count never grant permission to rewrite other shots.
        global_findings = [item for item in (findings or []) if isinstance(item, dict)
                           and not parse_finding_targets([item], len(existing_by_index))]
        if any(item.get("severity") in {"blocking", "major"} for item in global_findings):
            return False
        # Seed the untouched part of the board so only the named shots move.
        for index, row in existing_by_index.items():
            if str(index) not in state["valid"]:
                try:
                    state["valid"][str(index)] = validate(row, index)
                except (ValueError, TypeError):
                    continue
        scene_by_index: dict[int, dict] = {}
        for scene in segments:
            owned = []
            for row in existing_by_segment.get(scene["key"], []):
                key = row.get("order_index")
                if isinstance(key, int):
                    scene_by_index[key] = scene
                    owned.append(key)
            state["segment_shots"][scene["key"]] = sorted(
                {str(key) for key in owned}, key=int
            )
        # A named slot with no stored shot cannot be partially patched; leaving
        # it out of ``valid`` lets the scene path below generate it.
        completed = state.setdefault("repaired_indices", [])
        pending = [index for index in sorted(named) if index in existing_by_index
                   and not (index in completed and str(index) in state["valid"])]
        for index in pending:
            state["valid"].pop(str(index), None)
        await save(state)
        if not pending:
            return True
        repaired = 0
        insertions = state.setdefault("repair_insertions", {})
        async def repair_one(index):
            nonlocal repaired
            stored = existing_by_index[index]
            candidate_key = f"target-{index}"
            candidate = candidates.get(candidate_key, stored)
            if candidate_key in candidates:
                try:
                    state["valid"][str(index)] = validate(candidate, index)
                except (ValueError, TypeError):
                    pass
                else:
                    completed.append(index)
                    candidates.pop(candidate_key, None)
                    await save(state)
                    return
            scene = scene_by_index.get(index)
            issues = list(named.get(index) or [])
            if index in user_targets:
                issues.append({"issue": feedback})
            fields = finding_fields(issues)
            addition_issues = [issue for issue in issues if missing_shot_finding(issue)
                               and index == min(parse_finding_targets([issue], len(existing_by_index)) or [index])]
            allow_insert = bool(addition_issues) and not plan
            neighbors = {str(i): state["valid"].get(str(i)) or existing_by_index.get(i)
                         for i in (index - 1, index + 1)}
            if insertions.get(str(index - 1)):
                neighbors[str(index - 1)] = insertions[str(index - 1)][-1]
            request_prompt = (
                f'只修复第 {index} 镜，返回 {{"fields":{{...}}}}，只包含确实需要改的字段。'
                "未被审核点名、也不需要配合修改的字段必须原样省略，不要复述整个镜头。"
                "允许的字段：title、shot_type、duration_seconds、scene_description、"
                "action_description、dialogue、image_prompt、asset_names、"
                "continuity_group、frame_layout、combat_plan、emotion_plan、internal_shots。\n"
                f'合法时长：{json.dumps(durations)}'
                f"\n本镜审核意见：{json.dumps(named.get(index) or [], ensure_ascii=False)}"
                f"\n用户意见：{feedback if index in user_targets or not user_targets else '无'}"
                "\n资产清单："
                + json.dumps(
                    sorted({
                        str(name)
                        for row in existing_by_index.values()
                        for name in (row.get("asset_names") or [])
                    }),
                    ensure_ascii=False,
                )
                + f"\n【审核点名优先，只改被点名的字段】"
                f"\n相邻镜头仅供衔接：{json.dumps(neighbors, ensure_ascii=False)}"
            )
            if scene is not None:
                request_prompt += f"\n本镜所属场景正文：\n{scene['content']}"
            if allow_insert:
                request_prompt += (
                    '\n本次允许补镜：返回 {"fields":{本镜必要衔接补丁},"insert_after":[完整新镜头]}。'
                    'insert_after 插在本镜之后、下一原镜之前；只补审核明确缺失的剧情与台词，不复述已有动作。'
                    '必须给每个新增镜头分配模型合法时长，完整填写与原镜相同的字段。'
                    '如能在本镜合法时长内补全则返回空数组；禁止把缺失内容推给未修改的下一镜。'
                    f'\n补镜依据：{json.dumps(addition_issues, ensure_ascii=False)}'
                    f'\n全章目标秒数：{budget}；原镜总秒数：{sum(float(row["duration_seconds"]) for row in existing_by_index.values())}'
                )
            local_files = []
            if request.tool_mode == "retrieval":
                from app.services.retrieval_context import evidence_file, json_evidence
                constraints = request_prompt.split("\n资产清单：", 1)[0]
                local_files = [json_evidence("repair-neighbors", neighbors),
                    evidence_file("repair-source", scene["content"] if scene else "请按需要检索 chapter-script 原文，不能虚构依据"),
                    evidence_file("repair-instructions", request_prompt)]
                request_prompt = (constraints + "\n先 Read 当前镜头文件，按需 AstGrep 定位字段。"
                    "存在衔接问题时检索 project-files/retrieval-repair-neighbors/repair-neighbors.json；"
                    "台词和缺失剧情到 project-files/retrieval-repair-source/repair-source.md 查证。"
                    "资产名称、补镜授权及完整局部规则在 project-files/retrieval-repair-instructions/repair-instructions.md，"
                    "先搜索再读取相关内容，不读取全章分镜。")
            error_hint = ""
            for attempt in range(batch_attempts):
                try:
                    if request.tool_mode == "retrieval":
                        target = json_evidence("repair-shot", candidate)
                        operation = request_prompt + f"\n当前镜头文件：{target.path}" + error_hint
                        output = await call(operation, f"正在定点修复第 {index} 镜", [*local_files, target])
                    else:
                        output = await call(request_prompt + f"\n原始镜头：{json.dumps(candidate, ensure_ascii=False)}"
                                            + error_hint, f"正在定点修复第 {index} 镜")
                    rows = decode_repair_reply(output)
                    candidate = merge_shot_patch(candidate, rows, index, fields)
                    extras = rows[0].get("insert_after", []) if rows else []
                    if extras and not allow_insert:
                        raise ValueError("本次未允许增加片段，请在原时间窗内修复")
                    if not isinstance(extras, list) or len(extras) > 8:
                        raise ValueError("补镜必须为最多8项的镜头数组")
                    checked_extras = [validate({**row, "order_index": index}, index) for row in extras]
                    if checked_extras and budget:
                        from app.services.task_worker import validate_duration_budget
                        all_rows = [candidate if key == index else state["valid"].get(str(key), row)
                                    for key, row in existing_by_index.items()]
                        all_rows += checked_extras + [row for key, additions in insertions.items()
                                                     if key != str(index) for row in additions]
                        validate_duration_budget([GeneratedStoryboardShotPayload.model_validate(row)
                                                  for row in all_rows], budget)
                    candidates[candidate_key] = candidate
                    insertions[str(index)] = checked_extras
                    await save(state)
                    state["valid"][str(index)] = validate(candidate, index)
                    break
                except (ValueError, TypeError, RuntimeError, KeyError, httpx.TransportError) as error:
                    if "已停止" in str(error) or "上下文已失效" in str(error):
                        raise
                    error_hint = f"\n上次补丁校验失败，只修正本镜补丁：{str(error)[:1000]}"
                    if attempt + 1 == batch_attempts:
                        await save(state)
                        raise RuntimeError(f"第 {index} 镜局部修复暂未完成，其他镜头已保存：{error}") from error
                    await progress(f"第 {index} 镜补丁重试 {attempt + 1}/{batch_attempts - 1}")
            completed.append(index)
            candidates.pop(candidate_key, None)
            repaired += 1
            await save(state)
            await progress(f"已定点修复第 {index} 镜（{repaired}/{len(pending)}），其余镜头保持不变")
        # Adjacent targets and targets sharing a finding depend on each other.
        # Only independent heads run together; failures do not discard siblings.
        remaining = list(pending)
        errors = []
        failed = set()
        while remaining:
            blocked = [index for index in remaining if any(
                abs(index - other) <= 1 or any(issue in named.get(other, []) for issue in named.get(index, []))
                for other in failed)]
            for index in blocked:
                remaining.remove(index)
                errors.append(f"第 {index} 镜等待关联镜头修复，已保留断点")
            if not remaining:
                break
            wave = []
            for index in remaining:
                if any(abs(index - other) <= 1 or any(
                    issue in named.get(other, []) for issue in named.get(index, [])
                ) for other in wave):
                    continue
                wave.append(index)
                if len(wave) >= max(1, concurrency):
                    break
            results = await asyncio.gather(*(repair_one(index) for index in wave), return_exceptions=True)
            for index, result in zip(wave, results):
                remaining.remove(index)
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError) or "已停止" in str(result) or "上下文已失效" in str(result):
                        raise result
                    errors.append(str(result))
                    failed.add(index)
        if errors:
            raise RuntimeError("；".join(errors))
        return True

    if await targeted_repair():
        # Only return here when the targeted pass really covered the whole
        # board. A missing scene or an unpatched shot falls through to the
        # batch path, which regenerates just that unit.
        expected = (
            [index for scene in segments for index in
             (int(key) for key in state["segment_shots"].get(scene["key"], []))]
            if segments
            else list(range(1, len(plan) + 1)) if plan else sorted(existing_by_index)
        )
        if expected and all(str(index) in state["valid"] for index in expected):
            if segments and any(not state["segment_shots"].get(scene["key"]) for scene in segments):
                pass  # A scene with no shots must still be generated below.
            else:
                rows = []
                for index in sorted(expected):
                    rows.append(state["valid"][str(index)])
                    rows.extend(state.get("repair_insertions", {}).get(str(index), []))
                rows = [{**row, "order_index": index} for index, row in enumerate(rows, 1)]
                board = StoryboardGenerationPayload.model_validate({"shots": rows})
                return board.model_dump_json(), state.get("manifest", manifest)

    # Preserve completed scene rows independently from chapter-wide numbering.
    # If an earlier scene gains a shot, later scenes are reindexed, never erased.
    completed_batches = state.setdefault("completed_batches", {})
    for key, owned in state["segment_shots"].items():
        if owned and all(str(i) in state["valid"] for i in owned):
            completed_batches.setdefault(key, [state["valid"][str(i)] for i in owned])
    if segments:
        for owned in state["segment_shots"].values():
            for index in owned:
                state["valid"].pop(str(index), None)

    # Freeze the allocation and scene boundaries before parallel requests. The
    # ordered consumer below still owns global numbering and the actual budget.
    state.setdefault("generation_plan", [
        {"key": key, "label": segment["label"] if segment else f"时间窗 {number}",
         "target_seconds": _scene_budget(segment, segments, budget) if segment and budget else
             sum(float(slot["duration_seconds"]) for slot in group) or None}
        for number, (key, group, segment) in enumerate(units, 1)
    ])

    def generation_prompt(unit_no, key, group, segment, ceiling=None):
        base = _scene_prompt_base(request.prompt, script)
        if group:
            base = base.partition("\n原始剧本时间轴约束（")[0]
        previous_key = units[unit_no - 2][0] if unit_no > 1 else None
        previous = completed_batches.get(previous_key, [])[-1:]
        if group and group[0]["order_index"] > 1:
            previous = [state["valid"].get(str(group[0]["order_index"] - 1))]
            previous = [row for row in previous if row]
        prompt = base + '\n' + schema
        if ceiling is not None:
            prompt += f'\n已为其它批次保留时长，本批 duration_seconds 总和上限 {ceiling:g} 秒，不得改动其它批次。'
        if state.get("failed_units", {}).get(key):
            prompt += '\n上次本批错误，纠正本批即可：' + state["failed_units"][key][:1000]
        if group:
            prompt += ('\n本次仅生成以下时间槽，不能生成其它镜头，不能改变起止时间和时长：'
                       + json.dumps(group, ensure_ascii=False)
                       + '\n衔接上一镜：' + json.dumps(previous, ensure_ascii=False))
            current_window = [existing_by_index[i["order_index"]] for i in group if i["order_index"] in existing_by_index]
            if current_window:
                prompt += '\n本时间窗现有镜头（只修复指出的问题）：' + json.dumps(current_window, ensure_ascii=False)
            neighbors = plan[max(0, group[0]["order_index"] - 2):group[0]["order_index"] - 1]
            neighbors += plan[group[-1]["order_index"]:group[-1]["order_index"] + 1]
            prompt += '\n仅供衔接、不得重复生成的相邻时间槽：' + json.dumps(neighbors, ensure_ascii=False)
        elif segment is not None:
            # Never let an out-of-order completed future batch become the
            # "previous shot". All workers share the same source boundaries.
            context_state = {**state, "valid": {"previous": row for row in previous}}
            prompt += (_repair_brief(segment, segments, context_state,
                                     existing_by_segment.get(key, []), budget=budget) if repair else
                       _segment_brief(segment, segments, context_state, budget=budget))
            before = segments[unit_no - 2]["content"][-600:] if unit_no > 1 else "章节开始"
            after = segments[unit_no]["content"][:600] if unit_no < len(segments) else "章节结束"
            prompt += (
                '\n跨批次衔接契约（仅作上下文，不得生成相邻场景）：\n前场末尾：' + before
                + '\n后场开头：' + after
                + '\n本场必须承接前场动作方向、人物站位、服装伤势与道具归属；'
                  '结尾为后场保留可接续状态，不得自行复位、跳时或增加未发生的事件。'
            ) if concurrency > 1 else ''
        return prompt

    prefetches = {}

    def start_prefetch(position):
        if concurrency <= 1 or position >= total_units or position in prefetches:
            return
        key, group, segment = units[position]
        if key in state["raw"] or key in completed_batches:
            return
        if group and all(str(slot["order_index"]) in state["valid"] for slot in group):
            return

        async def fetch():
            try:
                state["raw"][key] = await call(generation_prompt(position + 1, key, group, segment),
                                              f"正在生成第 {position + 1}/{total_units} 批分镜")
                await save(state)
                return None
            except Exception as error:
                # The consumer applies the existing batch retry budget. Keep
                # exceptions observable even when another batch stops the run.
                return error

        prefetches[position] = asyncio.create_task(fetch())

    def independent_next(position):
        if position + 1 >= total_units:
            return False
        _, left, left_scene = units[position]
        _, right, right_scene = units[position + 1]
        if left_scene and right_scene:
            if left_scene.get("parts", 1) > 1 and left_scene["label"] == right_scene["label"]:
                return False
            return not (contains_combat(left_scene["content"]) and contains_combat(right_scene["content"]))
        return not (contains_combat(json.dumps(left, ensure_ascii=False)) and
                    contains_combat(json.dumps(right, ensure_ascii=False)))

    async def process_unit(unit_no, key, group, segment):
        # Missing timed rows use only scoped source evidence for local repair.
        base = _scene_prompt_base(request.prompt, script)
        if group:
            base = base.partition("\n原始剧本时间轴约束（")[0]
        indices = [slot["order_index"] for slot in group]
        ceiling = None
        if budget and not plan:
            previous_keys = {u[0] for u in units[:unit_no - 1]}
            spent = sum(float(row["duration_seconds"]) for k, rows in completed_batches.items()
                        if k in previous_keys for row in rows)
            reserve = sum(
                sum(float(row["duration_seconds"]) for row in completed_batches[future_key])
                if future_key in completed_batches else
                min(durations) * max(1, len(existing_by_segment.get(future_key, [])))
                for future_key, _, _ in units[unit_no:]
            )
            ceiling = float(budget + max(budget * .2, 5)) - spent - reserve
            if ceiling + .001 < min(durations) * max(1, len(existing_by_segment.get(key, []))):
                raise RuntimeError("本章时长预算不足以保留全部场景和现有镜头，请增加章节时长；已完成内容保留")
        if indices:
            for i in indices:
                if str(i) not in state["valid"]:
                    continue
                try:
                    validate(state["valid"][str(i)], i)
                except (ValueError, TypeError):
                    # Legacy timed checkpoints can contain rows accepted before
                    # timeline validation existed. Repair them, never loop on an
                    # invalid "completed" row without calling the model.
                    candidates.setdefault(key, {})[str(i)] = state["valid"].pop(str(i))
            if all(str(i) in state["valid"] for i in indices):
                return
            restored = [state["valid"].get(str(i)) or candidates.get(key, {}).get(str(i)) for i in indices]
            if key not in state["raw"] and any(restored):
                state["raw"][key] = json.dumps({"shots": [row for row in restored if row]}, ensure_ascii=False)
            await save(state)
        if segment is not None and key in completed_batches:
            # This scene was already generated in an earlier attempt; its shots
            # are checkpointed, so no paid request is repeated for it.
            completed = completed_batches[key]
            minimum = len(existing_by_segment.get(key, [])) if repair else 1
            repair_cached_rows = False
            if (len(completed) >= minimum and completed and
                    (ceiling is None or sum(float(row["duration_seconds"]) for row in completed) <= ceiling)):
                offset = segment_offset(segments, state, segment)
                owned = [str(offset + i) for i in range(1, len(completed) + 1)]
                try:
                    restored = {i: validate({**row, "order_index": int(i)}, int(i)) for i, row in zip(owned, completed)}
                except (ValueError, TypeError):
                    # Old checkpoints may predate a field-level validator. Feed
                    # their rows into local repair, not another scene generation.
                    state["raw"][key] = json.dumps({"shots": completed}, ensure_ascii=False)
                    repair_cached_rows = True
                else:
                    state["valid"].update(restored)
                    state["segment_shots"][key] = owned
                    await save(state)
                    return
            # A checkpoint named this scene but its shots are not there. Treating
            # that as "done" would publish a board that silently skips a scene,
            # so the scene is generated again instead.
            state["segment_shots"].pop(segment["key"], None)
            if not repair_cached_rows:
                state["raw"].pop(key, None)
            completed_batches.pop(key, None)
        # Built even when the reply is already checkpointed: a batch whose
        # stored reply cannot be validated is re-asked with this exact prompt.
        prompt = generation_prompt(unit_no, key, group, segment, ceiling)
        if key not in state["raw"]:
            state["raw"][key] = await call(prompt, f"正在生成第 {unit_no}/{total_units} 批分镜")
            await save(state)
        raw = state["raw"][key]
        rows = decode_shots(raw)
        # A wholly unreadable reply receives a formatting repair, not a fresh storyboard.
        if not rows or (group and len(rows) > len(group)) or (not group and not complete_output(raw)):
            if len(raw) > 40_000:
                await discard_batch(key)
                raise RuntimeError("分镜返回格式错误且内容过长，已保留其它批次；请缩小生成范围")
            fixed = await call(schema + '\n仅修复下列已有输出的JSON结构，不重写故事。'
                + '\n本批时间槽：' + json.dumps(group, ensure_ascii=False)
                + '\n已有输出：\n' + raw, f"正在修复第 {unit_no} 批返回格式")
            rows = decode_shots(fixed)
            state["raw"][key] = fixed
            await save(state)
            if not group and not complete_output(fixed):
                await discard_batch(key)
                raise RuntimeError(
                    f"第 {unit_no} 批分镜输出仍被截断，无法确定完整镜头数量；"
                    "已保留其它批次，重试只会重新生成这一批"
                )
        if group and len(rows) > len(group):
            await discard_batch(key)
            raise RuntimeError(
                f"第 {unit_no} 批分镜数超过时间槽数量，格式修复后仍不一致；"
                "已保留其它批次，重试只会重新生成这一批"
            )
        if not group:
            if not rows or len(rows) > 200:
                await discard_batch(key)
                raise RuntimeError(
                    f"第 {unit_no} 批分镜结构修复失败：缺少有效shots数组；"
                    "已保留其它批次，重试只会重新生成这一批"
                )
            rows = [row for row in rows if isinstance(row, dict)]
            minimum = len(existing_by_segment.get(key, [])) if repair else 1
            if len(rows) < minimum:
                await discard_batch(key)
                raise RuntimeError(
                    f"第 {unit_no} 批镜头数从 {minimum} 降到 {len(rows)}，仅重试本批修复"
                )
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
            pending_rows = candidates.setdefault(key, {})
            row = pending_rows.get(str(index), indexed.get(index, {}))
            error_hint = ""
            # One budget for this shot, including transport and parsing failures.
            # Keep each merged candidate so a later patch builds on prior fixes.
            for attempt in range(batch_attempts + 1):
                try:
                    valid = validate(row, index)
                    break
                except (ValueError, TypeError) as error:
                    if attempt == batch_attempts:
                        raise ShotRepairExhausted(
                            f"第{index}镜修复失败，已保留其它完成镜头及本镜补丁：{str(error)[:600]}"
                        ) from error
                    neighbors = {str(i): state["valid"].get(str(i), indexed.get(i))
                                 for i in (index - 1, index + 1)}
                    fields = invalid_shot_fields(error) if row else []
                    prompt = (schema + f'\n仅返回第{index}镜，shots内只能有一项；不要返回整份分镜。'
                        + '\n错误：' + str(error)[:1500]
                        + '\n合法时长：' + json.dumps(durations)
                        + '\n时间槽：' + json.dumps(plan[index - 1] if plan else {}, ensure_ascii=False)
                        + '\n原镜头：' + json.dumps(row, ensure_ascii=False)
                        + '\n相邻镜头仅供衔接：' + json.dumps(neighbors, ensure_ascii=False))
                    if fields:
                        prompt += ('\n本镜已有内容只需字段补丁，优先返回 {"fields":{...}}；'
                                   '仅允许修复以下字段，其余字段由平台保留：' + json.dumps(fields))
                    # Missing rows need source context; existing rows need only a targeted repair.
                    if not row:
                        source = base
                        if segment is not None:
                            source += f"\n本镜所属场景正文：\n{segment['content']}"
                        prompt = source + '\n' + prompt
                    try:
                        fixed = decode_repair_reply(await call(
                            prompt + error_hint, f"正在修复第 {index} 镜（{attempt + 1}/{batch_attempts}）"))
                        row = merge_shot_patch(row, fixed, index, fields) if row else (fixed[0] if len(fixed) == 1 else {})
                        pending_rows[str(index)] = row
                        await save(state)
                    except (ValueError, TypeError, RuntimeError, httpx.TransportError) as repair_error:
                        if "已停止" in str(repair_error) or "上下文已失效" in str(repair_error):
                            raise
                        error_hint = f"\n上次本镜补丁错误：{str(repair_error)[:1000]}"
            state["valid"][str(index)] = valid
            pending_rows.pop(str(index), None)
            await save(state)
            await progress(f"已完成 {len(state['valid'])}/{len(plan) or len(indices)} 镜，结果已保存")
        if ceiling is not None and sum(float(state["valid"][str(i)]["duration_seconds"]) for i in indices) > ceiling + .001:
            for i in indices:
                state["valid"].pop(str(i), None)
            await discard_batch(key)
            raise RuntimeError(f"本批总时长超过剩余预算 {ceiling:g} 秒，仅重新安排本批，不改动已完成批次")
        await progress(f"第 {unit_no}/{total_units} 批已校验并保存，共 {len(state['valid'])} 镜")
        if segment is not None:
            owned = state["segment_shots"].get(segment["key"], [])
            state["segment_shots"][segment["key"]] = sorted(
                set(owned) | {str(i) for i in indices if str(i) in state["valid"]},
                key=int,
            )
            completed_batches[key] = [state["valid"][str(i)] for i in indices]
            await save(state)

    async def consume_unit(unit_no, key, group, segment, prefetched_error=None):
        failures = state.setdefault("failed_units", {})
        for attempt in range(batch_attempts):
            try:
                if attempt == 0 and prefetched_error is not None:
                    raise prefetched_error
                await process_unit(unit_no, key, group, segment)
            except (RuntimeError, ValueError, TypeError, httpx.TransportError) as error:
                if "已停止" in str(error) or "上下文已失效" in str(error):
                    raise
                if hasattr(runtime_factory, "invalid_output"):
                    runtime_factory.invalid_output()
                failures[key] = str(error)[:1500]
                await save(state)
                if isinstance(error, ShotRepairExhausted) or attempt + 1 == batch_attempts:
                    raise RuntimeError(
                        f"第 {unit_no}/{total_units} 批暂未完成，已保存其他批次；"
                        f"继续将只重试本批：{str(error)[:1000]}"
                    ) from error
                await progress(
                    f"第 {unit_no}/{total_units} 批重试 {attempt + 1}/{batch_attempts - 1}，已完成镜头保留"
                )
            else:
                failures.pop(key, None)
                await save(state)
                break
    unfinished = []
    await save(state)
    start_prefetch(0)
    try:
        for position, (key, group, segment) in enumerate(units):
            error = None
            if independent_next(position):
                start_prefetch(position + 1)
            if position in prefetches:
                error = await prefetches.pop(position)
                if error and ("已停止" in str(error) or "上下文已失效" in str(error)):
                    raise error
            try:
                await consume_unit(position + 1, key, group, segment, error)
            except RuntimeError as error:
                if not isolate_failures or "已停止" in str(error) or "上下文已失效" in str(error):
                    raise
                unfinished.append(str(error))
    finally:
        pending = list(prefetches.values())
        for future in pending:
            if not future.done():
                future.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if unfinished:
        raise RuntimeError(f"{len(unfinished)} 批分镜未完成，其余批次已保存；继续仅补齐失败批次。" + "；".join(unfinished)[:1600])
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
