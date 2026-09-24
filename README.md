# CineForge AI 短剧制作平台

## 启动方式

重启后，Worker 会自动接续因服务中断而未完成的 AI 全自动流程，复用已保存的分批分镜、审核和提示词进度。恢复检查在启动时及之后每 15 秒执行；旧任务默认等待 90 秒租约过期，避免抢占其他仍在运行的 Worker。两步之间中断的流程会根据章节实际进度补上下一步，不需要手动点击“继续”。用户主动停止、等待确认、已完成或因真实业务错误失败的流程不会被自动重启。有服务商任务 ID 的媒体任务继续查询原任务；无法确认上游是否已受理的请求仍按现有恢复与退款规则处理。

### 方式一：Docker Compose 完整启动（推荐）

这是最接近正式环境的启动方式，会同时运行 PostgreSQL、pgvector、Redis、MinIO、AgentScope Runtime、FastAPI、异步 Worker、Vue Web 和 Nginx。

环境要求：

- Docker Engine 或 Docker Desktop
- Docker Compose v2
- 建议至少 4 核 CPU、8 GB 内存和足够的媒体文件存储空间

首次启动：

```powershell
Copy-Item .env.example .env
```

打开 `.env`，至少替换以下生产敏感配置：

- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `CREDENTIAL_ENCRYPTION_SECRET`
- `AGENT_RUNTIME_INTERNAL_TOKEN`
- `S3_SECRET_ACCESS_KEY`
- `MEDIA_SIGNING_SECRET`

然后构建并启动：

```powershell
docker compose up -d --build
docker compose ps
```

默认访问地址：

- Web：<http://127.0.0.1:8080>
- 健康检查：<http://127.0.0.1:8080/health>

查看核心服务日志：

```powershell
docker compose logs -f api worker agent-runtime web
```

停止服务但保留数据库和媒体卷：

```powershell
docker compose down
```

> `docker compose down -v` 会删除 PostgreSQL、Redis、MinIO、Skills 和 Runtime 状态卷。除非确认不需要数据，否则不要执行。

当 `SEED_DEMO_DATA=true` 时，可以使用演示账号：

| 角色 | 租户 | 账号 | 密码 |
| --- | --- | --- | --- |
| 管理员 | `demo` | `admin@cineforge.local` | `Admin123!` |
| 创作者 | `demo` | `creator@cineforge.local` | `Creator123!` |

正式部署应设置 `SEED_DEMO_DATA=false`，并通过安全方式创建首个租户管理员。

### 方式二：Windows 本地开发启动

环境要求：

- Python 3.12+
- Node.js 22+
- pnpm 11.7+
- FFmpeg（章节成片渲染需要，命令 `ffmpeg` 必须在 `PATH` 中）

安装依赖：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e "apps/api[dev,postgres,s3]"
.venv\Scripts\python.exe -m pip install -e "services/agent-runtime/agentscope[dev]"
corepack enable
corepack prepare pnpm@11.7.0 --activate
pnpm install --frozen-lockfile
```

分别打开四个终端，从仓库根目录执行。

终端 1，启动 AgentScope Runtime：

```powershell
Set-Location services/agent-runtime/agentscope
..\..\..\.venv\Scripts\python.exe -m uvicorn runtime.main:app --host 127.0.0.1 --port 8010
```

终端 2，启动 FastAPI：

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir apps/api --host 0.0.0.0 --port 8000
```

终端 3，启动持久化任务 Worker：

```powershell
Set-Location apps/api
..\..\.venv\Scripts\python.exe -m app.worker
```

终端 4，启动 Vue Web：

```powershell
pnpm dev:web
```

本地访问地址：

- Web：<http://127.0.0.1:5173>
- 局域网 Web：`http://<本机局域网 IP>:5173`
- API：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- Agent Runtime：<http://127.0.0.1:8010/health>

Vite 默认监听 `0.0.0.0:5173`，并把 `/api`、`/health`、`/uploads` 代理到 `VITE_API_PROXY`，默认值为 `http://127.0.0.1:8000`。

本地开发未设置 `DATABASE_URL` 时使用仓库根目录的 `cineforge.db`（SQLite），未设置 `REDIS_URL` 时 Worker 会轮询数据库。多人协作、并发恢复、pgvector 和正式部署必须使用 PostgreSQL + Redis，推荐直接使用 Docker Compose。

### 修改后需要重启哪些服务

| 修改范围 | 需要重启 |
| --- | --- |
| `apps/web/src/` | Vite 通常自动热更新 |
| `apps/api/app/api/`、`core/`、`db/` | API |
| `apps/api/app/services/`、`worker.py` | Worker；若 API 也导入了相关模块则一并重启 API |
| `services/agent-runtime/agentscope/` | Agent Runtime |
| 数据库迁移 | 先执行迁移，再重启 API 和 Worker |
| `.env` | 所有读取了对应变量的服务 |

API、Worker 和 Runtime 都不会自动热重载。只修改代码但不重启进程，会继续运行旧逻辑。

## 项目简介

CineForge 是一套面向 PC 与 H5 的多租户 AI 短剧生产平台。系统覆盖邀请注册、项目创建、原文导入、章节分析、剧本改编与审核、资产提取与生图、分镜、视频、台词配音、章节合成、Agent 对话、Skills、技能/模板/素材广场、记忆、积分和任务通知。

平台采用“业务后端负责权威状态，Agent 负责理解与编排，Worker 负责持久任务，媒体网关负责模型调用”的边界设计。AI 对话不会直接绕过业务规则修改数据库或调用媒体供应商，而是通过受控工具和平台 API 创建可审计、可计费、可重试的任务。

详细产品需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，更深入的系统设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 技术栈

### Web 前端

| 技术 | 用途 |
| --- | --- |
| Vue 3.5 + Composition API | 页面与业务组件 |
| TypeScript 5.9 | 类型约束 |
| Vite 8 | 开发服务器与生产构建 |
| vite-plugin-pwa + Workbox | 可安装 PWA、离线应用壳和 Service Worker 自动更新 |
| Vue Router 4 | 登录、创作台、Skills、导演台和管理后台路由 |
| Pinia 3 | 登录、项目、任务通知和 Toast 状态 |
| Reka UI | 可访问的高级交互基础组件 |
| Lucide Vue | 图标体系 |
| GSAP 3.15 | 页面动画、流式输出与微交互 |
| TanStack Vue Virtual | AI 长对话虚拟滚动 |
| markdown-it | Agent Markdown 实时渲染 |
| QRCode | 在浏览器本地生成邀请注册链接二维码 |
| 原生 SSE + Fetch | 用户级任务事件和 Agent 流式内容 |

### Python 核心后端

| 技术 | 用途 |
| --- | --- |
| Python 3.12 | API 和 Worker 运行时 |
| FastAPI | 异步 REST API、鉴权依赖和 SSE |
| Uvicorn | ASGI 服务 |
| SQLAlchemy 2 Async | 异步 ORM、事务、行锁和租户过滤 |
| Alembic | PostgreSQL 生产迁移 |
| Pydantic / pydantic-settings | API Schema 与环境配置 |
| httpx AsyncClient | Agent Runtime 与模型平台异步请求 |
| PyJWT | JWT 访问令牌 |
| cryptography | 模型平台 API Key 加密保存 |
| Pillow | 图片验证、转换和缩略处理 |
| EbookLib + BeautifulSoup | EPUB/TXT 章节解析 |
| pytest + pytest-asyncio + Ruff | 自动化测试和代码检查 |

### 数据、队列与媒体

