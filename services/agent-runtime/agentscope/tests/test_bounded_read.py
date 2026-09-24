import asyncio
import re

import pytest
from agentscope.tool import Grep

from runtime.bounded_read import PAGE_CHARS, BoundedRead
from runtime.path_guard import WorkspacePathGuard


def test_read_reconstructs_a_long_single_line_and_unicode_without_loss(tmp_path):
    content = "没有换行的剧本🙂" * 4000 + "最终的结局"
    path = tmp_path / "source.txt"
    path.write_text(content, encoding="utf-8")
    guard = WorkspacePathGuard(tmp_path, frozenset(), tmp_path / "new")
    tool = BoundedRead(middlewares=[guard])

    async def read_all():
        offset = 0
        pieces = []
        while True:
            response = await tool(file_path=str(path), char_offset=offset)
            chunks = [chunk async for chunk in response]
            output = chunks[0].content[0].text
            header, body = output.split("\n", 1)
            assert len(body) <= PAGE_CHARS
            pieces.append(body)
            if "EOF" in header:
                break
            offset = int(re.search(r"next_char_offset=(\d+)", header).group(1))
        return "".join(pieces)

    assert asyncio.run(read_all()) == content
    assert tool.name == "Read"
    assert "char_offset" in tool.input_schema["properties"]


def test_read_keeps_line_offsets_and_workspace_boundary(tmp_path):
    path = tmp_path / "script.md"
    path.write_text("第一行\n第二行\n第三行", encoding="utf-8")
    tool = BoundedRead(middlewares=[WorkspacePathGuard(tmp_path, frozenset(), tmp_path / "new")])

    async def verify():
        result = await tool(file_path=str(path), offset=2, limit=1)
        chunks = [chunk async for chunk in result]
        assert chunks[0].content[0].text.endswith("\n第二行\n")
        relative = await tool(file_path="script.md", offset=2, limit=1)
        relative_chunks = [chunk async for chunk in relative]
        assert relative_chunks[0].content[0].text.endswith("\n第二行\n")
        outside = await tool(file_path=str(tmp_path.parent / "outside.txt"), char_offset=0)
        with pytest.raises(PermissionError):
            async for _ in outside:
                pass

    asyncio.run(verify())


def test_grep_cannot_bypass_the_read_budget_with_a_single_long_match(tmp_path):
    from agentscope.message import TextBlock
    from agentscope.tool import ToolChunk

    guard = WorkspacePathGuard(tmp_path, frozenset(), tmp_path / "new")
    captured = {}

    async def handler(**kwargs):
        captured.update(kwargs)
        yield ToolChunk(content=[TextBlock(text="匹配" * 20_000)])

    async def verify():
        return [
            chunk
            async for chunk in guard.on_tool_call(
                Grep(), {"pattern": ".*", "head_limit": 0, "context": 1000}, handler
            )
        ]

    chunks = asyncio.run(verify())
    assert len(chunks[0].content[0].text) < 12_200
    assert captured["head_limit"] == 40 and captured["context"] == 2
