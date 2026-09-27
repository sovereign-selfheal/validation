"""Unit tests of the filter plugins of the roles."""

import importlib.util
from pathlib import Path

ROLES = Path(__file__).resolve().parents[1] / "roles"


def load(role, name):
    spec = importlib.util.spec_from_file_location(name, ROLES / role / "filter_plugins" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conditions = load("validate_platform", "conditions")
isolation = load("validate_isolation", "isolation")
report = load("validate_report", "report")


def pod(name, ip, ready=True, deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-09-27T10:00:00Z"
    return {"metadata": meta, "status": {"podIP": ip, "conditions": [{"type": "Ready", "status": str(ready)}]}}


def test_condition_status():
    assert conditions.condition_status(pod("a", "1"), "Ready") == "True"
    assert conditions.condition_status({}, "Ready") == "Unknown"
    assert conditions.condition_status({"status": {}}, "Programmed") == "Unknown"


def test_names_not_in_condition_and_ready_pods():
    pods = [pod("a", "1"), pod("b", "2", ready=False), pod("c", "3", deleting=True)]
    assert conditions.names_not_in_condition(pods) == ["b"]
    assert [p["metadata"]["name"] for p in conditions.ready_pods(pods)] == ["a"]


TARGETS = [{"name": "litellm", "port": 4000}, {"name": "presidio", "port": 3000}, {"name": "model", "port": 8080}]
RESULTS = [{"resources": [pod("l1", "10.0.0.1"), pod("l2", "10.0.0.2")]},
           {"resources": [pod("p1", "10.0.0.3")]},
           {"resources": []}]


def test_isolation_addresses():
    assert isolation.validate_isolation_addresses((RESULTS[0]["resources"], 4000)) == ["10.0.0.1:4000", "10.0.0.2:4000"]


def test_isolation_results():
    rc = {"10.0.0.1:4000": "124", "10.0.0.2:4000": "1", "10.0.0.3:3000": "0"}
    got = isolation.validate_isolation_results(RESULTS, TARGETS, rc, True)
    assert [r["status"] for r in got] == ["PASS", "FAIL", "FAIL"]
    assert got[1]["detail"] == "reachable: 10.0.0.3:3000"
    assert got[2]["detail"] == "no running pod found"


def test_isolation_inconclusive_without_network():
    got = isolation.validate_isolation_results(RESULTS[:1], TARGETS[:1], {}, False)
    assert got[0]["status"] == "WARN"


def test_report_lines_are_aligned():
    lines = report.validation_lines([
        {"status": "PASS", "id": "P1", "name": "a", "detail": "x"},
        {"status": "FAIL", "id": "R6b", "name": "b", "detail": "y"},
    ])
    assert lines == ["PASS  P1   a | x", "FAIL  R6b  b | y"]
