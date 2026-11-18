"""Agent-engine checks, fully offline: tools, answer checks, the case pipeline with a scripted fake
model, the answer cache and replay, reflection, fallback, offline mode and the missing-key exit.

No API key and no network: the Anthropic client inside aisuite is replaced by a fake.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aisuite.utils.tools import Tools
from fakes import FakeMessages, text_resp, tool_resp

from deletion_audit.agent.audit_agent import (
    AgentRunFailed,
    AgentRunner,
    MissingApiKey,
    ReplayMiss,
    run_agent,
    system_prompt,
)
from deletion_audit.agent.evaluate import evaluate_answer, parse_answer
from deletion_audit.agent.llm import DEFAULT_MODEL, make_client
from deletion_audit.agent.tools import make_tools
from deletion_audit.context import AuditContext, CaseView
from deletion_audit.contract import serialize_steps
from deletion_audit.data import DATASET, load_requests
from deletion_audit.engine_rules import run_rules
from deletion_audit.paths import ROOT
from deletion_audit.usage import UsageLog

CTX = AuditContext.load()
SAMPLES = load_requests(DATASET / "sample_deletion_requests.csv")
POOL = list({r.case_id: r for r in load_requests() + SAMPLES}.values())
BY_ID = {r.case_id: r for r in SAMPLES}
RULES = {r.case_id: r for r in run_rules(CTX, SAMPLES, POOL, workers=1)}


def rules_answer(case_id: str) -> dict:
    """A well-formed agent answer holding exactly the rules engine's resources."""
    rr = RULES[case_id]
    return {
        "authorized": rr.status != "NOT_APPLICABLE",
        "intent": "delete the account",
        "subject_user_id": "",
        "resources": [
            {
                "resource_id": d.resource_id,
                "category": d.category,
                "evidence_id": d.evidence_id,
                "provenance": d.provenance,
                "why": "test",
            }
            for d in rr.decisions
        ],
        "gaps": "",
    }


def fenced(answer) -> str:
    return "Done.\n```json\n" + json.dumps(answer) + "\n```"


class Scripted:
    """A fake aisuite client whose model replies follow a script; one per test."""

    def __init__(self, script):
        self.client = make_client(DEFAULT_MODEL)
        self.fake = FakeMessages(script)
        self.client.providers["anthropic"].client.messages.create = self.fake.create


def view(case_id="case_03"):
    v = CaseView(CTX, BY_ID[case_id])
    v.timing_candidates = RULES[case_id].timing_candidates
    return v


class AgentTools(unittest.TestCase):
    def test_schemas_build_from_docstrings(self):
        specs = {t["function"]["name"]: t["function"] for t in Tools(make_tools(view())).tools()}
        self.assertEqual(
            sorted(specs),
            [
                "deletion_rules",
                "list_case_activity",
                "read_evidence",
                "resolve_identity",
                "resource_state",
                "search_evidence",
                "timing_candidates",
            ],
        )
        for name, spec in specs.items():
            self.assertTrue(spec["description"], name)
            for pname, p in spec["parameters"].get("properties", {}).items():
                self.assertTrue(p.get("description"), f"{name}.{pname}")
        self.assertEqual(specs["read_evidence"]["parameters"]["required"], ["evidence_id"])

    def test_tools_return_dicts_and_never_raise(self):
        tools = {t.__name__: t for t in make_tools(view())}
        self.assertIn("error", tools["search_evidence"]("not an identifier"))
        self.assertIn("error", tools["read_evidence"]("evidence_9999"))
        self.assertIn("error", tools["resource_state"]("nonsense"))
        self.assertIn("error", tools["read_evidence"](None))
        ident = tools["resolve_identity"](BY_ID["case_03"].email)
        self.assertTrue(ident["user_id"])
        json.dumps([tools["timing_candidates"](), tools["deletion_rules"](), tools["list_case_activity"]()])

    def test_tools_hide_evidence_after_audit_at(self):
        v = view("case_03")
        later = [f for f in CTX.manifest.values() if f.captured_at > v.audit_at]
        self.assertTrue(later)
        tools = {t.__name__: t for t in make_tools(v)}
        self.assertIn("error", tools["read_evidence"](later[0].file_id))
        for tok in sorted(v.subject.identifiers):
            for o in tools["search_evidence"](tok)["observations"]:
                self.assertLessEqual(o["captured_at"], v.audit_at)


