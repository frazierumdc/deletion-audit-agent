"""Offline stand-in for Claude, so the agent pipeline runs with no API key.

The stand-in is not a model. For each case it replays a fixed script through the real tool loop:
it calls resolve_identity and search_evidence, then answers with the resources the rules engine
linked, in the agent's JSON answer format. Everything after the model (answer parsing,
evaluate_answer, reflection, fallback, state derivation and step rendering) runs unchanged.

What this exercises: the aisuite tool loop, the seven tools, the answer checks and the renderer.
What it does not exercise: Claude's judgment. For that, set ANTHROPIC_API_KEY and run live.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace as NS

from deletion_audit.decisions import CaseResult

OFFLINE_MODEL = "anthropic:claude-opus-5-5"  # only selects aisuite's provider; no request is sent
_CASE_RE = re.compile(r'"case_id": "([^"]+)"')
_EMAIL_RE = re.compile(r'"email": "([^"]+)"')


def scripted_answer(result: CaseResult) -> dict:
    """The agent's answer format, holding exactly the rules engine's resources."""
    return {
        "authorized": result.status != "NOT_APPLICABLE",
        "intent": "scripted offline answer (no model)",
        "subject_user_id": "",
        "resources": [
            {
                "resource_id": d.resource_id,
                "category": d.category,
                "evidence_id": d.evidence_id,
                "provenance": d.provenance,
                "why": "linked by the rules engine",
            }
            for d in result.decisions
        ],
        "gaps": "",
    }


def _usage() -> NS:
    return NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _tool(name: str, args: dict, n: int) -> NS:
    block = NS(type="tool_use", id=f"toolu_offline_{n}", name=name, input=args)
    return NS(content=[block], stop_reason="tool_use", usage=_usage())


def _text(text: str) -> NS:
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=_usage())


class ScriptedMessages:
    """Replaces the Anthropic SDK's messages.create inside aisuite. Thread-safe and stateless:
    the script step is read from the conversation it is sent."""

    def __init__(self, rules_results: dict[str, CaseResult]):
        self.rules_results = rules_results
        self.calls = 0
        self._lock = threading.Lock()

    def create(self, **kwargs) -> NS:
        with self._lock:
            self.calls += 1
        messages = kwargs["messages"]
        first = json.dumps(messages[0]["content"])
        case_id = _CASE_RE.search(first.replace('\\"', '"')).group(1)
        email = _EMAIL_RE.search(first.replace('\\"', '"')).group(1)
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if turns == 0:
            return _tool("resolve_identity", {"email": email}, 1)
        if turns == 1:
            return _tool("search_evidence", {"identifier": email}, 2)
        answer = scripted_answer(self.rules_results[case_id])
        return _text("Scripted offline answer.\n```json\n" + json.dumps(answer) + "\n```")


def offline_client(rules_results: dict[str, CaseResult]):
    """An aisuite client whose Anthropic calls are answered by ScriptedMessages (no network)."""
    from deletion_audit.agent.llm import make_client

    client = make_client(OFFLINE_MODEL)
    client.providers["anthropic"].client.messages.create = ScriptedMessages(rules_results).create
    return client