| 技术 | 用途 |
| --- | --- |
| PostgreSQL 16 | 多租户业务数据、任务、积分、通知和审计的权威存储 |
| pgvector | Agent 记忆向量检索；当前为 384 维向量 |
| SQLite + aiosqlite | 单机开发和测试，不作为生产数据库 |
| Redis 7 + hiredis | 队列唤醒、用户事件 Pub/Sub 和登录限流 |
| MinIO / S3 | 上传文件、图片、视频、音频和成片对象存储 |
| FFmpeg | 章节视频与音频合成 |

### Agent 与模型平台

| 技术 | 用途 |
| --- | --- |
| AgentScope `2.0.7.post1` | ReAct Agent、工具调用、状态快照和运行事件 |
| 独立 Runtime 协议 `v2` | 核心后端与 AgentScope 的稳定隔离层 |
| OpenAI 兼容协议 | 文本、图片、视频、TTS 模型接入基础 |
| Sub2API / NewAPI / Custom | 内置供应商类型与自定义适配器 |
| Skills + Prompt Templates | 视觉手册、导演手册、系统提示词和用户技能 |

### 部署

| 技术 | 用途 |
| --- | --- |
| Docker Compose | 本地完整栈和单机部署 |
| Nginx 1.27 | Vue 静态资源、API、媒体与 SSE 反向代理 |
| Docker named volumes | PostgreSQL、Redis、MinIO、Skills 和 Runtime 持久化 |

## 系统架构

```mermaid
flowchart LR
    U[PC / H5 用户] --> N[Nginx / Vite]
    N --> W[Vue 3 Web]
    N --> A[FastAPI]
    A --> P[(PostgreSQL + pgvector)]
    A --> R[(Redis)]
    A --> S[(MinIO / S3)]
    R --> K[异步 Worker]
    P --> K
    K --> G[AgentScope Runtime]
    K --> M[文本 / 图片 / 视频 / TTS 平台]
    K --> F[FFmpeg]
    G --> M
    K --> P
    K --> S
```

组件职责：

- **Vue Web**：用户创作体验、管理员配置、流式反馈和任务中心。
- **FastAPI**：多租户、RBAC、JWT、项目、资产、模型配置、积分、任务提交和结果查询。
- **PostgreSQL**：所有业务状态的唯一权威来源，Redis 不能替代数据库。
- **Redis**：加速队列消费和实时事件；Redis 不可用时任务仍保存在数据库中。
- **Worker**：异步领取任务、供应商并发控制、租约心跳、失败退款、恢复和结果持久化。
- **AgentScope Runtime**：只负责 Agent 推理、Skills、受控文件工具、上下文和运行状态，不负责租户、积分或媒体任务状态。
- **媒体网关**：统一调用图片、视频与 TTS 服务，支持同步结果和异步服务商任务 ID。
- **对象存储**：保存原文、封面、资产图、参考图、视频、音频和章节成片。

## 目录结构

```text
CineForge/
├─ apps/
│  ├─ web/                         Vue 3 前端
│  │  ├─ src/components/          通用组件和大型业务工作台组件
│  │  ├─ src/views/               登录、创作台、导演台、Skills、资源广场、管理后台
│  │  ├─ src/stores/              Pinia 状态
│  │  ├─ src/lib/                 API、Markdown、动画、流式打字机
│  │  ├─ src/router.ts             前端路由和权限守卫
│  │  ├─ src/types.ts              前端业务类型
│  │  ├─ src/styles.css            全局设计系统和响应式样式
│  │  ├─ vite.config.ts            开发服务器与 API 代理
│  │  ├─ nginx.conf                生产反向代理
│  │  └─ Dockerfile                Web 构建镜像
│  └─ api/                         FastAPI 和 Worker
│     ├─ app/api/routes/           REST/SSE 路由
│     ├─ app/core/                 配置、JWT、密码和加密
│     ├─ app/db/                   ORM、会话、种子数据、迁移保护
│     ├─ app/domain/               Pydantic 请求与响应模型
│     ├─ app/services/             Agent、任务、媒体、计费和工作流服务
│     ├─ app/main.py               API 入口
│     ├─ app/worker.py             多执行槽 Worker 入口
│     ├─ migrations/               Alembic 迁移
│     ├─ skills/                   初始演示手册包
│     ├─ tests/                    API、Worker、迁移和存储测试
│     └─ Dockerfile                API/Worker 共用镜像
├─ services/
│  └─ agent-runtime/
│     ├─ agentscope/               当前正式 AgentScope Runtime
│     │  ├─ runtime/               协议、适配器、上下文、路径保护
│     │  ├─ tests/                 Runtime 契约与隔离测试
│     │  ├─ README.md              Runtime 专项说明
│     │  └─ UPGRADING.md           AgentScope 独立升级流程
│     └─ deepseek-harness/         历史兼容实现，不是当前启动目标
├─ skills/tenants/                 按租户持久化的系统 Skills 和手册
├─ uploads/                        本地开发对象存储与媒体缓存
├─ runtime-data/                   本地 Runtime 状态、工作区和日志
├─ docs/ARCHITECTURE.md            深入架构、恢复和安全设计
├─ REQUIREMENTS.md                 产品需求基线
├─ docker-compose.yml              完整服务编排
├─ .env.example                    Compose 环境变量模板
├─ package.json                    根目录脚本
└─ pnpm-workspace.yaml             pnpm 工作区
```

### Web 主要组件

| 文件 | 职责 |
| --- | --- |
| `AppShell.vue` | 桌面侧栏、移动导航、账号菜单和消息中心入口 |
| `AgentChatPanel.vue` | 首页/导演台 Agent 对话、附件、流式输出、会话和模式切换 |
| `AgentExecutionPanel.vue` | 模型与工具调用状态 |
| `DirectorAgentWorkflowCard.vue` | 导演子智能体与审核决策卡片 |
| `DirectorChapterCanvas.vue` | 导演台章节创作区域 |
| `AssetLibraryWorkbench.vue` | 项目/全局资产、衍生关系、提示词、上传和生图 |
| `ChapterFinishingPanel.vue` | 台词、音频、时间线和章节成片 |
| `ActivityCenter.vue` | 用户隔离的任务与通知中心 |
| `AdminUsersPanel.vue` | 管理员用户、状态、密码和积分管理 |
| `VideoCapabilityEditor.vue` | 视频模型能力与参数编辑 |
| `SkillTree.vue` | 管理员 Skills 文件树和编辑器 |
| `MarketplaceView.vue` | 技能、模板和素材广场，共享、复制、同步与私人模板管理 |

### API 路由模块

| 模块 | 职责 |
| --- | --- |
| `auth.py` | 登录、刷新、注销、当前用户 |
| `projects.py` | 项目、配置、封面、删除 |
| `agent_chat.py` | 项目 Agent、个人 Agent、会话、附件和媒体模式 |
| `director.py` | 项目文件、章节、分析和剧本 |
| `director_workflows.py` | 剧本/分镜编排、子智能体、审核和用户决策 |
| `assets.py` | 项目/全局资产、衍生资产、版本、上传和生成任务 |
| `storyboards.py` | 分镜版本、镜头、提示词和视频任务 |
| `dubbing.py` | 台词、音色绑定、TTS 和音频版本 |
| `finishing.py` | 音轨、合成清单和 FFmpeg 成片任务 |
| `tasks.py` / `notifications.py` | 任务、事件、取消、重试、通知和 SSE |
| `memories.py` | Agent 记忆查询 |
| `user_skills.py` | 用户级 Skill 增删改查和启停 |
| `marketplace.py` | 三类资源广场、私人模板、发布快照、复制和版本同步 |
| `admin.py` | 用户、积分、供应商、模型、Agent、手册、提示词和计费规则 |
| `skills.py` | 管理员 Skills 文件树与文件保存 |

