"""Fake Anthropic responses for offline tests: no key, no network."""

import copy
from types import SimpleNamespace as NS


def _usage():
    return NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=3)


def text_resp(text):
    return NS(
        content=[NS(type="thinking", thinking="", signature="sig"), NS(type="text", text=text)],
        stop_reason="end_turn",
        usage=_usage(),
    )


def tool_resp(*calls):
    blocks = [NS(type="thinking", thinking="", signature="sig")]
    blocks += [
        NS(type="tool_use", id=f"toolu_{i}", name=name, input=args) for i, (name, args) in enumerate(calls)
    ]
    return NS(content=blocks, stop_reason="tool_use", usage=_usage())


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.script.pop(0)
