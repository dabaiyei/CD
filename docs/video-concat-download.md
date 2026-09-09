# 下载拼接视频

导演台的视频页在批量下载旁提供「下载拼接视频」。后端按镜头顺序拼接当前分镜中已完成、已生效的视频，输出一个 MP4；未完成镜头不包含在内。保留各段原声，无音轨的片段补静音，不叠加项目配音或背景音乐。

拼接使用现有持久化 Worker 任务 `storyboard_video_concat`，不调用 AI、不扣积分。提交接口立即返回任务 ID，动态中心显示排队、进行中、完成和失败状态。页面完成轮询后下载文件；页面关闭不取消任务，再点击可复用相同视频版本的任务或结果。成品也保存在项目文件库。

后端按项目画幅和分辨率统一为 H.264/AAC、30fps，再依次拼接，兼容不同源编码、尺寸、帧率和缺失音轨。每次只转码一个片段。Worker 重启后使用保存的视频版本清单重新执行；不从当前分镜重新挑选视频。下载接口校验项目、章节、分镜和任务所属账号，管理员也不能下载其他账号的任务。

拼接版本2以视频流时长为准，将每段音轨裁齐到30fps对应的样本数量；中间片段使用PCM音轨，最终只编码一次AAC，避免静音补齐和每段AAC尾部填充累积出空白。片段清单显式记录有效时长。旧版拼接结果不再作为新下载的缓存，原历史文件仍保留。

运行节点需要 `ffmpeg` 和 `ffprobe`。Docker API/Worker 镜像已安装 FFmpeg。Windows 可安装到 PATH，或将两个可执行文件放到项目 `.venv/Scripts` 中；不将二进制提交到 Git。

接口：

- `POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/videos/concat`：提交或复用任务。
- `GET /api/v1/tasks/{task_id}`：查看持久化进度。
- `GET /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/videos/concat/{task_id}/download`：下载已完成文件。

媒体处理依赖使用异步子进程；取消或超时会终止进程并清理临时片段。缺失文件、损坏媒体或转码错误会让任务失败并显示原因，不返回假成功。
