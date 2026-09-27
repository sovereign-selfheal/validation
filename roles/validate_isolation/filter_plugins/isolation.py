# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Filters of the validate_isolation role."""


def validate_isolation_addresses(pods_and_port):
    """(list of pods, port) -> ["<pod ip>:<port>", ...]."""
    pods, port = pods_and_port
    return [f"{p['status']['podIP']}:{port}" for p in pods or [] if p.get("status", {}).get("podIP")]


def validate_isolation_results(pod_results, targets, rc_by_address, control_ok):
    """One result per target: PASS when no pod of the target accepted a connection."""
    results = []
    for index, (res, target) in enumerate(zip(pod_results, targets, strict=True), start=1):
        addresses = validate_isolation_addresses((res.get("resources"), target["port"]))
        reachable = [a for a in addresses if str(rc_by_address.get(a)) == "0"]
        if not addresses:
            status, detail = "FAIL", "no running pod found"
        elif not control_ok:
            status, detail = "WARN", "inconclusive: the probe pod has no network"
        elif reachable:
            status, detail = "FAIL", f"reachable: {', '.join(reachable)}"
        else:
            status, detail = "PASS", f"{len(addresses)} pod(s) blocked on port {target['port']}"
        results.append({
            "group": "isolation",
            "id": f"I{index}",
            "name": f"{target['name']} not reachable from another namespace",
            "status": status,
            "detail": detail,
        })
    return results


class FilterModule:
    def filters(self):
        return {
            "validate_isolation_addresses": validate_isolation_addresses,
            "validate_isolation_results": validate_isolation_results,
        }