### 后端服务模块

| 模块 | 职责 |
| --- | --- |
| `task_submission.py` | 同事务扣费、任务创建、价格快照和入队 |
| `task_worker.py` | 所有持久任务执行、租约、恢复、Agent 流和媒体结果 |
| `task_queue.py` | Redis 队列和用户事件，失败时允许数据库降级 |
| `task_events.py` | 任务进度与状态事件 |
| `billing.py` | 积分账户、不可变流水、扣费和退款 |
| `provider_adapters.py` | Sub2API、NewAPI、OpenAI 兼容和自定义供应商适配 |
| `media_gateway.py` | 图片、视频、TTS 的统一请求与轮询 |
| `agent_runtime.py` | 调用私有 AgentScope Runtime |
| `agent_memory.py` | 记忆提炼、向量化和按需召回 |
| `director_orchestration.py` | 主 Agent、一次性子智能体、审核与流程推进 |
| `managed_skills.py` / `user_skills.py` | 固定 Skills、手册和用户 Skills |
| `object_storage.py` | 本地文件与 S3/MinIO 双实现 |
| `source_import.py` | TXT、EPUB 和粘贴文本章节识别 |
| `composition_renderer.py` | 异步 FFmpeg 渲染 |

## 核心功能

### 多租户、用户与权限

- 所有用户、项目、模型、任务、资产、积分和通知按租户隔离。
- 普通用户只能使用管理员配置，不能进入管理后台。
- 管理员可以创建用户、编辑显示名、角色和状态、重置密码。
- 不允许管理员禁用或降级自己，也不允许移除租户最后一个有效管理员。
- 用户采用停用而不是物理删除，保留项目、任务、积分和审计历史。
- JWT 使用短期访问令牌和 HttpOnly 刷新令牌，支持轮换、注销吊销和重放检测。
- 登录保护包含 Redis IP 限流、PostgreSQL 账号失败计数和安全审计。

### 模型平台与默认模型

- 支持 Sub2API、NewAPI、OpenAI 兼容和自定义供应商。
- 支持平台新增、编辑、删除、连通性测试、模型自动发现和批量导入。
- 模型分为文本、图片、视频和 TTS 四类。
- 每个供应商可以配置 `1-64` 个并发工作流；达到限制后任务继续排队。
- **默认文本、默认视频模型以及 1K、2K、4K 三档图片模型路由必须全部配置并启用；默认 TTS 可选。**
- 每档图片分辨率可独立选择任意已启用供应商下的指定图片模型；封面、资产和首页生图任务按请求分辨率自动路由。
- Agent 可单独绑定文本模型，未绑定时回退到租户默认文本模型。
- 思考程度与输出长度分两层配置：**模型**声明它真正接受的档位（`capabilities.reasoning_efforts`）、模型默认档位与默认输出上限，**Agent** 在模型声明的档位内选择，留空表示跟随模型默认。Agent 选了模型未声明的档位时该值被丢弃而不是照发，避免上游静默忽略后看起来是设置无效；模型未声明任何档位时保持原行为（任意合法档位均可下发）。输出上限取 Agent 覆盖值，否则模型默认值；对话记忆维护固定上限 4000 tokens。这些字段与 `agent_api_mode` 一样属于既有的模型/Agent 配置契约，仅补齐管理后台入口。
- 首页“生成图片”不是只调用图片模型：先由文本模型理解对话并生成最终提示词，再按所选分辨率调用对应图片模型。
- 首页“生成视频”同样先由文本模型整理视频提示词，再调用默认视频模型。
- 供应商 API Key 使用平台加密密钥加密保存，不在普通 API 中明文返回。

### 创作台与个人 Agent

- 用户只看到自己的短剧项目，可以创建、编辑和删除项目。
- 项目配置包含画风/导演手册、视频模型参数、比例与图片分辨率；底层图片平台和模型由管理员统一路由。
- 项目封面支持默认图、用户上传和积分生图。
- 首页个人 Agent 每次刷新默认开启新会话，历史会话可选择和删除。
- 支持对话、生成图片、生成视频和创作 Skill 四种模式。
- 支持上传图片和 `Ctrl+V` 粘贴图片。
- 上传照片做换妆、换造型等编辑时，参考图里的人脸是身份基准：平台在提交前强制追加身份约束（脸型、五官比例、眼型、鼻型、唇形、眉形、肤色与痣等不得重绘），只放行用户要求的妆容、发型、服装、姿态、场景与光线变化，避免把人物改成另一张脸。仅当用户明确要求换人（如“换成另一个人”“换脸”）时才不注入该约束，以免与意图冲突。约束只在确实附带参考图时追加，纯文生图不受影响；此前该约束只存在于资产衍生图路径，首页个人生图完全依赖模型自觉，因此常出现要求保留人脸却换脸的情况。
- 图片上传支持手机与相机常见的 JPG/JPEG（含 `.jpe`、`.jfif` 与 HDR/人像/连拍生成的 `.mpo`）、PNG 和 WebP，单张最大 100 MB。上传后统一规范化为 WebP；108MP、200MP 等高像素手机照片按需降采样后保存，长边最多 4096 像素。HEIC/HEIF 需先转为 JPG 或 PNG。
- 支持输入 `/` 检索并显式调用一个或多个用户 Skill。
- 长对话使用虚拟滚动；流式内容使用平滑打字机和实时 Markdown。
- 个人 Agent 不绑定项目，不能读写任何项目内容。
- Web 支持安装到 Windows、macOS、Android 桌面或移动设备主屏幕，并以独立应用窗口运行；iOS 通过 Safari“添加到主屏幕”完成安装。
- 移动端锁定页面缩放，保留正常纵向、横向滚动以及安全区适配，避免双击或手势误缩放创作工作区。

> PWA 的 Service Worker 和原生安装提示要求安全上下文。正式域名必须启用 HTTPS；开发时 `localhost` 可用，局域网 IP 的裸 HTTP 只能作为普通网页访问。

### 导演台生产流程

- 支持上传 TXT/EPUB 或直接粘贴小说、剧本，并自动识别章节。TXT 上限 100 MB，EPUB 上限 30 MB；上传接口按文件自身的类型读取上限，不会用较小的 EPUB 上限截断大体积 TXT。
- 章节列表按需下发：章节列表与导入结果只返回标题、状态、`content_length` 与 `has_content`，章节正文改由 `GET .../chapters/{chapter_id}/content` 单独读取，仅在阅读该章或启动全自动制作前的空内容预检时请求。此前列表和导入响应都携带每章完整 `original_content`，一部 1638 章的小说会一次性传输数 MB 正文，导致项目打开缓慢、章节无法显示；现在同样的列表约 700 KB。
- 每个项目拥有可下载、可新增、编辑和删除的项目文件库。
- 章节支持分析、剧本生成、多版本、审核轨迹和生效版本。
- 剧本切换会使下游资产提取、分镜、音频和成片按依赖规则失效。
- 主 Agent 负责用户沟通与流程推进；每个重复或专门任务创建独立子智能体。
- 子智能体任务一对一保存状态和记录，失败可按规则重试。
- 审核不通过时生成固定选项，等待用户选择局部修复、整体重做或继续。
- 剧本审核通过后可自动触发资产提取；分镜前会验证所需资产图片是否存在。

