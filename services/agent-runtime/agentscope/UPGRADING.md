# Upgrading AgentScope

The retrieval extension adds `tool_mode=retrieval` and optional `documents` to v2 requests. Upgrade API, Worker and AgentScope Runtime together; older runtimes reject these fields. Install the full Runtime dependency list (including ripgrep, ast-grep-py and MarkItDown), rebuild the web client for document attachments, and retain the old runtime until in-flight runs finish. No database migration is required. Verify `tests/test_retrieval_tools.py` and API `test_retrieval_context.py` / `test_chat_documents.py` in addition to the checks below.

1. Review upstream release notes and source changes for `Agent`, `reply_stream`, events, `AgentState`, `LocalWorkspace`, Skills, OpenAI credentials, tools and permissions.
2. Change only the exact `agentscope` pin in `pyproject.toml` and `AGENTSCOPE_VERSION` in `runtime/__init__.py`.
3. Run Runtime contract, Skills conversion, path-guard, state restore and event-redaction tests.
4. Run the full API suite to verify manifests, task retries, billing and project-file Diff handling.
5. Build a versioned Runtime image and verify it against an OpenAI-compatible test platform before rollout.
6. Keep the previous image available until in-flight tasks finish. Do not deserialize an incompatible state format; rebuild a fresh attempt from PostgreSQL history instead.
