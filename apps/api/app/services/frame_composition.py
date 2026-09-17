"""Static spatial contracts, separate from motion and identity reference sheets."""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class FrameSubject(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    asset_name: str = Field(min_length=1, max_length=160)
    screen_position: Literal["左", "中", "右"]
    depth: Literal["前景", "中景", "后景"]
    facing: str = Field(min_length=1, max_length=240)
    gaze_target: str = Field(min_length=1, max_length=240)
    pose: str = Field(min_length=1, max_length=500)
    held_items: str = Field(default="无", max_length=240)


class FrameLayout(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    camera_position: str = Field(min_length=1, max_length=400)
    camera_height: str = Field(min_length=1, max_length=200)
    viewing_direction: str = Field(min_length=1, max_length=300)
    shot_size: str = Field(min_length=1, max_length=100)
    axis_side: str = Field(min_length=1, max_length=200)
    subjects: list[FrameSubject] = Field(min_length=1, max_length=16)
    spatial_relations: str = Field(min_length=1, max_length=800)
    environment_anchors: str = Field(min_length=1, max_length=600)
    lighting: str = Field(min_length=1, max_length=300)
    visual_style: str = Field(min_length=1, max_length=800)


FRAME_RULES = """【首帧空间设计硬约束】
分镜生成/修复时，每镜输出 frame_layout，字段结构由平台提供。
先固定一台相机的物理位置、高度、观察方向、景别和轴线一侧，再定义画面坐标；
以上只定义0秒开场机位，不代表全程固定相机；后续跟拍、弧线移动和动作匹配切点写入 action_description。
画面左/右均以观众看图为准，人物左/右手属于人物自身解剖方向，不能混用。
每个可见主体写清 asset_name、画面左中右、前中后景、身体朝向、视线目标、静态姿态和持物手。
空间关系说明双方距离、相对尺度、遮挡和接触；巨物用同一环境锚点确定尺度，不能全员等大并排。
俯视/仰视/背面/正面只选同一相机能够同时看见的状态；从人物身后拍摄时不得又要求该人物正面五官完整展示。
双人对峙必须互相朝向且视线对应，不得为了展示资产正面让两人一起面对观众。
首帧只有0秒静态状态，不写‘随后/连续挥砍/镜头环绕/命中后落地’等过程；动作链留给 action_description。
这里的静态是某一瞬间的快照，不是让人物静立等候。实战可取突刺途中、兵器接触或借力反击瞬间，
描述该瞬间重心、接触点与运动方向；不得为了方便构图把已开始的交锋重置成双方武器垂地站立。
asset_names 是参考素材清单，不代表每张参考图都新增一个人物；主资产、衍生外观和招式图可属于同一主体。
身份图只参考外观，禁止复制其四视图、正面展示姿势、白底和居中构图；招式图只参考特效身份，不提前施放。
image_prompt 与 frame_layout 必须一致，实际生成以 frame_layout 的空间、相机和静态时刻为准。
审核逐项检查机位能否看到指定面、双方是否对视、左右手/画面方向是否混淆、首帧是否提前出现结束状态；
发现问题按major定位并修复。尾帧是实际视频结果，接续时不允许重新设计其站位、姿势或观察角度。
"""


def build_frame_prompt(shot, assets, raw_layout=None):
    names = {a.name for a in assets}
    if raw_layout:
        layout = FrameLayout.model_validate(raw_layout)
        unknown = {s.asset_name for s in layout.subjects} - names
        if unknown:
            raise RuntimeError("首帧站位引用了未绑定资产：" + "、".join(sorted(unknown)))
        prompt = "生成单幅0秒静态首帧，严格执行以下空间设计，不画后续动作。\n"
        prompt += f"相机位置：{layout.camera_position}；高度：{layout.camera_height}；观察方向：{layout.viewing_direction}；景别：{layout.shot_size}；轴线侧：{layout.axis_side}。\n"
        for subject in layout.subjects:
            prompt += (f"可见主体 {subject.asset_name}：画面{subject.screen_position}侧/{subject.depth}；"
                f"身体朝向：{subject.facing}；视线：{subject.gaze_target}；静态姿态：{subject.pose}；持物：{subject.held_items}。\n")
        prompt += (f"距离、尺度与遮挡：{layout.spatial_relations}\n环境锚点：{layout.environment_anchors}\n"
            f"光线：{layout.lighting}\n画风：{layout.visual_style}\n")
    else:
        if not shot.image_prompt.strip():
            raise RuntimeError("镜头缺少首帧图片提示词")
        prompt = "单幅0秒静态镜头，以下为首帧设计：\n" + shot.image_prompt.strip()
    prompt += ("\n空间规则：左右以观众画面为准；持物左右手以人物自身为准。保持单一相机位置与观察方向，"
        "人物的朝向、视线、脚下支撑与遮挡须物理一致。对峙主体朝向彼此，不能都朝镜头摆拍。"
        "仅画动作开始状态，不画多个时刻，不提前命中/落地，不画相机运动轨迹。\n"
        "参考图只约束身份、服装、物件形制和场景外观，不约束本镜站位、姿态、视角或背景构图。"
        "禁止复制资产四视图、拼贴、白底设定板、文字与水印。招式参考不能导致起始画面提前出现终结效果。\n参考图对应关系：\n")
    by_id = {a.id: a for a in assets}
    for index, asset in enumerate(assets, 1):
        parent = by_id.get(asset.parent_asset_id)
        if (asset.asset_metadata or {}).get("combat_technique"):
            role = f"招式外观参考，属于{parent.name if parent else '所属人物'}，不是额外人物，不复制施放姿态"
        elif asset.parent_asset_id:
            role = f"{parent.name if parent else '所属主资产'}的衍生外观，是同一主体而非另一个人物"
        else:
            role = f"{asset.asset_type.value}外观参考，不复制参考图机位与姿态"
        prompt += f"图{index}：{asset.name}；{role}。\n"
    return prompt
