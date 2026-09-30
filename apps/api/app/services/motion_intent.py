"""Current shot intent takes precedence over generic cinematic movement."""
import re

NATURAL_MOTION_RULES = (
    "【通用动作连续性】所有场景按原文动作意图表现自然、连贯、有重量的运动，流畅不等于加速。"
    "对已有动作保留起势、重心转移、执行、接触或落位、收势，上一动作结束姿态自然衔接下一动作，"
    "不跳姿势、不瞬移、不循环复位，不在每个动作间僵硬停顿；不得因此新增剧情动作。"
    "涉及转身、坐下、起身、弯腰时，视线、头肩、躯干、骨盆与支撑脚协调，重心落在合理支撑范围。"
    "涉及拿取、放下、递接或开门时，手先准确接触目标，再施力带动物体，释放后物体遵循支撑和惯性，"
    "避免隔空吸附、穿模、物体瞬现及左右手无故交换。"
    "已有行走或跑动按对应速度等级、步幅与步频表现，起步、加速、减速、停步连续，"
    "足部接地与重心交替一致，不滑步；不同人物与不同阶段不统一为同一速度。"
    "已有交谈按各人物语速、停顿和情绪安排表演，口型与台词同步，眼神与手势呼应语义而非机械重复；"
    "静音不新增声音，旁白或心声不要求出镜人物张嘴；边走边说可同时进行，顺序动作才累加时间。"
    "摄影机运动独立于人物运动，跟拍平稳保持主体尺度和空间方向，切镜承接原动作进度与物体位置，"
    "不重复上一段已完成的动作，也不省略关键接触造成动作跳跃。"
    "原文明示静止、停住、定格、固定机位、慢动作或风格化动作时以原文为准，"
    "不得添加身体晃动、口型、运镜或环境运动填充时间；只优化允许运动的对象与时间段。"
    "不得改变人物身份、造型、画风、原台词、模型时长与剧情结果。"
    "【通用动作连续性结束】"
)


def apply_motion_guidance(prompt: str) -> str:
    """Refresh a bounded common contract without disturbing the combat prefix."""
    prompt = re.sub(r"【通用动作连续性】.*?【通用动作连续性结束】", "", prompt, flags=re.S)
    prefix = re.match(r"^ACT视角[，,\s]*", prompt, re.I)
    if prefix:
        return prompt[:prefix.end()] + NATURAL_MOTION_RULES + prompt[prefix.end():]
    return NATURAL_MOTION_RULES + prompt

MOTION_RULES = """
当前镜头的动作与静止意图优先于通用打斗、电影感、手册及连续运动建议。
分别判断主体动作、摄影机运动和环境运动，禁止互相混淆。固定机位不是人物静止；
人物不动也不是镜头必须不动。明确要求静止、定格、停住时，不得添加摇摆、走动、
转身、推拉摇移或呼吸式镜头漂移来填满时长。台词允许必要口型，除非明确全画面定格。
前镜尾帧用于空间和身份连续；本镜要求停下时应停下，不能以惯性为由追加动作。
不自动生成独立打斗首帧。资产和招式图只参考身份、造型与招式特征；
仅真实首帧/前镜尾帧约束开场构图，不得把人物资产图当作整幅开场画面。
"""


def motion_contract(action: str, scene: str = "") -> str:
    # Preserve the actual instructions verbatim, including time ranges and exceptions.
    clauses = re.split(r"[。；;\n]", (action or "") + "\n" + (scene or ""))
    markers = ("不动", "不移动", "保持原位", "静止", "固定", "定格", "停住", "静态", "锁定机位",
               "locked", "static", "stationary", "motionless")
    locks = [clause.strip() for clause in clauses
             if any(word in clause.lower() for word in markers)]
    if not locks:
        return ""
    return ("\n本镜明确的运动限制（优先于通用动感与连续运动建议，保留其时间范围和例外）："
            + "；".join(locks)
            + "。只对上述指定对象执行限制；固定摄影机不得额外推拉摇移或漂移，"
              "主体静止不得擅自走动转身；未受限制的对象仍按分镜表演，不添加无关动作。")
