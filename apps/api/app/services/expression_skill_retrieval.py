"""Bounded, stage-scoped retrieval for the in-project drama expression library.

Mirrors ``martial_skill_retrieval``: the catalog carries emotion names and
descriptions only, and a category file is opened after routing decides the shot
actually needs it. Loading all sixty entries into every video request would
crowd the context window for no benefit, and the caller would then have to
ignore most of it anyway.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).with_name("expression_skills")
SOURCE = "skills/ai-drama-expressions"
MAX_SKILL_CHARS = 5200

# Category file per emotion. The keys are the canonical names from the library,
# so a storyboard or a shot that names an emotion routes deterministically.
EMOTION_FILES: dict[str, str] = {
    "平静克制": "emotion-basic.md", "轻微疑惑": "emotion-basic.md", "认真倾听": "emotion-basic.md",
    "若有所思": "emotion-basic.md", "欲言又止": "emotion-basic.md", "强装镇定": "emotion-basic.md",
    "礼貌微笑": "emotion-basic.md", "尴尬停顿": "emotion-basic.md", "不动声色": "emotion-basic.md",
    "暗自判断": "emotion-basic.md",
    "浅浅开心": "emotion-joy.md", "忍不住笑": "emotion-joy.md", "释然一笑": "emotion-joy.md",
    "久别重逢": "emotion-joy.md", "得意小表情": "emotion-joy.md", "调皮挑衅": "emotion-joy.md",
    "被夸后的害羞": "emotion-joy.md", "松了一口气": "emotion-joy.md", "温柔注视": "emotion-joy.md",
    "喜极而泣": "emotion-joy.md",
    "轻微紧张": "emotion-fear.md", "突然警觉": "emotion-fear.md", "不安预感": "emotion-fear.md",
    "被吓一跳": "emotion-fear.md", "强烈恐惧": "emotion-fear.md", "惊恐求救": "emotion-fear.md",
    "压抑害怕": "emotion-fear.md", "绝境慌乱": "emotion-fear.md", "发现真相": "emotion-fear.md",
    "大脑空白": "emotion-fear.md",
    "隐忍难过": "emotion-grief.md", "失望低头": "emotion-grief.md", "委屈想哭": "emotion-grief.md",
    "无声落泪": "emotion-grief.md", "压住哭腔": "emotion-grief.md", "彻底心碎": "emotion-grief.md",
    "崩溃大哭": "emotion-grief.md", "麻木悲伤": "emotion-grief.md", "悔恨自责": "emotion-grief.md",
    "告别时微笑": "emotion-grief.md",
    "轻微不满": "emotion-anger.md", "忍着怒气": "emotion-anger.md", "冷笑反击": "emotion-anger.md",
    "被激怒": "emotion-anger.md", "愤怒质问": "emotion-anger.md", "失控暴怒": "emotion-anger.md",
    "含泪愤怒": "emotion-anger.md", "被背叛的怒": "emotion-anger.md", "决裂表情": "emotion-anger.md",
    "战斗前狠劲": "emotion-anger.md",
    "笑里藏刀": "emotion-complex.md", "表面答应": "emotion-complex.md", "强颜欢笑": "emotion-complex.md",
    "半信半疑": "emotion-complex.md", "突然明白": "emotion-complex.md", "心虚躲闪": "emotion-complex.md",
    "嫉妒压抑": "emotion-complex.md", "被看穿后慌张": "emotion-complex.md",
    "重新燃起希望": "emotion-complex.md", "下定决心": "emotion-complex.md",
}

# Inference patterns, tried in order. The first match wins, so the more specific
# compound emotions are listed before the plain ones they contain. An exact
# library name is resolved before this table runs, so each of the sixty always
# routes to itself instead of being folded into a broader neighbour.
INFERENCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"笑里藏刀|口蜜腹剑", "笑里藏刀"),
    (r"冷笑|冷嘲|讥讽", "冷笑反击"),
    (r"忍(着|住)(怒|火|气)|压住火|咬牙| clenched", "忍着怒气"),
    (r"暴怒|失控|发狂|红眼|戾气", "失控暴怒"),
    (r"含泪|眼里有泪|泪中带怒|悲愤", "含泪愤怒"),
    (r"怒|愤|恨|吼|喝斥|质问|斥责|敌意|杀意|凶狠|狠劲", "被激怒"),
    (r"不屑|轻蔑|鄙夷|蔑视|看不起|轻慢", "轻微不满"),
    (r"崩溃|嚎啕|大哭|痛哭|撕心", "崩溃大哭"),
    (r"无声落泪|泪水滑落|一滴泪|流泪|落泪|掉泪", "无声落泪"),
    (r"隐忍|强忍|忍住(眼泪|哭|泪)|憋(着|住)|不让自己哭", "隐忍难过"),
    (r"哭腔|哽咽|抽噎|带哭", "压住哭腔"),
    (r"委屈|难过|伤心|悲伤|悲痛|痛苦|心碎|心凉|绝望|低落|失去|死亡|牺牲|离别|告别",
     "隐忍难过"),
    (r"发现真相|真相|恍然|原来如此|明白了一切|不敢置信|震惊", "发现真相"),
    (r"大脑空白|空白|呆住|失神|麻木|失魂", "大脑空白"),
    (r"警觉|戒备|警惕|察觉|察觉不对|感到危险", "突然警觉"),
    (r"恐惧|害怕|惊恐|畏惧|恐慌|毛骨悚然|战栗|吓", "强烈恐惧"),
    (r"紧张|不安|忐忑|慌乱|急促|危险|危急|压迫|威胁|逼近", "轻微紧张"),
    # Intense joy has no separate entry; it is a stronger 浅浅开心, so it must
    # not fall through to the neutral baseline. Listed before the plain joy rule
    # so the stronger reading wins.
    (r"狂喜|大喜|喜不自胜|欣喜若狂|喜出望外|惊喜|喜极而泣|喜极|激动地哭", "喜极而泣"),
    (r"久别重逢|重逢|终于见到|再次见到", "久别重逢"),
    (r"释然|放下了?心|松了一口气|松口气|舒展|解脱|释怀|如释重负|放松下来|缓过来", "释然一笑"),
    (r"开心|高兴|欢喜|喜悦|愉快|兴奋|乐趣|甜|满足|幸福|笑", "浅浅开心"),
    (r"坚定|决心|决意|拿定主意|义无反顾", "下定决心"),
    # 五味杂陈 is a mix rather than one dominant feeling; 半信半疑 is the closest
    # entry that reads as layered, so it must not collapse to the neutral face.
    (r"五味杂陈|百感交集|心绪复杂|情绪复杂|纠结|难以言说|说不清", "半信半疑"),
    (r"温柔|柔和|怜爱|保护|深情|宠溺", "温柔注视"),
    (r"挑衅|逗弄|俏皮|顽皮|捉弄|揶揄", "调皮挑衅"),
    (r"得意|炫耀|自豪|傲", "得意小表情"),
    (r"尴尬|窘迫|僵住|不知所措", "尴尬停顿"),
    (r"疑惑|困惑|不解|皱眉思考|疑问", "轻微疑惑"),
    (r"欲言又止|张了张嘴|欲语还休|话到嘴边", "欲言又止"),
    (r"强装|硬撑|若无其事|装作镇定|不动声色|面无表情|克制", "强装镇定"),
    (r"观察|打量|审视|评估|判断|盘算|考量", "暗自判断"),
    (r"倾听|注视|凝视|看着对方|认真听", "认真倾听"),
)


def _read(filename: str) -> str:
    return (ROOT / filename).read_text(encoding="utf-8")


_ENTRY = re.compile(r"^\s*\d+\.\s*(?:\*\*)?(?P<name>[^:*：]+?)(?:\*\*)?\s*[:：]\s*(?P<body>.+?)\s*$")


@lru_cache(maxsize=1)
def library() -> dict[str, str]:
    """Parse the library into `emotion -> performance description`.

    The markdown files are the single source of truth: the model reads them as
    reference, and the deterministic compiler reads the same text, so the two
    can never drift apart.
    """
    entries: dict[str, str] = {}
    for filename in dict.fromkeys(EMOTION_FILES.values()):
        for line in _read(filename).splitlines():
            match = _ENTRY.match(line)
            if match:
                entries[match.group("name").strip()] = match.group("body").strip()
    missing = set(EMOTION_FILES) - set(entries)
    if missing:
        raise ValueError("表情库缺少条目：" + "、".join(sorted(missing)))
    return entries


def performance(emotion: str) -> str:
    """The canonical performance sentence for one emotion, or its raw name."""
    return library().get(emotion.strip(), "眼神出现明显变化，嘴角收紧，身体有细微反应")


def query_text(row: dict) -> str:
    """Classify from the shot's own content, never from the library itself.

    Asset descriptions are deliberately excluded: a character sheet saying
    "冷酷" or a scene note mentioning a past death would otherwise decide the
    emotion of a shot that is actually calm. Who is in frame is resolved from
    the assets separately.
    """
    return json.dumps({key: row.get(key) for key in (
        "title", "shot_type", "scene_description", "action_description", "dialogue",
        "emotion_plan",
    )}, ensure_ascii=False)


def infer_emotion(text: str) -> str:
    # A literal library name is authoritative. Without this, a shot or an
    # emotion_plan that names one of the sixty could be folded into a broader
    # entry (ten distinct complex emotions all became 下定决心), which silently
    # changed the performance the caller asked for.
    for name in EMOTION_FILES:
        if name in text:
            return name
    for pattern, emotion in INFERENCE_PATTERNS:
        if re.search(pattern, text, re.I):
            return emotion
    return "平静克制"


def canonical_emotion(emotion: str, trigger: str = "") -> str:
    """Map a name the caller used onto a library entry.

    A storyboard is allowed to describe an emotion the library does not name;
    that must not fail the whole board. The nearest library emotion is resolved
    from the name and its trigger, so the compiled performance stays valid.
    """
    name = (emotion or "").strip()
    if name in EMOTION_FILES:
        return name
    return infer_emotion(f"{name} {trigger or ''}")

def resolve(row: dict) -> tuple[list[str], str]:
    """The emotions this shot performs and whether they were declared upstream."""
    declared = [canonical_emotion(beat.get("emotion"), beat.get("trigger"))
                for beat in ((row.get("emotion_plan") or {}).get("beats") or [])]
    declared = list(dict.fromkeys(emotion for emotion in declared if emotion in EMOTION_FILES))
    if declared:
        return declared, "declared"
    return [infer_emotion(query_text(row))], "inferred"


def retrieve(row: dict) -> tuple[str, dict]:
    """Return the inline reference block plus a manifest for cache invalidation."""
    emotions, origin = resolve(row)
    files = ["core.md", *dict.fromkeys(EMOTION_FILES[emotion] for emotion in emotions)]
    parts, hashes, skipped = [], {}, []
    for filename in files:
        content = _read(filename)
        block = f'<expression-reference name="{filename}">\n{content}\n</expression-reference>'
        # The budget is a hard limit for the request, not a reason to fail the
        # shot: dropping the later categories still ships a valid performance
        # because the compiled lock only needs the entry it names. Losing every
        # category would be a real problem, so that still raises.
        if parts and len("\n\n".join([*parts, block])) > MAX_SKILL_CHARS:
            skipped.append(filename)
            continue
        parts.append(block)
        hashes[filename] = hashlib.sha256(content.encode()).hexdigest()
    context = "\n\n".join(parts)
    if len(context) > MAX_SKILL_CHARS:
        raise ValueError("表演核心参考超出上下文预算，请精简 core.md")
    return context, {
        "source": SOURCE,
        "selected": [filename for filename in files if filename not in skipped],
        "skipped": skipped,
        "emotions": emotions,
        "origin": origin,
        "hashes": hashes,
        "characters": len(context),
    }


def version_marker(manifest: dict) -> str:
    """A stable cache key for the retrieved block, safe to store in versions."""
    digest = hashlib.sha256(
        json.dumps(manifest.get("hashes", {}), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"expression-skill:{manifest.get('source', '')}@{digest}"


def guidance() -> str:
    """The system-prompt half: rules that must apply even without retrieval."""
    return (
        "\n人物表演方法（内置 ai-drama-expressions 库，已按情绪检索并内联提供；"
        "参考正文按需读取，不要尝试调用外部技能或文件工具）：\n"
        "- 表情不是一个词，而是一段表演。禁止只写“很伤心”“很愤怒”这类情绪标签。\n"
        "- 优先使用库内六十条固定表情；库中没有的情绪用万能表情公式现场拆解，"
        "不要退回成情绪标签，也不要用一个近似的库内情绪顶替原本要演的情绪。\n"
        "- 每个表演段按五要素写全：眼神、眉毛、嘴角与嘴唇、身体反应、剧情状态，"
        "再用一句“像……”点明表演意图。缺少身体反应的表情视为不合格。\n"
        "- 一条镜头（或一个内部时间段）只给一个主情绪，情绪之间要递进，不得同段混三种主情绪。\n"
        "- 情绪强度默认中等档；只有剧情明确要求爆发时才用极致档，避免情绪太满没有层次。\n"
        "- 情绪只通过面部与身体表演表达，不得借表情新增台词、旁白或任何可听内容；"
        "音频关闭时嘴唇保持完全闭合，不出现说话、喊叫或呐喊口型。\n"
        "- 不得为了演出表情而擅自改动分镜已确定的景别、机位、动作结果与剧情胜负。"
    )
