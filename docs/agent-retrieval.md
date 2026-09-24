# Agent 按需检索

AgentScope Runtime 集成 `ripgrep 15.1.0`、`ast-grep-py 0.45.3` 和 `MarkItDown 0.1.8`。安装 Runtime 的 Python 依赖即可获得三种工具，Docker 镜像通过同一依赖清单安装。

## 对话与全自动流程

- 首页与项目 AI 对话使用 `workspace` 模式：可以检索当前账号、当前会话/项目已授权的文件，并保留已有平台授权写入能力。不会开放终端或任意命令执行。
- 全自动剧本、分镜、资产提取、审核、局部修复、视频提示词等通过共用请求构建器使用 `retrieval` 模式：只有读取、搜索和转换工具，最终 JSON 仍交由平台校验、逐项保存、计费和重试。
- 分镜审核在此模式下逐镜执行，可并行两个独立审核。当前镜头、对应原文、邻镜/资产沿革分别以只读文件提供，模型按疑点调用工具；不是把整章分镜塞进每次模型请求。
- 修复仍使用已有字段补丁、补镜和断点协议。超长局部操作和完整来源保存为文件，先读操作要求，再按镜号或字段检索。普通格式整理任务仍可使用无工具模式。
- 导演、画风、招式和用户技能等参考内容保存在只读文件；硬约束、优先级和输出协议保留。规则/参考版本改变会使相关缓存失效。文件传输到 Runtime 不等于文件正文注入模型上下文。

## 工具

### 固定分镜与视频提示词文件

每章维护 `章节名-分镜表.json` 和 `章节名-视频提示词.json` 两个固定文件，文件 ID 保持不变。生成、审核修复、逐镜提示词生成、手动编辑及资产参考更新都原位保存；不再为每次修改新增 `v2/v3` 文件。打开项目文件列表时，旧的自动生成副本会归档并退出列表与 Agent 检索，旧内容仍保留在数据库用于恢复。

Agent 先搜索并读取目标镜头，然后使用 `Edit` 修改原文件，保持 `shot_id` 和 `order_index` 不变。后台校验文件基准哈希、账号权限、字段和镜头状态后，将改动同步至真实镜头数据；只更新实际变化的镜头，失效相应旧视频，未修改的镜头保留。失败的文件修改整体回滚，不能通过另建修复副本绕过冲突检查。底层生产版本与媒体关联仍保留，不会因归档工作区文件删除已生成视频。

| 工具 | 用途 | 返回限制 |
| --- | --- | --- |
| Ripgrep | 检索台词、角色名、镜号、手册规则；支持限定目录、glob、文字/正则 | 每页最多30条，长行预览；缩小查询后用 Read 查原文 |
| AstGrep | 对单个 JSON/代码文件做 AST 匹配，例如 `"dialogue": $VALUE`、`kind=pair` | 每页最多20项，返回源文件行列，不执行替换 |
| MarkItDown | 本地 PDF、DOCX、PPTX、XLSX、文本等转 Markdown | 缓存转换结果，每次最多12000字符，返回可继续检索的文件路径 |
| Read | 读取已经定位的原文 | 12000字符分页，支持行号和 `char_offset` |

聊天附件支持上述 Office/PDF 文档及 TXT、MD、CSV、JSON，文档上限32MB，图片仍为100MB。文档附件不会作为图片参考提交给生图或视频模型；后续同一会话可以再次查询最近文档。Office 文档还检查解压后总大小不超过100MB。

转换在本地进行，不启用外部 LLM、插件或 URL 下载；扫描版 PDF 不保证有可提取文字，当前不包含 OCR。工具限于任务工作区，越界路径/符号链接被拒绝，子进程有时限，结果有分页上限。新工具名会出现在现有执行事件中。

## 升级和验证

本地执行 `.\.venv\Scripts\python.exe -m pip install -e services/agent-runtime/agentscope`，然后重启 API、Worker、Runtime；前端需重新构建。Docker 使用 `docker compose up -d --build`。无需数据库迁移。API 与 Runtime 应同步升级以支持 `retrieval` 模式和文档快照字段。

```powershell
.\.venv\Scripts\python.exe -m pytest services/agent-runtime/agentscope/tests -q
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_retrieval_context.py apps/api/tests/test_chat_documents.py -q
```