### 资产、分镜、视频和音频

- 剧本后仅提取基础人物、场景、道具；自动衍生造型与招式提取移至分镜生成/修复之后。整份分镜统一判断，超过输入预算时按完整镜头串行分批，每批携带项目目录和前批结果，复用同一身份及造型。
- 招式在剧本阶段登记、资产阶段落地、分镜与视频阶段复用：剧本生成/修复时通过 `technique_plan` 声明本章战斗真正用到的招式（短名、所属人物、用途、起手到收势的秒数、可选 `variant_of`），计划随剧本版本保存（`script_versions.technique_plan`）。资产提取在基础人物建立后就地设计新招式（复用已有招式名，不重复设计），写入招式资产并记录 `planned_duration_seconds`、`variant_of` 与所属人物；分镜提示词收到已登记招式表，必须把 `asset_name` 写进对应镜头的 `asset_names`，并按其时长预留起手到收势的最小占用。招式资产会进入生图队列并跳过主资产图片等待，因此不再出现在分镜后才建立、因而永远没有参考图的情况。剧本修复同样返回 `technique_plan`，避免修复过程悄悄清空已登记招式。
- 分镜草稿与已完成提取批次保存到任务中，失败重试复用；全部校验成功后，衍生资产、章节关联和镜头引用在同一事务写入。导演流程先补齐本版镜头所需衍生图，再进入分镜审核/视频流程；普通衍生图继续使用主资产图片作为参考。
- 历史资产不自动删除；手动创建衍生资产、手动设计人物招式仍然可用。

- 项目“塑造资产”可被当前项目 Agent 读取；租户全局资产库用于人工复用。
- 资产包括人物、场景、道具、素材和音频。
- 人物、场景、道具支持一级衍生资产及清晰的父子关系。
- 支持资产手工创建、AI 提取、手工提示词、AI 提示词、图片上传和生图。
- 提示词生成和图片生成是独立持久任务，可单项或批量执行。
- 图像平台以安全政策拒绝提示词（HTTP 400/403/451 且报错含政策语义）时不再直接判失败：平台用文本模型把提示词改写为等价但中性的版本后自动重试一次（资产图、项目封面、首页个人 Agent 生图均适用）。改写模板是受管系统提示词 `image-prompt-safety-rewrite`，可在管理后台编辑。改写必须保留主体身份、构图、光影与画风，只替换会被判定为暴力、血腥、色情或真实品牌/人物的表述；改写后与原提示词相同、几乎为空或丢失主体的，仍然判失败，避免用第二次生成去撞同一堵墙。用户保存的提示词不被改写覆盖，只影响本次生成。仅据此识别政策拒绝，配额、鉴权、参数错误与网络故障照常失败，不会浪费额外生成次数。
- 分镜按版本保存，镜头包含时长、动作、运镜、台词、资产引用和参考图。
- 分镜列表与详情按需下发：镜头列表只返回时长、动作、台词、资产引用等展示所需字段，以及 `has_image_prompt` / `has_video_prompt` 两个存在标记；体积最大的首帧与视频提示词正文改由 `GET .../shots/{shot_id}/prompts` 单独读取，仅在打开镜头编辑器或查看该镜提示词时请求。此前详情接口每次都带回全部镜头的完整提示词，单个分镜体积的大约 80% 是用户没点开的内容。
- 章节目标时长预算（AI 创作项目偏好中的 `chapter_duration_seconds`）是硬约束：全部镜头时长之和必须接近预算，超出约 20%（且不少于一个最短镜头时长）即判定分镜不合格并要求合并或删减镜头，而不是逐镜缩短或增加镜头数。留出这段余量是因为时长必须取自模型合法档位，30 秒预算用 8 秒镜头最接近只能到 32 秒；几倍量级的偏差（30 秒生成数分钟）会被直接拦下。预算同时写入分镜提示词与校验，此前只作为背景文字注入、未参与校验。原文自带完整时间轴时以原时间轴为准，不再施加预算。
- 视频提示词读取当前镜头、资产图片、视觉手册和视频提示词 Skill。
- 视频提示词任务按镜头逐个生成并保存进度；身份、模型绑定、手册与阶段指导等全部镜头相同的部分在任务开始时解析一次并复用，不再逐镜头重复读盘、解密凭据与重建上下文。普通镜头与打斗镜头一样对不合规回复重试三次（回灌上次错误并另起会话），连续失败才判定该镜头失败。单个镜头失败不影响其他镜头，已保存镜头在重试时跳过。
- 支持文生视频、首帧/参考图视频以及供应商自定义能力参数。
- 项目设置提供「首帧模式」，默认关闭，只有当前视频模型明确支持单张首帧输入才可开启。关闭时分镜不建立尾帧依赖，视频独立生成；仍通过人物/场景资产、明确站位与机位、轴线、持物、动作进出状态保持连贯，禁止漂移与重复邻镜剧情。独立首帧图片任务已停用，招式仍使用无人特效/武器资产。
- 开启时，新分镜通过 `continuity_group` 划分明确关联的连续区间，同组后镜等待前镜完成并提取真实尾帧，以 `first_frame` 模式提交。不同组和无关联镜头独立生成；不会仅凭相同人物或场景自动创建依赖。完整连招尽量规划在一个视频片段的内部切镜中。
- 依赖与参考文件持久保存，Worker 重启可恢复；上一镜失败时后镜不会空跑，重做上游后依赖它的旧视频自动失效。关闭首帧模式可解除尚未提交供应商的等待任务，已经提交的请求仍按原设置完成。旧分镜与已生成视频不会自动重写，详细行为见 `docs/storyboard-recovery.md`。
- 接缝检查会阻止明显黑白场和大幅开场画面跳变；这是技术检查，不代表能自动确认人物身份、动作语义完全连续。普通合并不叠化人物画面，音轨边界做短淡入淡出以减少爆音。已生成视频不会自动重制，要应用接力需从连续段起点依次重生成或批量生成。
- 台词按版本提取，人物资产可绑定 TTS 模型和音色。
- 章节合成冻结分镜、视频、音频、配乐、环境音和输出参数，通过 FFmpeg 生成 MP4。

### Agent 记忆与上下文

- 自动提炼长期记忆，并按租户、用户、项目和命名空间隔离。
- PostgreSQL + pgvector 保存记忆向量，按查询相似度和重要性进行召回。
- 当前嵌入实现为 `cineforge-hash-v1`、384 维，可替换为外部嵌入模型。
- 会话持续生成跨轮摘要，长历史按消息数和字符预算压缩。
- 项目大文件使用指针和按需读取，不在每轮对话重复发送全部正文。
- AgentScope 状态用于执行恢复，PostgreSQL 会话、摘要和记忆始终是权威来源。

### Skills 与提示词

- 管理员维护系统提示词、视觉手册、导演手册和底层 Skills 文件。
- 系统提示词只允许编辑固定功能项，不允许随意新增或删除。
- 视觉手册具有固定必填文件：总说明、前缀、角色、角色衍生、道具、道具衍生、场景、场景衍生、分镜、分镜视频和技法文件。
- 导演手册具有固定必填文件：`README.md`、导演规划和分镜表。
- 用户 Skills 可以创建、编辑、删除、启用和禁用，并绑定剧本、审核、资产、分镜或视频等触发阶段。
- Agent 只检索当前阶段启用 Skill 的说明，再按需加载一个或多个 Skill，不会无条件注入全部文件。
- 管理员 Skills 文件访问受根目录路径保护，不能越界访问系统文件。

### 技能、模板与素材广场

