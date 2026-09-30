import asyncio

from agentscope.message import Base64Source, DataBlock, TextBlock, UserMsg

from runtime.relay_formatter import GrokRelayChatFormatter


def test_grok_formatter_removes_speaker_names_preserving_images():
    messages = asyncio.run(GrokRelayChatFormatter().format([
        UserMsg(name="User", content=[TextBlock(text="Describe the image"), DataBlock(
            name="reference.webp", source=Base64Source(data="aGVsbG8=", media_type="image/webp"),
        )]),
    ]))
    assert all("name" not in message for message in messages)
    assert messages[0]["content"][0]["text"] == "Describe the image"
    assert messages[0]["content"][1]["image_url"]["url"] == "data:image/webp;base64,aGVsbG8="


def test_grok_formatter_preserves_tool_names_and_call_ids(monkeypatch):
    from agentscope.formatter import OpenAIChatFormatter

    async def formatted(*args, **kwargs):
        return [{"role": "assistant", "name": "Agent", "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
        ]}, {"role": "tool", "name": "Read", "tool_call_id": "call1", "content": "result"}]

    monkeypatch.setattr(OpenAIChatFormatter, "format", formatted)
    messages = asyncio.run(GrokRelayChatFormatter().format([]))
    assert messages[0]["tool_calls"][0]["function"]["name"] == "Read"
    assert messages[1]["tool_call_id"] == "call1"
