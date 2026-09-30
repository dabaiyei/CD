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
from app.services.speech_pacing import SPEECH_RULES, speech_budget, speech_duration
from app.services.motion_intent import NATURAL_MOTION_RULES
from app.services.locomotion import LOCOMOTION_PLANNING_RULES


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


def assemble_repaired_board(indices, valid, insertions):
    """Renumber explicit planning references after insertion, never dialogue.

    Work on copies so resuming the saved old-numbered patches cannot apply the
    mapping twice. References identify original shots, not the inserted rows.
    """
    mapping, rows = {}, []
    for index in sorted(indices):
        mapping[index] = len(rows) + 1
        rows.append(valid[str(index)])
        rows.extend(insertions.get(str(index), []))
    pattern = re.compile(r"(镜(?:头)?\s*)(\d+)(?!\d)")

    def remap(value):
        if isinstance(value, str):
            return pattern.sub(lambda m: m[1] + str(mapping.get(int(m[2]), int(m[2]))), value)
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {key: item if key in {"dialogue", "text", "asset_names", "asset_ids"}
                    else remap(item) for key, item in value.items()}
        return value

    fields = {"action_description", "scene_description", "image_prompt", "frame_layout",
              "emotion_plan", "combat_plan", "internal_shots"}
    return [{**{key: remap(value) if key in fields else value for key, value in row.items()},
             "order_index": index} for index, row in enumerate(rows, 1)]


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


def source_evidence_window(segments: list[dict], key: str) -> list[dict]:
    """A lexical match is a candidate, not proof of a shot's scene ownership.

    Repeated sets/props often match the previous scene. Supply bounded adjacent
    source segments to both reviewer and editor so they can verify the actual
    action/result, rather than alternately impose two different scene endings.
    """
    position = next((i for i, segment in enumerate(segments) if segment["key"] == key), None)
    if position is None:
        return []
    return [{"key": segment["key"], "label": segment.get("label", ""), "content": segment["content"]}
            for segment in segments[max(0, position - 1):position + 2]]


def compact_paged_generation_prompt(prompt: str) -> str:
    """Remove the full scene body before attaching the current source window."""
    marker = "\n本场正文：\n"
    start = prompt.find(marker)
    if start < 0:
        return prompt
    end_markers = ("\n跨批次衔接契约", "\n原分镜中本场", "\n本场必须")
    ends = [prompt.find(item, start + len(marker)) for item in end_markers]
    ends = [item for item in ends if item >= 0]
    end = min(ends) if ends else len(prompt)
    return prompt[:start] + "\n本场完整正文已拆分为下方 source_ids 分页窗口。" + prompt[end:]


SOURCE_EVIDENCE_RULES = (
    "候选源段来自文本相似度检索，不是已确认的镜头归属。source_context 为按原文顺序提供的邻近源段。"
    "先按具体动作、目标、发生顺序与结果定位本镜实际节拍；重复场景/角色/道具不构成归属证据。"
    "不能把候选源段结尾状态强加给前后场镜头，不能把同一场的早期状态套给后期节拍。"
    "涉及数量、落袋/死亡等结果或动作增删的主要问题，必须引用源段key与准确原句及本镜冲突原句；"
    "先核对source_context是否已有本镜动作，不能仅因source没写就认定凭空新增。"
    "来源不足或无法定位时，不猜测剧情事实，不按邻镜的错误状态反向改写剧本。"
    "修复建议不高于剧本；存在相反建议时依据原文具体节拍处理，不机械执行互斥建议。"
)


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


def unfinished_shot(text: str) -> bool:
    """Distinguish an unfinished row from merely missing outer brackets."""
    start = re.search(r'"shots"\s*:\s*\[', text)
    if not start:
        return False
    offset, decoder = start.end(), json.JSONDecoder()
    while offset < len(text):
        while offset < len(text) and text[offset] in " \r\n\t,":
            offset += 1
        if offset >= len(text) or text[offset] != "{":
            return False
        try:
            _, offset = decoder.raw_decode(text, offset)
        except ValueError:
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
        # Reviewers sometimes name a required companion field only in their
        # suggestion. Silently filtering that field out leaves the original
        # contradiction in place and forces another full review/repair round.
        for item in findings or []:
            if not isinstance(item, dict):
                continue
            suggestion = str(item.get("suggestion") or "")
            timing_clauses = re.split(r"[。；;\n]", suggestion)
            if any(re.search(r"延长|加长|增加时长|调整时长|改用.{0,16}档位|改为.{0,12}秒|重新分配时间轴", clause)
                   and not re.search(r"不(?:得|要|必|需|能)|禁止|保持.{0,8}时长|时长不变", clause)
                   for clause in timing_clauses):
                explicit.add("duration_seconds")
            if "dialogue" in explicit and re.search(r"时长|时间轴|秒上限", suggestion):
                explicit.add("duration_seconds")
            for field in REPAIR_FIELDS:
                token = re.escape(field)
                unchanged = re.search(r"(?:不要|不得|无需|不必|禁止|不修改|保留|保持)\s*`?" + token, suggestion)
                unchanged = unchanged or re.search(token + r"`?\s*(?:不变|无需修改)", suggestion)
                if not unchanged and re.search(r"(?<![a-zA-Z0-9_])" + token + r"(?![a-zA-Z0-9_])", suggestion):
                    explicit.add(field)
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


def coherent_repair_fields(fields):
    """Permit the same corrected fact to stay consistent across its renderings."""
    allowed = set(fields)
    spatial = {"frame_layout", "image_prompt", "scene_description", "action_description", "shot_type"}
    if allowed & spatial or "asset_names" in allowed:
        allowed.update(spatial)
        allowed.update({"emotion_plan", "combat_plan", "internal_shots"})
    if "duration_seconds" in allowed:
        allowed.update({"action_description", "emotion_plan", "combat_plan", "internal_shots"})
    return sorted(allowed)


