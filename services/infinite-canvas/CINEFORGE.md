# CineForge integration

Vendored from https://github.com/basketikun/infinite-canvas at commit
`dab19adc0847e32e39b7fc8ff90cb392561fb826` (MIT, copyright basketikun).
See [LICENSE](LICENSE) and [integration documentation](../../docs/infinite-canvas.md).

Platform adapters live under `web/src/cineforge`. System models use authenticated
platform API calls; explicitly configured custom channels retain upstream
OpenAI/Gemini protocols and scripts. Account storage includes settings and prompt sources.
The original canvas editor, node system, reference connections, exports and
plugin sources remain local. The bundled `canvas-agent` connects to Codex app-server
and exposes the original MCP tools. Admins can start an account-local service using
the platform bridge; users can also connect their own Agent by URL and token.
No upstream hosted server is required. See integration documentation for MCP setup.
