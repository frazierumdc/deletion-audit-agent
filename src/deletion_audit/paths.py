"""Repository paths, resolved relative to this file so they work from any checkout.

The data directory defaults to the committed synthetic dataset. Point AUDIT_DATA_DIR at another
folder with the same layout to audit different data.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parents[1]
DATASET = Path(os.environ.get("AUDIT_DATA_DIR") or ROOT / "data" / "synthetic").resolve()
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CACHE_DIR = Path(os.environ.get("AUDIT_CACHE_DIR") or ROOT / ".cache").resolve()
RUNS_DIR = ROOT / "runs"  # usage log and run manifests (git-ignored)
USAGE_LOG = RUNS_DIR / "agent_usage.jsonl"
OUTPUT_CSV = ROOT / "out" / "output.csv"
