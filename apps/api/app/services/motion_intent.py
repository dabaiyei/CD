"""Current shot intent takes precedence over generic cinematic movement."""
import re

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
