"""Small, intent-scoped locomotion references for video prompts."""
import re

GAITS = {
    1: ("极慢", "60—70", "步幅极小，身体直立，手臂放松下垂、轻微自然摆动，平稳缓慢但不是静帧", "散步、思考、闲逛"),
    2: ("慢速", "80—90", "步幅适中，身体自然直立，手臂轻摆，重心平稳交替", "日常对话、逛街、出发准备"),
    3: ("中速", "100—110", "步幅略大，步伐明显加快，摆臂增加，连续交替落脚", "赶时间、着急前往目的地"),
    4: ("中快速", "120—140", "较小步幅、较快步频，身体微前倾，轻巧低幅腾空，不演成长时间滞空的大跨步", "追公交、小跑追人、赶上同伴"),
    5: ("高速", "150—170", "大步幅、明显前倾，手臂有力摆动，支撑与短暂腾空交替，落地立即推进", "紧急追赶、逃跑、追逐"),
    6: ("极速", "180以上", "全力蹬地加速，身体随加速前倾，大步快速交替，发丝衣摆随高速运动翻飞", "生死时速、最后冲刺、爆发性移动"),
}
KEYWORDS = {
    1: ("悠闲行走", "漫步", "踱步", "散步", "stroll"),
    2: ("正常行走", "缓步前行", "慢走", "走路", "行走", "walking"),
    3: ("快步走", "快步行走", "快走", "疾走", "brisk walk"),
    4: ("小步快跑", "小碎步快跑", "慢跑", "小跑", "jogging", "jog"),
    5: ("快速奔跑", "奔跑", "追赶", "逃跑", "running"),
    6: ("急速狂奔", "极速狂奔", "全力冲刺", "狂奔", "冲刺", "sprinting", "sprint"),
}
PATTERN = re.compile("|".join(re.escape(word) for word in sorted(
    (word for words in KEYWORDS.values() for word in words), key=len, reverse=True)), re.I)
LEVEL_BY_WORD = {word: level for level, words in KEYWORDS.items() for word in words}
MARKER = "【人物行走速度参考】"
LOCOMOTION_PLANNING_RULES = (
    "\n【行走规划】只在原文需要人物位移时选择对应等级："
    + "；".join(f"{level}级{name}约{cadence}步/分钟" for level, (name, cadence, _, _) in GAITS.items())
    + "。将人物、速度、行进方向、起止位置与速度变化写入动作时间段，结合距离和动作安排时长；"
    "不为填满镜头新增走动，步频不等于视频帧率，不将人物步速强制当成语速。"
)


def locomotion_guidance(source: str) -> str:
    selected = {}
    for match in PATTERN.finditer(source or ""):
        before = source[max(0, match.start() - 18):match.start()]
        if re.search(r"(?:不(?:要|能|得|再)?|禁止|避免|停止|停下|无需|without|no)\s*$", before, re.I):
            continue
        if re.search(r"(?:镜头|摄影机|相机|汽车|车辆)\s*(?:正在|开始|缓缓)?$", before):
            continue
        word = match.group().lower()
        selected.setdefault(LEVEL_BY_WORD[word], []).append(match.group())
    if not selected:
        return ""
    rows = []
    for level, words in sorted(selected.items()):
        name, cadence, body, scene = GAITS[level]
        rows.append(f"{level}级{name}（对应原文{'、'.join(dict.fromkeys(words))}）：参考每分钟约{cadence}步；{body}；常见场景：{scene}。")
    return (MARKER + "".join(rows)
        + "只应用到原文对应人物及动作时间段，不把不同人物或阶段统一成最快档；速度变化应连续加减速。"
        "步频是视觉节奏参考，不是视频帧率或严格生理指标；短片按实际时长表现，不能每个短镜强塞整分钟步数。"
        "尊重原文明确的速度、距离、时长、伤势、负重和慢动作要求；上述适用场景不增加剧情。"
        "落脚接地、脚掌滚动、重心转移与反向摆臂协调，避免滑步、原地踏步和位置瞬移；"
        "摄影机速度独立处理。停步与静止要求优先，跑步自然短暂腾空不等于漂浮。"
        "【人物行走速度参考结束】")


def apply_locomotion_guidance(prompt: str, *, source: str | None = None) -> str:
    if MARKER in prompt:
        return prompt
    guidance = locomotion_guidance(prompt if source is None else source)
    prefix = re.match(r"^ACT视角[，,\s]*", prompt, re.I)
    if guidance and prefix:
        return prompt[:prefix.end()] + guidance + prompt[prefix.end():]
    return guidance + prompt if guidance else prompt