def validate_action_repair(stored, candidate, issues, reply):
    """A missing-action repair must change the executable action, not its title."""
    if reply.get("unchanged") is True:
        return  # An evidence-backed no-op still goes through the normal review.
    for issue in issues:
        if issue.get("severity") not in {"major", "blocking"}:
            continue
        suggestion = str(issue.get("suggestion", ""))
        missing = re.search(r"缺失|遗漏|漏动作|没有.{0,8}覆盖", str(issue.get("issue", "")))
        add_action = re.search(r"补齐|补足|补入|纳入本镜|动作链|补进", suggestion)
        if missing and add_action and "action_description" in (issue.get("fields") or []):
            if candidate.get("action_description") == stored.get("action_description"):
                raise ValueError("缺失动作仍未修复：action_description 与修复前完全相同；"
                    "不能仅修改标题、场景或首帧宣称动作已发生。请将审核要求的动作补入动作时间轴，"
                    "同步合法时长及关联字段；若问题已消除须返回unchanged及具体证据。")


def missing_shot_finding(item):
    text = json.dumps(item, ensure_ascii=False)
    return bool(re.search(r"(?:补充|补|新增|增加|插入)\s*[一二三四五六七八九十\d]*\s*个?镜", text)
                or "缺镜" in text)


def split_shot_finding(item):
    """Explicit overload/splitting findings authorize local clip insertion."""
    text = str(item.get("issue", "")) + " " + str(item.get("suggestion", ""))
    if not re.search(r"过载|装不进|不足|超出|不够|无法.{0,12}完成|时长", text):
        return False
    # Negated instructions are not permission to change the shot count.
    text = re.sub(r"(?:不要|不得|无需|不必|禁止|不能)[^。；;\n]{0,12}拆[^。；;\n]*", "", text)
    return bool(re.search(r"拆镜|拆(?:成|为)[^。；;\n]{0,15}(?:镜|条)|拆分[^。；;\n]{0,12}镜"
                         r"|新增(?:一|1)?段(?:承接)?镜|拆为连续小段落", text))


REPAIR_FIELDS = {name for name, _ in FIELD_HINTS} | {"shot_type", "internal_shots"}


def invalid_shot_fields(error: Exception) -> list[str]:
    """Restrict schema repairs to the fields the validator actually rejected."""
    if isinstance(error, ValidationError):
        return sorted({str(item["loc"][0]) for item in error.errors()
                       if item["loc"] and item["loc"][0] in REPAIR_FIELDS})
    message = str(error)
    if "语速预算不足" in message:
        return ["duration_seconds", "action_description", "emotion_plan", "combat_plan", "internal_shots"]
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
    target = rows[0].get("order_index", index) if len(rows) == 1 else None
    if isinstance(target, str) and target.isascii() and target.isdigit():
        target = int(target)
    if len(rows) != 1 or target != index:
        raise ValueError("补丁必须且只能对应本镜")
    if rows[0].get("unchanged") is True:
        if rows[0].get("fields") or rows[0].get("edits") or not str(rows[0].get("reason") or "").strip():
            raise ValueError("unchanged 只能附具体理由，不能同时修改字段")
        return dict(stored)  # Normal review still decides whether the issue is resolved.
    patch = rows[0].get("fields", rows[0])
    if not isinstance(patch, dict):
        raise ValueError("fields 必须是字段补丁对象")
    allowed = {key: value for key, value in patch.items()
               if key in (set(fields) if fields else REPAIR_FIELDS)}
    edits = rows[0].get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("edits 必须是原句替换数组")
    edited = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("每项 edits 必须含 field、old、new")
        field = edit.get("field")
        if not isinstance(field, str) or field not in (set(fields) if fields else REPAIR_FIELDS):
            raise ValueError("原句替换超出本次允许的修复字段")
        if field in allowed:
            raise ValueError(f"{field} 不能同时使用 fields 与 edits")
        original = edited.get(field, stored.get(field))
        old, new = edit.get("old"), edit.get("new")
        if not isinstance(original, str) or not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("原句替换仅支持文本字段，old 必须是非空原文")
        count = original.count(old)
        if not count or (count > 1 and edit.get("replace_all") is not True):
            raise ValueError(f"{field} 原句匹配 {count} 处（{old[:180]}）；请逐字使用原文唯一片段，多处替换须明确 replace_all=true")
        edited[field] = original.replace(old, new, -1 if edit.get("replace_all") is True else 1)
    allowed.update(edited)
    if not allowed:
        raise ValueError("补丁没有可应用的目标字段")
    merged = {**stored, **allowed, "order_index": index}
    if "action_description" in allowed or "duration_seconds" in allowed:
        from app.services.source_timeline import extract
        duration = float(merged["duration_seconds"])
        if any(span.end > duration + .01 for span in extract(str(merged.get("action_description") or ""))):
            raise ValueError(f"action_description 时间轴超过本镜 {duration:g} 秒；只修正本镜，不得带入邻镜动作")
    return merged


