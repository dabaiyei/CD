# MiniMax H3 视频提示词专用协议

本协议只在平台确认目标模型为 MiniMax H3 时加载。主体描述和固定字段使用英文，`dialogue_locks`、歌词及画内文字仍逐字保留原语言。

## 模式映射

- `text_to_video` 使用 T2VA：没有关键帧，直接进入三个核心字段。
- `image_to_video` 或 `first_frame` 使用 I2VA：`<Picture 1>` 锚定 0.00 秒首帧。
- `first_last_frame` 使用 FL2VA：首尾图片分别锚定 0.00 秒与目标结束时刻。
- `last_frame_to_video` 或 `last_frame` 使用 L2VA：图片锚定目标结束时刻。
- `reference_to_video`、`full_reference` 使用 Ref2VA：参考资产提供人物、场景、风格、动作、结构或声音。

I2VA 第一行必须是：

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
```

FL2VA 使用：

```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.
```

L2VA 使用：

```text
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

## T2VA / I2VA / FL2VA / L2VA 正文

对齐指令后按顺序输出以下三个英文字段；T2VA 直接从字段开始：

```text
integrated_multimodal_description: ...

overall_soundscape: ...

non_diegetic_music: ...
```

`integrated_multimodal_description` 从 `[Shot 1]` 开始描述连续时间线。后续真实切镜使用严格递增且不超过总时长的 `[Shot N] At MM:SS.mmm, the camera cuts to...`；轻微角度或距离变化使用自然运镜，不创建假切镜。I2VA 从首帧向前发展，FL2VA 写出可观察的中间变化并精确落到尾帧，L2VA 从合理此前状态逐步收束到尾帧。

只有执行契约中的 `audio_enabled_for_this_task` 为 `true` 时，`overall_soundscape` 才能概括环境声、物理动作声和非语言人声，`non_diegetic_music` 才能描述角色听不到的配乐。该值为 `false` 时两个字段都必须严格写 `N/A`，并且主描述不得包含发声、旁白、歌唱、音乐、音效或虚构语言。

## Ref2VA 正文

Ref2VA 不使用三字段结构，严格按以下六段顺序输出：

```text
subject_definitions:
...

summary:
...

retention_analysis:
...

detailed_description:
...

overall_soundscape:
...

non_diegetic_music:
...
```

`<Subject N>` 是从参考资产抽象出的可复用人物、场景、道具、服装、动作或风格；`<Picture N>` 只在图片充当关键帧或构图锚点时单独定义。`summary` 以 `keyframe completion`、`reference generation`、`video editing`、`video continuation`、`audio reuse` 或 `audio reference` 开头。`retention_analysis` 对每个可见引用使用 `fully_preserved`、`partially_preserved`、`attribute_transfer`、`weak_reference`；音频使用 `fully_copy`、`partially_copy`、`reference`、`weak_reference`。`detailed_description` 按播放顺序描述，并在引用实际生效处使用对应标签。

人物参考音频使用平台给出的 `<Audio N>`，必须在 `subject_definitions` 和 `retention_analysis` 中写明唯一对应人物。它只提供 voice identity、timbre、range、accent、pacing 和 vocal texture；禁止复制参考音频中的 spoken words，禁止跨人物复用，实际对白仍只能来自 `dialogue_locks`。

## 台词与连续性

- 实际发声主体按首次发声顺序分配稳定 `(S1)`、`(S2)`；不同人物不能复用编号。
- 台词放入 `<d>[Chinese]原始台词</d>`，不得翻译或润色。画外旁白使用 `says in an off-screen voiceover`，并明确画面人物嘴唇保持闭合。
- 相邻镜头检查身份、朝向、视线、服装、伤势、道具、空间、光线、运动方向和声音连续性。
- H3 单次目标时长、分辨率、画幅、生成模式、参考媒体数量和格式必须服从平台本次提供的完整执行契约，不得在协议中写死范围；单个片段优先一个主要动作链，不为凑长度增加无叙事价值动作。
- H3 正文不包含独立负向段；除非平台能力明确支持，`negative_prompt` 返回空字符串。

最终仍严格遵循通用模板的 JSON 输出契约，`prompt` 保存完整 H3 英文正文。
