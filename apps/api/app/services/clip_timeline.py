"""Editorial cuts inside one model-generated video clip use clip-local seconds."""
from pydantic import BaseModel, Field, model_validator

CLIP_RULES = (
    "\n平台视频片段编排规则：数据中的一条shot是一段提交给视频模型的完整视频，"
    "可包含多个剪辑分镜。模型时长只约束整段视频，内部镜头可短至1–2秒。"
    "internal_shots是权威内部时间轴，秒数从本片段0秒起算；保留每段事件和切镜，"
    "不得为满足模型下限延长内部短镜头，不得让通用一镜到底模板覆盖原文切镜。"
    "明确要求一镜到底时仍保持一镜到底。首帧只描绘片段开头；不要拼贴多幅镜头。"
)


class InternalShot(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    source_start_seconds: float = Field(ge=0)
    source_end_seconds: float = Field(gt=0)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_seconds <= self.start_seconds or self.source_end_seconds <= self.source_start_seconds:
            raise ValueError("片段内部时间段必须从早到晚")
        return self


def video_timeline_instruction(rows: list[dict]) -> str:
    if not rows:
        return ""
    parts = [InternalShot.model_validate(row) for row in rows]
    lines = [f"{s.start_seconds:g}–{s.end_seconds:g}秒：{s.description}" for s in parts]
    return ("\n视频片段内的分镜编排（以下秒数从本次生成视频的0秒起算）：\n"
        + "\n".join(lines)
        + "\n这些是同一条视频内部的镜头与事件，不是分别提交的视频任务。"
          "按上述时刻完成景别、机位、动作和对白的衔接，可在片段内切镜；"
          "不得因视频模型最短时长而拉长其中的短镜头；原文有切镜时不得合并成单一机位，"
          "原文要求一镜到底时不擅自切镜。"
          "重叠区间代表同时发生的画面/对白，只在原文要求时切镜，不叠加计算时长。"
          "保持角色身份、动作方向和空间关系连贯。时间数字与编排标签只是导演指令，"
          "不得显示为字幕或念出；人物台词维持原文语言，发声与否遵循本任务的音频开关。")
