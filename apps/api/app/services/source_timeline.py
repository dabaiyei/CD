"""Preserve explicit source timing independently of rewritten prose."""
from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN = r"(?:\d+分\d+(?:\.\d+)?秒|\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d+)?|\d+(?:\.\d+)?)"
RANGE = re.compile(
    rf"(?<![\d:])(?P<a>{TOKEN})\s*(?P<au>秒|s)?\s*"
    rf"(?:-->|[-—–－~～至到])\s*(?P<b>{TOKEN})\s*(?P<bu>秒|s)?", re.I
)


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    cue: str

    @property
    def label(self) -> str:
        return f"{self.start:g}—{self.end:g}秒"


def seconds(value: str) -> float:
    if "分" in value:
        minutes, remainder = value.split("分")
        return float(minutes) * 60 + float(remainder.removesuffix("秒"))
    total = 0.0
    for part in value.replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def extract(source: str) -> list[Span]:
    spans, seen = [], set()
    matches = list(RANGE.finditer(source.replace("：", ":")))
    for index, match in enumerate(matches):
        if not (match["au"] or match["bu"] or ":" in match["a"] or ":" in match["b"] or "分" in match["a"]):
            continue  # Do not confuse dates, chapter counts or dimensions with a timeline.
        start, end = seconds(match["a"]), seconds(match["b"])
        if end <= start or (start, end) in seen:
            continue
        seen.add((start, end))
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        tail = source[match.end():stop].splitlines()
        cue = " ".join(line.strip() for line in tail[:2])[:180]
        spans.append(Span(start, end, cue))
    return spans


def contract(source: str) -> str:
    spans = extract(source)
    if not spans:
        return ""
    rows = "\n".join(f"- {s.label}：{s.cue}" for s in spans)
    if len(rows) > 14000:
        raise RuntimeError("原始时间轴过长，请按章节拆分导入；不能截掉后续时间段")
    return (
        "\n原始剧本时间轴约束（优先于通用节奏优化、目标时长和手册的默认安排）：\n"
        "以下均为本章绝对时间，不是各镜头重新从0开始。保持事件、台词与对应时间段的绑定，"
        "不得提前、后移、删掉或压缩。剧本正文必须在对应内容前保留时间段标记；可优化段内表达，"
        "但不能以强化钩子或删除口水话为由改变原始安排。\n"
        "区分剪辑分镜与视频生成片段：一条视频可含多个短分镜，模型时长下限不限制内部切镜。"
        "按合法总时长组合视频片段，原始边界可位于片段内部；internal_shots保存内部相对时间及原文绝对时间。"
        "不要把战斗窗口的镜内相对秒数当章节时间。"
        "明确的静止、停顿也要保留。重叠时间段可能是同步对白或画面，不能相加重复计时。"
        "每镜动作说明标出承接的原文时间段和该镜章节绝对起止时间，事件与台词不能跨段挪用。"
        "只能选择模型合法时长；若无法同时满足时间轴与模型能力，说明冲突，不得自行取整、延长或缩短。"
        "审核与修复同样检查此约束。\n" + rows
    )


def validate_script(source: str, content: str) -> None:
    present = {(s.start, s.end) for s in extract(content)}
    missing = [s.label for s in extract(source) if (s.start, s.end) not in present]
    if missing:
        raise ValueError("改编剧本遗漏原始时间段：" + "、".join(missing[:12]) + "；请在对应正文处保留时间标记与事件")


def validate_shots(source: str, shots) -> None:
    spans = extract(source)
    if not spans:
        return
    # Overlays share time: take the covered union, never sum overlapping captions/dialogue.
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    if ordered[0].start != 0:
        return
    covered = 0.0
    for span in ordered:
        if span.start > covered + .001:
            return  # A partial timeline does not define the chapter's full runtime.
        covered = max(covered, span.end)
    boundaries, total = [], 0.0
    for shot in shots:
        offset = total
        total += float(shot.duration_seconds)
        boundaries.append(total)
        for part in getattr(shot, "internal_shots", []):
            if (part.start_seconds < 0 or part.end_seconds > float(shot.duration_seconds) + .01
                or part.end_seconds <= part.start_seconds
                or abs(part.source_start_seconds - (offset + part.start_seconds)) > .01
                or abs(part.source_end_seconds - (offset + part.end_seconds)) > .01):
                raise ValueError("视频片段内部时间轴与章节绝对时间不一致")
            boundaries.append(offset + part.end_seconds)
    expected = covered
    if abs(total - expected) > .01:
        raise ValueError(f"分镜总时长{total:g}秒偏离原始时间轴{expected:g}秒；不得默认缩短或延长")
    required = {s.end for s in ordered if not any(other.start < s.end < other.end for other in ordered)}
    missing = [end for end in sorted(required) if not any(abs(end - b) < .01 for b in boundaries)]
    if missing:
        raise ValueError(f"原始段落边界{missing}秒未在视频片段或内部时间轴中保留，请补齐内部切镜安排")


async def run_validated(request, runtime_factory, validate):
    """Retry only invalid generated timing; transport/provider errors propagate normally."""
    error = ""
    for attempt in range(3):
        current = request.model_copy(deep=True)
        if attempt:
            current.session_id = f"{request.session_id}-timing-{attempt}"
            current.prompt += "\n上次时间轴校验失败，请返回修正后的完整JSON：" + error[:1800]
        result = await runtime_factory().run(current)
        try:
            validate(result.final_response)
            return result
        except ValueError as exc:
            error = str(exc)
    raise RuntimeError("原始时间轴校验失败（已尝试3次）：" + error)
