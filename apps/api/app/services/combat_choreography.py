"""Versioned combat planning and deterministic video prompt compilation."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.martial_skill_retrieval import CATALOG_GUIDANCE
from app.services.clip_timeline import CLIP_RULES

VERSION = "2"

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


class CombatDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: CombatPlan
    setting: str = Field(min_length=1, max_length=4000)
    beats: list[CombatBeat] = Field(min_length=1, max_length=40)
    end_state: str = Field(min_length=1, max_length=2000)

    def validate_timeline(self, duration: float, expected: CombatPlan | None = None):
        self.plan.validate_duration(duration)
        if expected and self.plan != expected:
            raise ValueError("打斗模块不能修改已确认的时间轴、实力关系和结果")
        cursor = 0.0
        for beat in self.beats:
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
                if sum(b.phase == "decisive_strike" for b in beats) != 1 or any(b.phase == "exchange" for b in beats):
                    raise ValueError("碾压段应一次决定性交击，不得编排成往返拉锯")
                strike = next(b for b in beats if b.phase == "decisive_strike")
                if strike.end - strike.start > min(1, window.end - window.start) + .001:
                    raise ValueError("碾压的决定性交击应在一秒内完成，蓄能/特效/余波另分时间段")


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
势均力敌：以密集连攻、拆招、反击、借力与位移形成高速实时攻防簇，
actions逐条写具体攻防因果，不以“激战数回合”代替。局部短距离接触可每秒2—4次，不固定配额。
2秒及以上的balanced且tempo=fast段，至少max(4,ceil(段时长*1.5))条不重复攻防，每条包含攻击与对手的实际应对；
至少80%时长为exchange，每个exchange节拍不超过2秒，连续时间轴不得用一段长描述藏掉中间节奏。
这是最低密度校验，不是每秒招数的上限；局部连攻可更密，位移、完整摔投与重击仍需真实完成过程。
仅明确要求慢节奏时使用tempo=measured，其最低攻防条目max(4,ceil(段时长*0.8))，禁止自行把高速要求降级。
碾压：一次decisive_strike瞬间破防，禁止对手反复接住强者攻击；其余时间展示技能形态、冲击传播与后果，
不能为凑模型时长延长接触或反复杀死目标；不能把炫技蓄能误作势均力敌。
decisive_strike最多1秒；蓄能、显形、冲击传播与余波使用独立spectacle段。
逆转：先表现优势方压制，再以已设定破绽/招式翻盘。逃脱：围绕摆脱追击而非强行打赢。
showdown与明确静止要求保持，不自作主张添加交锋。只有大招显形和关键命中展示才短暂放慢。
每beat明确camera跟随目标、路径与速度，lighting写主光和随动作变化的局部特效照明。
同组承接真实尾帧，连续动作不复位；不得新增硬切。若确需换机位，保持既有分镜的切点与轴线。
读取模型能力、手册、人物/招式参考、邻镜和台词；无人招式图仅约束特效武器，明确其所属人物与释放点。
setting仅写场景、外观与参考关系，不重复动作。end_state保留下一镜所需姿态、持物与场景结果。
不生成任何新台词或音频描述，声音由平台保留原台词并按模型能力处理。
按protocol.preferred_prompt_language写setting/actions/camera/lighting/end_state，计划字段保持原文。
输出的所有动作由程序按时间编译，之后不会再交给另一个AI摘要；不要自行压缩必要攻防。
"""


def fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps({"version": VERSION, **snapshot}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def compile_prompt(design: CombatDesign, protocol: dict) -> str:
    lines = [design.setting]
    for beat in design.beats:
        zh = str(protocol.get("preferred_prompt_language", "zh-CN")).startswith("zh")
        lines.append(f"{beat.start:g}–{beat.end:g}s: " + " ".join(beat.actions) +
                     (f" 运镜：{beat.camera} 光影：{beat.lighting}" if zh else
                      f" Camera: {beat.camera} Lighting: {beat.lighting}"))
    lines.append(design.end_state)
    body = "\n".join(lines)
    if protocol.get("name") == "minimax_h3":
        body = "integrated_multimodal_description: " + body + "\noverall_soundscape: N/A\nnon_diegetic_music: N/A"
    if len(body) > 26000:
        raise ValueError("打斗编排过长，请减少冗余描述；不会静默截断动作")
    return body


async def generate(task_id: str, row: dict, contract: dict, protocol: dict, runtime_factory):
    from app.services import task_worker as worker
    from app.services.martial_skill_retrieval import retrieve, shot_query, MAX_REQUEST_CHARS
    # Image prompts and old video prose are redundant with authoritative scene/assets and can be enormous.
    shot = {k: v for k, v in row.items() if k not in {"image_prompt", "current_video_prompt"}}
    shot["assets"] = [{k: v for k, v in asset.items() if k != "generation_prompt"}
                      for asset in row.get("assets", [])]
    skill_context, retrieval = retrieve(shot)
    snapshot = {"shot": shot, "model": contract, "protocol": protocol}
    base_request = await worker.runtime_request(
        task_id, prompt_code="video-prompt-generation", combat_design=True, combat_query=shot_query(shot),
        prompt="本镜按需武指参考：\n" + skill_context + "\n输出schema：\n"
            + json.dumps(CombatDesign.model_json_schema(), ensure_ascii=False)
            + CLIP_RULES
            + "\n任务快照：\n" + json.dumps(snapshot, ensure_ascii=False),
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
        return compile_prompt(design, protocol), cached
    error = ""
    for attempt in range(3):
        await worker.record_progress(task_id, 35,
            f"镜头 {row['order_index']}：独立打斗编排 {attempt + 1}/3")
        request = base_request.model_copy(deep=True)
        if error:
            request.prompt += "\n上次结果校验失败，请修正：" + error
        if len(request.prompt) + len(request.system_prompt) > MAX_REQUEST_CHARS:
            raise RuntimeError("本镜打斗编排输入超出32000字符预算，请精简本镜描述或所选手册；未发送超长请求")
        request.session_id += f"-combat-{row['shot_id']}-{attempt}"
        result = await runtime_factory().run(request)
        try:
            design = CombatDesign.model_validate(worker.parse_json_object(result.final_response))
            design.validate_timeline(duration, expected)
            if expected is None and not re.search(r"慢速|慢节奏|缓慢|克制|slow|measured", shot_query(shot), re.I):
                if any(w.balance == "balanced" and w.tempo == "measured" for w in design.plan.windows):
                    raise ValueError("当前镜头未要求慢节奏，不能把实时交锋降为measured")
            text = compile_prompt(design, protocol)
        except (ValueError, TypeError, RuntimeError) as exc:
            error = str(exc)[:1500]
            continue
        cached = {"fingerprint": key, "version": VERSION, "design": design.model_dump(),
                  "runtime_manifest": result.manifest, "skill_retrieval": retrieval}
        async with worker.SessionLocal() as session:
            task = await worker.owned_task_for_update(session, task_id)
            if not worker.owns_running_task(task):
                raise RuntimeError("打斗编排任务已停止，结果未保存")
            task.request_payload = {**task.request_payload,
                "combat_designs": {**(task.request_payload.get("combat_designs") or {}), row["shot_id"]: cached}}
            await session.commit()
        return text, cached
    raise RuntimeError("打斗编排连续三次未通过时间轴/实力关系校验：" + error)
