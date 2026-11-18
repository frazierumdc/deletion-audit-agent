"""Offline guarantees for every test: no API key, and any request that escapes a fake goes nowhere."""

import os

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
