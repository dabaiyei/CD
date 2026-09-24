"""Project-controlled frame chaining, separate from narrative continuity."""
import re

from fastapi import HTTPException

from app.db.models import AIModel, ModelType


def validate_independent_text(text: str, field: str = "prompt") -> None:
    """Catch explicit media dependencies, not ordinary matching-action cuts."""
    for sentence in re.split(r"[。；;\n]", text):
        previous_tail = re.search(
            r"(?:上一(?:个)?(?:镜头|镜|段(?:视频)?)|前镜|前一镜|前段|上段).{0,24}(?:尾帧|末帧|末尾画面|最后一帧)"
            r"|(?:previous|preceding).{0,30}(?:end|last|final)[ -]?frame", sentence, re.I)
        if previous_tail and not re.search(r"禁止|不得|无需|不依赖|不使用|不能使用|不提供|未提供|without|do not|must not", sentence, re.I):
            raise ValueError(f"{field}：首帧模式关闭，不能依赖前镜尾帧；请改为本镜完整的站位、姿态和机位描述，保留动作与剧情")


def supports_first_frame(capabilities: dict | None) -> bool:
    caps = capabilities or {}
    images = (caps.get("reference_limits") or {}).get("image") or {}
    formats = images.get("accepted_mime_types") or ["image/png", "image/jpeg", "image/webp"]
    return bool("first_frame" in (caps.get("generation_modes") or [])
                and images.get("enabled") and int(images.get("max_count") or 0) >= 1
                and int(images.get("min_count") or 0) <= 1
                and set(formats).intersection({"image/png", "image/jpeg", "image/webp"}))


async def validate_project_first_frame(session, values) -> None:
    if not values.get("first_frame_mode"):
        return
    model = await session.get(AIModel, values.get("video_model_id")) if values.get("video_model_id") else None
    if not model or not model.enabled or model.model_type != ModelType.VIDEO or not supports_first_frame(model.capabilities):
        raise HTTPException(422, "首帧模式需要当前视频模型支持单张首帧输入及 PNG、JPEG 或 WebP 图片；请更换模型或关闭首帧模式")


def planning_contract(enabled: bool) -> str:
    common = (
        "\n项目镜头执行策略（优先于手册中的默认接续建议）："
        "人物身份和外观沿用绑定资产；逐镜明确入镜/出镜站位、朝向、左右手持物、视线、伤势、光向、"
        "180度轴线与相机路径，位置变化必须有实际动作原因，禁止人物滑移、相机无目的漂移。"
        "每镜只表现自己的新剧情节拍，不复播邻镜动作或对白，不用重复构图、定格或停顿填时长。"
    )
    if enabled:
        return common + (
            "\n项目首帧模式：开启。只有同一地点、同一时间、同一运动过程且需要匹配构图的相邻片段"
            "使用同一非空 continuity_group。平台等待前镜视频成功，再提取其真实尾帧作为本镜0秒首帧；"
            "仅这种关联允许依赖真实尾帧。转场、时间跳跃、独立新机位或硬切必须换组或留空，独立镜不等待。"
            "关联镜先承接尾帧再运镜，不能重播前镜动作。图片引用以实际 reference_map 为准，不能虚构已上传的尾帧。"
            "视频提示词快照的 tail_frame_source_order_index 为空时，本镜没有尾帧来源，必须独立描述开场。"
        )
    return common + (
        "\n项目首帧模式：关闭。所有镜头 continuity_group 必须为空字符串，不建立前镜视频依赖。"
        "每段视频必须能凭本镜完整描述、已绑定人物/场景/道具参考图独立生成；"
        "禁止要求等待、提取或使用上一个视频的尾帧作为本镜首帧，也不能仅写‘从上一镜接着来’。"
        "相邻分镜只提供叙事和空间对照，需把本镜开场的站位、视角、距离、姿态、动作阶段明确写全。"
        "完整高速攻防动作链优先安排在同一视频片段的内部切镜中；跨视频在可读的动作节点切出，"
        "用明确的新景别/视角和匹配视线、运动方向承接，不重播同一击或凭空复位。"
        "审核检查人物、空间、动作与剧情连贯，不能以‘未使用前镜尾帧’作为错误或擅自添加依赖。"
    )


def independent_video_contract(shot) -> str:
    return planning_contract(False) + (
        "\n本镜实际拍摄依据（制作约束，不显示为字幕）：\n场景与站位：" + shot.scene_description
        + "\n本镜动作与相机：" + shot.action_description
        + "\n入镜画面描述：" + shot.image_prompt
        + "\n只使用本次实际上传的参考图；资产设定图约束外观，不照搬其拼图排版、姿势或背景。"
    )
