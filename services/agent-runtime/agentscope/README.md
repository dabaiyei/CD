# CineForge AgentScope Runtime

This private service is CineForge's only AgentScope integration boundary. The SaaS core remains authoritative for tenants, RBAC, billing, tasks, conversation history, long-term memory and project files.

## Runtime contract

- AgentScope is pinned to `2.0.7.post1`.
- The core calls `POST /internal/v2/runs` with an internal token.
- OpenAI-compatible credentials, base URLs and optional platform headers are supplied for one task only.
- Every result identifies the Runtime, AgentScope, contract, model, prompt and Skills versions.
- `/internal/v1/runs` remains as a deployment compatibility route.

## Isolation

Each persistent chat session rebuilds a stable tenant/project/session workspace from immutable snapshots. One-off workflow and memory-maintenance calls remain task-isolated. AgentScope receives only `Read`, `Write`, `Edit`, `Glob` and `Grep`; shell tools are never registered. A path-guard middleware restricts reads to the workspace and writes to platform-authorized editable files or `project-files/new`.

The Runtime converts CineForge handbook snapshots into standard AgentScope packages with generated `SKILL.md` files. Original handbook files remain read-only resources. Returned changes are structured diffs that the core revalidates before PostgreSQL is updated.

AgentScope state and the last completed run receipt are saved together atomically under tenant/project/session scope. Chat turns reuse one stable session, while retrying the same task returns its durable receipt instead of replaying the turn. PostgreSQL conversation summaries, history and vector memory remain authoritative recovery state. Runtime events exclude thinking blocks, streamed text, tool arguments and tool result content.

## Local verification

```powershell
C:\Code\CD\.venv\Scripts\python.exe -m pip install -e ".[dev]"
C:\Code\CD\.venv\Scripts\python.exe -m pytest tests -q
C:\Code\CD\.venv\Scripts\python.exe -m ruff check runtime tests
```

Run from this directory so the new Runtime package is selected even if the legacy adapter was previously installed in the same virtual environment:

```powershell
C:\Code\CD\.venv\Scripts\uvicorn.exe runtime.main:app --host 127.0.0.1 --port 8010
```
