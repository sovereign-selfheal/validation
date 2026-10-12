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



def _without_trace(decision):
    return {k: v for k, v in decision.items() if k != "trace_id"}


# The fail-closed decision of the router has no trace id, even with tracing on.
FAIL_CLOSED = {"policy": "chain", "routed_to": "local-fast", "reason": "chain: fail-closed on error (x) -> LOCAL"}


def test_new_traceparent_is_w3c_and_sampled():
    from policy_router import new_traceparent

    header, trace_id = new_traceparent()
    version, tid, span_id, flags = header.split("-")
    assert (version, tid, flags) == ("00", trace_id, "01")
    assert len(trace_id) == 32 and len(span_id) == 16
    assert int(trace_id, 16) and int(span_id, 16)
    assert new_traceparent()[1] != trace_id


def test_lookup_plugin_builds_the_same_header():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "roles/validate_router/lookup_plugins/validate_router_traceparent.py"
    spec = importlib.util.spec_from_file_location("validate_router_traceparent", path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    [got] = plugin.LookupModule().run([])
    assert got["header"].split("-")[1] == got["trace_id"] and got["header"].endswith("-01")


def test_pick_decision_by_trace_among_other_traffic():
    from policy_router import pick_decision

    agent = dict(DECISION, trace_id="aaa", team="agents", routed_to="sota-smart")
    mine = dict(DECISION, trace_id="bbb")
    later = dict(DECISION, trace_id="ccc", team="agents")
    assert pick_decision([agent, mine, later], "bbb", strict=True) == (mine, "trace")
    assert pick_decision([agent, mine, later], "bbb") == (mine, "trace")


def test_pick_decision_takes_the_newest_of_the_same_trace():
    from policy_router import pick_decision

    first = dict(DECISION, trace_id="bbb", routed_to="sota-smart")
    retry = dict(DECISION, trace_id="bbb")
    assert pick_decision([first, retry], "bbb", strict=True) == (retry, "trace")


def test_strict_never_takes_another_decision():
    from policy_router import pick_decision

    agent = dict(DECISION, trace_id="aaa", team="agents")
    assert pick_decision([agent], "bbb", strict=True) == ({}, "none")
    # A fail-closed decision of another client has no trace id: still not ours.
    assert pick_decision([FAIL_CLOSED], "bbb", strict=True) == ({}, "none")
    assert pick_decision([agent, FAIL_CLOSED], "bbb", strict=True) == ({}, "none")
    assert pick_decision([], "bbb", strict=True) == ({}, "none")


def test_not_strict_takes_the_newest():
    from policy_router import pick_decision

    old, new = _without_trace(DECISION), dict(_without_trace(DECISION), routed_to="sota-smart")
    assert pick_decision([old, new], "bbb") == (new, "newest")
    assert pick_decision([old, new]) == (new, "newest")
    assert pick_decision([dict(DECISION, trace_id="aaa"), FAIL_CLOSED], "bbb") == (FAIL_CLOSED, "newest")
    assert pick_decision([], "bbb") == ({}, "none")


def test_wait_returns_when_the_trace_appears(monkeypatch):
    import policy_router

    agent = dict(DECISION, trace_id="aaa", team="agents")
    mine = dict(DECISION, trace_id="bbb")
    reads = iter([[agent], [agent, FAIL_CLOSED], [agent, FAIL_CLOSED, mine]])
    monkeypatch.setattr(policy_router, "read_decisions", lambda *a, **k: [dict(d) for d in next(reads)])
    monkeypatch.setattr(policy_router.time, "sleep", lambda s: None)
    new, decision, matched_by = policy_router.wait_decision(None, "ns", "app=litellm", 5, timeout=60,
                                                            trace_id="bbb", strict=True)
    assert len(new) == 3 and decision["trace_id"] == "bbb" and matched_by == "trace"


def test_wait_not_strict_returns_at_the_first_decision(monkeypatch):
    import policy_router

    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        return [_without_trace(DECISION)]

    monkeypatch.setattr(policy_router, "read_decisions", read)
    new, decision, matched_by = policy_router.wait_decision(None, "ns", "app=litellm", 5, timeout=60,
                                                            trace_id="bbb")
    assert matched_by == "newest" and len(new) == 1 and len(calls) == 1


def test_wait_strict_for_a_missing_trace_stops_at_the_timeout(monkeypatch):
    import types

    import policy_router

    clock = iter(range(0, 100, 4))
    fake_time = types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None)
    monkeypatch.setattr(policy_router, "time", fake_time)
    monkeypatch.setattr(policy_router, "read_decisions", lambda *a, **k: [dict(FAIL_CLOSED)])
    new, decision, matched_by = policy_router.wait_decision(None, "ns", "app=litellm", 5, timeout=10,
                                                            trace_id="bbb", strict=True)
    assert (decision, matched_by) == ({}, "none") and len(new) == 1
