"""Normalize incomplete Responses relay events before AgentScope parses them."""
from types import SimpleNamespace


class ResponseToolStream:
    def __init__(self, source):
        self.source = source

    async def __aenter__(self):
        self.stream = await self.source.__aenter__()
        self.iterator = self._events().__aiter__()
        return self

    async def __aexit__(self, *args):
        return await self.source.__aexit__(*args)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.iterator.__anext__()

    async def _events(self):
        items, indices, emitted = {}, {}, {}

        def register(item, index):
            identity = getattr(item, "id", None) or getattr(item, "call_id", None)
            if not identity or not getattr(item, "call_id", None) or not getattr(item, "name", None):
                raise ValueError("Responses 工具调用缺少标识或名称")
            fresh = identity not in items
            normalized = SimpleNamespace(id=identity, call_id=item.call_id, name=item.name,
                                         type="function_call")
            items[identity] = normalized
            if index is not None:
                indices[index] = identity
            return identity, normalized, fresh

        def complete(item, index):
            identity, normalized, fresh = register(item, index)
            events = []
            if fresh:
                events.append(SimpleNamespace(type="response.output_item.added", item=normalized,
                                              output_index=index))
            full = getattr(item, "arguments", None)
            prior = emitted.get(identity, "")
            if full is None or (not full and prior):
                return events
            full = full or "{}"
            if not full.startswith(prior):
                raise ValueError("Responses 工具调用的完整参数与增量不一致")
            if full != prior:
                events.append(SimpleNamespace(type="response.function_call_arguments.delta",
                                              item_id=identity, delta=full[len(prior):], output_index=index))
                emitted[identity] = full
            return events

        async for event in self.stream:
            kind = event.type
            item = getattr(event, "item", None)
            if kind == "response.output_item.added" and getattr(item, "type", None) == "function_call":
                _, normalized, _ = register(item, getattr(event, "output_index", None))
                yield SimpleNamespace(type=kind, item=normalized, output_index=getattr(event, "output_index", None))
                continue
            if kind == "response.function_call_arguments.delta":
                identity = getattr(event, "item_id", None)
                if identity not in items:
                    identity = indices.get(getattr(event, "output_index", None))
                if identity is None and len(items) == 1:
                    identity = next(iter(items))
                if identity not in items:
                    # Completed items carry authoritative full arguments. Do not
                    # assign an ambiguous delta to a different parallel tool.
                    continue
                delta = getattr(event, "delta", "") or ""
                emitted[identity] = emitted.get(identity, "") + delta
                yield SimpleNamespace(type=kind, item_id=identity, delta=delta)
                continue
            if kind == "response.output_item.done" and getattr(item, "type", None) == "function_call":
                for repaired in complete(item, getattr(event, "output_index", None)):
                    yield repaired
            if kind == "response.completed":
                for index, output in enumerate(getattr(event.response, "output", []) or []):
                    if getattr(output, "type", None) == "function_call":
                        for repaired in complete(output, index):
                            yield repaired
            yield event
