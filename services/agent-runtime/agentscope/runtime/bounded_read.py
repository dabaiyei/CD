"""A bounded Read tool with lossless character paging for long chapter lines."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Read, ToolChunk

PAGE_CHARS = 12_000


class BoundedRead(Read):
    input_schema = deepcopy(Read.input_schema)
    input_schema["properties"]["char_offset"] = {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Zero-based character offset for lossless text paging. Overrides line offset/limit. "
            "Use the next_char_offset returned by the previous read."
        ),
    }

    @property
    def description(self) -> str:
        return (
            "Read an authorized workspace file. Text returns at most 12000 characters per call. "
            "First locate relevant files/sections; do not read every file. offset and limit select lines; "
            "char_offset selects a zero-based character position, including inside a very long line. "
            "Continue with next_char_offset only when the remaining content is relevant. "
            "For images/PDFs the standard Read behavior applies. file_path must be absolute."
        )

    async def call(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        pages: str | None = None,
        _agent_state: AgentState | None = None,
        char_offset: int | None = None,
    ) -> ToolChunk:
        if Path(file_path).suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml"}:
            return await super().call(file_path, offset, limit, pages, _agent_state)
        try:
            raw = await self._backend.read_file(file_path)
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            lines = text.splitlines(keepends=True)
            if _agent_state is not None:
                await _agent_state.tool_context.cache_file(file_path=file_path, lines=lines)
            start = (
                max(0, char_offset) if char_offset is not None else sum(map(len, lines[: max(0, offset - 1)]))
            )
            end = min(len(text), start + PAGE_CHARS)
            if char_offset is None:
                end = min(
                    end, start + sum(map(len, lines[max(0, offset - 1) : max(0, offset - 1) + max(1, limit)]))
                )
            start = min(start, len(text))
            continuation = f"next_char_offset={end}" if end < len(text) else "EOF"
            return ToolChunk(
                content=[
                    TextBlock(
                        text=f"[characters {start}:{end} of {len(text)}; {continuation}]\n{text[start:end]}"
                    )
                ],
                state=ToolResultState.SUCCESS,
            )
        except (OSError, UnicodeError) as exc:
            return ToolChunk(
                content=[TextBlock(text=f"Error reading file: {exc}")], state=ToolResultState.ERROR
            )
