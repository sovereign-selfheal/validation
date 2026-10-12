# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Read the routing decisions of the policy router from the LiteLLM pod logs.

The router hook prints one line per request: ``[policy-router] {...}``, where ``{...}`` is
the repr of a Python dict (not JSON). LiteLLM runs with several replicas, so the logs of
every pod are read and merged by timestamp.

Used by the module ``policy_decisions`` and by ``harness/load.py``.

Other clients (for example the triage agent, run by real alerts) can send requests at the same
time. Each check therefore sends its own W3C ``traceparent`` header: the router keeps that trace
id in ``trace_id``, so the check finds its own decision among the others. Without tracing on the
cluster the decisions have no ``trace_id``; then the newest new decision is used.
"""

import ast
import secrets
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
            # Raw response: with a non-ASCII log (the LiteLLM start banner) the client returns
            # the repr of the bytes ("b'...'") instead of the text.
            raw = core_v1.read_namespaced_pod_log(pod.metadata.name, namespace, _preload_content=False, **kwargs)
            text = raw.data.decode("utf-8", errors="replace")
        except ApiException:
            continue  # pod restarting or deleted meanwhile
        for decision in parse_decisions(text):
            decision["_pod"] = pod.metadata.name
            decisions.append(decision)
    decisions.sort(key=lambda d: d.get("_ts", ""))
    return decisions


def new_traceparent():
    """A new W3C ``traceparent`` header value and its trace id: ``(header, trace_id)``.

    The sampled flag (``01``) is set: with a parent-based sampler an unsampled parent gives no
    span, so the router would log no ``trace_id``.
    """
    trace_id = secrets.token_hex(16)
    return f"00-{trace_id}-{secrets.token_hex(8)}-01", trace_id


def pick_decision(new, trace_id=None):
    """The decision of one request among the new decisions: ``(decision, matched_by)``.

    ``matched_by`` is ``trace`` (the newest decision with ``trace_id``; a retry can log the same
    id twice), ``newest`` (no ``trace_id`` given, or no decision has one: tracing is off) or
    ``none`` (other decisions have a trace id, but not this one: the decision of the request
    was not found; never use the decision of another request).
    """
    if trace_id:
        mine = [d for d in new if d.get("trace_id") == trace_id]
        if mine:
            return mine[-1], "trace"
        if any(d.get("trace_id") for d in new):
            return {}, "none"
    return (new[-1] if new else {}), "newest"


def wait_new_decisions(core_v1, namespace, label_selector, since_seconds, exclude=(), timeout=10.0,
                       interval=1.0, container=None, trace_id=None):
    """Poll the logs until a new decision (not in ``exclude``) appears, or the timeout.

    With ``trace_id``, poll until the decision with that trace id appears; stop early only when
    the new decisions have no trace id at all (tracing is off, see ``pick_decision``).
    The kubelet serves a log line a little after the process writes it, so one read right
    after the HTTP answer can miss it. Returns the new decisions, oldest first.
    """
    excluded = set(exclude)
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        new = [d for d in read_decisions(core_v1, namespace, label_selector, since_seconds + elapsed, container)
               if decision_key(d) not in excluded]
        if new and pick_decision(new, trace_id)[1] != "none":
            return new
        if elapsed >= timeout:
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
