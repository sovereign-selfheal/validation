# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Filters to read status conditions of Kubernetes objects."""


def condition_status(obj, cond_type="Ready"):
    """Status ("True", "False", ...) of the condition cond_type, or "Unknown"."""
    for cond in ((obj or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") == cond_type:
            return str(cond.get("status", "Unknown"))
    return "Unknown"


def names_not_in_condition(objs, cond_type="Ready", status="True"):
    """Names of the objects whose condition cond_type is not status."""
    return [o["metadata"]["name"] for o in objs or [] if condition_status(o, cond_type) != status]


def ready_pods(pods):
    """Pods that are Ready and not being deleted."""
    return [p for p in pods or []
            if not p["metadata"].get("deletionTimestamp") and condition_status(p, "Ready") == "True"]


class FilterModule:
    def filters(self):
        return {
            "condition_status": condition_status,
            "names_not_in_condition": names_not_in_condition,
            "ready_pods": ready_pods,
        }
