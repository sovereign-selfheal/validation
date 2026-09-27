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


class _Raw:
    def __init__(self, data):
        self.data = data


class _Pod:
    def __init__(self, name):
        self.metadata = type("M", (), {"name": name})()
        self.status = type("S", (), {"phase": "Running"})()


class _FakeCoreV1:
    """Two LiteLLM pods; the logs hold non-ASCII bytes like the LiteLLM start banner."""

    def __init__(self, logs):
        self.logs = logs

    def list_namespaced_pod(self, namespace, label_selector):
        return type("L", (), {"items": [_Pod(name) for name in self.logs]})()

    def read_namespaced_pod_log(self, name, namespace, _preload_content=True, **kwargs):
        assert _preload_content is False  # the preloaded text is the repr of the bytes
        return _Raw(self.logs[name].encode("utf-8"))


def test_read_decisions_merges_pods_by_time_and_decodes_utf8():
    from policy_router import read_decisions

    first = dict(DECISION, trace_id="aaa")
    second = dict(DECISION, trace_id="bbb", routed_to="sota-smart")
    core = _FakeCoreV1({
        "litellm-1": f"2026-09-27T10:00:00Z ██╗ banner\n2026-09-27T10:00:05Z [policy-router] {second}\n",
        "litellm-2": f"2026-09-27T10:00:01Z [policy-router] {first}\n",
    })
    got = read_decisions(core, "maas-routing", "app=litellm", 60, "litellm")
    assert [d["trace_id"] for d in got] == ["aaa", "bbb"]
    assert [d["_pod"] for d in got] == ["litellm-2", "litellm-1"]
