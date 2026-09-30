"""Bounded, shared motion direction for home prompt authoring, not raw prompt rewriting."""

import re

from app.services.cinematography import VIDEO_RULES as CAMERA_RULES
from app.services.cinematography import scene_guidance as camera_scene_guidance
from app.services.creation_context import COMBAT_ACTION_QUALITY, COMBAT_PRIORITY, contains_combat
from app.services.locomotion import LOCOMOTION_PLANNING_RULES, locomotion_guidance
from app.services.motion_intent import NATURAL_MOTION_RULES
from app.services.speech_pacing import SPEECH_RULES, prompt_dialogue, speech_guidance


def personal_creation_guidance(message, *, mode="chat", recent_messages=(), selected_modules=None):
    source = message[:6000]
    # Follow-ups may rely on the latest creative request, but a new unrelated
    # question must not inherit combat rules from old conversation history.
    if re.fullmatch(r"\s*(?:继续|开始|全部开始|好了开始吧|按这个来|改一下|优化一下)[。！!\s]*", message):
        source = (
            "\n".join(str(item.get("content", ""))[:2000] for item in recent_messages[-4:]) + "\n" + source
        )
    if (
        selected_modules is None
        and mode != "video"
        and not re.search(r"视频|镜头|分镜|运镜|打斗.{0,8}提示词|动作.{0,8}提示词", source)
    ):
        return ""
    rules = [
        "【首页AI创制导演规则】以下规则用于你受托设计、优化视频提示词和连续创作方案，"
        "不是要求你执行付费生成。用户要求原样提交、只复制或不改提示词时不得改写原文。"
        "编写的实际提示词须体现具体动作、时间段、运镜、光线与人物表演，不能只列规则或口头承诺。"
        "已有完整方案确认执行时沿用原方案，不再重写。",
        NATURAL_MOTION_RULES,
        CAMERA_RULES + camera_scene_guidance(source),
        "【画面与表演】逐段交代主体位置、景别、视线、运动方向、机位路径和速度；"
        "光源方向、色温、阴影及环境反馈连续，切镜承接动作进度，不重演命中、不突然换站位。"
        "人物眼神、眉毛、嘴部和身体反应符合当前处境，一拍一个主情绪，避免木脸。"
        "静止主体、固定机位和环境运动分别判断，不为填满时长强加运动。",
        "【模型与时长】只用当前模型目录支持的时长、画幅、参考媒体与声音能力。"
        "一个生成片段可容纳多个连续子镜头，时间轴从0开始并覆盖实际片长，不能每个子镜头都当一个生成任务。"
        "有台词时按人物语速、停顿估算所需时长，走路速度不强制决定语速；"
        "时长不足则在模型合法范围内调整或提出分段，不靠吞字或擅改台词塞进去。"
        "模型不支持音频时不承诺人声；用户要求静音时不添加对白、配乐或环境音。",
    ]
    if selected_modules is not None:
        from app.services.jev_control import module_instructions

        rules.append(module_instructions(selected_modules))
    if selected_modules["combat"] != "none" if selected_modules is not None else contains_combat(source):
        rules += [
            COMBAT_PRIORITY,
            COMBAT_ACTION_QUALITY,
            "【战斗提示词落地】实际视频提示词以‘ACT视角，’开头。按实际片长设计时间段："
            "均势交锋用约1—2秒的连续攻防簇，每簇多个具体变招与应对，抢攻→格挡/闪避→借惯性变线→反击/追击；"
            "不等于每簇必须切镜，不套固定12秒八镜模板。碾压用短促决胜动作与技能后果，"
            "只有大招显形或关键重击适度慢镜、特写；普通交锋保持高速实时。"
            "每段写攻击者、目标、方向、接触点、卸力/反弹、重心与落脚点，以及下一招如何接上。"
            "高速移动时摄影机锁定交锋主体同步跟移，用背景视差表现速度；变换视角守住空间轴线和动作匹配。"
            "主光效、贴刃拖尾、碰撞火星、体积烟尘、受力碎片和冲击波按接触因果分层，写发生点、方向、消散，"
            "不靠爆炸、抖动或粒子糊屏代替动作。"
            "CG/UE5画风使用Nanite精细几何、Lumen动态光照、Niagara分层粒子、HDR、真实反射与受控动态模糊；"
            "2D、真人或已有画风不强制变成3D，8K只是细节描述，不覆盖真实输出分辨率。"
            "参考图逐人绑定身份、服装与武器，无人招式图只绑定特效与武器；无图片不捏造图片编号。"
            "写完后保留逐段攻防细节，不再概括成‘两人激烈战斗’。",
        ]
    gait = (
        locomotion_guidance(source)
        if selected_modules is None or selected_modules["locomotion"] == "yes"
        else ""
    )
    if gait:
        rules.append(gait)
    elif selected_modules is not None and selected_modules["locomotion"] == "yes":
        rules.append(LOCOMOTION_PLANNING_RULES)
    speech = (
        speech_guidance(prompt_dialogue(source), source)
        if selected_modules is None or selected_modules["speech"] == "yes"
        else ""
    )
    if speech:
        rules.append(speech)
    elif selected_modules is not None and selected_modules["speech"] == "yes":
        rules.append(SPEECH_RULES)
    return "\n\n" + "\n".join(rules)


