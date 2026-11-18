"""Model calls for the agent engine.

run_tools() is my own tool loop over aisuite. It never passes
max_turns: aisuite's built-in loop drops the system prompt after turn 1,
keeps only the first parallel tool call, and doesn't send thinking blocks back.
media_call() talks to the Anthropic SDK directly, because aisuite 0.2.0
can't send PDF document blocks.

Caching: an explicit breakpoint on the system prompt (shared by every case) plus top-level
automatic caching, which moves a breakpoint to the end of the growing transcript each turn.
Usage: aisuite's usage drops cache writes, so make_client() records the SDK's own usage object
for each call (per thread) and run_tools() reports that one when it is available.
"""

import base64
import json
import threading
import time
from pathlib import Path

import aisuite
import anthropic

DEFAULT_MODEL = "anthropic:claude-opus-5-5"
MAX_TOKENS = 16000  # Opus 5.5 thinking counts toward max_tokens
EFFORT = "medium"  # set explicitly so a change of API default can't change cost
RETRY_DELAYS = (2, 4, 8)  # seconds between the 4 tries for 429 / 5xx


class TurnLimitExceeded(RuntimeError):
    pass


_RAW = threading.local()  # .usage: the SDK usage of this thread's latest call


def make_client(model: str = DEFAULT_MODEL) -> aisuite.Client:
    """aisuite client with its provider initialised now, since lazy init isn't thread-safe.

    The provider's SDK call is wrapped to keep the full usage (including cache writes).
    """
    client = aisuite.Client()
    client.chat.completions._resolve_provider(model)
    sdk = getattr(client.providers.get(model.partition(":")[0]), "client", None)
    if sdk is not None:
        create = sdk.messages.create

        def recording_create(*args, **kwargs):
            resp = create(*args, **kwargs)
            _RAW.usage = getattr(resp, "usage", None)
            return resp

        sdk.messages.create = recording_create
    return client


def _with_retries(fn):
    """Retry 429 and 5xx with backoff. Never retry a 400: the caller logs it and falls back."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return fn()
        except anthropic.APIStatusError as exc:
            if delay is None or not (exc.status_code == 429 or exc.status_code >= 500):
                raise
        time.sleep(delay)


def run_tools(client, model, system_prompt, user_content, tools, max_turns=12, on_call=None, transcript=None):
    """Run the tool loop until the model answers without a tool call.

    Returns (final_text, transcript). Pass the returned transcript back with a new
    user_content to continue the conversation (reflection); history is only appended to,
    so the cached prefix stays valid. on_call(turn, usage, seconds) is called once per
    API call.
    """
    by_name = {t.__name__: t for t in tools}
    if transcript is None:
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            }
        ]
    else:
        messages = list(transcript)
    messages.append({"role": "user", "content": user_content})
    for turn in range(1, max_turns + 1):
        started = time.monotonic()
        _RAW.usage = None
        # aisuite pops the system message off the list it is given, so send a fresh copy.
        resp = _with_retries(
            lambda: client.chat.completions.create(
                model=model,
                messages=list(messages),
                tools=tools,
                max_tokens=MAX_TOKENS,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
            )
        )
        if on_call:
            on_call(turn, _RAW.usage or resp.usage, time.monotonic() - started)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", messages + [msg]
        messages.append(msg)
        for tc in msg.tool_calls:
            try:
                result = by_name[tc.function.name](**json.loads(tc.function.arguments))
            except Exception as exc:  # a tool failure must never kill the case
                result = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result),
                }
            )
    raise TurnLimitExceeded(max_turns)


def media_block(path: Path) -> dict:
    """Native Anthropic content block for a PNG or PDF file."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    if suffix == ".pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    raise ValueError(f"unsupported media type: {suffix}")


def media_call(path: Path, system_prompt: str, model: str = DEFAULT_MODEL, sdk_client=None, followups=()):
    """One vision call: only the file block goes in the user turn (no name or path).

    followups: further messages after the file turn (the vision retry sends the first reply and
    the error list). Returns (text, usage, stop_reason). usage is the SDK's, which also reports
    cache writes.
    """
    sdk_client = sdk_client or anthropic.Anthropic()
    model_name = model.split(":", 1)[-1]
    messages = [{"role": "user", "content": [media_block(path)]}, *followups]
    resp = _with_retries(
        lambda: sdk_client.messages.create(
            model=model_name,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": EFFORT},
        )
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage, resp.stop_reason
