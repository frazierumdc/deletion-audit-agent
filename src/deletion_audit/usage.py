"""Per-call model usage log (one JSON line per API call) and secret redaction."""

import json
import re
import threading
import time
import uuid
from pathlib import Path

from deletion_audit.paths import USAGE_LOG

_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def redact(text) -> str:
    """Remove anything that looks like an Anthropic API key from text bound for disk."""
    return _KEY_RE.sub("[REDACTED]", str(text))


def usage_fields(usage) -> dict:
    """Token counts from either an aisuite CompletionUsage or an anthropic Usage object.

    aisuite reports uncached input as prompt_tokens and cache reads as
    prompt_tokens_details.cached_tokens; it does not report cache writes.
    """
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": None}
    if hasattr(usage, "input_tokens"):  # anthropic SDK
        return {
            "prompt_tokens": usage.input_tokens or 0,
            "completion_tokens": usage.output_tokens or 0,
            "cached_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": usage.prompt_tokens or 0,
        "completion_tokens": usage.completion_tokens or 0,
        "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
        "cache_write_tokens": None,
    }


class UsageLog:
    """Thread-safe append-only JSONL writer. Records token counts and outcomes only.

    path=None keeps rows in memory only (offline runs, which make no API calls).
    """

    def __init__(self, path: Path | None = USAGE_LOG, run_id: str | None = None):
        self.path = Path(path) if path is not None else None
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self._lock = threading.Lock()

    def append(
        self,
        *,
        case_id: str,
        purpose: str,
        model: str,
        turn: int,
        usage,
        seconds: float,
        outcome: str,
        backend: str = "aisuite",
    ) -> dict:
        provider, _, model_name = model.partition(":") if ":" in model else ("anthropic", "", model)
        row = {
            "run_id": self.run_id,
            "case_id": case_id,
            "purpose": purpose,
            "backend": backend,
            "provider": provider,
            "model": model_name,
            "turn": turn,
            **usage_fields(usage),
            "seconds": round(seconds, 3),
            "outcome": redact(outcome),
        }
        if self.path is None:
            return row
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        return row


# --- usage report ------------------------------------------------------------------------------

# USD per million tokens. Cache writes are the 5-minute TTL rate (1.25x input).
PRICES = {"claude-opus-5-5": {"input": 4.00, "cache_read": 0.20, "cache_write": 5.00, "output": 20.00}}
TOKEN_FIELDS = ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens")


def load_usage(path: Path = USAGE_LOG, run_ids=None) -> list[dict]:
    rows = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if run_ids is None or row["run_id"] in run_ids:
                    rows.append(row)
    return rows


def cost(row_totals: dict, model: str) -> float | None:
    p = PRICES.get(model)
    if p is None:
        return None
    return (
        row_totals["prompt_tokens"] * p["input"]
        + row_totals["cached_tokens"] * p["cache_read"]
        + row_totals["cache_write_tokens"] * p["cache_write"]
        + row_totals["completion_tokens"] * p["output"]
    ) / 1e6


def aggregate(rows: list[dict]) -> dict:
    """(provider, model, purpose) -> {calls, prompt_tokens, cached_tokens, cache_write_tokens, completion_tokens}."""
    out = {}
    for r in rows:
        key = (r["provider"], r["model"], r["purpose"])
        t = out.setdefault(key, {"calls": 0, **{f: 0 for f in TOKEN_FIELDS}})
        t["calls"] += 1
        for f in TOKEN_FIELDS:
            t[f] += r.get(f) or 0
    return out


def _sum(totals: list[dict]) -> dict:
    return {k: sum(t[k] for t in totals) for k in ("calls", *TOKEN_FIELDS)}