- 所有登录用户都可以从顶部或 H5 导航进入技能广场、模板广场和素材广场。
- 用户可以把自己的 Skill 发布到技能广场，也可以再次发布修改后的新版本；其他用户一键复制到“我的 Skills”。
- 用户可以创建和维护私人文本模板，发布到模板广场；复制后的模板可以继续独立编辑。
- 用户可以把自己创建的全局资产发布到素材广场；其他用户一键导入到本人所属租户的全局资产库。
- 广场保存发布时的公开快照，不直接读取或暴露发布者的私人源对象。跨租户只共享用户主动发布的内容。
- 复制产生归当前用户所有的独立副本，作者后续编辑不会静默覆盖副本；作者重新发布后，使用者会看到新版提示并主动同步。
- 删除源 Skill、私人模板或源全局资产会自动下架对应广场条目；其他用户已经复制的副本不会被删除。

### 持久任务、消息与积分

- Agent、提示词、图片、视频、TTS 和成片统一进入持久任务系统。
- 状态包含排队中、进行中、已完成、失败和已取消，并保存真实进度与事件时间线。
- PostgreSQL 保存任务真值；Redis 只负责低延迟唤醒和事件传播。
- Worker 使用可配置异步执行槽，默认 `WORKER_CONCURRENCY=8`。
- Worker 领取任务时写入身份、心跳和租约，过期任务可被其它 Worker 安全恢复。
- 异步媒体供应商任务 ID 会先持久化，Worker 重启后继续轮询而不是重复提交。
- 无法确认上游结果的同步任务在 Worker 中断后会明确失败并退款，不会永久卡在进行中。
- Agent 对话默认只在连续 300 秒没有任何响应或进度时中断；正常流式输出会刷新空闲计时。
- 任务创建与积分扣减在同一数据库事务中完成，并冻结单价、数量、总价和规则版本。
- 失败与取消退款使用原始价格快照，且同一任务只能退款一次。
- 积分流水不可变，管理员手工增减积分同样记录流水和安全审计。
- SSE 只向当前登录用户推送自己的任务与通知，前端同时低频对账 PostgreSQL。

## 数据目录与持久化

### Docker 卷

| 卷 | 内容 |
| --- | --- |
| `postgres_data` | PostgreSQL 和 pgvector 数据 |
| `redis_data` | Redis AOF |
| `object_storage_data` | MinIO 对象 |
| `uploads_data` | API/Worker 媒体缓存 |
| `skills_data` | 租户 Skills 和手册文件 |
| `agent_runtime_data` | AgentScope 会话状态和任务工作区 |

### 本地目录

- `cineforge.db`：默认 SQLite 开发数据库。
- `uploads/`：本地对象存储和媒体缓存。
- `skills/tenants/<tenant-id>/`：租户系统提示词、视觉手册和导演手册。
- `runtime-data/states/`：Runtime 状态。
- `runtime-data/workspaces/`：Agent 受控工作区。
- `runtime-data/logs/` 及根目录 `*.log`：本地运行日志。

这些目录属于运行数据，默认不会提交到 Git。服务运行期间优先通过后台和 API 修改 Skills，不要同时手工覆盖文件。

## 环境变量

主要配置项如下，完整 Compose 示例见 [.env.example](.env.example)。

| 变量 | 默认值/说明 |
| --- | --- |
| `APP_ENV` | `development`；生产为 `production` |
| `DATABASE_URL` | 本地默认 SQLite；生产使用 `postgresql+asyncpg://...` |
| `REDIS_URL` | Redis 地址；为空时退化为数据库轮询 |
| `JWT_SECRET` | JWT 签名密钥，生产必须替换 |
| `CREDENTIAL_ENCRYPTION_SECRET` | 供应商凭据加密密钥，变更前必须规划密钥迁移 |
| `CORS_ORIGINS` | 允许访问 API 的前端 Origin JSON 数组 |
| `SKILLS_ROOT` | Skills 根目录 |
| `UPLOADS_ROOT` | 本地媒体缓存目录 |
| `STORAGE_BACKEND` | `local` 或 `s3` |
| `S3_ENDPOINT_URL` | MinIO 或其它 S3 兼容地址 |
| `S3_BUCKET` | 对象存储桶 |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | S3 凭据 |
| `MEDIA_SIGNING_SECRET` | 媒体路径 HMAC 签名密钥 |
| `REQUIRE_SIGNED_MEDIA_URLS` | 生产应为 `true` |
| `AGENT_RUNTIME_URL` | Runtime 地址，本地默认 `http://127.0.0.1:8010` |
| `AGENT_RUNTIME_INTERNAL_TOKEN` | API/Worker 与 Runtime 共享的内部令牌 |
| `AGENT_RUNTIME_MAX_CONCURRENT_RUNS` | Runtime 最大并发 Agent 数，默认 4 |
| `STORYBOARD_REVIEW_CONCURRENCY` | 分镜审核并发数，默认 4（1–8），仍受供应商额度约束 |
| `WORKER_CONCURRENCY` | Worker 异步执行槽，默认 `8` |
| `AGENT_CHAT_TASK_TIMEOUT_SECONDS` | Agent 无响应空闲超时，默认且最大 `300` 秒 |
| `TASK_RECOVERY_INTERVAL_SECONDS` | 过期租约扫描周期 |
| `SEED_DEMO_DATA` | 是否创建演示租户和账号 |
| `VITE_API_PROXY` | Vite 代理目标，默认 `http://127.0.0.1:8000` |
| `WEB_PORT` | Docker Web 暴露端口，默认 `8080` |
| `CINEFORGE_SOURCE_ROOT` | Compose 构建上下文根目录 |
| `CINEFORGE_NETWORK_SUBNET` | Compose 私有网络段 |

生产环境应通过密钥管理系统注入敏感变量，不要把真实 API Key 或 `.env` 提交到仓库。

## 数据库与迁移

开发模式会为一次性 SQLite 数据库执行 `create_all`。生产模式不会自动补表，而是要求 Alembic revision 与代码一致。

手动执行迁移：

```powershell
Set-Location apps/api
..\..\.venv\Scripts\alembic.exe upgrade head
..\..\.venv\Scripts\alembic.exe current
```

Docker API 容器会在启动 Uvicorn 前执行 `alembic upgrade head`。API 和 Worker 都会校验迁移版本，数据库落后时拒绝启动，防止旧结构继续接收请求或执行任务。

旧版本 PostgreSQL 卷如果已经具备初始完整结构但没有 Alembic 标记，应先备份并停止 API/Worker，再根据 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 的基线说明处理。不要对结构不完整的数据库直接执行 `alembic stamp`。

## API 与安全约定

- API 统一前缀：`/api/v1`。
- 本地开发 Swagger：`/docs`；生产关闭 Swagger 和 ReDoc。
- 管理接口在后端强制校验管理员角色，不能依赖前端隐藏菜单。
- 所有业务查询必须包含租户边界，用户级资源还必须包含用户边界。
- 刷新令牌存放在 HttpOnly Cookie；访问令牌通过请求头发送。
- SSE 使用 Authorization 请求头，不在 URL 中携带 Token。
- 安全事件只保存邮箱和 IP 的 HMAC 指纹，不保存明文。
- Agent Runtime 只允许部署在私有网络，不应直接暴露给公网。
- Runtime 不注册 Shell 工具，仅开放受路径保护的 `Read`、`Write`、`Edit`、`Glob`、`Grep` 和平台 Skills/业务工具。
- Runtime 返回的文件 Diff 会由核心后端再次校验，AgentScope 不能直接写业务数据库。
- 生产媒体 URL 使用与对象路径绑定的 HMAC 签名，禁止未签名直链。

