"""Transport complete long documents without putting their bodies in the prompt."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from app.services.agent_runtime import AgentRuntimeProjectFileSnapshot

# Runtime accepts at most 2M characters per snapshot. A 100MB imported source
# fits in <=105 parts, leaving room for script/context and project references.
SNAPSHOT_PART_CHARS = 1_000_000


def paged_snapshot(snapshot: AgentRuntimeProjectFileSnapshot) -> list[AgentRuntimeProjectFileSnapshot]:
    if len(snapshot.content) <= SNAPSHOT_PART_CHARS:
        return [snapshot]
    path = PurePosixPath(snapshot.path)
    parts = []
    entries = []
    for index, start in enumerate(range(0, len(snapshot.content), SNAPSHOT_PART_CHARS), 1):
        content = snapshot.content[start : start + SNAPSHOT_PART_CHARS]
        name = f"{path.stem[:160]}.part-{index:04d}{path.suffix}"
        part_path = str(path.with_name(name))
        parts.append(
            snapshot.model_copy(
                update={
                    "id": f"{snapshot.id[:110]}-part-{index:04d}",
                    "directory_id": snapshot.directory_id or snapshot.id,
                    "path": part_path,
                    "name": name,
                    "content": content,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "editable": False,
                }
            )
        )
        entries.append(f"- 字符 {start}–{start + len(content)}（末端不含）：{part_path}")
    index_content = (
        f"# {snapshot.name} · 完整原文分段索引\n\n"
        f"共 {len(snapshot.content)} 字符，以下分段按顺序无损覆盖全文。"
        "先定位与本轮问题相关的段落，再用 Read 的 char_offset 分页读取；"
        "不要一次读取所有分段或把全文复述进对话。分段只读，不得编辑索引替代原文。\n\n" + "\n".join(entries)
    )
    return [
        snapshot.model_copy(
            update={
                "content": index_content,
                "sha256": hashlib.sha256(index_content.encode("utf-8")).hexdigest(),
                "editable": False,
            }
        ),
        *parts,
    ]