async def controlled_guidance(tenant_id, message, *, mode="chat", recent_messages=(), checkpoint=None,
                              optional=False):
    """Optional writing guidance must not veto an already authorized text reply."""
    from app.services.jev_control import JevDecisionPending
    try:
        return await _controlled_guidance(tenant_id, message, mode=mode,
            recent_messages=recent_messages, checkpoint=checkpoint)
    except JevDecisionPending as exc:
        if not optional:
            raise
        if checkpoint is not None:
            checkpoint['optional_creation_guidance'] = {'status': 'skipped', 'reason': str(exc)}
        # Do not infer creative modules, rewrite permission or media authority.
        # The upstream text-only route remains authoritative.
        return ""


async def _controlled_guidance(tenant_id, message, *, mode="chat", recent_messages=(), checkpoint=None):
    """JEV owns activation and modules; history resolves references, never authorizes rendering."""
    from app.services.jev_control import choice, controller, modules

    control = await controller(tenant_id, checkpoint)
    if control is None:
        return personal_creation_guidance(message, mode=mode, recent_messages=recent_messages)
    # Keep routing evidence small; the creative model still receives the
    # original conversation and can retrieve older material as before.
    history = [
        {"role": item["role"], "content": item["content"][:1200], "partial": len(item["content"]) > 1200}
        for item in recent_messages[-4:]
    ]
    evidence = {"message": message, "mode": mode, "history": history}
    decision = await control.choose(
        "对话创作规则启用",
        evidence,
        {
            "authoring": choice(
                "用户现在要编写或修改视频/分镜提示词吗？只判断是否委托写作，与画面有没有战斗无关。"
                "‘写视频提示词’选yes；‘原样提交，不要修改’选no；普通聊天选no。"
                "询问报错原因、问‘什么不足’、要求本轮不使用之前上下文，都选no。"
                "当前话题优先于历史，不能因历史曾生成视频就启用。没有委托写视频提示词是no，不是unknown。"
                "‘继续优化这个方案’承接history中最近的视频写作请求，选yes。",
                {
                    "yes": "编写或修改视频提示词",
                    "no": "没有委托视频提示词写作",
                    "unknown": "指代缺失无法判断",
                },
            )
        },
    )
    if decision["authoring"] == "no":
        return ""
    selected = await modules(
        control,
        {"action_description": message},
        context={"history": history},
    )
    return personal_creation_guidance(
        message, mode=mode, recent_messages=recent_messages, selected_modules=selected
    )
