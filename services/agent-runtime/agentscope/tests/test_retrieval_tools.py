import asyncio
import base64
import hashlib
import json

import pytest

from runtime.retrieval_tools import Ripgrep, AstGrep, MarkItDownTool
from runtime.context import prepare_run_context
from runtime.contracts import AgentRunRequest
from test_contract import project_file_payload


def value(chunk):
    assert chunk.state.value == "success", chunk.content[0].text
    return json.loads(chunk.content[0].text)


def test_real_ripgrep_and_ast_grep_locate_chinese_shot(tmp_path):
    file = tmp_path / "shot-023.json"
    file.write_text(json.dumps({"order_index": 23, "dialogue": "你退后，看我杀敌", "title": "救援"},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    async def verify():
        matches = value(await Ripgrep(tmp_path).call("杀敌", glob="*.json"))
        assert "shot-023.json" in matches["matches"] and "杀敌" in matches["matches"]
        matches = value(await AstGrep(tmp_path).call("shot-023.json", "json", pattern='"dialogue": $VALUE'))
        assert len(matches["matches"]) == 1
        assert "杀敌" in matches["matches"][0]["text"]
    asyncio.run(verify())


def test_retrieval_cannot_escape_workspace_or_use_url(tmp_path):
    async def verify():
        for tool, kwargs in [(Ripgrep(tmp_path), {"pattern": "secret", "path": "../"}),
                             (AstGrep(tmp_path), {"path": "../secret.json", "language": "json", "kind": "pair"}),
                             (MarkItDownTool(tmp_path), {"path": "https://example.com/private.pdf"})]:
            output = await tool.call(**kwargs)
            assert output.state.value == "error"
    asyncio.run(verify())


def test_real_markitdown_converts_docx_and_searches_result(tmp_path):
    # Minimal valid DOCX without depending on python-docx.
    import zipfile
    source = tmp_path / "reference.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>角色台词：不要走</w:t></w:r></w:p></w:body></w:document>')
    async def verify():
        converted = value(await MarkItDownTool(tmp_path).call("reference.docx"))
        assert "不要走" in converted["text"]
        found = value(await Ripgrep(tmp_path).call("不要走", path=converted["path"]))
        assert "不要走" in found["matches"]
    asyncio.run(verify())


def test_documents_are_materialized_without_inlining_their_bodies(tmp_path):
    payload = project_file_payload()
    data = b"PRIVATE_DOCUMENT_BODY"
    payload["documents"] = [{"id": "doc-1", "name": "notes.txt", "data": base64.b64encode(data).decode(),
                             "sha256": hashlib.sha256(data).hexdigest()}]
    payload["tool_mode"] = "retrieval"
    request = AgentRunRequest.model_validate(payload)
    context = prepare_run_context(tmp_path, request)
    assert (context.workspace / ".cineforge/documents/doc-1.txt").read_bytes() == data
    assert "PRIVATE_DOCUMENT_BODY" not in context.prompt
    assert "MarkItDown" in context.prompt
    request.documents[0].sha256 = "0" * 64
    with pytest.raises(ValueError):
        prepare_run_context(tmp_path, request)


@pytest.mark.parametrize("mode,writable", [("retrieval", False), ("workspace", True)])
def test_adapter_registers_real_tools_and_automatic_mode_has_no_writes(tmp_path, monkeypatch, mode, writable):
    from agentscope.agent import Agent
    from agentscope.message import AssistantMsg
    from runtime.adapter import AgentScopeAdapter
    from runtime.config import Settings
    captured = {}
    original = Agent.__init__
    def initialize(self, *args, **kwargs):
        captured["toolkit"] = kwargs["toolkit"]
        original(self, *args, **kwargs)
    async def reply(self, *args, **kwargs):
        toolkit = captured["toolkit"]
        for name in ("Ripgrep", "AstGrep", "MarkItDown", "Read"):
            assert await toolkit.get_tool(name) is not None
        assert (await toolkit.get_tool("Write") is not None) == writable
        assert (await toolkit.get_tool("Edit") is not None) == writable
        assert await toolkit.get_tool("Bash") is None
        yield AssistantMsg(name="CineForgeAgent", content='{"approved":true}')
    monkeypatch.setattr(Agent, "__init__", initialize)
    monkeypatch.setattr(Agent, "reply_stream", reply)
    request = AgentRunRequest.model_validate({**project_file_payload(), "tool_mode": mode})
    response = asyncio.run(AgentScopeAdapter(Settings(data_root=tmp_path)).run(request))
    assert response.final_response == '{"approved":true}'
