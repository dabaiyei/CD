# 无限画布

视频复刻导航已替换为 `/canvas`。旧 `/video-replicas` 页面地址跳转到新入口，历史项目和媒体数据保留。

集成上游：https://github.com/basketikun/infinite-canvas ，MIT，源码基准 `dab19adc0847e32e39b7fc8ff90cb392561fb826`。
源代码位于 `services/infinite-canvas`，保留 LICENSE。React 画布作为本地静态模块构建到 `apps/web/public/canvas-app`，通过同源 iframe 嵌入 Vue。

## 已接入

- 节点编辑、缩放与平移、连线、分组、撤销与重做、素材导入导出。
- 文本、图片、视频、配音节点使用系统模型平台；API Key 仅由后端解密使用。
- 连线中的文本、图片、视频和音频作为生成输入，后台按模型能力校验。
- 顶部资产库可从全局资产与现有项目资产导入当前版本图片。
- 画布助手参考选中节点，按当前画布保存会话，可将回复加入文本节点。
- 存储使用 `/api/v1/canvas/storage`，按租户和账号隔离，修改携带 revision，冲突返回 409，避免覆盖。
- `/api/v1/canvas/generate` 创建 `canvas_generation` 任务，使用现有积分、任务队列、Worker、失败退款和重试机制。
- 视频与配音保存供应商任务 ID，恢复时继续查询原任务。生图上游状态未知时保守提示手动重试，避免重复计费。
- 新节点的默认画幅、时长、分辨率和声音设置遵循所选模型能力；停止节点生成同时取消后台任务。
- 参考素材为独立任务输入快照；删除或修改画布节点不会让排队任务丢失输入。
- 数据库表 `canvas_storage_items` 与 uploads 素材一起进入现有全站备份。
- 完整恢复配置页：系统模型渠道、自定义 OpenAI/Gemini 渠道、API Key、模型能力、调用脚本、默认模型、生成参数、本地代理、WebDAV 与配置导入导出。自定义渠道不走系统积分任务，沿用其自己的供应商协议。
- 模板中心、模板来源、定时刷新、搜索、加入资产、节点引用与配置导入导出保留；模板来源与缓存也保存到当前账号。
- 配置刷新时仅更新系统管理渠道，保留自定义渠道、调用脚本、默认模型和用户偏好。
- 原生 Codex 面板：地址/Token 连接、实际模型及推理强度、聊天和图片附件、引用画布节点、历史、权限确认、诊断日志、Skills 管理与按需选择。
- 本地 `canvas-agent` 保留 34 个 MCP 工具，支持读取画布、创建和修改节点、连线、启动生成、查询生成状态、检索提示词、资产操作及工作台操作。

项目剧本、分镜、首页对话的生成链路保持原有实现；画布助手与 Codex 面板是两个独立入口。

## Codex 连接

在画布右上角点击 `Codex`。管理员可点击「启动并连接本地 Codex」，后端启动仓库内的服务，通过 `/api/v1/canvas/agent` 转发 HTTP、媒体与 SSE。手机和 HTTPS 页面使用同域入口，无需连接手机的 localhost，也无需单独暴露 Agent 端口。

服务使用部署机器的 Codex 登录与供应商配置；工作区、活动线程、附件与补充历史存入 `runtime-data/canvas-agent/<账号摘要>`（以 `SITE_BACKUP_RUNTIME_ROOT` 为准）。服务器本机 Codex 入口仅对管理员开放。任何用户都可填写自己电脑上 Agent 的地址和 Token，浏览器须能访问该地址。

从其他 Codex 会话操作画布时，点击「复制 Codex 配置」，将返回的 TOML 合并到该机器的 Codex 配置；其他 MCP 客户端可使用「复制 MCP 配置」返回的 JSON。保留其中的 `INFINITE_CANVAS_AGENT_HOME`，它将工具连接到当前账号的服务；网页画布须保持打开。此操作不会自动修改全局 Codex 配置或安装插件。

独立运行仓库内模块：`pnpm start:canvas-agent`。自定义 Node/Agent 路径可配置 `CANVAS_AGENT_NODE`、`CANVAS_AGENT_ENTRY`，指定 Codex 可执行文件可配置 `CANVAS_AGENT_CODEX_BIN`。

插件交接地址：`<站点>/canvas?mode=new#agentUrl=<URL编码后的Agent地址>&agentToken=<URL编码后的Token>`。支持 new/recent/choose，连接参数保留在 fragment 并在接收后清除。连接设置按账号隔离；Skills 不会被全部主动加载。

自定义渠道需要本地代理时，在仓库根目录运行 `node services/infinite-canvas/canvas-proxy/index.js`，再填写配置页中的代理地址。直接浏览器请求仍遵循供应商的 CORS 和浏览器混合内容规则。

## 构建与部署

```powershell
pnpm install --frozen-lockfile
pnpm build:canvas-agent
pnpm build:web
```

`build:web` 先构建画布再构建 Vue；`restart-all.ps1` 同时构建 Canvas Agent。开发启动前也应运行一次 `pnpm build:canvas`；画布源码修改后重新构建。

数据库迁移：`f4c8a61d9032_add_infinite_canvas_storage.py`。Docker 继续由原 API 启动入口迁移数据库，API 镜像同时打包 Node、Canvas Agent 和 Codex CLI，无需另行部署上游服务。

Docker 的 Codex 配置目录为 `/var/lib/cineforge-agent-runtime/codex`，使用现有 runtime 持久卷并纳入站点备份。首次使用须在容器内完成 Codex 登录或配置自己的模型供应商。可通过 `docker compose exec api node /opt/infinite-canvas/canvas-agent/node_modules/@openai/codex/bin/codex.js login --device-auth` 完成设备授权；画布 Token 不是模型平台 API Key。外部 Codex 客户端使用的 MCP 命令必须在能访问该容器路径的环境执行。

## 验证

`apps/api/tests/test_infinite_canvas.py` 验证存储隔离、revision 冲突、图片读取、参考图改图、视频续查、失败单任务重试。
`apps/api/tests/test_canvas_agent.py` 验证本机连接权限、账号隔离、专用桥接 Token、HTTP/SSE 转发和私有服务 Token 不外泄。两组共 9 项通过。
`pnpm --filter @basketikun/canvas-agent test`：124 项通过，2 项 Unix 文件权限测试在 Windows 跳过。
前端 `pnpm --filter infinite-canvas typecheck` 和 `pnpm typecheck:web`，完整构建 `pnpm build:web`。

`node scripts/probe_infinite_canvas.mjs` 在桌面和手机尺寸运行实际生产前端，模拟 API，验证 Codex 面板、渠道/脚本刷新保留、模板来源、画布创建、资产导入、助手参考图片、保存恢复、生图节点和视频参数，以及横竖屏切换。

`python scripts/probe_canvas_agent.py` 连接真实本地服务，读取实际 Codex 模型列表，通过 MCP stdio → 本地服务 → 同域 SSE → 画布结果回传完成读写验证，不发送推理请求。已验证 8 个实际可用模型和 34 个工具。供应商调用使用测试替身，不消耗真实模型积分。Docker 构建配置已更新，本机无 Docker 环境，尚未执行容器构建。
