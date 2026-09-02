from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

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

        target = Path(raw_path).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError(f"{tool.name} path is outside the task workspace")
        if tool.name in {"Write", "Edit"} and not self._is_writable(target):
            raise PermissionError(f"{tool.name} path is not platform-authorized for editing")
        guarded[path_key] = str(target)
        async for chunk in next_handler(**guarded):
            yield chunk

    def _is_writable(self, target: Path) -> bool:
        return target in self.writable_files or target.is_relative_to(self.new_files_root)
