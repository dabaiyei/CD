"""Versioned combat planning and deterministic video prompt compilation."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.martial_skill_retrieval import CATALOG_GUIDANCE
from app.services.clip_timeline import CLIP_RULES
from app.services.combat_effects import CombatEffect, RENDERERS, effect_context, compile_effect

logger = logging.getLogger(__name__)

# v5 adds the per-beat performance field, so cached designs must be rebuilt.
VERSION = "5"

# Failed replies are kept for diagnosis; three of them must not bloat the task row.
MAX_ATTEMPT_CHARS = 4000

MartialSystem = Literal["close-quarters", "grappling", "sword", "polearm", "flexible", "spell", "giant", "chase"]


class CombatWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    balance: Literal["balanced", "overwhelming", "reversal", "escape", "showdown"]
    participants: list[str] = Field(min_length=2, max_length=10)
    dominant: str = ""
    objective: str = Field(min_length=1, max_length=1000)
    outcome: str = Field(min_length=1, max_length=1000)
    techniques: list[str] = Field(default_factory=list, max_length=20)
    constraints: str = Field(default="", max_length=2000)
    martial_systems: list[MartialSystem] = Field(default_factory=list, max_length=2)
    tempo: Literal["fast", "measured"] = "fast"

    @model_validator(mode="after")
    def valid_window(self):
        if self.end <= self.start:
            raise ValueError("打斗结束时间必须晚于开始时间")
        if self.balance == "overwhelming" and self.dominant not in self.participants:
            raise ValueError("碾压段必须指定参战者中的优势方")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("参战人物不能重复")
        return self


class CombatPlan(BaseModel):
    windows: list[CombatWindow] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def ordered(self):
        end = 0.0
        for window in self.windows:
            if window.start < end:
                raise ValueError("打斗时间段不能重叠或倒序")
            end = window.end
        return self

    def validate_duration(self, duration: float):
        if self.windows[-1].end > duration + .001:
            raise ValueError("打斗时间轴超出当前镜头时长")


class CombatBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    window: int | None = Field(default=None, ge=0)
    phase: Literal["exchange", "decisive_strike", "spectacle", "transition", "hold"]
    actions: list[str] = Field(min_length=1, max_length=16)
    camera: str = Field(min_length=1, max_length=1800)
    lighting: str = Field(min_length=1, max_length=1800)
    effects: list[CombatEffect] = Field(default_factory=list, max_length=8)
    impact: str = Field(default="", max_length=1600)
    continuity: str = Field(default="", max_length=1600)
    # Performance rides on the beat so a fight is not two blank faces hitting
    # each other: the same five-element rule the plain path uses.
    expression: str = Field(default="", max_length=1600)


class CombatDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: CombatPlan
    setting: str = Field(min_length=1, max_length=4000)
    beats: list[CombatBeat] = Field(min_length=1, max_length=40)
    end_state: str = Field(min_length=1, max_length=2000)
    renderer: Literal["", "UE5", "Octane", "V-Ray", "Eevee", "Redshift"] = ""
    identity_lock: str = Field(default="", max_length=2400)

    def validate_quality(self):
        if not self.identity_lock.strip():
            raise ValueError("缺少身份锁定：按实际参考绑定人物外观、服装、体型及武器，不能虚构图片编号")
        for beat in self.beats:
            if not beat.continuity.strip():
                raise ValueError(f"{beat.start:g}秒节拍缺少衔接：写明出拍姿态、动量、武器位置及下一拍承接")
            # A fight beat without a face is a pose, not a performance. Pure
            # spectacle (a technique firing with nobody in frame) is exempt.
            if beat.phase in {"exchange", "decisive_strike"} and not beat.expression.strip():
                raise ValueError(
                    f"{beat.start:g}秒交锋缺少人物表演：按五要素写明眼神、眉毛、嘴部、身体反应与此刻处境，"
                    "不能只写动作和特效"
                )
            if beat.phase in {"exchange", "decisive_strike"} and not beat.impact.strip():
                raise ValueError(f"{beat.start:g}秒交锋缺少打击反馈：写接触或落空位置、受力反应及后续变招；不能只写火花震动")

    def validate_timeline(self, duration: float, expected: CombatPlan | None = None):
        self.plan.validate_duration(duration)
        if expected and self.plan != expected:
            raise ValueError("打斗模块不能修改已确认的时间轴、实力关系和结果")
        cursor = 0.0
        for beat in self.beats:
            for effect in beat.effects:
                if effect.start < beat.start - .001 or effect.start >= beat.end or effect.end > duration + .001:
                    raise ValueError(
                        f"特效必须从所属动作节拍内产生，且不得超过镜头时长 {duration:g} 秒；"
                        f"该特效 {effect.start:g}—{effect.end:g}s 不属于节拍 {beat.start:g}—{beat.end:g}s。"
                        "请把特效 start 放在本节能触发的位置、end 不超过本镜末尾；余波可用后续spectacle节拍承载。"
                    )
            if abs(beat.start - cursor) > .001 or beat.end <= beat.start:
                raise ValueError("详细动作时间轴必须连续、无重叠、无空缺")
            if beat.window is not None:
                if beat.window >= len(self.plan.windows):
                    raise ValueError("未知打斗时间段")
                window = self.plan.windows[beat.window]
                if beat.start < window.start - .001 or beat.end > window.end + .001:
                    raise ValueError("详细动作越过指定打斗时间段")
            elif any(beat.start < w.end and beat.end > w.start for w in self.plan.windows):
                raise ValueError("打斗时间内不能用未绑定的普通动作替代")
            if any(not action.strip() or len(action) > 2000 for action in beat.actions):
                raise ValueError("动作条目为空或过长")
            cursor = beat.end
        if abs(cursor - duration) > .001:
            raise ValueError("详细动作必须覆盖整个镜头时长")
        for index, window in enumerate(self.plan.windows):
            beats = [b for b in self.beats if b.window == index]
            if not beats or abs(beats[0].start - window.start) > .001 or abs(beats[-1].end - window.end) > .001:
                raise ValueError("打斗时间段未完整编排")
            if window.balance == "balanced" and window.end - window.start >= 2:
                fast = window.tempo == "fast"
                minimum = max(4, math.ceil((window.end - window.start) * (1.5 if fast else .8)))
                exchange = [b for b in beats if b.phase == "exchange"]
                actions = {"".join(a.split()) for b in exchange for a in b.actions}
                if len(actions) < minimum:
                    raise ValueError("势均力敌段缺少连续攻防，不能只编排一两招")
                if fast and (any(b.end - b.start > 2.001 for b in exchange)
                             or sum(b.end - b.start for b in exchange) < (window.end - window.start) * .8 - .001):
                    raise ValueError("高速势均力敌至少80%时间为连续交锋，每个攻防节拍最多2秒；不能用长段蓄力或对峙稀释")
            if window.balance == "overwhelming":
                strikes = [b for b in beats if b.phase == "decisive_strike"]
                exchanges = [b for b in beats if b.phase == "exchange"]
                # A shot may show the crushing blow or the aftermath of one that
                # already happened, so zero strikes is legal for a continuation
                # shot; what is never legal is trading blows (exchange) or
                # killing the target again (a second decisive_strike).
                if len(strikes) > 1 or exchanges:
                    found = []
                    if len(strikes) > 1:
                        spans = "、".join(f"{b.start:g}—{b.end:g}s" for b in strikes)
                        found.append(f"{len(strikes)} 个 decisive_strike（{spans}）")
                    if exchanges:
                        spans = "、".join(f"{b.start:g}—{b.end:g}s" for b in exchanges)
                        found.append(f"{len(exchanges)} 个 exchange（{spans}）")
                    raise ValueError(
                        "碾压段最多 1 个 decisive_strike，且不得出现 exchange；"
                        + "；".join(found)
                        + "。碾压是被压制方无法还手：请把这些节拍改为 spectacle，"
                        "写冲击传播、位移与后果；若本镜只表现上一次交击的余波，可以没有 decisive_strike。"
                    )
                if strikes:
                    strike = strikes[0]
                    allowed = min(1, window.end - window.start)
                    if strike.end - strike.start > allowed + .001:
                        raise ValueError(
                            f"碾压的 decisive_strike 必须不超过 {allowed:g} 秒；"
                            f"当前 {strike.start:g}—{strike.end:g}s 共 {strike.end - strike.start:g} 秒。"
                            "请把超出部分拆成独立 spectacle 节拍（蓄能、冲击传播、余波），交击本身保持一秒内。"
                        )


PLANNING_RULES = """
【战斗模块分工 v2，优先于旧版逐招编排要求】
剧本仅规划几秒到几秒的战斗段、参战者、实力关系、目标、结果、招式资产需求及进出状态，
先判断本章是否有战斗，没有则不新增打斗。
不展开逐招挥砍与运镜细节；剧情、台词与胜负仍需明确。剧本时间以本章为参照。
分镜将战斗段映射到当前模型合法时长，在每镜 combat_plan.windows 中保存镜内相对秒数；
字段为 start、end、balance、participants、dominant、objective、outcome、techniques、constraints。
tempo默认fast；仅用户或剧情明确要求克制、缓慢演示时设measured，不为了容易生成而降速。
balance: balanced势均力敌、overwhelming碾压、reversal逆转、escape逃脱、showdown对峙。
碾压必须指定dominant。非战斗镜头 combat_plan=null。跨镜延续结果只发生一次，不每镜重复击杀。
scene_description保留空间、身份、光线和进镜状态；action_description只描述各时间段目的与结果。
详细连招由视频提示词阶段的独立打斗模块设计；前置审核检查时间、实力、结果、资产与连续性，
不得因尚无逐招细节而判失败，也不得为每次抬手/蓄力各拆一镜。
"""

PLANNING_RULES += CATALOG_GUIDANCE

DESIGN_RULES = """
你是独立战斗编排模块，只返回给定CombatDesign JSON结构，不返回shots/prompts结构或视频全文。
按给定combat_plan扩写详细动作，不改变时间窗口、参战者、实力关系、胜负、对白、世界观。
本阶段明确授权补充粗略分镜未写的具体攻防、轨迹和特效表现；旧手册中“不能补动作”
或“只保留一个主要动作”的要求不用于删减详细编排，但不能改变已确认的叙事结果。
旧分镜没有combat_plan时先从当前动作及邻镜推断plan；明确静止对峙用showdown，禁止凭空改成实战。
beats连续覆盖镜头0秒到duration_seconds。window为对应windows的零基索引；非战斗时间window=null。
phase只能取以下五个值，按语义选择，不要用exchange表达位移或后果：
exchange＝双方互有攻防、对手做出应对；decisive_strike＝一次决定胜负的命中（≤1秒）；
spectacle＝技能显形、冲击传播、被击飞/翻滚/下坠、环境崩毁等后果，无人还手；
transition＝镜头或空间转换；hold＝明确静止、对峙或定格。
碾压段禁止用exchange：被击飞、翻滚、位移都属于spectacle，不是对手在还手。
势均力敌：以密集连攻、拆招、反击、借力与位移形成高速实时攻防簇，
actions逐条写具体攻防因果，不以“激战数回合”代替。局部短距离接触可每秒2—4次，不固定配额。
2秒及以上的balanced且tempo=fast段，至少max(4,ceil(段时长*1.5))条不重复攻防，每条包含攻击与对手的实际应对；
至少80%时长为exchange，每个exchange节拍不超过2秒，连续时间轴不得用一段长描述藏掉中间节奏。
这是最低密度校验，不是每秒招数的上限；局部连攻可更密，位移、完整摔投与重击仍需真实完成过程。
仅明确要求慢节奏时使用tempo=measured，其最低攻防条目max(4,ceil(段时长*0.8))，禁止自行把高速要求降级。
碾压：一次decisive_strike瞬间破防，禁止对手反复接住强者攻击；其余时间展示技能形态、冲击传播与后果，
不能为凑模型时长延长接触或反复杀死目标；不能把炫技蓄能误作势均力敌。
碾压段禁止出现exchange：被压制方无法还手，写成exchange即校验失败。
整段最多1个decisive_strike；若本镜只承接上一镜已发生的交击余波（跨镜延续结果只发生一次），
可以完全没有decisive_strike，全部用spectacle写冲击传播、位移与后果。
decisive_strike最多1秒；蓄能、显形、冲击传播与余波使用独立spectacle段。
逆转：先表现优势方压制，再以已设定破绽/招式翻盘。逃脱：围绕摆脱追击而非强行打赢。
showdown与明确静止要求保持，不自作主张添加交锋。只有大招显形和关键命中展示才短暂放慢。
每beat明确camera跟随目标、路径与速度，lighting写主光和随动作变化的局部特效照明。
按项目首帧模式处理接续：开启且明确同组时才承接真实尾帧；关闭时凭资产与明确入镜状态独立生成，禁止依赖前镜尾帧。连续动作不复位；保留原文及internal_shots明确的切点和一镜到底要求。
未锁定机位的粗略战斗段允许在本阶段细化动作匹配切镜，不改变原时间段、事件或结局；
camera明确切点、前后机位与同一轴线侧，切前切后承接同一动量，不重演命中、不跳过关键接触。
读取模型能力、手册、人物/招式参考、邻镜和台词；无人招式图仅约束特效武器，明确其所属人物与释放点。
setting仅写场景、外观与参考关系，不重复动作。end_state保留下一镜所需姿态、持物与场景结果。
不生成任何新台词或音频描述，声音由平台保留原台词并按模型能力处理。
按protocol.preferred_prompt_language写setting/actions/camera/lighting/end_state，计划字段保持原文。
输出的所有动作由程序按时间编译，之后不会再交给另一个AI摘要；不要自行压缩必要攻防。
【强制质量标准：密集、打击感、流畅度同时成立】
identity_lock必须按实际reference_map/人物资产逐人绑定五官、发型、服装、体型、持有武器；
无人技能图仅锁定武器特效，不取代人物图；缺少真实图片不得捏造图片一/二或服装细节。
每个exchange/decisive_strike必须输出impact：具体接触点、力的方向、武器反弹/卸力、
受击者重心和位移、相应环境反应。落空则写擦身距离、惯性和暴露的破绽，不虚构命中火花。
关键重击可有极短命中顿挫、短促相机反冲并立即恢复实时速度；不是低帧率、停格或每击慢放。
每beat必须输出continuity：当前动量、落脚点、兵器方位、出拍姿态及下一拍如何接招，
最后一拍接end_state；不每1.5秒重置站位，不为切镜凭空腾空、落地或恢复破损场景。
每beat必须输出expression：按五要素写明参战者的眼神、眉毛、嘴部、身体反应与此刻处境，
让打斗有真实反应而不是两张面无表情的脸互撞；exchange/decisive_strike缺失表演即校验失败。
表演服从本镜emotion_plan的主情绪与强度，同一节拍只给一个主情绪，不堆三种情绪。
音频关闭时嘴唇保持完全闭合，情绪只走眼神、呼吸、肩颈和手部；打斗中的表情不得新增台词或声音。
高速均势战斗优先用约1—2秒的攻防簇：抢攻→格挡/闪避→顺反弹变线→反击/追击，
每簇包含多次具体应对，以优势变化推进；这不是必须每簇硬切，也不是固定12秒八镜模板。
示例节奏：低位突进硬碰后卸力侧移，侧向追拍接连续拆招，利用破绽变线追击，
一次有结果的重击改变空间，承接惯性继续战斗；只有剧情要求结束才收束对峙/拉远。
camera写清锁定谁、跟随方向、景别与速度，背景视差体现高速，接触点清晰；
lighting保持主光来源，命中火光短促照亮邻近表面；动态模糊保留脸与兵器识别，不靠震动糊屏。
游戏CG/UE5画风使用电影级游戏CG、Nanite精细几何、Lumen动态光照、Niagara分层粒子、
HDR高光、真实反射、景深与受控动态模糊的完整质感；8K是细节风格词，不覆盖模型真实分辨率。
动作密度、受力与连续性要求高于旧手册的保守动作限制；渲染材质仍适配项目画风。
"""


def fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps({"version": VERSION, **snapshot}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# Finish reasons that mean the model stopped early rather than finishing its answer.
TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens", "exceed_max_iters", "content_filter"}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def unterminated_json(raw: str) -> bool:
    """Whether the reply is not a complete JSON document.

    AgentScope reports `completed` even when the provider stopped on the token
    limit, so the finish reason cannot be trusted on its own; a document that
    never closes its braces is the durable evidence that the tail was cut off.
    """
    clean = _FENCE.sub("", raw or "").strip()
    if not clean:
        return False
    try:
        json.loads(clean)
        return False
    except json.JSONDecodeError:
        pass
    depth = 0
    in_string = False
    escaped = False
    for character in clean:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
    return in_string or depth > 0


def truncated_output(result) -> bool:
    """Whether the reply was cut short, making any missing field a symptom.

    A chopped JSON tail and a model that simply forgot a field both surface as
    "end_state missing", but only the first one is fixed by shortening the
    answer; retrying with a field list cannot help when tokens ran out.
    """
    if str(getattr(result, "finish_reason", "") or "").strip().lower() in TRUNCATED_FINISH_REASONS:
        return True
    return unterminated_json(str(getattr(result, "final_response", "") or ""))


def repair_instruction(exc: Exception, *, truncated: bool) -> str:
    """Turn a raw validation failure into an instruction the model can act on.

    Pydantic's own text leads with the version banner and an elided
    `input_value` dump, which tells the model neither what to change nor that
    the answer was cut off.
    """
    missing = []
    invalid = []
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc") or ()) or "根字段"
            if error.get("type") == "missing":
                missing.append(field)
            else:
                invalid.append(f"{field}（{error.get('msg') or error.get('type')}）")
    else:
        # Rule violations raised by our own validators already read as an
        # instruction ("打斗模块不能修改…"), so pass them through unchanged.
        return str(exc)[:1200]
    if truncated:
        head = "上次输出的 JSON 在结束前被截断，末尾字段未能写完。"
        advice = ("请压缩 setting、camera、lighting、actions 的文字长度，不要减少节拍数量，"
                  "并确保 JSON 以完整的 end_state 收尾。")
    else:
        head = "上次输出的 JSON 结构不完整。"
        advice = "请补全后重新输出完整 JSON，不要省略任何必填字段。"
    parts = [head]
    if missing:
        parts.append("缺少必填字段：" + "、".join(dict.fromkeys(missing)) + "。")
    if invalid:
        parts.append("字段不合法：" + "；".join(dict.fromkeys(invalid)) + "。")
    if not missing and not invalid:
        parts.append("未通过校验。")
    parts.append(advice)
    return "".join(parts)


def compile_prompt(design: CombatDesign, protocol: dict) -> str:
    lines = [design.setting]
    zh = str(protocol.get("preferred_prompt_language", "zh-CN")).startswith("zh")
    if design.identity_lock:
        lines.append(("身份与参考锁定：" if zh else "Identity and reference lock: ") + design.identity_lock)
    for beat in design.beats:
        zh = str(protocol.get("preferred_prompt_language", "zh-CN")).startswith("zh")
        lines.append(f"{beat.start:g}–{beat.end:g}s: " + " ".join(beat.actions) +
                     (f" 运镜：{beat.camera} 光影：{beat.lighting}" if zh else
                      f" Camera: {beat.camera} Lighting: {beat.lighting}"))
        lines.extend(compile_effect(effect, zh) for effect in beat.effects)
        if beat.impact:
            lines.append(("打击反馈：" if zh else "Impact response: ") + beat.impact)
        if beat.expression:
            lines.append(("人物表演：" if zh else "Performance: ") + beat.expression)
        if beat.continuity:
            lines.append(("动作衔接：" if zh else "Motion continuity: ") + beat.continuity)
    lines.append(design.end_state)
    if design.renderer:
        lines.append(RENDERERS[design.renderer])
    lines.append("负面约束：身份漂移、服装发型突变、武器变形、多余角色、脸手畸变、动作僵硬、无支撑漂浮、逐招复位、低帧率卡顿、粒子糊屏、无因背景突变。保留剧情明确的损伤与环境破坏。" if zh else
                 "Avoid identity drift, wardrobe/hair changes, deformed weapons, extra characters, malformed faces/hands, stiff motion, unsupported floating, pose resets, stutter, obscuring particles and unexplained background changes. Preserve story-required damage.")
    body = "\n".join(lines)
    if protocol.get("name") == "minimax_h3":
        body = "integrated_multimodal_description: " + body + "\noverall_soundscape: N/A\nnon_diegetic_music: N/A"
    if len(body) > 26000:
        raise ValueError("打斗编排过长，请减少冗余描述；不会静默截断动作")
    return body


def _shrink_skill_reference(prompt: str, over: int) -> str:
    """Cut `over` characters from the martial-arts reference block of a prompt.

    The block sits between its heading and the output schema; everything after
    it is the schema, rules and shot data the reply is validated against, so
    only this section may be shortened.
    """
    start = prompt.find("本镜按需武指参考：")
    end = prompt.find("\n输出schema：", start)
    if start < 0 or end <= start:
        return prompt
    body_start = start + len("本镜按需武指参考：")
    body = prompt[body_start:end]
    keep = max(0, len(body) - over)
    trimmed = body[:keep]
    if keep < len(body):
        trimmed += "（本镜数据较大，武指参考已截断）"
    return prompt[:body_start] + trimmed + prompt[end:]


async def generate(task_id: str, row: dict, contract: dict, protocol: dict, runtime_factory):
    from app.services import task_worker as worker
    from app.services.generation_parallel import task_write_lock
    from app.services.martial_skill_retrieval import retrieve, shot_query, MAX_REQUEST_CHARS
    # Image prompts and old video prose are redundant with authoritative scene/assets and can be enormous.
    shot = {k: v for k, v in row.items() if k not in {"image_prompt", "current_video_prompt"}}
    shot["assets"] = [{k: v for k, v in asset.items() if k != "generation_prompt"}
                      for asset in row.get("assets", [])]
    skill_context, retrieval = retrieve(shot)
    snapshot = {"shot": shot, "model": contract, "protocol": protocol}
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    schema = json.dumps(CombatDesign.model_json_schema(), ensure_ascii=False)
    trailer = CLIP_RULES + effect_context(row)
    # The schema and rules are parsed against, so they are never dropped; the
    # retrieved guidance is optional and shrinks when the shot data is large.
    base_request = await worker.runtime_request(
        task_id, prompt_code="video-prompt-generation", combat_design=True, combat_query=shot_query(shot),
        prompt=(f"本镜按需武指参考：\n{skill_context}\n输出schema：\n{schema}{trailer}"
                f"\n任务快照：\n{snapshot_json}"),
    )
    over = len(base_request.prompt) + len(base_request.system_prompt) - MAX_REQUEST_CHARS
    if over > 0:
        # Drop the guidance before failing the shot: the shot data is what the
        # user asked about, and guidance is a reference the model already has.
        shrunk = _shrink_skill_reference(base_request.prompt, over)
        retrieval["trimmed_skill_chars"] = len(base_request.prompt) - len(shrunk)
        base_request = await worker.runtime_request(
            task_id, prompt_code="video-prompt-generation", combat_design=True, combat_query=shot_query(shot),
            prompt=shrunk,
        )
    if len(base_request.prompt) + len(base_request.system_prompt) > MAX_REQUEST_CHARS:
        # Only reachable when the shot JSON itself does not fit, which the user
        # can act on by shortening the shot.
        raise RuntimeError(
            f"本镜数据约 {(len(base_request.prompt) + len(base_request.system_prompt)):,} 字符，"
            f"超出 {MAX_REQUEST_CHARS:,} 预算且无法通过裁剪参考解决；"
            "请精简该镜的场景与动作描述后重试"
        )
    key = fingerprint({**snapshot, "retrieval": retrieval, "system": base_request.system_prompt,
                       "prompt": base_request.prompt})
    retrieval["handbook_files"] = [{"path": s["path"], "sha256": s["sha256"], "characters": len(s["content"])}
                                   for s in base_request.skills]
    retrieval["request_characters"] = len(base_request.prompt) + len(base_request.system_prompt)
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            raise RuntimeError("打斗编排任务已停止")
        cached = (task.request_payload.get("combat_designs") or {}).get(row["shot_id"])
    expected = CombatPlan.model_validate(row["combat_plan"]) if row.get("combat_plan") else None
    duration = float(row["duration_seconds"])
    if expected:
        expected.validate_duration(duration)
    if cached and cached.get("fingerprint") == key:
        design = CombatDesign.model_validate(cached["design"])
        design.validate_timeline(duration, expected)
        design.validate_quality()
        return compile_prompt(design, protocol), cached
    error = ""
    attempts = []
    for attempt in range(3):
        await worker.record_progress(task_id, 35,
            f"镜头 {row['order_index']}：独立打斗编排 {attempt + 1}/3")
        request = base_request.model_copy(deep=True)
        if error:
            request.prompt += "\n上次结果校验失败，请修正：" + error
        # The correction is worth more than the optional reference, so reclaim
        # room from the guidance instead of failing the shot on the retry.
        over = len(request.prompt) + len(request.system_prompt) - MAX_REQUEST_CHARS
        if over > 0:
            request.prompt = _shrink_skill_reference(request.prompt, over)
        if len(request.prompt) + len(request.system_prompt) > MAX_REQUEST_CHARS:
            raise RuntimeError(
                f"本镜数据约 {(len(request.prompt) + len(request.system_prompt)):,} 字符，"
                f"超出 {MAX_REQUEST_CHARS:,} 预算；请精简本镜的场景与动作描述后重试"
            )
        request.session_id += f"-combat-{row['shot_id']}-{attempt}"
        result = await runtime_factory().run(request)
        truncated = truncated_output(result)
        try:
            design = CombatDesign.model_validate(worker.parse_json_object(result.final_response))
            design.validate_timeline(duration, expected)
            design.validate_quality()
            if expected is None and not re.search(r"慢速|慢节奏|缓慢|克制|slow|measured", shot_query(shot), re.I):
                if any(w.balance == "balanced" and w.tempo == "measured" for w in design.plan.windows):
                    raise ValueError("当前镜头未要求慢节奏，不能把实时交锋降为measured")
            text = compile_prompt(design, protocol)
            if row.get("first_frame_mode") is False:
                from app.services.first_frame_policy import validate_independent_text
                validate_independent_text(text)
        except (ValueError, TypeError, RuntimeError) as exc:
            if hasattr(runtime_factory, "invalid_output"):
                runtime_factory.invalid_output()
            error = repair_instruction(exc, truncated=truncated)
            attempts.append({"attempt": attempt + 1, "truncated": truncated,
                             "finish_reason": str(getattr(result, "finish_reason", "") or ""),
                             "reason": error,
                             "raw_response": result.final_response[:MAX_ATTEMPT_CHARS]})
            continue
        cached = {"fingerprint": key, "version": VERSION, "design": design.model_dump(),
                  "runtime_manifest": result.manifest, "skill_retrieval": retrieval}
        async with task_write_lock(task_id), worker.SessionLocal() as session:
            task = await worker.owned_task_for_update(session, task_id)
            if not worker.owns_running_task(task):
                raise RuntimeError("打斗编排任务已停止，结果未保存")
            task.request_payload = {**task.request_payload,
                "combat_designs": {**(task.request_payload.get("combat_designs") or {}), row["shot_id"]: cached}}
            await session.commit()
        return text, cached
    await _record_attempts(worker, task_id, row["shot_id"], attempts)
    if all(item["truncated"] for item in attempts):
        raise RuntimeError(
            "打斗编排连续三次因输出被截断而失败：模型在写完 JSON 前用尽输出长度。"
            "请在 Agent 配置中提高该文本模型的 max_tokens，或缩短本镜时长/减少节拍数量后重试。"
            "原始输出已保存在任务的 combat_attempts 中。"
        )
    raise RuntimeError("打斗编排连续三次未通过时间轴/实力关系校验：" + error)


async def _record_attempts(worker, task_id: str, shot_id: str, attempts: list[dict]) -> None:
    """Keep failed replies so a run can be diagnosed without re-running the model.

    Diagnostics must never replace the real failure: a task that has already
    stopped, or an unavailable session, only loses the saved evidence.
    """
    if not attempts:
        return
    from app.services.generation_parallel import task_write_lock

    try:
        async with task_write_lock(task_id), worker.SessionLocal() as session:
            task = await worker.owned_task_for_update(session, task_id)
            if task is None:
                return
            payload = dict(task.request_payload or {})
            payload["combat_attempts"] = {**(payload.get("combat_attempts") or {}), shot_id: attempts}
            task.request_payload = payload
            await session.commit()
    except Exception:
        logger.warning("Failed to persist combat attempts for task %s", task_id, exc_info=True)
