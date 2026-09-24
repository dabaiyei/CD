from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolBase, ToolChunk, ToolMiddlewareBase


class WorkspacePathGuard(ToolMiddlewareBase):
    """Enforce CineForge's workspace and editable-file boundary before I/O."""

    def __init__(
        self,
        workspace: Path,
        writable_files: frozenset[Path],
        new_files_root: Path,
    ) -> None:
        self.workspace = workspace.resolve()
        self.writable_files = writable_files
        self.new_files_root = new_files_root.resolve()

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        guarded = dict(input_kwargs)
        path_key = "file_path" if tool.name in {"Read", "Write", "Edit"} else "path"
        raw_path = guarded.get(path_key)
        if raw_path is None and tool.name in {"Glob", "Grep"}:
            raw_path = str(self.workspace)
            guarded[path_key] = raw_path
        if not isinstance(raw_path, str) or not raw_path:
            raise PermissionError(f"{tool.name} requires an explicit workspace path")

        candidate = Path(raw_path)
        target = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError(f"{tool.name} path is outside the task workspace")
        if tool.name in {"Write", "Edit"} and not self._is_writable(target):
            raise PermissionError(f"{tool.name} path is not platform-authorized for editing")
        guarded[path_key] = str(target)
        if tool.name == "Grep":
            guarded["head_limit"] = min(max(int(guarded.get("head_limit") or 40), 1), 40)
            for key in ("context", "-A", "-B", "-C"):
                if key in guarded:
                    guarded[key] = min(max(int(guarded[key]), 0), 2)
        remaining = 12_000
        async for chunk in next_handler(**guarded):
            if tool.name in {"Grep", "Glob"}:
                blocks = []
                for block in chunk.content:
                    if isinstance(block, TextBlock):
                        excerpt = block.text[:remaining]
                        remaining -= len(excerpt)
                        if len(excerpt) < len(block.text):
                            excerpt += (
                                "\n[结果过长：请缩小检索路径或关键词，"
                                "再用 Read 的 char_offset 读取相关段落。]"
                            )
                        blocks.append(block.model_copy(update={"text": excerpt}))
                    else:
                        blocks.append(block)
                chunk = chunk.model_copy(update={"content": blocks})
            yield chunk

    def _is_writable(self, target: Path) -> bool:
        return target in self.writable_files or target.is_relative_to(self.new_files_root)