class AnswerChecks(unittest.TestCase):
    def test_parse_answer(self):
        self.assertEqual(parse_answer('x ```json\n{"a": 1}\n``` y ```json\n{"a": 2}\n```')[0], {"a": 2})
        self.assertEqual(parse_answer('thinking... {"a": 3}')[0], {"a": 3})
        self.assertIsNone(parse_answer("no json here")[0])
        self.assertIsNone(parse_answer("")[0])

    def test_rules_answer_passes(self):
        for cid in ("case_03", "case_01", "case_06"):
            ok, report = evaluate_answer(view(cid), rules_answer(cid))
            self.assertTrue(ok, f"{cid}: {report}")

    def test_bad_answers_are_reported(self):
        v = view("case_03")
        later = next(f.file_id for f in CTX.manifest.values() if f.captured_at > v.audit_at)
        base = rules_answer("case_03")
        flow = next((r for r in base["resources"] if r["category"] == "DATA_FLOW"), None)
        cases = {
            "not a resource id": [
                {
                    "resource_id": "account_99999",
                    "category": "USER_PROFILE",
                    "provenance": ["evidence:evidence_01"],
                }
            ],
            "provenance": [dict(base["resources"][0], provenance=["guess:account_0420"])],
            "after audit_at": [dict(base["resources"][0], evidence_id=later)],
            "needs at least one provenance": [dict(base["resources"][0], provenance=[])],
        }
        if flow:
            cases["use category DATA_FLOW"] = [dict(flow, category="USER_PROFILE")]
        for expected, resources in cases.items():
            ok, report = evaluate_answer(v, dict(base, resources=resources))
            self.assertFalse(ok, expected)
            self.assertIn(expected, report)
        ok, report = evaluate_answer(v, dict(base, authorized=False))
        self.assertIn("resources: []", report)
        self.assertFalse(evaluate_answer(v, None, "the final message is empty")[0])

    def test_prompt_is_case_independent_and_has_rules(self):
        p = system_prompt(CTX.rules)
        self.assertNotIn("{{", p)
        self.assertIn("| rule_15 | ACCOUNT_TOMBSTONE |", p)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "agent"
        self.usage = UsageLog(Path(self.tmp.name) / "usage.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, script, replay=False):
        s = Scripted(script) if script is not None else None
        r = AgentRunner(
            CTX, replay=replay, usage_log=self.usage, client=s.client if s else None, cache_dir=self.cache
        )
        return r, s

    def usage_rows(self):
        p = self.usage.path
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_case_03_end_to_end_then_replay(self):
        req = BY_ID["case_03"]
        script = [
            tool_resp(("resolve_identity", {"email": req.email})),
            tool_resp(("search_evidence", {"identifier": req.email})),
            text_resp(fenced(rules_answer("case_03"))),
        ]
        runner, s = self.runner(script)
        res = runner.audit(req, RULES["case_03"])
        self.assertEqual(res.engine, "agent")
        self.assertEqual(
            (res.status, serialize_steps(res.steps)),
            (RULES["case_03"].status, serialize_steps(RULES["case_03"].steps)),
        )
        self.assertEqual(len(s.fake.calls), 3)
        for sent in s.fake.calls:  # the system prompt is on every turn and identical
            self.assertEqual(sent["system"][0]["text"], runner.prompt)
        rows = self.usage_rows()
        self.assertEqual(
            [(r["case_id"], r["purpose"], r["turn"]) for r in rows],
            [("case_03", "investigate", 1), ("case_03", "investigate", 2), ("case_03", "investigate", 3)],
        )
        # Replay: no client, no key, the same row from the cache, and no new usage lines.
        replayer, _ = self.runner(None, replay=True)
        again = replayer.audit(req, RULES["case_03"])
        self.assertEqual(serialize_steps(again.steps), serialize_steps(res.steps))
        self.assertEqual(len(self.usage_rows()), 3)
        with self.assertRaises(ReplayMiss):
            replayer.audit(BY_ID["case_01"], RULES["case_01"])

    def test_reflection_then_success(self):
        bad = dict(rules_answer("case_01"), resources=[])
        runner, s = self.runner([text_resp(fenced(bad)), text_resp(fenced(rules_answer("case_01")))])
        res = runner.audit(BY_ID["case_01"], RULES["case_01"])
        self.assertEqual(res.engine, "agent-retry")
        last_user = [m for m in s.fake.calls[1]["messages"] if m["role"] == "user"][-1]
        self.assertIn("Fix these problems", json.dumps(last_user))
        self.assertEqual([r["purpose"] for r in self.usage_rows()], ["investigate", "reflect"])

    def test_two_failures_fall_back_to_rules(self):
        runner, _ = self.runner([text_resp("I could not decide."), text_resp("still nothing")])
        res = runner.audit(BY_ID["case_01"], RULES["case_01"])
        self.assertEqual(res.engine, "rules-fallback")
        self.assertEqual(serialize_steps(res.steps), serialize_steps(RULES["case_01"].steps))

    def test_not_authorized(self):
        req = replace(BY_ID["case_01"], request_text="Please send me a copy of my data.")
        answer = {
            "authorized": False,
            "intent": "access request",
            "subject_user_id": "",
            "resources": [],
            "gaps": "",
        }
        runner, _ = self.runner([text_resp(fenced(answer))])
        res = runner.audit(req, RULES["case_01"])
        self.assertEqual((res.status, res.steps, res.engine), ("NOT_APPLICABLE", [], "agent"))

    def test_api_error_needs_allow_fallback(self):
        s = Scripted([])  # an empty script: the fake raises IndexError, standing in for an API error
        with self.assertRaises(AgentRunFailed):
            run_agent(
                CTX,
                [BY_ID["case_01"]],
                POOL,
                workers=1,
                client=s.client,
                usage_log=self.usage,
                cache_dir=self.cache,
            )
        s = Scripted([])
        res = run_agent(
            CTX,
            [BY_ID["case_01"]],
            POOL,
            workers=1,
            client=s.client,
            usage_log=self.usage,
            allow_fallback=True,
            cache_dir=self.cache,
        )
        self.assertEqual(res[0].engine, "rules-fallback")

    def test_missing_key(self):
        with self.assertRaises(MissingApiKey):
            run_agent(CTX, [BY_ID["case_01"]], POOL, workers=1)

    def test_offline_mode_runs_the_real_pipeline(self):
        res = run_agent(CTX, SAMPLES, POOL, workers=2, offline=True, cache_dir=self.cache)
        self.assertEqual({r.engine for r in res}, {"agent"})
        for r in res:
            self.assertEqual(serialize_steps(r.steps), serialize_steps(RULES[r.case_id].steps), r.case_id)
        self.assertTrue(any(self.cache.iterdir()))


class UsageReport(unittest.TestCase):
    def test_totals_costs_and_per_request(self):
        from deletion_audit.usage import render_usage_report

        row = {
            "run_id": "r1",
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "prompt_tokens": 1000,
            "cached_tokens": 10000,
            "cache_write_tokens": 2000,
            "completion_tokens": 500,
        }
        run = {
            "run_id": "r1",
            "started": "s",
            "finished": "f",
            "command": "python -m deletion_audit --engine agent",
            "requests": "data/synthetic/deletion_requests.csv",
            "output": "output.csv",
            "output_sha256": "ab" * 32,
            "cases": 2,
            "engines": {"agent": 2},
            "model": "anthropic:claude-opus-5-5",
            "backend": "b",
            "effort": "medium",
            "max_tokens": 16000,
        }
        md = render_usage_report(
            run,
            [dict(row, purpose="investigate")] * 3 + [dict(row, purpose="reflect")],
            [dict(row, run_id="v1", purpose="vision")],
        )
        # one call costs (1000*4 + 10000*0.2 + 2000*5 + 500*20) / 1e6 = 0.026; 5 calls = 0.13, / 2 requests
        self.assertIn(
            "| **overall** | | | 5 | 5,000 | 50,000 | 10,000 | 2,500 | 67,500 | 33,750 | 0.13 | 0.0650 |", md
        )
        self.assertIn("| anthropic | claude-opus-5-5 | vision (cached extraction) | 1 |", md)
        self.assertIn("`v1`", md)
        self.assertNotIn("sk-ant", md)


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, *args):
        env = dict(os.environ, ANTHROPIC_API_KEY="")  # empty: .env cannot fill it in (override=False)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
        env["AUDIT_CACHE_DIR"] = self.cache_dir
        return subprocess.run([sys.executable, "-m", "deletion_audit", *args], capture_output=True,
                              text=True, env=env, timeout=600)  # fmt: skip

    def test_missing_key_exits_non_zero_unless_allow_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            reqs = str(DATASET / "sample_deletion_requests.csv")
            p = self.run_main("--engine", "agent", "--requests", reqs, "--output", str(out))
            self.assertEqual(p.returncode, 2)
            self.assertIn("ANTHROPIC_API_KEY is not set", p.stderr)
            self.assertFalse(out.exists())
            p = self.run_main(
                "--engine", "agent", "--allow-fallback", "--requests", reqs, "--output", str(out)
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(out.read_text().splitlines()), 9)
            self.assertNotIn("sk-ant", p.stdout + p.stderr)

    def test_offline_and_rules_runs_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "rules.csv", Path(tmp) / "offline.csv"
            self.assertEqual(self.run_main("--engine", "rules", "--output", str(a)).returncode, 0)
            p = self.run_main("--engine", "agent", "--offline", "--output", str(b))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("offline: scripted model", p.stdout)
            self.assertEqual(a.read_bytes(), b.read_bytes())
