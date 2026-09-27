"""Unit tests of the log parser shared by the module policy_decisions and the harness."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/validate_router/module_utils"))

from policy_router import decision_key, parse_decisions  # noqa: E402

# Same shape as the dict printed by router/litellm/policy_hook_chain.py (router v0.5.0).
DECISION = {
    "policy": "chain",
    "requested": "auto",
    "routed_to": "local-fast",
    "decided_by": "privacy",
    "chain": ["efficiency: 42 words -> SOTA", "privacy: score 0.91 >= 0.6 (IT_FISCAL_CODE@1.00:0.90) -> LOCAL"],
    "reason": "privacy: score 0.91 >= 0.6 (IT_FISCAL_CODE@1.00:0.90) -> LOCAL",
    "team": "research",
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
}


def test_parses_a_line_with_timestamp():
    text = f"2026-09-27T10:00:01.123456789Z [policy-router] {DECISION}\n"
    [d] = parse_decisions(text)
    assert d["routed_to"] == "local-fast"
    assert d["decided_by"] == "privacy"
    assert d["_ts"] == "2026-09-27T10:00:01.123456789Z"


def test_parses_a_line_without_timestamp():
    [d] = parse_decisions(f"[policy-router] {DECISION}")
    assert d["team"] == "research"
    assert "_ts" not in d


def test_ignores_other_router_lines_and_noise():
    text = "\n".join([
        "2026-09-27T10:00:00Z INFO: LiteLLM proxy started",
        "2026-09-27T10:00:00Z [policy-router] metrics on :9091/metrics",
        "2026-09-27T10:00:00Z [policy-router] SOTA budget: 120/2000000 tokens used",
        "2026-09-27T10:00:00Z [policy-router] {broken",
        "2026-09-27T10:00:00Z [policy-router] {'no': 'routed_to key'}",
    ])
    assert parse_decisions(text) == []


def test_fail_closed_decision_is_parsed():
    fail_closed = {"policy": "chain", "routed_to": "local-fast", "reason": "chain: fail-closed on error (x) -> LOCAL"}
    [d] = parse_decisions(f"[policy-router] {fail_closed}")
    assert d["reason"].startswith("chain: fail-closed")


def test_key_is_the_trace_id_when_present():
    assert decision_key(DECISION) == DECISION["trace_id"]


def test_key_without_trace_id_ignores_the_timestamp():
    a = {k: v for k, v in DECISION.items() if k != "trace_id"}
    b = dict(a, _ts="2026-09-27T10:00:05Z")
    assert decision_key(a) == decision_key(b)
    assert decision_key(a) != decision_key(dict(a, reason="other"))
