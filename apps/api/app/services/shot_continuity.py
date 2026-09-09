"""Shared editing constraints; neighboring shots are context, never extra shot content."""

CONTINUITY_RULES = """相邻镜头衔接规范（制作说明，不得作为字幕或朗读）：
1. 每镜 action_description 明确入镜状态、动作推进、出镜状态、与下一镜的剪辑衔接。
2. 同一场连续动作，下一镜从上一镜出镜瞬间继续，不擅自跳过伸手、接触、取物、转身等中间阶段，
也不从准备姿势重复表演。匹配左右手、持物、身体朝向、视线、人物位置、运动方向和道具状态。
3. image_prompt 对应入镜状态，不能把本镜结束后的动作结果提前画进首帧。
4. 动作接切须写明切点，如手指刚碰到杯柄时切近景，下一镜从同一接触状态开始。
保持180度轴线和视线匹配；换景别要有明确目的，避免近似构图的无意跳切。
5. 明确以哪个稳定画面结束、下一镜从何处开始；不得自行添加淡入淡出、黑场、空白尾帧或时间省略。
不要用冻结、重复动作或额外几秒停顿填满时长；根据叙事和模型合法时长重新分配动作节奏。
6. 只有剧本要求转场或时间跳跃时才允许不连续，需明确转场动机、时间关系与视觉/声音桥梁。
7. 邻镜快照用于核对边界，不得把前后镜的全部动作、对白复制到当前镜；当前镜头仅覆盖自己的节拍。
审核与修复逐对核对前镜结束和后镜开始，发现漏动作、重复动作、瞬移或持物变化时明确指出镜号并修正。
"""


def neighboring_shots(rows) -> dict:
    ordered = sorted(rows, key=lambda row: (row.order_index, row.id))

    def brief(row):
        return {
            "shot_order_index": row.order_index,
            "title": row.title,
            "scene_description": row.scene_description[:300],
            "action_description": (
                row.action_description
                if len(row.action_description) <= 1200
                else row.action_description[:390] + "\n…中段省略…\n" + row.action_description[-790:]
            ),
        }

    return {
        row.id: {
            "previous": brief(ordered[index - 1]) if index else None,
            "next": brief(ordered[index + 1]) if index + 1 < len(ordered) else None,
        }
        for index, row in enumerate(ordered)
    }
