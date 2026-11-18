"""Offline checks of the aisuite 0.2.0 behaviour the agent relies on.

No API key, no network: the Anthropic client inside aisuite is replaced by a fake
that records each request. If a future aisuite changes any of this, a test fails.

"""

import unittest
from types import SimpleNamespace as NS

from fakes import FakeMessages, text_resp, tool_resp

from deletion_audit.agent.llm import DEFAULT_MODEL, make_client, run_tools

SYSTEM = "You are a test system prompt."
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]


def lookup_code(name: str) -> dict:
    """Look up the code for a name.

    Args:
        name: The name to look up.
    """
    return {"code": f"code-for-{name}"}


def broken_tool(name: str) -> dict:
    """A tool that always raises.

    Args:
        name: Ignored.
    """
    raise RuntimeError("boom")


def fake_client(script):
    client = make_client(DEFAULT_MODEL)
    fake = FakeMessages(script)
    client.providers["anthropic"].client.messages.create = fake.create
    return client, fake


class AisuiteContract(unittest.TestCase):
    def test_run_tools_keeps_system_prompt_on_every_turn(self):
        client, fake = fake_client(
            [
                tool_resp(("lookup_code", {"name": "a"})),
                tool_resp(("lookup_code", {"name": "b"})),
                text_resp("done"),
            ]
        )
        calls = []
        text, transcript = run_tools(
            client,
            DEFAULT_MODEL,
            SYSTEM,
            "go",
            [lookup_code],
            on_call=lambda turn, usage, s: calls.append((turn, usage)),
        )
        self.assertEqual(text, "done")
        self.assertEqual(len(fake.calls), 3)
        for sent in fake.calls:
            self.assertEqual(sent["system"], SYSTEM_BLOCKS)  # blocks pass through unchanged
        self.assertEqual([t for t, _ in calls], [1, 2, 3])
        self.assertEqual(transcript[0]["role"], "system")

    def test_builtin_loop_drops_system_prompt_after_turn_one(self):
        client, fake = fake_client([tool_resp(("lookup_code", {"name": "a"})), text_resp("done")])
        client.chat.completions.create(
            model=DEFAULT_MODEL,
            tools=[lookup_code],
            max_turns=3,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": "go"}],
        )
        self.assertEqual(fake.calls[0]["system"], SYSTEM)
        self.assertEqual(fake.calls[1]["system"], [])

    def test_only_first_parallel_tool_call_kept(self):
        client, _ = fake_client([tool_resp(("lookup_code", {"name": "a"}), ("lookup_code", {"name": "b"}))])
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL, tools=[lookup_code], messages=[{"role": "user", "content": "go"}]
        )
        self.assertEqual(len(resp.choices[0].message.tool_calls), 1)

    def test_thinking_blocks_not_sent_back(self):
        client, fake = fake_client([tool_resp(("lookup_code", {"name": "a"})), text_resp("done")])
        run_tools(client, DEFAULT_MODEL, SYSTEM, "go", [lookup_code])
        assistant = [m for m in fake.calls[1]["messages"] if m["role"] == "assistant"][0]
        self.assertEqual([b["type"] for b in assistant["content"]], ["tool_use"])

    def test_tool_errors(self):
        client, _ = fake_client([tool_resp(("broken_tool", {"name": "a"})), text_resp("done")])
        with self.assertRaises(RuntimeError):
            client.chat.completions.create(
                model=DEFAULT_MODEL,
                tools=[broken_tool],
                max_turns=3,
                messages=[{"role": "user", "content": "go"}],
            )
        # run_tools turns the same failure, and an unknown tool name, into an error result.
        client, fake = fake_client(
            [tool_resp(("broken_tool", {"name": "a"})), tool_resp(("no_such_tool", {})), text_resp("done")]
        )
        text, _ = run_tools(client, DEFAULT_MODEL, SYSTEM, "go", [broken_tool])
        self.assertEqual(text, "done")
        results = [
            b
            for m in fake.calls[2]["messages"]
            for b in (m["content"] if isinstance(m["content"], list) else [])
            if b.get("type") == "tool_result"
        ]
        self.assertIn("RuntimeError: boom", results[0]["content"])
        self.assertIn("KeyError", results[1]["content"])

    def test_kwargs_forwarded_unchanged(self):
        client, fake = fake_client([text_resp("done")])
        run_tools(client, DEFAULT_MODEL, SYSTEM, "go", [lookup_code])
        sent = fake.calls[0]
        self.assertEqual(sent["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(sent["output_config"], {"effort": "medium"})
        self.assertEqual(sent["cache_control"], {"type": "ephemeral"})  # automatic caching of the transcript
        self.assertEqual(sent["max_tokens"], 16000)
        self.assertEqual(sent["model"], "claude-opus-5-5")
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, sent)
        self.assertEqual(sent["tools"][0]["name"], "lookup_code")
        self.assertEqual(
            sent["tools"][0]["input_schema"]["properties"]["name"]["description"], "The name to look up."
        )

    def test_image_part_converted_and_pdf_rejected(self):
        client, fake = fake_client([text_resp("ok")])
        client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
                }
            ],
        )
        block = fake.calls[0]["messages"][0]["content"][0]
        self.assertEqual(
            block, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        )
        with self.assertRaisesRegex(ValueError, "Unsupported content part type"):
            client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
                            }
                        ],
                    }
                ],
            )

    def test_usage_fields(self):
        from deletion_audit.usage import usage_fields

        client, _ = fake_client([text_resp("ok")])
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL, messages=[{"role": "user", "content": "go"}]
        )
        self.assertEqual(
            usage_fields(resp.usage),
            {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 3, "cache_write_tokens": None},
        )

    def test_sdk_usage_recorded_with_cache_writes(self):
        # aisuite's usage has no cache writes; make_client's wrapper keeps the SDK usage, which does.
        from unittest import mock

        from anthropic.resources.messages import Messages

        from deletion_audit.usage import usage_fields

        resp = NS(
            content=[NS(type="text", text="ok")],
            stop_reason="end_turn",
            usage=NS(
                input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=7
            ),
        )
        with mock.patch.object(Messages, "create", lambda self, **kw: resp):
            client = make_client(DEFAULT_MODEL)
            seen = []
            run_tools(
                client, DEFAULT_MODEL, SYSTEM, "go", [lookup_code], on_call=lambda t, u, s: seen.append(u)
            )
        self.assertEqual(
            usage_fields(seen[0]),
            {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 3, "cache_write_tokens": 7},
        )
