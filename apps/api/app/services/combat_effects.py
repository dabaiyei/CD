"""Action-grounded, layered VFX; only relevant effect vocabulary enters the prompt."""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CombatEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    layer: Literal["primary", "particles", "volume", "debris", "optical"]
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    emitter: str = Field(min_length=1, max_length=400)
    appearance: str = Field(min_length=1, max_length=600)
    motion: str = Field(min_length=1, max_length=600)
    decay: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def interval(self):
        if self.end <= self.start:
            raise ValueError("特效消散时间必须晚于发生时间")
        return self


RENDERERS = {
    "UE5": "Unreal Engine 5 cinematic game CG, Nanite high-detail geometry, Lumen dynamic global illumination, Niagara dense layered particle bursts, ray tracing, PBR, HDR highlights, realistic reflections, depth of field, controlled motion blur, crisp 8K-style detail (not an output resolution setting)",
    "Octane": "Octane Render, physically based rendering, spectral lighting",
    "V-Ray": "V-Ray, physically based materials, volumetric lighting",
    "Eevee": "Blender Eevee, stylized shading, graphic impact FX",
    "Redshift": "Redshift, layered particle effects, volumetric rendering",
}

EFFECT_RULES = """
特效按主光效primary、细粒子particles、烟雾/流体volume、受力碎片debris、光学辅助optical分层。
并非所有特效都是粒子：光带是沿运动轨迹展开的带状光效；冲击波是压力波前，空气扭曲是折射；
烟团/体积雾是连续体积结构。可裹挟粒子，但不能把所有层写成发光小点。
beats[].effects逐项写绝对镜内start/end、emitter具体释放点/碰撞位置、appearance颜色形状尺度、
motion方向速度与受力、decay寿命和消散。仅选择当前动作真实需要的层，不强制每击凑齐五层。
明确发生碰撞或法术释放的节拍至少设计一种适合的特效；通常每拍1—3个关键层即可，
没有特效的过渡/静止节拍effects为空。各字段用具体短语，不能用长篇空泛形容词挤占动作预算。
普通攻防用短促火星和贴刃拖尾，烟尘/碎片仅在有接触、破坏或明确气流时出现。
火星命中才迸发，光带绑定挥砍轨迹，碎片有重力和惯性；过镜残余烟尘与碎片保持连续。
冲击波由命中点向外传播一次，不循环爆炸；慢镜大招显示层次，普通交锋保持高速，特效不替代攻防动作。
动态模糊/残影匹配速度与曝光，不克隆人物；短促闪光与震颤绑定冲击，不持续频闪或用抖动藏招。
体积光须有光源和介质，镜头光晕须有对应亮源与机位。控制遮挡，人物轮廓、兵器接触与攻防结果可读。
已有招式的颜色、纹样、武器、召唤物身份不可随特效变换。明确静止段不新增冲击/火花/爆炸。
renderer只选与当前画风及用户要求相符的一套：游戏CG可UE5，通透玄幻可Octane，真人可V-Ray，
卡通可Eevee，工业多层体积特效可Redshift。手绘2D/赛璐璐或画风不明确时留空，不强加3D材质；
不得叠加多个渲染器。已有手册指定优先，渲染词只是风格描述，不代表实际调用这些引擎。
"""

CATALOG = [
    (r"刀|剑|拳|脚|兵刃|格挡|sword|blade|punch|parry", "近战：橙白高温金属火星从兵刃接触点沿切向短促飞散并熄灭；刀剑光带贴合刃轨迹，挥完迅速收尾；拳风只在气流扰动处卷起半透明微尘。"),
    (r"地面|墙|石|破碎|撞|落地|ground|wall|impact", "环境反馈：石块碎屑从受力表面飞出、翻滚并受重力下落；灰黄扬尘先向外扩散再缓慢沉降，保留已经破坏的地形，不凭空补回。"),
    (r"灵力|法术|剑气|能量|神诀|宝术|energy|spell|magic", "能量：灵力光点沿既定纹样聚集与运行；爆发先有主光形，再有向外喷射的发光微粒、体积流和环形半透明冲击波，按先后衰减，不让强光吞掉形状。"),
    (r"雷|电弧|闪电|lightning|electric", "雷电：分叉细丝沿明确路径脉冲传播，接触点短促电流碎屑飞溅，保留主电弧走向而非全屏随机闪烁。"),
    (r"火焰|烈焰|燃烧|fire|flame", "火焰：热核、上升翻滚火舌、卷动烟体与灰烬余烬分层，余烬随热流上升后冷却，照亮邻近表面。"),
    (r"冰|霜|雪|ice|frost", "冰霜：透明冰屑与六角晶片有清楚棱边和折射，撞击时从破裂处飞出，避免写成不分材质的白色粉尘。"),
]


def effect_context(row: dict) -> str:
    query = json.dumps({k: row.get(k) for k in ("action_description", "combat_plan", "internal_shots")}, ensure_ascii=False)
    return EFFECT_RULES + '\n' + '\n'.join(text for pattern, text in CATALOG if re.search(pattern, query, re.I))


def compile_effect(effect: CombatEffect, zh: bool) -> str:
    labels = {"primary": "主光效", "particles": "粒子", "volume": "烟尘流体",
              "debris": "冲击碎片", "optical": "光学辅助"}
    label = labels[effect.layer] if zh else effect.layer
    return (f"{effect.start:g}–{effect.end:g}s [{label}]: "
            f"{effect.emitter}; {effect.appearance}; {effect.motion}; {effect.decay}")