## 测试与构建

从仓库根目录运行：

```powershell
pnpm typecheck:web
pnpm build:web
pnpm lint:api
pnpm test:api
pnpm lint:runtime
pnpm test:runtime
```

单独运行常用命令：

```powershell
# 前端类型检查和生产构建
pnpm --filter @cineforge/web typecheck
pnpm --filter @cineforge/web build

# API 指定测试
.venv\Scripts\python.exe -m pytest apps/api/tests/test_api.py -q

# Runtime 指定测试
.venv\Scripts\python.exe -m pytest services/agent-runtime/agentscope/tests -q
```

提交涉及共享任务、计费、鉴权、租户过滤、数据库迁移或 Agent 协议的修改时，应运行完整 API 与 Runtime 测试，而不是只运行单个用例。

## AgentScope 独立升级

正式 Runtime 位于 `services/agent-runtime/agentscope/`，当前固定 `agentscope==2.0.7.post1`。升级时不要把上游源码直接复制进核心业务，也不要让 API 依赖 AgentScope 内部存储格式。

升级步骤：

1. 阅读上游 Agent、事件、状态、Skills、Workspace 和模型接口变更。
2. 修改 Runtime `pyproject.toml` 中的精确版本和 Runtime 版本声明。
3. 运行 Runtime 契约、路径保护、Skills 转换、状态恢复和事件脱敏测试。
4. 运行完整 API 测试，验证任务、积分、会话、Diff 和重试。
5. 构建新 Runtime 镜像并使用 OpenAI 兼容测试模型验证。
6. 保留旧镜像直到在途任务结束，状态不兼容时从 PostgreSQL 权威历史创建新尝试。

详细步骤见 [services/agent-runtime/agentscope/UPGRADING.md](services/agent-runtime/agentscope/UPGRADING.md)。`services/agent-runtime/deepseek-harness/` 只作为历史回滚参考，不应与 AgentScope Runtime 同时启动在 `8010` 端口。

## 常见问题

### Agent 提示“文本模型不可用”

依次检查：

1. 管理后台是否存在已启用的默认文本模型。
2. 当前 Agent 绑定模型是否仍然存在且启用。
3. 模型所属供应商是否启用并配置 API Key。
4. API 与 Worker 是否在修改模型路由代码后都已重启。
5. API 和 Worker 是否连接同一个数据库。

首页生图任务同时保存 `text_model_id` 和 `media_model_id`。Worker 必须使用文本模型规划提示词，再用图片模型生成，不能把任务主 `model_id` 中的图片模型当作文本模型。

### 任务一直排队或没有进度

- 确认 Worker 正在运行，并检查启动日志中的数据库目标和并发数。
- 确认 API 与 Worker 的 `DATABASE_URL`、`REDIS_URL`、Skills 和对象存储配置一致。
- 检查供应商并发上限是否已占满。
- 检查 Agent Runtime `8010` 健康状态。
- 查看任务事件和 Worker 日志中的真实错误，不要仅依据前端模拟状态。

### 修改后接口仍然是旧行为或返回 404

FastAPI 和 Worker 默认不带 `--reload`。修改 API 路由后重启 API，修改任务或模型路由后重启 Worker。可通过进程启动时间判断是否仍在运行旧代码。

### Agent 在 300 秒后被中断

该限制是连续无响应空闲超时，不是总执行时长。正常文本流、模型事件、工具事件或任务进度都会刷新计时。如果持续输出仍被中断，应检查 Runtime 到 Worker 的流事件是否正常转发。

### Docker 构建路径出现重复目录

部分 NAS 会把 `docker-compose.yml` 复制到生成的子目录，出现 `/CD/CD/services`。在 `.env` 中设置：

```text
CINEFORGE_SOURCE_ROOT=..
```

也可以使用绝对路径，例如：

```text
CINEFORGE_SOURCE_ROOT=/vol1/1000/dockerApps/CD
```

目标目录必须包含 `apps/`、`services/`、`package.json` 和 `pnpm-lock.yaml`。

### Docker 网络地址池耗尽或网段冲突

Compose 默认使用：

```text
CINEFORGE_NETWORK_SUBNET=10.253.0.0/24
```

如果与 NAS、局域网或 VPN 冲突，修改为另一个未使用的私有 `/24` 网段。

### PostgreSQL 迁移版本不一致

先停止 Worker，备份数据库，再执行 `alembic current` 和 `alembic upgrade head`。不要通过关闭迁移保护让旧 Worker 继续执行任务，也不要在未确认结构完整时直接 stamp。

## AI 创建画风手册

管理员进入「创作手册 → AI 创建画风」，上传 1–4 张 JPG/JPEG、PNG 或 WebP 参考图即可提交后台任务（单图最多 32MB，合计最多 64MB）。使用当前租户的默认文本模型分析图片，该模型必须支持视觉输入且平台已配置 API Key。手机照片可直接使用，HEIC/HEIF 需先转为 JPG 或 PNG。

任务先逐图分析色彩、线条、五官妆容、材质与光影，再分批生成完整的 12 文件视觉 Skills 技能包，包含角色四视图和衍生资产规则。多图风格不同时以首图为主，首图同时用作手册封面。生成后可直接查看编辑，并在项目中选择使用。任务与通知仅当前管理员可见；关闭页面不影响后台执行，失败或取消后可从创建记录重试，复用已经完成的分析与文件。

图片会按现有上传流程规范化为 WebP。手册提炼可复用风格，不将参考图中的特定人物或背景强制复制到所有项目；最终出图仍受图片模型能力影响，需要结合实际生成结果校正。

## 全站备份与还原

管理员后台的 **备份与还原** Tab 支持创建、下载、上传、校验、还原和删除 `.cfbackup` 备份包，支持本地数据迁移到 Docker 部署。

- **备份范围**：全库业务表，包括全部租户的账号、邀请、积分、项目、章节、对话、任务、资产、模型平台及其加密凭据、提示词、导演手册、画风手册、个人 Skills 与广场数据；同时包含 Uploads、Skills、Agent Runtime 工作目录和记忆文件，以及配置的 S3/MinIO 专用桶内对象。
- **权限**：默认仅最早创建的管理员可以操作全站备份。可在部署环境中设置 `SITE_BACKUP_ADMIN_EMAILS='["owner@example.com"]'`，授权指定管理员；其他租户管理员不会获得全站数据访问权。
- **创建**：填写当前管理员密码和至少 12 位的备份密码，创建后等待完成并下载。备份使用 AES-256-GCM 加密，备份密码不会写入数据库，丢失后无法解密。
- **还原**：在目标站点上传备份，填写目标站点管理员密码和原备份密码，先校验，再输入“还原整个站点”确认覆盖。目标程序需与备份数据结构一致。完成后使用备份中的账号登录。
- **恢复点**：覆盖前自动保存目标站点的完整恢复点，使用本次备份密码加密，可在还原记录中下载。恢复失败会尝试回滚；回滚失败保持维护状态并提供重试入口，不能继续业务写入。
- **执行与进度**：后台异步执行，页面展示进度；备份及还原期间暂停新业务请求和 Worker 领任务，最多等待 60 秒让已有请求结束，超时则不覆盖数据。运行中的任务不会被强制中断。进程意外退出后，旧请求锁由系统文件锁识别并清理，未完成还原会在 API 启动时尝试回滚。