def render_usage_report(run: dict, case_rows: list[dict], vision_rows: list[dict]) -> str:
    """Markdown usage report for one full agent run (run: the manifest main.py writes)."""
    n = run["cases"]
    groups = aggregate(case_rows)
    for key, t in aggregate(vision_rows).items():
        groups[(key[0], key[1], "vision (cached extraction)")] = t
    lines = [
        "# Usage Report",
        "",
        "Model usage of the agent run that produced the submitted `output.csv`. Every number comes from "
        "`runs/agent_usage.jsonl` (one line per API call) and is generated by "
        "`python -m deletion_audit.evaluation.usage_report`. Claude Code development usage is not included.",
        "",
        "## Run",
        "",
        f"- Command: `{run['command']}`",
        f"- Started / finished (UTC): {run['started']} / {run['finished']}",
        f"- Usage run id: `{run['run_id']}`",
        f"- Requests: {n} (`{run['requests']}`); output `{run['output']}` (sha256 `{run['output_sha256'][:16]}…`)",
        "- Engine per case: " + ", ".join(f"{k} {v}" for k, v in run["engines"].items()),
        f"- Backend: {run['backend']}; effort `{run['effort']}`; max_tokens {run['max_tokens']}",
        "",
        "The vision step ran before this run: its extractions (one per PNG/PDF evidence file) were "
        "cached, and this run reused them. Their calls are listed as `vision (cached extraction)` because "
        "their output feeds this run's answers; run ids: "
        + ", ".join(f"`{r}`" for r in sorted({r["run_id"] for r in vision_rows}))
        + ".",
        "",
        "## Tokens and cost by provider, model and purpose",
        "",
        "Input is split into uncached input, cache reads and cache writes; total = all input + output. "
        f"Averages and cost per request divide by the {n} requests.",
        "",
        "| Provider | Model | Purpose | Calls | Uncached input | Cache read | Cache write | Output | Total tokens "
        "| Avg tokens / request | Est. cost (USD) | Cost / request (USD) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def row(provider, model, purpose, t):
        total = sum(t[f] for f in TOKEN_FIELDS)
        c = cost(t, model)
        cs = f"{c:,.2f}" if c is not None else "n/a"
        cpr = f"{c / n:.4f}" if c is not None and n else "n/a"
        return (
            f"| {provider} | {model} | {purpose} | {t['calls']:,} | {t['prompt_tokens']:,} | {t['cached_tokens']:,} "
            f"| {t['cache_write_tokens']:,} | {t['completion_tokens']:,} | {total:,} | {total / n:,.0f} | {cs} | {cpr} |"
        )

    models = sorted({(k[0], k[1]) for k in groups})
    for provider, model in models:
        keys = sorted(k for k in groups if k[:2] == (provider, model))
        for k in keys:
            lines.append(row(provider, model, k[2], groups[k]))
        if len(keys) > 1:
            lines.append(row(provider, model, "**all purposes**", _sum([groups[k] for k in keys])))
    overall = _sum(list(groups.values()))
    total_cost = sum(c for (p, m, _), t in groups.items() if (c := cost(t, m)) is not None)
    total_tokens = sum(overall[f] for f in TOKEN_FIELDS)
    lines += [
        f"| **overall** | | | {overall['calls']:,} | {overall['prompt_tokens']:,} | {overall['cached_tokens']:,} "
        f"| {overall['cache_write_tokens']:,} | {overall['completion_tokens']:,} | {total_tokens:,} "
        f"| {total_tokens / n:,.0f} | {total_cost:,.2f} | {total_cost / n:.4f} |",
        "",
        "## Summary",
        "",
        f"- Model calls: {overall['calls']:,} ({overall['calls'] / n:.1f} per request)",
        f"- Input tokens: {overall['prompt_tokens'] + overall['cached_tokens'] + overall['cache_write_tokens']:,} "
        f"(uncached {overall['prompt_tokens']:,}, cache read {overall['cached_tokens']:,}, "
        f"cache write {overall['cache_write_tokens']:,}); output tokens: {overall['completion_tokens']:,} "
        "(includes thinking)",
        f"- Total tokens: {total_tokens:,}; average per request: {total_tokens / n:,.0f}",
        f"- Estimated cost: ${total_cost:,.2f} in total, ${total_cost / n:.4f} per request",
        "",
        "## Prices used",
        "",
        "| Model | Input | Cache read | Cache write (5 min) | Output |",
        "|---|---:|---:|---:|---:|",
    ]
    for model, p in PRICES.items():
        lines.append(
            f"| {model} | ${p['input']:.2f} | ${p['cache_read']:.2f} | ${p['cache_write']:.2f} "
            f"| ${p['output']:.2f} |"
        )
    lines += [
        "",
        "USD per million tokens, list prices. Costs are estimates from logged token counts, not invoices.",
        "Cache writes are counted from the Anthropic SDK's own usage object: aisuite's usage drops them, so "
        "`agent/llm.py` records the SDK usage for every call.",
        "",
    ]
    return "\n".join(lines)
