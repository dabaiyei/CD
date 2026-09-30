# Infinite Canvas Plugin

让 Codex / ZCode 可以打开并操作 Infinite Canvas。

CineForge 集成无需安装在线版插件。画布的 Codex 面板可启动本地服务并提供当前账号的 MCP 配置；
将该配置用于外部 Codex 会话，即可使用原有画布工具。详见仓库 `docs/infinite-canvas.md`。
下面的安装方式适用于原始独立应用。

## 安装

### Codex

macOS / Linux：

```bash
git clone https://github.com/basketikun/infinite-canvas.git
cd infinite-canvas
codex plugin marketplace add "$(pwd)"
codex plugin add infinite-canvas@infinite-canvas-local
```

Windows PowerShell：

```powershell
git clone https://github.com/basketikun/infinite-canvas.git
cd infinite-canvas
codex plugin marketplace add "$PWD"
codex plugin add infinite-canvas@infinite-canvas-local
```

Windows CMD 将 `$PWD` 替换为 `%cd%`。

### ZCode

- 打开 **Settings → Plugin Management → Discover**，点击右上角 **`+`** 添加 marketplace。
- 选择 **本仓库目录**（`plugins/infinite-canvas`）或本仓库根目录，即可发现 `infinite-canvas` 插件并安装。
- 或在 ZCode 界面直接以本地目录方式加载该插件目录。

安装后新建一个任务，然后输入：

```text
帮我打开并连接到 Infinite Canvas
```
