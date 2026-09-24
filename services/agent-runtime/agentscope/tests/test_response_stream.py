import asyncio
from types import SimpleNamespace as NS

from runtime.response_stream import ResponseToolStream


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            yield event


def item(identity="fc_1", call="call_1", arguments='{"file_path":"token.json"}'):
    return NS(id=identity, call_id=call, type="function_call", name="Read", arguments=arguments)


def normalized(events):
    source = Stream(events)

    async def run():
        async with ResponseToolStream(source) as stream:
            return [event async for event in stream]

    result = asyncio.run(run())
    assert source.closed
    return result


def calls(events):
    output = {}
    for event in events:
        if event.type == "response.function_call_arguments.delta":
            output[event.item_id] = output.get(event.item_id, "") + event.delta
    return output


def test_missing_item_id_is_resolved_without_duplicating_completed_arguments():
    target = item()
    output = normalized([
        NS(type="response.output_item.added", item=target, output_index=1),
        NS(type="response.function_call_arguments.delta", item_id=None, output_index=1,
           delta=target.arguments),
        NS(type="response.output_item.done", item=target, output_index=1),
        NS(type="response.completed", response=NS(output=[target])),
    ])
    assert calls(output) == {"fc_1": target.arguments}


def test_done_only_tool_is_reconstructed_before_completed():
    target = item()
    output = normalized([NS(type="response.completed", response=NS(output=[target]))])
    assert [event.type for event in output] == [
        "response.output_item.added", "response.function_call_arguments.delta", "response.completed"]
    assert output[0].item.call_id == "call_1"
    assert calls(output) == {"fc_1": target.arguments}


def test_parallel_tools_use_output_index_and_ambiguous_deltas_use_full_items():
    first, second = item(), item("fc_2", "call_2", '{"file_path":"second.json"}')
    output = normalized([
        NS(type="response.output_item.added", item=first, output_index=1),
        NS(type="response.output_item.added", item=second, output_index=2),
        NS(type="response.function_call_arguments.delta", item_id=None, output_index=2,
           delta=second.arguments),
        NS(type="response.function_call_arguments.delta", item_id=None, delta="ambiguous"),
        NS(type="response.completed", response=NS(output=[first, second])),
    ])
    assert calls(output) == {"fc_1": first.arguments, "fc_2": second.arguments}


def test_standard_stream_passes_through_and_partial_arguments_are_completed():
    target = item()
    output = normalized([
        NS(type="response.output_item.added", item=target, output_index=1),
        NS(type="response.function_call_arguments.delta", item_id="fc_1", delta=target.arguments[:8]),
        NS(type="response.output_item.done", item=target, output_index=1),
        NS(type="response.completed", response=NS(output=[target])),
    ])
    assert calls(output) == {"fc_1": target.arguments}
