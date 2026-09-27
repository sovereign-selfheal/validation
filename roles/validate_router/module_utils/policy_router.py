# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Read the routing decisions of the policy router from the LiteLLM pod logs.

The router hook prints one line per request: ``[policy-router] {...}``, where ``{...}`` is
the repr of a Python dict (not JSON). LiteLLM runs with several replicas, so the logs of
every pod are read and merged by timestamp.

Used by the module ``policy_decisions`` and by ``harness/load.py``.
"""

import ast
import time

MARKER = "[policy-router] {"


def parse_decisions(text):
    """Return the decision dicts found in a log text, in order.

    Lines may start with a kubelet timestamp (``timestamps=True``); it is kept in the
    ``_ts`` key so that decisions from several pods can be ordered.
    """
    found = []
    for line in text.splitlines():
        idx = line.find(MARKER)
        if idx < 0:
            continue
        try:
            decision = ast.literal_eval(line[idx + len(MARKER) - 1:])
        except (ValueError, SyntaxError):
            continue
        if not isinstance(decision, dict) or "routed_to" not in decision:
            continue
        prefix = line[:idx].strip()
        if prefix:
            decision["_ts"] = prefix.split()[0]
        found.append(decision)
    return found


def decision_key(decision):
    """Identity of a decision: the trace id when there is one, else the whole content."""
    if decision.get("trace_id"):
        return str(decision["trace_id"])
    return repr(sorted((k, repr(v)) for k, v in decision.items() if k != "_ts"))


def read_decisions(core_v1, namespace, label_selector, since_seconds, container=None):
    """Decisions of all running pods that match the selector, oldest first."""
    from kubernetes.client.exceptions import ApiException

    decisions = []
    pods = core_v1.list_namespaced_pod(namespace, label_selector=label_selector).items
    for pod in pods:
        if pod.status.phase != "Running":
            continue
        kwargs = {"since_seconds": max(1, int(since_seconds)), "timestamps": True}
        if container:
            kwargs["container"] = container
        try:
            text = core_v1.read_namespaced_pod_log(pod.metadata.name, namespace, **kwargs)
        except ApiException:
            continue  # pod restarting or deleted meanwhile
        for decision in parse_decisions(text):
            decision["_pod"] = pod.metadata.name
            decisions.append(decision)
    decisions.sort(key=lambda d: d.get("_ts", ""))
    return decisions


def wait_new_decisions(core_v1, namespace, label_selector, since_seconds, exclude=(), timeout=10.0,
                       interval=1.0, container=None):
    """Poll the logs until at least one decision not in ``exclude`` appears, or the timeout.

    The kubelet serves a log line a little after the process writes it, so one read right
    after the HTTP answer can miss it. Returns the new decisions, oldest first.
    """
    excluded = set(exclude)
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        new = [d for d in read_decisions(core_v1, namespace, label_selector, since_seconds + elapsed, container)
               if decision_key(d) not in excluded]
        if new or elapsed >= timeout:
            return new
        time.sleep(interval)


def load_core_v1(kubeconfig=None, context=None):
    """CoreV1Api from the kubeconfig ($KUBECONFIG, ~/.kube/config) or the in-cluster account."""
    from kubernetes import client, config
    from kubernetes.config.config_exception import ConfigException

    try:
        config.load_kube_config(config_file=kubeconfig, context=context)
    except (ConfigException, FileNotFoundError, TypeError):
        config.load_incluster_config()
    return client.CoreV1Api()
