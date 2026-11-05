"""A stand-in for ``anthropic.Anthropic`` that records requests and returns scripted Messages."""

import json
from types import SimpleNamespace


def text_block(obj) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=obj if isinstance(obj, str) else json.dumps(obj))


def tool_use(name: str, args: dict, idx: int = 0) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{name}_{idx}", name=name, input=args)


def message(*blocks, stop_reason: str | None = None) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class FakeClaude:
    """``turns`` is a list of messages, or a callable ``(request_kwargs) -> message``."""

    def __init__(self, turns):
        self.requests: list[dict] = []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.requests.append(kw)
                return turns(kw) if callable(turns) else turns.pop(0)

        self.beta = SimpleNamespace(messages=_Messages())
