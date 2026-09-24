"""Versioned performance timeline and deterministic expression compilation.

Mirrors ``combat_choreography``: the storyboard only registers *who feels what
from which second to which second*, and the video stage turns that into
executable performance. The library sentences are compiled by program, so a
model that forgot to write the face still ships a shot with a real performance
instead of a blank one.

No extra model call is made: the per-shot video request already reads the shot
row, so the emotion timeline and the routed reference travel with it. This
keeps the shot path at one model call, which is what the generation budget
allows.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.expression_skill_retrieval import (
    canonical_emotion,
    infer_emotion,
    performance,
    query_text,
)

VERSION = "1"

Intensity = Literal["slight", "medium", "extreme"]

INTENSITY_LABEL = {"slight": "轻微", "medium": "中等", "extreme": "极致"}
INTENSITY_LABEL_EN = {"slight": "subtle", "medium": "medium", "extreme": "extreme"}

# The library is written for a Chinese model, but H3 and other English
# protocols must not receive Chinese performance prose. Each entry carries the
# English name the lock uses so the target model reads a language it honours.
EMOTION_NAME_EN = {
    "平静克制": "calm restraint", "轻微疑惑": "faint confusion", "认真倾听": "attentive listening",
    "若有所思": "pensive thought", "欲言又止": "words held back", "强装镇定": "forced composure",
    "礼貌微笑": "polite smile", "尴尬停顿": "awkward pause", "不动声色": "unreadable calm",
    "暗自判断": "silent assessment",
    "浅浅开心": "soft happiness", "忍不住笑": "stifled laugh", "释然一笑": "relieved smile",
    "久别重逢": "long-awaited reunion", "得意小表情": "smug satisfaction", "调皮挑衅": "playful teasing",
    "被夸后的害羞": "bashful at praise", "松了一口气": "letting out a breath",
    "温柔注视": "tender gaze", "喜极而泣": "joyful tears",
    "轻微紧张": "mild tension", "突然警觉": "sudden alertness", "不安预感": "uneasy foreboding",
    "被吓一跳": "being startled", "强烈恐惧": "intense fear", "惊恐求救": "terror and pleading",
    "压抑害怕": "suppressed fear", "绝境慌乱": "cornered panic", "发现真相": "realizing the truth",
    "大脑空白": "mind going blank",
    "隐忍难过": "restrained grief", "失望低头": "disappointed lowering of the head",
    "委屈想哭": "hurt and about to cry", "无声落泪": "silent tears", "压住哭腔": "choking back sobs",
    "彻底心碎": "utter heartbreak", "崩溃大哭": "breaking down in tears",
    "麻木悲伤": "numb sorrow", "悔恨自责": "remorse and self-blame", "告别时微笑": "a farewell smile",
    "轻微不满": "mild displeasure", "忍着怒气": "suppressed anger", "冷笑反击": "cold counter-smile",
    "被激怒": "being provoked", "愤怒质问": "angry interrogation", "失控暴怒": "uncontrolled rage",
    "含泪愤怒": "tearful anger", "被背叛的怒": "the rage of betrayal", "决裂表情": "a decisive break",
    "战斗前狠劲": "pre-fight ruthlessness",
    "笑里藏刀": "a smile hiding a blade", "表面答应": "outward agreement",
    "强颜欢笑": "a forced smile", "半信半疑": "half-believing", "突然明白": "sudden understanding",
    "心虚躲闪": "guilty evasion", "嫉妒压抑": "suppressed jealousy",
    "被看穿后慌张": "panic at being seen through", "重新燃起希望": "renewed hope",
    "下定决心": "a firm decision",
}


class EmotionBeat(BaseModel):
    """One performance segment: a single character, one dominant emotion."""

    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    character: str = Field(min_length=1, max_length=120)
    emotion: str = Field(min_length=1, max_length=40)
    intensity: Intensity = "medium"
    trigger: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def valid_window(self):
        if self.end <= self.start:
            raise ValueError("表演结束时间必须晚于开始时间")
        # The declared emotion is never rewritten. The source skill is
        # library-first: use one of the sixty when it fits, and only then invent
        # an emotion outside them and perform it with the universal formula.
        # Overwriting an invented name with the nearest library entry would
        # silently replace the performance the storyboard asked for.
        return self


def library_emotion(name: str, trigger: str = "") -> str:
    """The library entry a declared emotion is routed to, without overwriting it."""
    return canonical_emotion(name, trigger)


class EmotionPlan(BaseModel):
    """The upstream half: a timeline only, no performance prose."""

    beats: list[EmotionBeat] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def ordered(self):
        end = 0.0
        for beat in self.beats:
            if beat.start < end - .001:
                raise ValueError("表演时间段不能重叠或倒序")
            end = beat.end
        return self

    def validate_duration(self, duration: float):
        for beat in self.beats:
            if beat.end > duration + .001:
                raise ValueError("表演时间轴超出当前镜头时长")


PLANNING_RULES = """
【表演模块分工】
剧本与分镜登记人物情绪状态，不写五要素表演细节。
情绪必须转译为可执行信息：谁、在几秒到几秒、什么主情绪、什么强度、由什么触发。
分镜在每镜 emotion_plan 中保存镜内相对时间轴（非人物镜头为 null）：
{"beats":[{"start":0,"end":3,"character":"人物名","emotion":"隐忍难过","intensity":"medium","trigger":"听到真相"}]}
emotion 必须取自表情库的情绪名（平静克制、欲言又止、强装镇定、浅浅开心、突然警觉、
发现真相、隐忍难过、无声落泪、忍着怒气、含泪愤怒、下定决心、强颜欢笑等）。
优先使用库内情绪名；只有库内六十条确实无法准确表达时才允许自造，且自造名必须写成
“情绪名：具体状态”这样的可执行描述，不能是“复杂”“难以言说”这类空泛词。
自造情绪由视频提示词阶段用万能表情公式（眼神、眉毛、嘴角与嘴唇、身体反应、剧情状态）展开。
一条镜头只安排一个主情绪；情绪要递进（如强装镇定→发现真相→含泪愤怒→下定决心），
不得同段混三种主情绪，也不得一上来就崩溃。
intensity 只取 slight/medium/extreme，默认 medium；只有剧情明确要求爆发时才用 extreme。
不要写“很伤心”“很愤怒”这类标签，也不要在此阶段展开眼神眉毛嘴角身体反应；
详细表演由视频提示词阶段按表情库展开，不因为没有表演细节而判为不合格。
非人物镜头（空镜、纯环境、纯道具）emotion_plan=null，不要给物体安排表情。
"""

DESIGN_RULES = """
【人物表演设计（本轮必须执行）】
表情不是一个词，而是一段表演。禁止只写“很伤心”“很愤怒”“表情复杂”这类标签。
每条表演按五要素写全，缺一个就视为不合格：
1 眼神（看向哪里、躲闪/发亮/失焦/盯住）；2 眉毛（放松/上扬/压低/皱起）；
3 嘴角与嘴唇（抿住/颤抖/张开/压住笑）；4 身体反应（后退/前倾/攥紧手/肩膀放下/呼吸）；
5 剧情状态（此刻刚发生了什么）；最后用一句“像……”点明表演意图。
必须有身体反应：只有脸没有身体的表情不合格，手、肩、呼吸、后退半步往往比脸更真实。
一条镜头（或一个 internal_shots 时间段）只给一个主情绪；情绪按 emotion_plan 的时间轴递进，
不要在同段混三种主情绪，也不要让情绪突然爆炸。
强度按 emotion_plan.intensity 执行：slight 只给细微变化，medium 是默认，extreme 才允许崩溃/爆发。
不认识的情绪先用“万能表情公式”现场拆解，不要直接照搬情绪名。
表情戏优先近景或面部特写，但不能擅自修改分镜已经确定的景别、机位、动作结果与剧情胜负。
表情必须与动作和台词对齐：情绪变化发生在触发它的那一拍，不在画面之外提前发生。
音频关闭时嘴唇保持完全闭合，只用眼神、呼吸、眉眼、肩颈和手部表演；
不得出现张口说话、喊叫、呐喊的口型或任何可听内容。音频开启时口型只对应已锁定台词。
不得用表情新增台词、旁白、环境声或虚构语言。
"""


def fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(
        json.dumps({"version": VERSION, **snapshot}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def person_names(row: dict) -> list[str]:
    """Characters this shot actually shows, from assets and dialogue speakers."""
    names: list[str] = []
    for asset in row.get("assets") or []:
        if str(asset.get("asset_type") or "") == "character":
            name = str(asset.get("name") or "").strip()
            if name:
                names.append(name)
    for reference in row.get("reference_map") or []:
        if str(reference.get("asset_type") or "") == "character":
            name = str(reference.get("asset_name") or reference.get("asset_names") or "").strip()
            if name:
                names.append(name)
    for line in re.split(r"[\r\n]+", str(row.get("dialogue") or "")):
        match = re.match(r"^\s*([^：:]{1,40})[：:]", line)
        if match:
            names.append(match.group(1).strip())
    return list(dict.fromkeys(name for name in names if name))


# Long shots and extreme wide shots cannot read a face, so the performance has
# to live in the body. Forcing "eyebrows and lips" there would push the video
# model to cut in for a close-up the storyboard never asked for.
_DISTANT_FRAMINGS = ("远景", "大全景", "超远景", "全景", "wide", "long shot", "establishing")


def distant_framing(row: dict) -> bool:
    shot_type = str(row.get("shot_type") or "").lower()
    return any(marker in shot_type for marker in _DISTANT_FRAMINGS)


def beats(row: dict) -> tuple[list[dict], str]:
    """The performance beats for one shot, declared upstream or inferred here.

    Declared beats use the storyboard timeline verbatim. Without one the emotion
    is inferred from the shot's own text and spread over the whole shot, so a
    shot that skipped the plan still gets a real performance.
    """
    plan = row.get("emotion_plan")
    if isinstance(plan, dict) and plan.get("beats"):
        declared = EmotionPlan.model_validate(plan)
        return [beat.model_dump() for beat in declared.beats], "declared"
    names = person_names(row)
    duration = float(row.get("duration_seconds") or 0)
    if not names or duration <= 0:
        return [], "none"
    return ([{"start": 0.0, "end": duration, "character": names[0],
              "emotion": infer_emotion(query_text(row)),
              "intensity": "slight" if distant_framing(row) else "medium",
              "trigger": ""}], "inferred")


def describe(beat: dict, *, language: str = "zh-CN", distant: bool = False,
             silent: bool = False) -> str:
    """Compile one beat into the exact instruction the video model must perform."""
    name = beat["character"]
    span = f"{beat['start']:g}–{beat['end']:g}s"
    # An emotion the library does not name still has to be performable, so it is
    # routed to the nearest entry for the reference sentence and the English
    # name, while the declared name is kept in the instruction.
    routed = library_emotion(beat["emotion"], beat.get("trigger", ""))
    text = performance(routed)
    silent_note = ("本任务关闭音频：嘴唇必须完全闭合，不出现说话、喊叫或呐喊的口型，"
                   "情绪改由眼神、呼吸、眉眼、肩颈和手部承担。")
    if language.lower().startswith("zh"):
        label = INTENSITY_LABEL[beat["intensity"]]
        if routed == beat["emotion"]:
            parts = [f"{name}在{span}的情绪是“{beat['emotion']}”（{label}档）：{text}"]
        else:
            # A declared emotion outside the sixty. The source skill answers this
            # with the universal formula, so the declared name stays authoritative
            # and the nearest library entry is offered only as a nearby sample.
            parts = [f"{name}在{span}的情绪是“{beat['emotion']}”（{label}档）：固定表情库中没有这一条，"
                     "按万能表情公式现场拆解——眼神、眉毛、嘴角与嘴唇、身体反应、剧情状态五项写全，"
                     f"再以“像……”收尾；最接近的库内参照是“{routed}”（{text}），"
                     "可借鉴写法但不要照搬，也不要退化成情绪标签"]
        if beat.get("trigger"):
            parts.append(f"触发：{beat['trigger']}")
        if silent:
            parts.append(silent_note)
        if distant:
            parts.append("本镜景别较远，表演落在体态、肩颈、呼吸和手部动作上，"
                         "不要为看清表情而改变已确定的景别或机位。")
        if beat["intensity"] == "slight":
            parts.append("只给细微变化，不放大成夸张表演。")
        elif beat["intensity"] == "extreme":
            parts.append("这是本镜唯一允许放开的爆发点。")
        else:
            parts.append("保持中间强度的真实反应，不抢戏。")
        # Each clause is its own instruction. Joining them bare produced run-on
        # text such as "触发：听到威胁只给细微变化", which the video model reads
        # as a single clause instead of two separate requirements.
        return "；".join(part.rstrip("。；") for part in parts) + "。"
    # Non-Chinese protocols (MiniMax H3 and other English models) must not
    # receive the Chinese library sentence in the final prompt. The library is
    # already inlined in the reference block for the text model, so here the
    # lock names the emotion and forces every element to be written out in the
    # prompt's own language.
    english = EMOTION_NAME_EN.get(routed, routed)
    if routed == beat["emotion"]:
        parts = [
            f"{name} at {span} must visibly perform {english} on the "
            f"{INTENSITY_LABEL_EN[beat['intensity']]} level, expressed through gaze direction, "
            "eyebrows, mouth and lips, a clear body reaction, and the situation that just triggered it."
        ]
    else:
        # The declared name is Chinese and has no English equivalent, so the lock
        # points at the beat in the shot data instead of naming a different
        # library emotion and silently replacing the performance.
        parts = [
            f"{name} at {span} must visibly perform the emotion declared for this beat in the "
            f"shot data on the {INTENSITY_LABEL_EN[beat['intensity']]} level, using the universal "
            "five-element formula: gaze direction, eyebrows, mouth and lips, a clear body reaction, "
            f"and the situation that just triggered it. The nearest library emotion is {english}, "
            "which is a reference for styling only, not a replacement."
        ]
    if beat.get("trigger"):
        # The stored trigger is Chinese story context. Pasting it into an
        # English protocol body is exactly the mixed-language prose that makes
        # these models drift into invented languages and stray audible speech.
        parts.append("Trigger: the story event recorded for this beat in the shot.")
    if silent:
        # The lock names mouth and lips, so a silent task must say explicitly
        # that the lips stay sealed, or the model may read the lock as licence
        # to speak. Audio-disabled is the common case in this project.
        parts.append("Audio is disabled: the lips stay completely closed with no speech, shout "
                     "or vocal mouth shape; carry the emotion through gaze, breathing, brows, "
                     "shoulders and hands instead.")
    if distant:
        parts.append("This shot is framed at a distance, so carry the performance through posture, "
                     "shoulders, breathing and hands instead of changing the established framing "
                     "to reach a close-up.")
    if beat["intensity"] == "slight":
        parts.append("Keep it a subtle micro-expression, never exaggerated.")
    elif beat["intensity"] == "extreme":
        parts.append("This is the only beat in the shot allowed to break open.")
    else:
        parts.append("Keep the reaction at a believable middle intensity.")
    parts.append("Do not reduce it to a single emotion word.")
    return " ".join(parts)


_ZH_PERFORMANCE_MARKERS = r"眼|眉|嘴|唇|手指|肩膀|呼吸|后退|前倾|攥|低头|抬头|胸口"
_EN_PERFORMANCE_MARKERS = (
    r"gaze|eyes|eyebrow|brow|lip|lips|mouth|jaw|chin|hand|hands|finger|shoulder|breath|"
    r"chest|fist|kneel|lean|steps? back|trembl|stiffen|swallow|exhale|inhale"
)


def _performed(beat: dict, existing: str, *, language: str = "zh-CN") -> bool:
    """Whether a reply already performs this beat instead of only labelling it."""
    if not existing:
        return False
    # An emotion outside the sixty has no library sentence to match, so the beat
    # counts as performed when the reply writes a real reaction for this
    # character. Requiring the name too would append a duplicate lock on top of a
    # correct universal-formula performance whenever the model described the
    # feeling without echoing its label.
    if library_emotion(beat["emotion"], beat.get("trigger", "")) != beat["emotion"]:
        markers = _ZH_PERFORMANCE_MARKERS if language.lower().startswith("zh") else _EN_PERFORMANCE_MARKERS
        if not re.search(markers, existing, re.I):
            return False
        return beat["character"] in existing or beat["emotion"] in existing
    # The canonical library sentence, when the reply copied the Chinese entry.
    if performance(beat["emotion"])[:18] in existing:
        return True
    # A shot that wrote its own five-element performance of the same emotion
    # still counts, but the emotion name alone is a label, not a performance.
    if language.lower().startswith("zh"):
        if beat["emotion"] not in existing:
            return False
        return bool(re.search(_ZH_PERFORMANCE_MARKERS, existing))
    # English protocols (H3 and other English models) perform the beat in
    # English, so matching Chinese keywords would always miss and duplicate the
    # lock into the prompt. Match the English emotion name plus a real body or
    # face reaction instead.
    english = EMOTION_NAME_EN.get(beat["emotion"], beat["emotion"])
    if english.lower() not in existing.lower():
        return False
    return bool(re.search(_EN_PERFORMANCE_MARKERS, existing, re.I))


def expression_contract(row: dict, *, language: str = "zh-CN", existing: str = "") -> str:
    """The deterministic performance floor appended after prompt generation.

    The model writes the performance itself, but a shot must never ship without
    one because the model summarized it away. Beats the reply already performed
    are not repeated, so this adds text only where it is actually missing.
    Returns "" for shots with nobody in frame.
    """
    found, origin = beats(row)
    if not found:
        return ""
    distant = distant_framing(row)
    silent = row.get("audio_enabled") is False
    lines = [describe(beat, language=language, distant=distant, silent=silent) for beat in found
             if not _performed(beat, existing, language=language)]
    if not lines:
        return ""
    if language.lower().startswith("zh"):
        header = (f"\n本镜人物表演要求（来源：内置表情库，{origin}，逐条执行；不得改成情绪标签，"
                  "不得新增台词或可听内容）：\n")
    else:
        header = (f"\nPerformance locks for this shot (source: built-in expression library, {origin}; "
                  "execute each line; do not reduce them to an emotion label and do not add dialogue "
                  "or any audible content):\n")
    return header + "\n".join(lines)


def inject_contract(prompt: str, block: str) -> str:
    """Place the performance lock inside the described body, not after it.

    H3 and the other structured protocols end with `overall_soundscape` and
    `non_diegetic_music`. Appending the lock there leaves stray prose after the
    last field, which a strict model can read as part of the audio field. The
    reference and dialogue locks already insert themselves after the body
    marker, so the performance lock does the same.
    """
    block = block.strip()
    if not block:
        return prompt
    for marker in ("integrated_multimodal_description:", "detailed_description:"):
        if marker in prompt:
            return prompt.replace(marker, f"{marker} {block}\n", 1)
    return f"{prompt}\n{block}"


def audit(text: str, row: dict, *, language: str = "zh-CN") -> list[str]:
    """Missing performances, for diagnostics; an empty list means compliant."""
    found, _ = beats(row)
    return [beat["emotion"] for beat in found
            if not _performed(beat, text, language=language)]
