"""Speech delivery references and bounded timing estimates, shared by video paths."""
import re

from app.services.video_text import video_text_tracks

RATES = {
    1: (90, 110, "低声慢语、从容低语、沉思诉说", "停顿较多、尾音舒展；语调低沉平稳，情绪起伏极小"),
    2: (120, 140, "平缓叙述、从容交谈", "自然断句、柔和稳定，轻微语调起伏"),
    3: (150, 170, "正常陈述、清晰交谈", "清晰自然，中等起伏，重点词轻微加重"),
    4: (180, 200, "急促表述、略带慌张", "句间间隔缩短，气息略急，语气紧张、起伏增大"),
    5: (210, 230, "急促呼喊、焦急诉说", "声音紧绷、情绪激动，短句间穿插喘息"),
    6: (240, 280, "嘶吼、爆发式喊话、上气不接下气", "爆发短句与喘息交替，声音张力强；喘息单独占时间，不以持续机关枪语速省时"),
}
SPEECH_RULES = (
    "\n【语速与镜头时长规划】先读取每位人物的原台词与发声情境，再分配视频片段时长。"
    + "；".join(f"{level}级{low}—{high}字/分钟：{words}，{delivery}" for level, (low, high, words, delivery) in RATES.items())
    + "。6级为240字/分钟以上，240—280仅作初始预算参考，不是生理上限。"
    "用户指定语速优先，明确说话关键词次之，无描述默认3级；边走边说时可参考同级行走，"
    "但不将走路速度强制等同语速，内心独白与旁白不随跑步强制加速。"
    "只计算实际发声文字，不计角色名、时间码、动作括注或后期字幕。"
    "台词时间约为字数×60÷语速，再加明确停顿、换人及喘息；轮流对白累加，"
    "画面动作与说话同时发生取两者较大值，先行动后说话则分段相加。"
    "从当前模型实际支持的时长中选择足够容纳对白和动作的档位，并同步动作、表情与内部切镜时间轴；"
    "不得机械固定镜头秒数，也不得用提速、删台词或多人抢话硬塞。"
    "超过模型最长时长时，按语义拆镜为连续视频片段，可用同一动作的不同景别承接，原台词完整保留且只说一次。"
    "原文有固定时间轴时不擅自扩展时间窗；先在合法时间窗内重新分配，确实冲突时明确报告具体台词和所需时间。"
    "实际发声与否服从音频开关；旁白/心声不强制人物张嘴。语速等级和数字是指令，不显示成字幕或念出。"
)
START, END = "【人声语速控制】", "【人声语速控制结束】"


def speech_level(context: str) -> int:
    explicit = re.search(r"(?:人声|语速|说话速度)(?:等级)?\s*[:：为]?\s*([1-6])\s*级", context)
    if explicit:
        return int(explicit[1])
    for level, (_, _, words, _) in RATES.items():
        if any(word in context for word in words.split("、")):
            return level
    if not re.search(r"旁白|心声|独白|画外音|OS|VO", context, re.I) and re.search(r"边.+边|喊话|交谈|说话", context):
        from app.services.locomotion import KEYWORDS
        for level in (6, 5, 4, 3, 1, 2):
            if any(word in context for word in KEYWORDS[level]):
                return level
    return 3


def speech_lines(dialogue: str) -> list[str]:
    spoken, _ = video_text_tracks(dialogue or "")
    if spoken.strip().strip("。.") in {"", "无", "无对白", "无台词", "无对话", "（无）", "none", "None"}:
        return []
    lines = []
    for line in spoken.splitlines():
        line = re.sub(r"【[^】]*】|[（(][^）)]*[）)]", "", line).strip()
        line = re.sub(r"^\s*\d+(?:\.\d+)?\s*[-—～~–]\s*\d+(?:\.\d+)?\s*(?:s|秒)\s*[:：]?", "", line)
        line = re.sub(r"^[^:：\n]{1,24}[:：]\s*", "", line).strip(' “”。"')
        if line:
            lines.append(line)
    return lines