def direct_patch_request(request, instructions, candidate, neighbors, *, budget=50000):
    """Small, evidence-complete field patches do not need a file-reading agent."""
    if request.tool_mode != "retrieval" or request.documents or request.attachments:
        return None
    from app.services.storyboard_review import local_asset_evidence
    from app.services.clip_timeline import CLIP_RULES
    references = [{"path": file.path, "content": file.content} for file in request.project_files
                  if file.id.startswith(("retrieval-task-rules", "retrieval-task-memory"))]
    # Long task-input files contain whole chapters and boards. Only their
    # complete contract header is needed here; never inline their remaining body.
    inputs = [file for file in request.project_files if file.id.startswith("retrieval-task-input")]
    if inputs:
        header = next((file.content.split("\n用户意见：", 1)[0] for file in inputs
                       if "\n用户意见：" in file.content and "目标视频模型" in file.content), None)
        if header is None:
            return None
        references.append({"path": "model-contract", "content": header})
    assets = local_asset_evidence(request, {"shots": [candidate],
        "neighbors": [row for row in neighbors.values() if row], "asset_history": []})
    prompt = instructions + "\n原始镜头：" + json.dumps(candidate, ensure_ascii=False)
    prompt += "\n本次局部规则和资产证据：" + json.dumps({"references": references, "assets": assets}, ensure_ascii=False)
    from app.services.cinematography import REVIEW_RULES as CAMERA_REVIEW_RULES
    system = (CLIP_RULES + CAMERA_REVIEW_RULES + "\n你是单镜字段补丁编辑器。平台已提供本次完整局部证据，不调用工具。"
              "只修正本镜明确点名的问题，不重新设计或改写其它镜头，保留原画风、台词和未涉及字段。"
              "字段内部未涉及问题的有效信息也必须保留。画风与导演风格沿用原字段，"
              "具体违反规则的内容以审核意见中的证据和修复建议为准，不主动重新解释全部手册。"
              "本次输出协议优先于参考资料中的整章输出协议：只返回 JSON fields 和/或 edits 补丁；"
              "仅明确授权补镜时允许 insert_after，禁止返回整章或审核结论。")
    # A repair includes both the existing field values and its exact change
    # instructions. It may be larger than a verdict packet; keeping that local
    # evidence in one bounded call is cheaper than rereading it over many turns.
    if len(system) + len(prompt) > budget:
        return None
    return request.model_copy(update={"prompt": prompt, "system_prompt": system,
        "tool_mode": "none", "project_files": [], "skills": [], "memory_context": [],
        "recent_messages": [], "conversation_summary": None, "state_mode": "ephemeral"})


def patch_format_request(request, saved, candidate, fields):
    """Correct a returned patch against its exact source, without redesigning."""
    try:
        rows = decode_repair_reply(saved["response"])
        touched = {key for row in rows for key in row.get("fields", row) if key in REPAIR_FIELDS}
        touched.update(edit.get("field") for row in rows for edit in row.get("edits", [])
                       if isinstance(edit, dict) and isinstance(edit.get("field"), str))
    except (ValueError, TypeError, KeyError):
        touched = set(fields)
    touched &= REPAIR_FIELDS
    touched.add("duration_seconds")
    prompt = json.dumps({"order_index": candidate["order_index"], "allowed_fields": fields,
        "original_fields": {key: candidate.get(key) for key in touched},
        "returned_patch": saved["response"], "validation_error": saved["error"]}, ensure_ascii=False)
    if len(prompt) > 50000:
        return None
    return request.model_copy(update={"prompt": prompt,
        "system_prompt": "你是单镜补丁格式校正器，不重新审核、不重新设计动作。"
            "保留已返回补丁的修改意图、合法修改及字段范围，只校正校验错误。"
            "edits.old 必须逐字复制 original_fields 中真实存在的唯一片段；不能自己改写原句。"
            "不能匹配的编辑按原意重新定位，必要时可返回该文本字段合并后的 fields，保留未修改句子。"
            "返回 JSON fields/edits，不能对同一字段同时使用两种方式；不得丢弃其它有效补丁。"
            "不得改其它镜头、原台词和未涉及字段；动作时间轴不能超过 duration_seconds。",
        "tool_mode": "none", "project_files": [], "skills": [], "memory_context": [],
        "recent_messages": [], "conversation_summary": None, "attachments": [], "documents": [],
        "state_mode": "ephemeral"})


def snapshot_text(request, identifier):
    """Reassemble paged snapshots without treating the index as source text."""
    parts = sorted((f for f in request.project_files if f.id.startswith(identifier + "-part-")),
                   key=lambda f: f.id)
    if parts:
        return "".join(f.content for f in parts)
    return next((f.content for f in request.project_files if f.id == identifier), "")


def scoped_generation_request(request, prompt):
    """Only expose this batch's source; keep selected stage rules/handbooks."""
    from app.services.retrieval_context import evidence_file, mount

    files = [f for f in request.project_files
             if not f.id.startswith(("retrieval-task-input", "retrieval-local-operation",
                                     "retrieval-chapter-source", "retrieval-chapter-script",
                                     "retrieval-chapter-shots",
                                     "retrieval-project-assets"))]
    catalog = snapshot_text(request, "retrieval-project-assets")
    if catalog:
        assets = json.loads(catalog)
        selected = {a["id"] for a in assets if a.get("name") and a["name"] in prompt}
        while True:
            expanded = selected | {a.get("parent_asset_id") for a in assets if a["id"] in selected}
            if expanded == selected:
                break
            selected = expanded
        # Full relevant identities plus a compact index for aliases/implicit
        # references. Never repeat unrelated image prompts or whole boards.
        scoped_assets = [a if a["id"] in selected else {
            k: a[k] for k in ("id", "name", "asset_type", "parent_asset_id", "aliases") if k in a
        } for a in assets]
        files.append(evidence_file("project-assets", json.dumps(scoped_assets, ensure_ascii=False), suffix="json"))
    current = request.model_copy(update={
        "project_files": files,
        "system_prompt": request.system_prompt + "\n本批正文、时间槽和必要衔接信息已完整提供在本次局部操作中。"
            "整章原稿、整章剧本和旧分镜刻意不挂载，不要寻找或重读它们。"
            "只按本批人物、动作和字段检索适用规则，资料已足够时直接输出完整JSON，不反复检索同一证据。",
    })
    current = mount(current, evidence_file("local-operation", prompt))
    return current


