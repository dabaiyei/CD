# CineForge Agent Runtime

This service is the only integration boundary between CineForge and DeepSeek Harness. It is an internal service, not a public API and not the SaaS business backend.

## Boundary

- Contract: HTTP `POST /internal/v2/runs` with `X-Internal-Token`; the v1 route remains available for compatibility.
- Harness SDK: pinned to `deepseek-harness-sdk==0.1.1rc1`.
- Runtime profile: `sdk-minimal` by default.
- Isolation: Harness homes are scoped by tenant and project; workspaces are scoped by tenant, project and task.
- Auditability: every response includes runtime, Harness, contract, model, prompt and Skills versions.
- Media generation and billing remain in the Python core backend.

The service materializes immutable task snapshots of Skills and controlled project text files into the task workspace and verifies every SHA-256 digest before execution. Existing files stay under `project-files/<file-id>/`; Agent-created files must be placed under `project-files/new/`. The adapter returns structured file diffs to the core API, which remains solely responsible for authorization, concurrency checks and PostgreSQL writes. The upstream SDK profile currently grants broad local tool access, so production deployment must also isolate this container with filesystem, process, network, CPU and memory limits. Application-level paths are not a security sandbox.

## Development

Install the adapter without the large Harness runtime to run contract tests:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\pytest.exe -q
```

Install the pinned upstream runtime for real model calls:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,harness]" --pre
.venv\Scripts\uvicorn.exe runtime.main:app --host 127.0.0.1 --port 8010
```

The health endpoint is public only for infrastructure probes. Run requests require the internal token and the service should be reachable only from the private application network.