Docker 已配置持久化 `site_backup_data` 卷，API 与 Worker 共享维护锁，API 同时挂载 Runtime 数据卷。API 镜像为备份目录配置了运行用户权限。当前备份协调器限定 **一个 API 进程**，可运行多个 Worker；不要使用多个 Uvicorn Worker 或多个 API 副本共享此目录。

本地默认备份目录为仓库下 `site-backups/`，可通过 `SITE_BACKUP_ROOT` 修改；`SITE_BACKUP_RUNTIME_ROOT` 必须指向实际 Runtime 数据目录，独立远程 Runtime 需将该目录挂载给 API。`SITE_BACKUP_MAX_BYTES` 默认 100 GiB，同时限制上传和解包大小；调大时需同步调整 Nginx 的上传限制。创建/还原需要容纳解包内容、临时压缩包和恢复点的额外磁盘空间。

备份保存站点业务数据，不包含源代码、Docker 镜像、数据库服务自身的用户权限或 `.env`。目标服务器的数据库、Redis、S3 连接信息保持不变；模型平台凭据会重新使用目标服务器密钥加密。Redis 是可重建的任务加速层，不导出缓存和临时锁；排队任务仍由数据库扫描恢复。第三方视频/图片 URL 只保留链接，若希望资源随站点迁移，应先上传到站点存储。请使用专属 S3/MinIO 桶，还原会替换其对象集合。

## 当前边界

- 当前仓库适合单机 Docker Compose 和本地开发；大规模生产仍需外部密钥管理、TLS、备份、监控、告警、容器资源限制和 Agent 网络沙箱。
- SQLite 只用于开发便利，不能验证 PostgreSQL 行锁、`SKIP LOCKED`、pgvector 和多实例一致性。
- Redis 是加速层而非权威存储；即使 Redis 丢失，任务也必须能从 PostgreSQL 恢复。
- 异步供应商任务只有在服务商任务 ID 已持久化后才能无损恢复；无法确认结果的同步调用会失败退款并等待用户重试。
- TTS 默认模型可选，但未配置时配音功能不可用；默认文本、默认视频和三档图片分辨率路由是完整功能的强制前置条件。
- 模型供应商的能力参数、时长、分辨率、比例和参考图要求以后台模型配置及连通性测试结果为准。

## 文档维护

### 独立打斗编排

- 模块：`apps/api/app/services/combat_choreography.py`，包含时间轴契约、编排规则、校验与视频提示词编译。
- 剧本只规划战斗时间与叙事结果；分镜通过 `combat_plan.windows` 保存镜内相对秒数、参战者、实力关系、目标、结果及招式需求，镜头时长仍服从当前视频模型。
- 生成视频提示词时才调用独立编排：势均力敌采用连续密集攻防，碾压采用一次决定性交击及技能效果/余波；另支持逆转、逃脱与对峙。
- 编排按时间段保存动作、运镜、光影和结束状态，程序直接编译为最终提示词，不再经过第二轮 AI 摘要。人物、招式图片、台词、模型能力和所选手册仍参与约束。
- 结果持久化在任务 `request_payload.combat_designs` 和分镜版本 `content[].combat_design`；相同输入重试复用，镜头或引用变化则重新编排。每镜独立保存，结构校验最多修正三次。
- 校验失败时重试提示只说明缺少或不合法字段及修复方向，不转发 Pydantic 版本横幅与截断的 `input_value`。若判定为输出被截断（JSON 未闭合，或供应商返回长度上限），提示改为压缩说明文字而不是减少节拍；三次均为截断时直接报告输出长度不足并指向 `max_tokens`，不再误报字段缺失。失败原始回复与 `finish_reason` 保存在任务 `request_payload.combat_attempts` 供诊断。
- 分镜级任务按镜头保存进度：单个打斗镜头三次失败不丢弃整份结果，已成功镜头保留，任务失败后重试只处理未完成镜头。
- 校验检查时间覆盖、重叠、越界、计划变更、低密度攻防及碾压被写成拉锯；不代表自动验证真实视频的视觉质量。旧分镜无计划时会按其动作推断，明确静止要求仍保留。
- 碾压段最多 1 个 `decisive_strike`、不允许 `exchange`，但**允许 0 个交击**：只承接上一镜已发生交击余波的镜头不应重复命中。被击飞、翻滚、位移属于 `spectacle` 后果而非对手还手。校验失败时按节拍时间点指出违规项（例如“1 个 exchange（2—6s）”），而不是复述规则；`phase` 五个取值的语义在系统提示中明确定义，避免模型用 exchange 表达位移或后果。

#### 导入剧本时间轴

导入剧本带时间轴时，`source_timeline.py` 从章节原文提取秒数区间、分秒时间码及 SRT 区间，独立传给剧本改编/修复/审核与分镜生成/修复/审核；不依赖改编正文传递时间。剧本必须保留对应时间标记；从零开始连续覆盖的时间轴还校验分镜累计时长和非重叠段落边界，同步对白/字幕不重复计时。校验失败最多尝试三次，不合格草稿不进入资产提取；模型合法时长不能满足原始安排时明确失败，不静默取整。非零起点、存在空档等不完整时间轴仍传给 AI 作事件绑定约束，但不猜测全章总时长。旧版本不会自动重写，需重新生成或修复。没有时间轴的原文沿用原有节奏规划。

#### 按需武指技能（CY 项目适配）