def scope_inline_assets(base, local_text):
    """Keep identity descriptions for alias resolution, not unrelated image prompts."""
    marker = "资产清单："
    start = base.find(marker)
    if start < 0:
        return base
    offset = start + len(marker)
    while offset < len(base) and base[offset].isspace():
        offset += 1
    try:
        assets, end = json.JSONDecoder().raw_decode(base, offset)
    except ValueError:
        return base
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        return base
    scoped = [a if a.get("name") and a["name"] in local_text else {
        k: a[k] for k in ("name", "type", "description", "parent_asset_id") if k in a
    } for a in assets]
    return base[:offset] + json.dumps(scoped, ensure_ascii=False) + base[end:]


def storyboard_format_request(request, raw, schema, group, source=""):
    """Formatting is one bounded inference, not an agent researching a story."""
    contract = schema.split('\n输出JSON Schema：', 1)[-1]
    prompt = ('输出JSON Schema：' + contract + '\n仅修复下列已有输出的JSON结构，不重写故事。'
              + '\n本批时间槽：' + json.dumps(group, ensure_ascii=False)
              + ('\n仅用于补齐截断末镜的本批原文：\n' + source if source else '')
              + '\n已有输出：\n' + raw)
    return request.model_copy(update={
        "prompt": prompt,
        "system_prompt": "你是分镜JSON格式校正器，不是创作或审核Agent。仅处理已提供的本批输出。"
            "禁止搜索、读取文件或技能，不输出计划。保留全部完整镜头、字段和原文值，不缩写台词或动作。"
            "截断末镜只能依据已有输出和本批原文补齐；不能通过删除末镜、删除字段或提前结束剧情来凑合法JSON。"
            "缺少恢复依据时返回JSON错误说明，不能编造或谎称完成。只返回完整JSON对象。",
        "tool_mode": "none", "state_mode": "ephemeral", "project_files": [], "skills": [],
        "attachments": [], "documents": [], "memory_context": [], "recent_messages": [],
        "conversation_summary": None,
    })


