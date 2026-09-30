"""Message envelope compatibility for Grok OpenAI-compatible relays."""

from agentscope.formatter import OpenAIChatFormatter


class GrokRelayChatFormatter(OpenAIChatFormatter):
    async def format(self, *args, **kwargs):
        messages = await super().format(*args, **kwargs)
        # This relay rejects optional speaker names, not function/tool names.
        return [{key: value for key, value in message.items() if key != "name"} for message in messages]