- 补充参考：[qualsenWeb/fight-video-create-skill](https://github.com/qualsenWeb/fight-video-create-skill)，源提交 `f0626a01d32a476c4e827b3fa1aba6dcdf571ead`。独立实现受限空间、多目标重叠威胁、差异对抗三类方法，按镜头内容最多选两份，不复制上游大篇幅正文。纯碾压不加载破绽拉锯，对峙不加载攻击方法；重复关键词不提高检索权重。新增规则核对持械手、落点、招式前提与持续伤损，摄影/粒子本身不算攻击。原有4800字符总预算不变，记录两处参考来源与版本供追溯。
- 融合来源：[CY-CHENYUE/martial-arts-director-cy](https://github.com/CY-CHENYUE/martial-arts-director-cy)，固定源提交 `65c7c1dd35fcf4a01bfc1372ab506b60e2d0d26d`，Apache-2.0。适配说明、分模块资料和完整许可证位于 `apps/api/app/services/combat_skills/`，随 API/Worker 镜像一起部署，不依赖运行时访问 GitHub。
- 分镜阶段只提供简短分类目录，AI 可在 `combat_plan.windows[].martial_systems` 选择体系。视频打斗阶段才读取核心、摄影及最多四种匹配资料（刀剑、枪棍、徒手、摔投、软兵器、术法、巨物、追击）；旧分镜按当前镜头与资产描述匹配。不把全部参考库放入普通对话、剧本或分镜请求。
- 武指正文最多 4800 字符；当前阶段手册/个人技能检索片段合计最多 9000 字符，每文件最多1600字符。个人技能先根据名称及说明目录匹配，最多两项。仅打斗子任务使用此预算，不修改用户保存的完整手册。单次打斗编排输入上限 96000 字符（Runtime 上下文窗口为 128k，留出生成余量）；该上限只用于拦住异常输入，超出时先裁剪武指参考片段并保留输出 schema 与镜头数据，仅当镜头本身的描述确实装不下才明确失败，绝不把裁剪后的动作冒充完整结果。此前上限为 32000，低于「输出 schema + 手册 + 单镜资产数据」的常态规模，且重试回灌错误后必然进一步膨胀，导致正常镜头被判超限而全部失败。
- 打斗子任务移除重复规则、整章大纲和全项目招式目录；保留本镜实际资产、参考图、邻镜边界、模型契约与手册片段。`combat_design.skill_retrieval` 记录选中文件、内容哈希及字符数，技能/手册内容改变会使编排缓存失效。
- v2 的快速势均力敌段至少包含 `ceil(秒数×1.5)` 条不重复攻防（最低4条），至少80%时长为交锋、每节拍最多2秒；这是结构下限，不保证视频模型逐招执行。`tempo=measured` 仅用于已明确指定的慢节奏；碾压仍只允许一次不超过1秒的决定性交击。
- 保留上游的发力余势、对手反应、空间几何、环境反馈与摄影方法；不引入固定15秒、强制多宫格海报、白模替换或逐镜重新起势，继续沿用项目人物、招式资产和真实尾帧接力。

#### 分镜方法内化（shuohao-skills 适配）

- 融合来源：[eternityspring/shuohao-skills](https://github.com/eternityspring/shuohao-skills)，固定源提交 `7ebef4f2f53159ee1eaaec2793271a114a8be8cc`，Apache-2.0。适配说明、分模块资料和完整许可证位于 `apps/api/app/services/storyboard_skills/`，随 API/Worker 镜像一起部署，不依赖运行时访问 GitHub。
- 上游是面向 Claude Code / codex 的五段短剧 skill 集合（拆角色、排大纲、出场景道具、写剧本、切分镜）。本项目只吸收与**分镜阶段**直接相关的部分：切镜分寸与常见病、参考图纪律、H3 提示词补充写法、质量门。
- `storyboard_skill_retrieval.py` 按阶段检索，不整包加载：分镜生成与修复读取切镜、参考图纪律和质量门；分镜审核读取质量门（涉及资产引用时再加参考图纪律）；视频提示词阶段只读取 H3 补充。剧本、资产提取、普通对话和武指子任务完全不加载这个技能包。
- 技能包**没有吸收**上游的 `params.maxSegmentSeconds` 段/分镜两层 schema、`beats:[起,止]` 字段、`promptLang` 开关和门失败累积文件。这些需要改动本项目分镜数据结构或落盘约定，收益不足以抵掉兼容风险；本项目继续使用现有分镜结构、场景分段覆盖机制和模型时长契约。
- `video-prompt-generation-h3.md` 仍是 H3 的唯一权威协议。补充文件只写该协议未覆盖的几条经验（语言分工、运镜词必须落在自己那行、关键帧位置状态、声音字段分工与「声景也是动作指令」），并显式声明以那份协议为准，不新增第二套字段结构或对齐指令，避免提示词冲突。
- `storyboard_quality.py` 把上游「质量门由脚本确定性检查」的思路落成程序检查，但只保留不会误伤正常分镜的条目：台词能否在镜头时长内念完、整批镜头是否集中在同一时长。**这些检查只作为审核意见呈现，不会自动否决分镜，也不会改写台词、剧情或胜负。**
- 阈值取自本项目真实生效分镜的实测分布（台词时长比中位数约 1.0、九十分位约 1.4），因此只在明显超出常规语速时提示。画面字幕与后期文字不计入台词时长，避免把字幕误判成念不完的台词。按镜内文本判定运镜与静止冲突的规则在真实数据上大面积误报（一条分镜记录本身是一段含多次切镜的完整视频），已撤掉，改回由生成与审核阶段按语义判断。
- 技能内化文件与提示词模板解耦：融合内容通过任务上下文注入，不写入数据库中的系统提示词模板，因此不会覆盖管理员在后台对模板的自定义，也不受模板基线版本升级影响。

#### 人物表演与表情链路（ai-drama-expressions 适配）

- 来源：项目内置 `skills/ai-drama-expressions`（AI 漫剧人物表情提示词库，60 条固定表情 + 万能表情公式）。适配版本位于 `apps/api/app/services/expression_skills/`，随 API/Worker 镜像部署，不依赖运行时访问外部仓库。
- 表情链路和打斗编排一样分两段执行。**剧本与分镜阶段只登记情绪时间轴**：每镜 `emotion_plan` 保存「谁、几秒到几秒、什么主情绪、什么强度、由什么触发」，不展开表演细节，也不加载表情正文。此前分镜模板要求避免情绪描写，导致情绪信息在进入视频阶段前就被丢掉，现在改为转译而不是删除。
- 视频提示词阶段才展开五要素表演（眼神、眉毛、嘴角与嘴唇、身体反应、剧情状态 + 一句"像……"）。生成与战斗子任务都会带上这套规则；战斗编排的每个 `exchange`/`decisive_strike` 节拍新增 `expression` 字段，`hold`/`spectacle`/`transition` 不需要。
- **按需检索，不整包加载**：`expression_skill_retrieval.py` 按镜头实际情绪只打开命中的类别文件（基础 / 开心 / 紧张恐惧 / 悲伤 / 愤怒 / 复杂），参考正文上限 5200 字符；同时检索只读取 `emotion_plan` 与镜头自身文本，不读资产描述，避免人物卡的"冷酷"或场景提到的旧事决定一个本来平静的镜头。
- **确定性兜底**：模型可能把表演总结掉，因此每条镜头在生成后都会经过 `expression_contract()`，把缺失的表演按库句逐条补上（已经演过的节拍不重复）。中文模型输出中文指令，H3 等英文协议输出纯英文锁（情绪名映射、不夹带库中文正文，也不夹带中文触发文本），避免多语言混杂引出乱语音；结构化协议里锁会注入到正文描述内部，而不是悬在 `non_diegetic_music` 之后。
- 景别自适应：远景/大全景/全景不强行要求眉眼与嘴唇，表演落在体态、肩颈、呼吸和手部，且不得为看清表情改变已确定的景别或机位；没有情绪轴的人物镜仍会按镜头文本推断一个最轻档的表演，非人物空镜不会得到表情。
- 单人镜头走原来的**一次模型调用**：情绪时间轴与检索到的参考随镜头数据一起提交，不额外增加一次请求。参考、情绪名与哈希记录在检索清单里，库内容变化会使缓存失效。
- 表情模块不新增台词、旁白、环境声或虚构语言；音频关闭时嘴唇保持完全闭合，只走眼神、呼吸、眉眼、肩颈与手部。这条与既有的静音视频策略、台词锁定保持一致。
- **六十条与万能公式都完整保留**。库内六十条按原库逐字使用（名称与描写均未改写），分六大类各十条。原库的「万能表情公式」也完整保留：优先用库内情绪，库内六十条确实无法表达时才自造，并在视频阶段按五要素现场拆解。自造的库外情绪**不会被改写成近似的库内情绪**；检索只借最近的一条做写法参照，声明名始终是权威。库内名称始终优先于关键词推断，因此六十条每条都能路由到自己，不会再被折叠到相邻的更宽泛条目。



Agent 全自动流程与 AI 对话已接入 ripgrep、ast-grep、MarkItDown 按需检索：大段输入保存在任务文件中，模型先定位再读取；分镜逐镜审核，图片与文档附件分开处理。升级依赖、权限范围和格式限制见 [Agent 按需检索说明](docs/agent-retrieval.md)。

- 产品功能或业务规则变化：更新 [REQUIREMENTS.md](REQUIREMENTS.md)。
- 服务边界、恢复、并发、安全或存储变化：更新 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
- 启动命令、依赖、目录、环境变量或用户可见功能变化：同步更新本 README。
- AgentScope 版本或协议变化：更新 Runtime 的 `README.md` 与 `UPGRADING.md`。