async def generate(request, runtime_factory, *, plan, durations, state, save, progress,
                   script: str = "", budget: int | None = None,
                   repair_shots: list[dict] | None = None,
                   findings: list[dict] | None = None,
                   feedback: str = "", batch_attempts: int = 3, partial: bool = True,
                   concurrency: int = 1, isolate_failures: bool = False,
                   first_frame_mode: bool | None = None, repair_blocking_only: bool = False):
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
    schema += SPEECH_RULES + NATURAL_MOTION_RULES + LOCOMOTION_PLANNING_RULES
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
                    "source_ids": {"type": "array", "items": {"type": "string"},
                        "description": "原文分页时必填，精确引用本页实际呈现的原文id"},
                    "duration_seconds": {"type": "number", "enum": durations},
                    "order_index": {"type": "integer", "minimum": 1}}}}}}, ensure_ascii=False)
    from app.services.storyboard_preparation import prepare, direct_request
    preparation = prepare(request, state) if request.tool_mode == "retrieval" else None
    allowed = {Decimal(str(d)) for d in durations}
    state.setdefault("valid", {})
    state.setdefault("raw", {})
    candidates = state.setdefault("repair_candidates", {})
    manifest = state.get("manifest", {})
    from app.services.jev_control import controller, repair_fields, pending as jev_pending
    repair_control = await controller(request.tenant_id, state.setdefault('jev_control', {})) if repair_shots is not None else None
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
        # Validate only new/planned dialogue, not unrelated fields of a legacy
        # local repair. Report a local timing defect instead of rewriting text.
        if repair_shots is None and not plan:
            speech = speech_budget(shot.dialogue, shot.action_description)
            if speech["minimum_seconds"] > float(shot.duration_seconds) + .75:
                suggested = speech_duration(shot.dialogue, shot.action_description,
                    durations, float(shot.duration_seconds))
                # This format-repair path cannot insert shots. Leave overflow
                # splitting to the review/targeted-repair path that can, instead
                # of looping on an impossible one-shot patch.
                if suggested is not None:
                    raise ValueError(f"镜头{index}语速预算不足：{speech['units']}个台词单位按各句语速"
                        f"含停顿至少约{speech['minimum_seconds']:g}秒，当前仅{shot.duration_seconds}秒；"
                        f"改为模型合法时长{suggested:g}秒并同步动作/表情/内部时间轴，不修改台词")
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

    async def execute_call(current, label):
        from app.services.storyboard_review import run_review_attempt
        result = await run_review_attempt(runtime_factory(), current, progress, label)
        state["manifest"] = result.manifest
        finish = getattr(result, "finish_reason", None)
        state.setdefault("call_diagnostics", {})[label] = {
            "finish_reason": finish, "response_chars": len(result.final_response),
            "tool_mode": current.tool_mode, "max_tokens": current.model_binding.get("max_tokens"),
            "input_chars": len(current.system_prompt) + len(current.prompt),
            "model_calls": sum(e.get("type") == "MODEL_CALL_START" for e in getattr(result, "events", []) or []),
            "tool_calls": sum(e.get("type") == "TOOL_CALL_START" for e in getattr(result, "events", []) or []),
        }
        await save(state)
        if finish in {"length", "max_tokens", "max_output_tokens", "incomplete"}:
            await progress(f"{label}：模型返回未完整结束（{finish}），保留已返回内容，仅处理本批")
        return result.final_response

    async def call(prompt, label, evidence_files=(), *, reference_query=""):
        await progress(label)
        if preparation is not None:
            # Retrieval is performed once by the host. Neither generation nor
            # a malformed field should start another open-ended agent loop.
            requested = []
            for supplement in range(3):
                current = direct_request(request, prompt + CLIP_RULES, preparation,
                                         reference_query or prompt, requested=requested)
                raw = await execute_call(current, label + (f" · 补充资料 {supplement}" if supplement else ""))
                try:
                    reply = json.loads(raw)
                except (ValueError, TypeError):
                    return raw
                if not isinstance(reply, dict) or "needs_context" not in reply:
                    return raw
                ids = reply["needs_context"]
                if (not isinstance(ids, list) or not ids or
                        any(not isinstance(i, str) for i in ids) or
                        set(ids).issubset(requested)):
                    raise ValueError("资料补充请求无效或重复；已保留本批结果")
                requested = list(dict.fromkeys([*requested, *ids]))
            raise ValueError("本批定向资料补充仍未收敛；已保留其它批次")
        original_prompt = prompt
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
        if label.startswith("正在生成") and current.tool_mode == "retrieval":
            current = scoped_generation_request(current, original_prompt)
        return await execute_call(current, label)

    # Fixed timing allows small batches; un-timed scripts retain model-directed pacing.
    if plan:
        from app.services.storyboard_preparation import output_capacity
        # Existing checkpoints used four slots. Preserve their batch identities;
        # only new allocations adapt to the configured output capacity.
        size = state.setdefault("timed_batch_size", 4 if state.get("raw") or state.get("valid")
            or state.get("generation_plan") or preparation is None else output_capacity(request.model_binding))
        units = [(str(number), plan[i:i + size], None)
                 for number, i in enumerate(range(0, len(plan), size))]
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
        state.get("generation_pages", {}).pop(batch_key, None)
        state.get("source_coverage", {}).pop(batch_key, None)
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
        active_findings = [item for item in findings or []
                           if not repair_blocking_only or item.get("severity") != "minor"]
        named = parse_finding_targets(active_findings, len(existing_by_index))
        user_targets = set() if repair_blocking_only else parse_user_targets(feedback, len(existing_by_index))
        for index in user_targets:
            named.setdefault(index, [])
        if not named:
            if repair_blocking_only and findings and not active_findings:
                for index, row in existing_by_index.items():
                    state["valid"].setdefault(str(index), validate(row, index))
                return True
            return False
        # Severity and target count never grant permission to rewrite other shots.
        global_findings = [item for item in active_findings if isinstance(item, dict)
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
            named_fields = finding_fields(issues)
            fields = coherent_repair_fields(named_fields)
            addition_issues = [issue for issue in issues if (missing_shot_finding(issue) or split_shot_finding(issue))
                               and index == min(parse_finding_targets([issue], len(existing_by_index)) or [index])]
            allow_insert = bool(addition_issues) and not plan
            allow_split = allow_insert and any(split_shot_finding(issue) for issue in addition_issues)
            if allow_split:
                # Redistributing this shot's beats also needs its emotion and
                # combat timing, not only the field the reviewer happened to name.
                fields = sorted(REPAIR_FIELDS)
            neighbors = {str(i): state["valid"].get(str(i)) or existing_by_index.get(i)
                         for i in (index - 1, index + 1)}
            if insertions.get(str(index - 1)):
                neighbors[str(index - 1)] = insertions[str(index - 1)][-1]
            deferred_fields = []
            if repair_control:
                try:
                    mandatory = sorted(set(named_fields) | ({'duration_seconds', 'action_description',
                        'dialogue', 'emotion_plan', 'combat_plan', 'internal_shots'} if allow_split else set()))
                    fields = await repair_fields(repair_control, candidate, issues, fields, mandatory,
                                                 neighbors=neighbors, deferred=deferred_fields)
                    state.setdefault('jev_repair_deferred_fields', {})[str(index)] = deferred_fields
                finally:
                    await save(state)
                if not fields:
                    from app.services.jev_control import JevDecisionPending
                    raise JevDecisionPending(f'镜头 {index} 尚未定位可修改字段')
            request_prompt = (
                f'只修复第 {index} 镜，返回 {{"fields":{{...}},"edits":[{{"field":"action_description","old":"待替换的唯一原句","new":"修正后的原句"}}]}}。'
                '文本字段局部修正优先用 edits 精确替换，未匹配文字由平台逐字保留；不要重新输出整段动作和图片提示词。'
                'old 必须逐字来自本镜原文且唯一；确需替换全部相同原句时才设置 replace_all=true。'
                '结构化字段或明确需要重排的时间轴可用 fields，同一个字段不得同时使用 fields 与 edits。'
                "未被审核点名、也不需要配合修改的字段必须原样省略，不要复述整个镜头。"
                "允许的字段：title、shot_type、duration_seconds、scene_description、"
                "action_description、dialogue、image_prompt、asset_names、"
                "continuity_group、frame_layout、combat_plan、emotion_plan、internal_shots。\n"
                f'合法时长：{json.dumps(durations)}'
                f"\n本镜审核意见：{json.dumps(named.get(index) or [], ensure_ascii=False)}"
                f"\n直接点名字段：{json.dumps(named_fields, ensure_ascii=False)}"
                f"\n同一事实的关联同步字段：{json.dumps(sorted(set(fields) - set(named_fields)), ensure_ascii=False)}"
                + ("\n【JEV修复范围交接】以下关联字段尚未确定是否需要改动："
                   + json.dumps(deferred_fields, ensure_ascii=False)
                   + "。请根据本镜现有内容与具体审核意见核验，仅当修复点名问题必须同步同一事实时提交补丁；"
                     "不确定本身不是错误，没有直接矛盾就省略该字段。JEV已确定保留的字段不能改动，不扩大到其它镜头。"
                   if deferred_fields else '')
                +
                "\n先修复点名问题，再检查同一人物姿势、机位、持物、造型和时间在本镜各字段中的表述。"
                "只在存在直接矛盾时同步关联字段中的相应句子，其余有效内容必须保留；不要另起一套机位或姿势。"
                "表情、战斗与内部镜头中的同一持物、视线和伤势也须同步；仅同步该事实，不改变未被点名的时间点和编排。"
                "机位或姿势改动必须同时保持本镜首帧、动作起点和邻镜出入状态连贯，不能修一个字段留下另一套相反描述。"
                "审核意见引用的是修复前快照；下面相邻镜头是当前已保存状态，若引用已过时，以当前邻镜和剧本原文为准。"
                '先确认问题在当前快照中是否仍存在；若已被邻镜修复消除，返回 {"unchanged":true,"reason":"当前证据说明"}，'
                "不要机械执行旧建议把问题反向移回其它镜头，平台仍会重新审核。"
                "审核建议的二选一方案不是新增剧情命令：优先选不改变连续动作既定持物手、接触部位、伤势与入出镜状态的方案。"
                "左右或遮挡矛盾优先澄清机位、身体朝向和画面坐标；不能为了修本镜擅自把邻镜确立的右肩改成左肩，"
                "或让未修改邻镜必须配合新动作。必须变更姿势时在本镜内写清连续过渡，并在出镜时接回下一镜的既定状态。"
                "补台词前核对当前邻镜 dialogue，已由邻镜承载的同一句不能重复添加；旧审核的遗漏猜测不能覆盖实际存在的台词证据。"
                "未明确要求改台词时逐字保留台词；不得为了同步而改写剧情、增删人物或整段重写无关动作。"
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
                + "\n【审核点名优先；关联字段仅同步同一事实，未涉及内容保持原样】"
                f"\n相邻镜头仅供衔接：{json.dumps(neighbors, ensure_ascii=False)}"
            )
            if scene is not None:
                request_prompt += ("\n" + SOURCE_EVIDENCE_RULES
                    + f"\n本镜候选源段key：{scene['key']}\nsource_context："
                    + json.dumps(source_evidence_window(segments, scene["key"]), ensure_ascii=False))
            if allow_insert:
                request_prompt += (
                    '\n本次允许补镜：返回 {"fields":{本镜必要衔接补丁},"insert_after":[完整新镜头]}。'
                    'insert_after 插在本镜之后、下一原镜之前；只补审核明确缺失的剧情与台词，不复述已有动作。'
                    '必须给每个新增镜头分配模型合法时长，完整填写与原镜相同的字段。'
                    '如能在本镜合法时长内补全则返回空数组；禁止把缺失内容推给未修改的下一镜。'
                    f'\n补镜依据：{json.dumps(addition_issues, ensure_ascii=False)}'
                    f'\n全章目标秒数：{budget}；原镜总秒数：{sum(float(row["duration_seconds"]) for row in existing_by_index.values())}'
                )
                if allow_split:
                    request_prompt += (
                        '\n本镜因时长过载允许局部拆镜：fields 保留本镜前半段，insert_after 承载后续节拍。'
                        '原镜的全部台词与必要动作按原顺序分配且只出现一次，不删词、不加速朗读硬塞进模型上限。'
                        '同步调整各片段的首帧、动作、表情和战斗时间轴，不能把原镜整段表演复制到每个新片段。'
                        '只拆当前镜头，不改其它原镜。')
            local_files = []
            direct_instructions = request_prompt
            if request.tool_mode == "retrieval":
                from app.services.retrieval_context import evidence_file, json_evidence
                constraints = request_prompt.split("\n资产清单：", 1)[0]
                local_files = [json_evidence("repair-neighbors", neighbors),
                    evidence_file("repair-source", SOURCE_EVIDENCE_RULES + "\n" + json.dumps(
                        source_evidence_window(segments, scene["key"]), ensure_ascii=False)
                        if scene else "请按需要检索 chapter-script 原文，不能虚构依据"),
                    evidence_file("repair-instructions", request_prompt)]
                request_prompt = (constraints + "\n先 Read 当前镜头文件，按需 AstGrep 定位字段。"
                    "存在衔接问题时检索 project-files/retrieval-repair-neighbors/repair-neighbors.json；"
                    "台词和缺失剧情到 project-files/retrieval-repair-source/repair-source.md 查证。"
                    "资产名称、补镜授权及完整局部规则在 project-files/retrieval-repair-instructions/repair-instructions.md，"
                    "先搜索再读取相关内容，不读取全章分镜。")
            error_hint = ""
            if repair_control:
                constraint = ('\n【单镜修复写入边界】唯一允许写入的字段：' + json.dumps(fields, ensure_ascii=False)
                    + '。不得自行扩大范围或重新选择字段；生成补丁只负责修改这些字段的内容。')
                request_prompt += constraint
                direct_instructions += constraint
            direct_output_retry = bool(state.get("direct_output_repairs", {}).get(str(index)))
            raw_repairs = state.setdefault("repair_outputs", {})
            for attempt in range(batch_attempts):
                output = None
                try:
                    saved_patch = raw_repairs.get(str(index))
                    direct = patch_format_request(request, saved_patch, candidate, fields) if saved_patch else None
                    direct = direct or direct_patch_request(request, direct_instructions + error_hint, candidate, neighbors)
                    if direct is not None:
                        if direct_output_retry:
                            direct = direct.model_copy(update={"model_binding": {
                                **direct.model_binding, "reasoning_effort": "none"}})
                        from app.services.storyboard_review import run_review_attempt
                        await progress(f"正在直接修复第 {index} 镜的指定字段")
                        response = await run_review_attempt(runtime_factory(), direct, progress, f"修复第 {index} 镜")
                        state["manifest"] = response.manifest
                        output = response.final_response
                    elif request.tool_mode == "retrieval":
                        target = json_evidence("repair-shot", candidate)
                        operation = request_prompt + f"\n当前镜头文件：{target.path}" + error_hint
                        output = await call(operation, f"正在定点修复第 {index} 镜", [*local_files, target])
                    else:
                        output = await call(request_prompt + f"\n原始镜头：{json.dumps(candidate, ensure_ascii=False)}"
                                            + error_hint, f"正在定点修复第 {index} 镜")
                    rows = decode_repair_reply(output)
                    candidate = merge_shot_patch(candidate, rows, index, fields)
                    validate_action_repair(stored, candidate, issues, rows[0])
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
                    raw_repairs.pop(str(index), None)
                    break
                except (ValueError, TypeError, RuntimeError, KeyError, httpx.TransportError) as error:
                    if jev_pending(error):
                        raise
                    if "任务已停止" in str(error) or "上下文已失效" in str(error):
                        raise
                    if "未返回正文" in str(error) and request.model_binding.get("api_mode") == "responses":
                        direct_output_retry = True
                        state.setdefault("direct_output_repairs", {})[str(index)] = True
                        await save(state)
                    if output and isinstance(error, (ValueError, TypeError, KeyError)):
                        raw_repairs[str(index)] = {"response": output, "error": str(error)}
                        await save(state)
                    error_hint = f"\n上次补丁校验失败，只修正本镜补丁：{str(error)[:1000]}"
                    if attempt + 1 == batch_attempts:
                        await save(state)
                        raise RuntimeError(f"第 {index} 镜局部修复暂未完成，其他镜头已保存：{error}") from error
                    await progress(f"第 {index} 镜补丁重试 {attempt + 1}/{batch_attempts - 1}：{str(error)[:300]}")
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
        active = {}

        def related(index, other):
            return abs(index - other) <= 1 or any(
                issue in named.get(other, []) for issue in named.get(index, []))

        try:
            while remaining or active:
                for index in list(remaining):
                    if any(related(index, other) for other in failed):
                        remaining.remove(index)
                        errors.append(f"第 {index} 镜等待关联镜头修复，已保留断点")
                for index in list(remaining):
                    if len(active) >= max(1, concurrency):
                        break
                    if any(related(index, other) for other in active.values()) or any(
                        other < index and related(index, other) for other in remaining
                    ):
                        continue
                    remaining.remove(index)
                    active[asyncio.create_task(repair_one(index))] = index
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    index = active.pop(task)
                    try:
                        task.result()
                    except BaseException as error:
                        if (jev_pending(error) or isinstance(error, asyncio.CancelledError)
                                or "任务已停止" in str(error) or "上下文已失效" in str(error)):
                            raise
                        if not isinstance(error, Exception):
                            raise
                        errors.append(str(error))
                        failed.add(index)
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
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
                rows = assemble_repaired_board(expected, state["valid"], state.get("repair_insertions", {}))
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
        full_input = snapshot_text(request, "retrieval-task-input") or request.prompt
        base = _scene_prompt_base(full_input, script)
        if group:
            base = base.partition("\n原始剧本时间轴约束（")[0]
        previous_key = units[unit_no - 2][0] if unit_no > 1 else None
        previous = completed_batches.get(previous_key, [])[-1:]
        if group and group[0]["order_index"] > 1:
            previous = [state["valid"].get(str(group[0]["order_index"] - 1))]
            previous = [row for row in previous if row]
        base = scope_inline_assets(base, (segment["content"] if segment else json.dumps(group, ensure_ascii=False))
                                   + json.dumps(previous, ensure_ascii=False))
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

    async def generate_unit(unit_no, key, group, segment, ceiling=None):
        prompt = generation_prompt(unit_no, key, group, segment, ceiling)
        label = f"正在生成第 {unit_no}/{total_units} 批分镜"
        if preparation is None or segment is None or repair:
            return await call(prompt, label)
        from app.services.storyboard_preparation import source_units, source_window, coverage, output_capacity
        spans = source_units(key, segment["content"])
        if not spans:
            raise ValueError("本批原文为空，不能发布空分镜")
        pages = state.setdefault("generation_pages", {}).setdefault(key, [])
        capacity = output_capacity(request.model_binding)
        missing = coverage(pages, spans) if pages else {s["id"] for s in spans}
        # Each accepted page must cover new source evidence. A failed call can
        # never erase previous pages; restarts continue with the missing spans.
        while missing:
            remaining = source_window(spans, missing)
            # One source copy per request. Coverage is tracked against all spans,
            # but a small output page does not need the entire scene twice.
            page_prompt = compact_paged_generation_prompt(prompt) + (
                f'\n分页输出：本次最多{capacity}个完整视频片段，宁可分页，不截断JSON。'
                '每个shots项额外提供source_ids数组，精确引用下面实际呈现的原文编号。'
                '按原文顺序覆盖；可以多镜覆盖同一段，但不得虚报覆盖、跳过台词或压缩剧情来塞满一页。'
                '本批正文仅供背景，本次只生成以下未覆盖原文；已完成内容不得重复生成。'
                '\n待覆盖原文：' + json.dumps(remaining, ensure_ascii=False)
                + '\n已完成片段数：' + str(len(pages))
                + '\n上一片段仅供衔接：' + json.dumps(pages[-1:], ensure_ascii=False))
            saved_page = state.setdefault("page_raw", {}).get(key, {})
            raw = saved_page.get("raw") if saved_page.get("offset") == len(pages) else None
            if not raw:
                raw = await call(page_prompt, label + f" · 第 {len(pages) + 1} 起片段",
                                 reference_query=json.dumps(remaining, ensure_ascii=False))
            state["page_raw"][key] = {"offset": len(pages), "raw": raw}
            await save(state)
            rows = decode_shots(raw)
            if not rows:
                raw = await execute_call(storyboard_format_request(request, raw, schema, [],
                    segment["content"]), f"正在修复第 {unit_no} 批分页格式")
                rows = decode_shots(raw)
            if not rows or len(rows) > capacity:
                state["page_raw"].pop(key, None)
                await save(state)
                raise ValueError("本页缺少完整镜头或超过输出容量，已保留之前分页")
            # A truncated object can represent an additional shot of the same
            # source span. Do not mark that span complete from its earlier shot.
            if not complete_output(raw):
                fixed = await execute_call(storyboard_format_request(request, raw, schema, [],
                    segment["content"]), f"正在修复第 {unit_no} 批分页尾部")
                repaired = decode_shots(fixed)
                if not complete_output(fixed) or len(repaired) < len(rows) + int(unfinished_shot(raw)):
                    raise ValueError("分页尾部未完整，已保存原始输出，不将截断内容标记为覆盖")
                repaired[:len(rows)] = rows
                rows = repaired
            try:
                pending = coverage(rows, remaining)
            except ValueError:
                from app.services.storyboard_preparation import mapping_request, merge_mapping
                mapped = await execute_call(mapping_request(request, rows, remaining),
                    f"正在修复第 {unit_no} 批原文关联（不重写镜头）")
                try:
                    rows = merge_mapping(rows, remaining, mapped)
                except (ValueError, TypeError, json.JSONDecodeError) as mapping_error:
                    # A malformed mapping must never keep the same bad page in
                    # the checkpoint. Preserve accepted pages, then regenerate
                    # only this page against the same missing source window.
                    state["page_raw"].pop(key, None)
                    await save(state)
                    raise ValueError(
                        f"本页原文关联修复未收敛，已丢弃当前坏分页并仅重试本页：{mapping_error}"
                    ) from mapping_error
                pending = coverage(rows, remaining)
            gained = {s['id'] for s in remaining} - pending
            if not gained:
                raise ValueError("本页没有覆盖新的原文，已保留完成片段")
            # Enforce sequential coverage, so later missing-only pages cannot
            # be appended after a future event generated out of order.
            ordered = [s["id"] for s in remaining]
            if gained != set(ordered[:len(gained)]):
                state["page_raw"].pop(key, None)
                await save(state)
                raise ValueError("本页跳过前段原文；仅重试当前页，已完成分页保留")
            pages.extend(rows)
            state["page_raw"].pop(key, None)
            missing = coverage(pages, spans)
            state.setdefault("source_coverage", {})[key] = {
                "total": len(spans), "covered": len(spans) - len(missing), "missing": sorted(missing)}
            await save(state)
            await progress(f"第 {unit_no}/{total_units} 批已保存 {len(pages)} 个片段，原文覆盖 {len(spans) - len(missing)}/{len(spans)}")
        return json.dumps({"shots": pages}, ensure_ascii=False)

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
                state["raw"][key] = await generate_unit(position + 1, key, group, segment)
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
            if re.search(r"接上|紧接|继续|话音未落|与此同时|接过|递给|追上", right_scene["content"][:300]):
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
        if key not in state["raw"]:
            state["raw"][key] = await generate_unit(unit_no, key, group, segment, ceiling)
            await save(state)
        raw = state["raw"][key]
        rows = decode_shots(raw)
        # A wholly unreadable reply receives a formatting repair, not a fresh storyboard.
        if not rows or (group and len(rows) > len(group)) or (not group and not complete_output(raw)):
            if len(raw) > 40_000:
                await discard_batch(key)
                raise RuntimeError("分镜返回格式错误且内容过长，已保留其它批次；请缩小生成范围")
            label = f"正在修复第 {unit_no} 批返回格式"
            await progress(label + " · 定向校正，不调用检索工具")
            fixed = await execute_call(storyboard_format_request(
                request, raw, schema, group, segment["content"] if segment else ""), label)
            original_rows = rows
            rows = decode_shots(fixed)
            minimum_rows = len(original_rows) + int(unfinished_shot(raw))
            if len(rows) < minimum_rows:
                raise RuntimeError("格式修复丢失完整镜头，已保留本批原始输出，不能发布减少的镜头")
            # Formatting must not rewrite previously complete, parseable rows.
            if original_rows:
                if any(old.get("order_index") != new.get("order_index")
                       for old, new in zip(original_rows, rows[:len(original_rows)], strict=True)):
                    raise RuntimeError("格式修复改变已有镜号顺序，已保留本批原始输出")
                rows[:len(original_rows)] = original_rows
                if complete_output(fixed):
                    fixed = json.dumps({"shots": rows}, ensure_ascii=False)
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
                        if "任务已停止" in str(repair_error) or "上下文已失效" in str(repair_error):
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
                if "任务已停止" in str(error) or "上下文已失效" in str(error):
                    raise
                if hasattr(runtime_factory, "invalid_output"):
                    runtime_factory.invalid_output()
                failures[key] = str(error)[:1500]
                await save(state)
                from app.services.storyboard_preparation import budget_blocked
                if budget_blocked(error) or isinstance(error, ShotRepairExhausted) or attempt + 1 == batch_attempts:
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
                if error and ("任务已停止" in str(error) or "上下文已失效" in str(error)):
                    raise error
            try:
                await consume_unit(position + 1, key, group, segment, error)
            except RuntimeError as error:
                if not isolate_failures or "任务已停止" in str(error) or "上下文已失效" in str(error):
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
