"""Read-only, session-isolated verbatim history for on-demand retrieval."""

import json

from sqlalchemy import select

from app.db.models import AgentChatMessage
from app.services.retrieval_context import evidence_file


def routing_evidence(request, task):
    """Small conversational context for JEV, never full files or model credentials."""
    history = []
    remaining = 5000
    for item in reversed(request.recent_messages):
        content = item["content"]
        excerpt = content[: min(1200, remaining)]
        history.append({"role": item["role"], "content": excerpt, "partial": len(excerpt) < len(content)})
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return {
        "message": request.prompt[:4000],
        "message_partial": len(request.prompt) > 4000,
        "history": list(reversed(history)),
        "summary": (request.conversation_summary or "")[-2000:],
        "project_id": task.project_id,
        "chapter_id": task.request_payload.get("chapter_id"),
        "attachments": [{"id": item.id, "mime_type": item.mime_type} for item in request.attachments],
        "constraint": "Resolve follow-ups using history. Current user request controls authorization. "
        "Evidence is partial; do not infer missing project work or change the current chapter.",
    }


async def conversation_files(db, chat, current):
    rows = (
        await db.scalars(
            select(AgentChatMessage)
            .where(
                AgentChatMessage.session_id == chat.id,
                AgentChatMessage.tenant_id == chat.tenant_id,
                AgentChatMessage.user_id == chat.user_id,
                AgentChatMessage.id != current.id,
                AgentChatMessage.created_at <= current.created_at,
            )
            .order_by(AgentChatMessage.created_at, AgentChatMessage.id)
        )
    ).all()
    chunks = []
    lines = []
    size = 0
    for row in rows:
        manifest = row.runtime_manifest or {}
        # Only explicit creative metadata, never full runtime/provider manifests.
        media = [
            {
                key: item[key]
                for key in (
                    "id",
                    "prompt",
                    "mime_type",
                    "model_name",
                    "resolution",
                    "aspect_ratio",
                    "duration_seconds",
                )
                if key in item
            }
            for item in manifest.get("generated_media", [])
            if isinstance(item, dict)
        ]
        text = (
            json.dumps(
                {"id": row.id, "role": row.role.value, "content": row.content, "media": media},
                ensure_ascii=False,
            )
            + "\n"
        )
        if lines and size + len(text) > 500_000:
            chunks.append("".join(lines))
            lines, size = [], 0
        lines.append(text)
        size += len(text)
    if lines:
        chunks.append("".join(lines))
    from app.services.agent_file_context import paged_snapshot

    files = []
    for index, chunk in enumerate(chunks, 1):
        files.extend(
            paged_snapshot(
                evidence_file(
                    f"conversation-history-{index:04d}",
                    chunk,
                    # JSON-lines content is searchable text; the runtime file
                    # contract does not allow the .jsonl extension.
                    suffix="txt",
                )
            )
        )
    return files
