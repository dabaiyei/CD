"""把“左一右三”人物设定图排版同步到用户自建/AI 生成的画风手册。

内置手册由 `upgrade_visual_handbook_persona_sheet.py` 处理；本脚本覆盖不在内置包里的
自定义手册，因为它们不在 `resources/visual-handbooks/expanded-2026-09` 的目录白名单内。

安全策略与内置脚本一致：精确原文替换，命中数不足即中止，写入前备份、写入后回读核验，
异常时用备份回滚。默认 dry-run，需显式 `--apply` 才写入。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from sqlalchemy import select

from app.db.models import Handbook, HandbookType
from app.db.session import SessionLocal
from app.services.managed_skills import (
    handbook_usage_instructions,
    read_handbook_files,
    replace_handbook_files,
    validate_handbook_files,
)

TARGET_FILES = ("character.md", "character-derivative.md")

# 结构完全一致的“表格 + 提示词模板”系列（2D 扁平风 / 90 年代日式动画 / 国风二次元新国潮 等），
# 两套文件都按同一口径改写。
TABLE_STYLE_NAMES = ("90年代日式动画风格", "国风二次元新国潮风格", "2D扁平风")

# 这些手册结构相近，除各自的专属规则外，再叠加通用规则兜底（表格行、提示词模板、规则表）。
COMMON_FALLBACK_NAMES = (*TABLE_STYLE_NAMES, "清透诗性青春动画风格", "澄光绘境")

# 迁移完成后才会出现的表述；据此把脚本做成可重复执行（已迁移则安全跳过）。
UPGRADED_MARKERS = ("左一右三",)

COMMON_REPLACEMENTS: list[tuple[str, str]] = [
    (
        "### 视图定义\n\n| 位置 | 视图 | 角度 | 景别 | 要求 | 提示词 |",
        "### 视图定义\n\n"
        "> 人物设定图按“左一右三”排布：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面；"
        "四视图画在同一张完整画面里，不是照片拼接或分格拼板。\n\n"
        "| 位置 | 视图 | 角度 | 景别 | 要求 | 提示词 |",
    ),
    ("| 左一 | 人像特写 | 正面平视 | 头顶至锁骨 |", "| 左一 | 人像特写 | 正面平视 | 头顶至胸部下方 |"),
    ("| 左一 | 人像特写 | 正面平视 | 面部至锁骨 |", "| 左一 | 人像特写 | 正面平视 | 头顶至胸部下方 |"),
    ("| 左一 | 面部特写 | 头顶至锁骨 |", "| 左一 | 面部特写 | 头顶至胸部下方 |"),
    ("从头顶到锁骨完整展示，面部占60%+", "从头顶到胸部下方完整展示，面部占60%+"),
    ("从头顶到锁骨完整展示, 面部占60%+", "从头顶到胸部下方完整展示, 面部占60%+"),
    ("从头顶到锁骨完整展示不裁切", "从头顶到胸部下方完整展示不裁切"),
    (
        "人像特写从头顶到锁骨完整展示, 不裁切头顶, head to collarbone complete,",
        "左侧上半身正面特写从头顶到胸部下方完整展示, 不裁切头顶, head to chest complete,",
    ),
    ("面部至锁骨", "头顶至胸部下方"),
    ("| 左二 | 正视图 |", "| 右一 | 正视图 |"),
    ("| 右一 | 后视图 |", "| 右三 | 后视图 |"),
    (
        "| 布局 | 同一画面从左至右并排四视图 |",
        "| 布局 | 一张完整画面按左一右三排布：左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面 |",
    ),
    (
        "| 画面比例 | 建议 4:1 或 3:1 |",
        "| 画面比例 | 保持项目已选比例；左一右三为宽幅横构图，不使用 4:1 超宽条幅 |",
    ),
    (
        "| 特写展示 | 人像特写必须从头顶到锁骨完整入画，严禁裁切头顶，头发、额头、下巴均需完整 |",
        "| 特写展示 | 上半身正面特写必须从头顶到胸部下方完整入画，严禁裁切头顶，头发、额头、下巴均需完整 |",
    ),
    (
        "| 特写展示 | 从头顶到锁骨完整入画，严禁裁切头顶 |",
        "| 特写展示 | 从头顶到胸部下方完整入画，严禁裁切头顶 |",
    ),
    (
        "| 特写展示 | 人像特写必须从头顶到锁骨完整入画，严禁裁切 |",
        "| 特写展示 | 上半身正面特写必须从头顶到胸部下方完整入画，严禁裁切 |",
    ),
    (
        "同一画面左至右并排：人像特写+正视图+侧视图+后视图",
        "一张完整画面，左一右三排布：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面；"
        "四视图画在同一张连续画面里，不是照片拼接、分格拼板或贴纸排布，共用同一套光线与背景",
    ),
    ("画面从左至右并排四视图", "一张完整画面按左一右三排布"),
    ("画面从左到右并排四视图", "一张完整画面按左一右三排布"),
    ("同一画面从左至右并排四视图", "一张完整画面按左一右三排布"),
    (
        "同一画面从左到右并排面部特写、正面全身、右侧全身、背面全身",
        "一张完整画面按左一右三排布：左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面",
    ),
    ("面部特写从头顶到锁骨完整", "左侧上半身正面特写从头顶到胸部下方完整"),
    ("| R8 | 人像特写必须从头顶到锁骨完整展示，严禁裁切头顶 |",
     "| R8 | 左侧上半身正面特写必须从头顶到胸部下方完整展示，严禁裁切头顶 |"),
    ("| X7 | 人像特写裁切头顶，必须从头顶到锁骨完整入画 |",
     "| X7 | 左侧特写裁切头顶，必须从头顶到胸部下方完整入画 |"),
    # 背景统一为米灰棚拍（三套浅色背景的自定义手册）
    ("| 背景 | 纯净中性灰 #E8E8E8 |", "| 背景 | 米灰色柔光摄影棚背景，干净均匀无场景元素 |"),
    ("| 背景 | 暖调米白 #F8F4E8 |", "| 背景 | 米灰色柔光摄影棚背景，干净均匀无场景元素 |"),
    ("| 背景 | 月白纯色 #E8EAF5 |", "| 背景 | 米灰色柔光摄影棚背景，干净均匀无场景元素 |"),
    ("自然站立，纯净中性灰背景，无光影，无渐变，", "自然站立，米灰色柔光摄影棚背景，无光影，无渐变，"),
    ("自然站立，纯净中性灰背景，柔和电影光，无硬阴影，", "自然站立，米灰色柔光摄影棚背景，柔和电影光，无硬阴影，"),
    ("自然站立, 月白纯色背景, 均匀柔光, 无硬阴影,", "自然站立, 米灰色柔光摄影棚背景, 均匀柔光, 无硬阴影,"),
    ("必须指定「纯净中性灰背景」", "必须指定「米灰色柔光摄影棚背景」"),
    ("必须指定「暖调米白背景 #F8F4E8」", "必须指定「米灰色柔光摄影棚背景」"),
    ("必须指定「月白纯色背景」", "必须指定「米灰色柔光摄影棚背景」"),
    ("复杂场景背景（必须纯灰底）", "复杂场景背景（必须米灰色柔光棚拍底）"),
    ("复杂场景背景（必须暖色调背景）", "复杂场景背景（必须米灰色柔光棚拍底）"),
    ("复杂场景背景（必须纯色）", "复杂场景背景（必须米灰色柔光棚拍底）"),
    ("**四视图一致** — 轮廓/体型/发型/基础服装跨视图高度统一",
     "**四视图一致（左一右三）** — 轮廓/体型/发型/基础服装跨视图高度统一"),
    ("3. **四视图一致** — 面容/体型/发型/基础服装跨视图高度统一",
     "3. **四视图一致（左一右三）** — 面容/体型/发型/基础服装跨视图高度统一"),
    # 兜底：把任何残留的“锁骨”特写口径统一成“胸部下方”（含半角逗号与英文变量）
    ("人像特写从头顶到锁骨完整展示，不裁切头顶，head to collarbone complete，",
     "左侧上半身正面特写从头顶到胸部下方完整展示，不裁切头顶，head to chest complete，"),
    ("人像特写从头顶到锁骨完整展示，head to collarbone complete，",
     "左侧上半身正面特写从头顶到胸部下方完整展示，head to chest complete，"),
    ("人像特写从头顶到锁骨完整展示，", "左侧上半身正面特写从头顶到胸部下方完整展示，"),
    ("人像特写从头顶到锁骨完整展示,", "左侧上半身正面特写从头顶到胸部下方完整展示,"),
    ("head to collarbone complete", "head to chest complete"),
]

PER_HANDBOOK_REPLACEMENTS: dict[str, list[tuple[str, str]]] = {
    "清透诗性青春动画风格": [
        (
            "画面从左至右并排四视图，背景固定珍珠纸白 `#F3F0EA`，角色脚底位于同一基线。"
            "使用柔和前侧光，禁止环境场景、道具、文字、身高刻度和标签。建议画幅 3:1 或 4:1。",
            "一张完整画面按“左一右三”排布：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面；"
            "角色脚底位于同一基线，四视图画在同一张连续画面里，不是照片拼接或分格拼板。"
            "使用柔和前侧光与米灰色柔光摄影棚背景，禁止环境场景、道具、文字、身高刻度和标签。"
            "宽幅横构图，不使用 3:1 或 4:1 超宽条幅。",
        ),
        ("珍珠纸白#F3F0EA纯色背景", "米灰色柔光摄影棚背景"),
        ("背景固定珍珠纸白 `#F3F0EA`", "背景固定米灰色柔光摄影棚背景"),
        ("背景固定 `#F3F0EA`", "背景固定米灰色柔光摄影棚背景"),
        ("必须使用珍珠纸白背景", "必须使用米灰色柔光摄影棚背景"),
        (
            "沿用人物基础四视图：面部特写、正面全身、右侧全身、背面全身。",
            "沿用人物基础四视图的“左一右三”布局：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面。",
        ),
    ],
    "澄光绘境": [
        (
            "保持原模板的四视图职责：从左到右为人像特写、正面全身、右侧全身、背面全身。"
            "人像从头顶到锁骨完整，三个全身视图从头顶到脚底完整，共享地面基线与相同人物比例。",
            "人物设定图按“左一右三”排布：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面三个视图。"
            "特写从头顶到胸部下方完整，三个全身视图从头顶到脚底完整，共享脚底基线与相同人物比例；"
            "四个视图画在同一张连续画面里，不是照片拼接或分格拼板。",
        ),
        ("纸雪白 `#F1F4F3` 纯色背景，均匀中性柔光", "米灰色柔光摄影棚背景，均匀中性柔光"),
        (
            "同一人物从左至右依次展示头顶至锁骨人像特写、正面全身、右侧全身、背面全身；"
            "三幅全身像共享比例与地面基线，头发与双脚完整入画。",
            "同一人物按“左一右三”排布：左侧为头顶至胸部下方的上半身正面特写，右侧并列全身正面、全身侧面、全身背面；"
            "三幅全身像共享比例与脚底基线，头发与双脚完整入画；四视图画在同一张连续画面里，不是照片拼接或分格拼板。",
        ),
        ("自然站姿，纸雪白纯色背景", "自然站姿，米灰色柔光摄影棚背景"),
        (
            "沿用人物基础图的特写、正面、右侧、背面四视图，保持身体姿态、视图比例、背景和中性照明。",
            "沿用人物基础图的“左一右三”四视图布局（左侧上半身正面特写，右侧全身正面、全身侧面、全身背面），"
            "保持身体姿态、视图比例、背景和中性照明。",
        ),
        (
            "从左至右为人像特写、正面全身、右侧全身、背面全身；三个全身像共享比例与地面基线",
            "左一右三排布：左侧为上半身正面特写，右侧并列全身正面、全身侧面、全身背面；三个全身像共享比例与脚底基线",
        ),
        ("纸雪白纯色背景，中性均匀柔光", "米灰色柔光摄影棚背景，中性均匀柔光"),
    ],
    "精致暗调汉服古风人像": [
        (
            "为确保人物基础形象在正面、侧面、背面、三分之四视角的生成中不变成四个不同人物",
            "为确保人物基础形象在同一张设定图里按“左一右三”排布（左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面）"
            "时不变成四个不同人物",
        ),
        (
            "4. **统一光线与背景**：所有视图必须处于同一低饱和暗调背景（深绿/深红/黑）中，主光集中在面部与上半身，"
            "边缘光勾勒轮廓，背景保持重度景深虚化。",
            "4. **统一光线与背景**：四个视图必须处于同一低饱和暗调棚拍背景（深绿/深红/黑）中，背景干净、没有人为场景元素，"
            "主光集中在面部与上半身，边缘光勾勒轮廓，背景保持重度景深虚化；四个视图画在同一张连续画面里，"
            "不是照片拼接或分格拼板。",
        ),
        (
            "低饱和暗调背景，浅景深虚化背景花朵，戏剧性主光聚焦面部",
            "同一张设定图左一右三排布：左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面，"
            "四视图画在同一张连续画面里，不是照片拼接或分格拼板；低饱和暗调棚拍背景，浅景深虚化背景花朵，戏剧性主光聚焦面部",
        ),
        (
            "5. 四视图一致性校验：对同一角色的变化衍生进行设计时，正面、侧面、背面、三分之四视角的设定必须以该角色为基础",
            "5. 四视图一致性校验：对同一角色的变化衍生进行设计时，必须按“左一右三”排布在同一张完整画面里"
            "（左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面），以该角色为基础",
        ),
        (
            "- 妆容与光效调节：`半写实数字平滑质感，妆容随情绪演变",
            "- 妆容与光效调节：`同一张画面左一右三排布（左特写＋右正侧背），半写实数字平滑质感，妆容随情绪演变",
        ),
    ],
    "写实人像摄影风格 · 柔雾灰调与高反差哥特": [
        (
            "衍生必须满足一条硬性底线：**同一人物的正面、侧面、背面、三分之四视角四视图必须看起来是同一个人**",
            "衍生必须满足一条硬性底线：**同一人物在同一张设定图里按“左一右三”排布的四视图必须看起来是同一个人**",
        ),
        (
            "人物衍生（含服装 / 状态 / 时期变化）必须输出**同一人物的正面、侧面、背面、三分之四视角四视图**：",
            "人物衍生（含服装 / 状态 / 时期变化）必须在一张完整画面里输出**按“左一右三”排布的同一人物四视图**"
            "（左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面）：",
        ),
        (
            "- **同一人物**：四视图的眉眼距离、鼻翼宽度、唇峰位置、下巴长度必须一致；四张图并排时应被识别为一个人。",
            "- **同一人物**：四视图的眉眼距离、鼻翼宽度、唇峰位置、下巴长度必须一致；四个视图画在同一张连续画面里，"
            "应被识别为一个人，不得出现照片拼接、分格拼板或贴纸排布。",
        ),
        (
            "同一人物四视图：正面、侧面、背面、三分之四视角。",
            "同一人物四视图，一张完整画面“左一右三”排布：左侧上半身正面特写，右侧并列全身正面、全身侧面、全身背面。",
        ),
        (
            "- [ ] 四视图（正、侧、背、三分之四）齐全，且是**同一人物的同一款式服装**。",
            "- [ ] 四视图按“左一右三”排布齐全（左侧特写＋右侧正、侧、背），画在同一张连续画面里，且是**同一人物的同一款式服装**。",
        ),
    ],
}

# 写给缺少“人物设定图”章节的手册（写实人像摄影风格），插在“## 可复用提示词片段”之前。
INSERT_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "写实人像摄影风格 · 柔雾灰调与高反差哥特": [
        (
            "## 可复用提示词片段",
            "## 人物设定图（左一右三）\n\n"
            "- 默认输出**一张**同一角色的设定图，不是四张独立图片、也不是四个独立生图任务。\n"
            "- 画面左侧为上半身正面特写，从头顶到胸部下方完整，清楚呈现面部比例、妆容、发丝走向与领口结构。\n"
            "- 画面右侧并列全身正面、全身侧面、全身背面三个视图，服装、发型、配饰完全一致，完整站立、全身入镜、比例协调，共用一条脚底基线。\n"
            "- 四个视图画在同一张连续画面里，共用同一套光线与背景，不是照片拼接、分格拼板或贴纸排布。\n"
            "- 背景沿用本画风的米灰色柔光摄影棚背景，干净均匀、没有场景元素，保持柔雾散射光与低到中低反差。\n\n"
            "## 可复用提示词片段",
        ),
    ],
}


def apply_replacements(
    content: str,
    replacements: list[tuple[str, str]],
    *,
    required_anchors: tuple[str, ...] = (),
    already_upgraded_markers: tuple[str, ...] = (),
    handbook_label: str = "",
) -> tuple[str, list[str]]:
    hits: list[str] = []
    missing = [anchor for anchor in required_anchors if anchor not in content]
    if missing:
        raise RuntimeError(
            "缺少必需锚点，原文可能已被编辑或不属于本批次，需人工合并："
            + "；".join(anchor[:40] for anchor in missing)
        )
    updated = content
    for old, new in replacements:
        if old not in updated:
            continue
        if old in new and new in updated:
            # 插入型规则：新段落里包含原锚点，已插入过则跳过，保证可重复执行。
            continue
        count = updated.count(old)
        updated = updated.replace(old, new)
        hits.append(f"{old[:28]}…×{count}")
    if not hits:
        if any(marker in content for marker in already_upgraded_markers):
            # 已迁移过：可安全重复执行，不再报错。
            return content, []
        raise RuntimeError(
            f"没有任何替换命中且不含已迁移标记，原文可能已被编辑，需人工合并：{handbook_label}"
        )
    return updated, hits


def plan_handbook(handbook: Handbook, current: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Return the updated file map plus a human-readable report of applied replacements."""
    updated = dict(current)
    report: list[str] = []

    rules = [
        *PER_HANDBOOK_REPLACEMENTS.get(handbook.name, []),
        *INSERT_SECTIONS.get(handbook.name, []),
    ]
    if handbook.name in COMMON_FALLBACK_NAMES:
        rules.extend(COMMON_REPLACEMENTS)
    if not rules:
        raise RuntimeError(f"没有为该手册登记替换规则：{handbook.name}")

    for filename in TARGET_FILES:
        content = updated.get(filename)
        if content is None:
            continue
        content, hits = apply_replacements(
            content,
            rules,
            already_upgraded_markers=UPGRADED_MARKERS,
            handbook_label=f"{handbook.name}/{filename}",
        )
        updated[filename] = content
        report.extend(f"{filename} :: {hit}" for hit in hits)
    return updated, report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--handbook-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    pending: list[tuple[Handbook, dict[str, str], dict[str, str]]] = []
    async with SessionLocal() as session:
        statement = select(Handbook).where(
            Handbook.tenant_id == args.tenant_id,
            Handbook.handbook_type == HandbookType.VISUAL,
        )
        if args.handbook_id:
            statement = statement.where(Handbook.id.in_(args.handbook_id))
        handbooks = list(await session.scalars(statement))

        handled = set(PER_HANDBOOK_REPLACEMENTS) | set(INSERT_SECTIONS) | set(TABLE_STYLE_NAMES)
        for handbook in sorted(handbooks, key=lambda item: item.name):
            if handbook.name not in handled:
                continue
            current = {
                item["filename"]: item["content"].strip()
                for item in read_handbook_files(handbook)
            }
            updated, report = plan_handbook(handbook, current)
            validate_handbook_files(HandbookType.VISUAL, updated)
            route = handbook_usage_instructions([handbook], prompt_code="asset-prompt-generation")
            assert "character.md" in route and "character-derivative.md" in route
            if updated == current:
                print(f"已是最新，跳过：{handbook.name}")
                continue
            pending.append((handbook, current, updated))
            print(f"[待更新] {handbook.name} v{handbook.version}（命中 {len(report)} 处）")
            for line in report:
                print(f"    {line}")

        print(f"待更新 {len(pending)} 套")
        if not args.apply or not pending:
            return

        backup = ROOT / "output/handbook-backups" / datetime.now().strftime("persona-sheet-custom-%Y%m%d-%H%M%S-%f")
        backup.mkdir(parents=True)
        for handbook, current, _ in pending:
            (backup / f"{handbook.id}.json").write_text(
                json.dumps(
                    {
                        "name": handbook.name,
                        "version": handbook.version,
                        "skill_path": handbook.skill_path,
                        "files": current,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        try:
            for handbook, _, updated in pending:
                replace_handbook_files(handbook, updated)
                handbook.version += 1
                actual = {
                    item["filename"]: item["content"].strip()
                    for item in read_handbook_files(handbook)
                }
                assert actual == updated, f"回读核验失败：{handbook.name}"
                print(f"已更新并回读核验：{handbook.name} v{handbook.version}")
            await session.commit()
        except Exception:
            await session.rollback()
            for path in backup.glob("*.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                replace_handbook_files(
                    SimpleNamespace(
                        handbook_type=HandbookType.VISUAL,
                        skill_path=saved["skill_path"],
                    ),
                    saved["files"],
                )
            raise
        print(f"备份：{backup}")


if __name__ == "__main__":
    asyncio.run(main())