def speech_budget(dialogue: str, context: str = "") -> dict:
    # Quoted utterances are content, never pace instructions.
    context = re.sub(r'[“"].*?[”"]', '', context)
    spoken, _ = video_text_tracks(dialogue or "")
    segments = []
    for raw in spoken.splitlines():
        lines = speech_lines(raw)
        if not lines:
            continue
        metadata = " ".join(match.group(0) for match in re.finditer(r"【[^】]*】|[（(][^）)]*[）)]", raw))
        prefix = re.sub(r"【[^】]*】|[（(][^）)]*[）)]", "", raw)
        if re.match(r"^[^:：\n]{1,24}[:：]", prefix):
            metadata += " " + re.split(r"[:：]", prefix, maxsplit=1)[0]
        specific = re.search(r"语速|人声|每分钟", metadata) or any(
            word in metadata for _, _, words, _ in RATES.values() for word in words.split("、"))
        local = speech_budget_line(" ".join(lines), metadata if specific else context + " " + metadata)
        pauses = sum(float(value) for value in re.findall(r"(?:停顿|喘息|沉默)\s*(\d+(?:\.\d+)?)\s*秒", metadata))
        local["minimum_seconds"] += pauses
        local["recommended_seconds"] += pauses
        segments.append(local)
    level = speech_level(context)
    low, high, words, delivery = RATES[level]
    units = sum(s["units"] for s in segments)
    pause = max(0, len(segments) - 1) * .3
    pause += sum(float(value) for value in re.findall(r"(?:停顿|喘息|沉默)\s*(\d+(?:\.\d+)?)\s*秒", context))
    return dict(level=level, units=units, low=low, high=high, keywords=words, delivery=delivery,
        segments=segments,
        minimum_seconds=round(sum(s["minimum_seconds"] for s in segments) + pause, 2) if units else 0,
        recommended_seconds=round(sum(s["recommended_seconds"] for s in segments) + pause, 2) if units else 0)


def speech_budget_line(text: str, context: str) -> dict:
    level = speech_level(context)
    low, high, _, _ = RATES[level]
    explicit = re.search(r"每分钟\s*(\d{2,3})(?:\s*[-—～~–至到]\s*(\d{2,3}))?\s*字", context)
    if explicit:
        low, high = sorted((int(explicit[1]), int(explicit[2] or explicit[1])))
        low, high = max(1, low), max(1, high)
    # Chinese characters and non-Chinese word tokens, not metadata or punctuation.
    units = len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z]+)*", text))
    pause = {1: .6, 5: .5, 6: .8}.get(level, 0)
    return dict(level=level, low=low, high=high, units=units,
        minimum_seconds=units * 60 / high + pause,
        recommended_seconds=units * 60 / ((low + high) / 2) + pause)


def speech_guidance(dialogue: str, context: str = "", *, audio_enabled: bool = True) -> str:
    data = speech_budget(dialogue, context)
    if not audio_enabled or not data["units"]:
        return ""
    profiles = list(dict.fromkeys((s['level'], s['low'], s['high']) for s in data['segments']))
    delivery = "；".join(f"{level}级，每分钟约{low}—{high}字，{RATES[level][2]}，{RATES[level][3]}" for level, low, high in profiles)
    return (START + "人声等级：" + delivery + "。各句沿用各自标注，不将最快语速套用所有人物。"
        f"本段实际台词约{data['units']}个计量单位，轮流发声含停顿参考约{data['recommended_seconds']:g}秒；"
        "以原台词和原文明确的逐人语速、停顿为准，不改写台词。仅对白口型与发音同步，五官和身份稳定；"
        "心声/旁白不强制张嘴。不把语速数字当作字幕或朗读内容，不因跑步强行加速独白。" + END)


def prompt_dialogue(prompt: str) -> str:
    """Only explicitly quoted speech in home prompts; never count scene prose."""
    return "\n".join(re.findall(
        r"(?:说(?:道)?|喊(?:道)?|问(?:道)?|回答|台词|对白|独白|旁白|心声)\s*[:：]?\s*[“\"]([^”\"\n]+)[”\"]", prompt))


def apply_speech_guidance(prompt: str, dialogue: str, context: str = "", *, audio_enabled=True) -> str:
    prompt = re.sub(re.escape(START) + r".*?" + re.escape(END), "", prompt, flags=re.S)
    guidance = speech_guidance(dialogue, context, audio_enabled=audio_enabled)
    if not guidance:
        return prompt
    prefix = re.match(r"^ACT视角[，,\s]*", prompt, re.I)
    if prefix:
        return prompt[:prefix.end()] + guidance + prompt[prefix.end():]
    return guidance + prompt


def speech_duration(dialogue: str, context: str, durations: list[float], current: float) -> float | None:
    """Choose a legal sufficient clip, or signal that the utterance must split."""
    needed = max(current, speech_budget(dialogue, context)["recommended_seconds"])
    return next((value for value in sorted(durations) if value >= needed), None)
