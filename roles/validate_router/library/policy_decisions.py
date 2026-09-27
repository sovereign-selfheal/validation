#!/usr/bin/python
# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Ansible module: read new routing decisions from the logs of all LiteLLM pods."""

DOCUMENTATION = r"""
---
module: policy_decisions
short_description: Read new routing decisions of the policy router from the LiteLLM logs
description:
  - Reads the C([policy-router] {...}) lines of every running pod that matches
    I(label_selector), merges them by timestamp and returns the decisions that are not in
    I(exclude).
  - Polls until at least one new decision appears or I(timeout) seconds pass. Read-only.
  - C(kubernetes.core.k8s_log) reads one pod only when it uses a label selector; LiteLLM
    runs with several replicas, hence this module.
options:
  namespace:
    description: Namespace of the LiteLLM pods.
    type: str
    required: true
  label_selector:
    description: Label selector of the LiteLLM pods.
    type: str
    default: app=litellm
  container:
    description: Container name, needed only when the pod has more than one container.
    type: str
  since_seconds:
    description: Read the log lines of the last N seconds (the wait time is added).
    type: int
    required: true
  exclude:
    description: Keys (trace ids, or content keys) of decisions already seen.
    type: list
    elements: str
    default: []
  timeout:
    description: Seconds to wait for a new decision.
    type: float
    default: 10
  kubeconfig:
    description: Path of a kubeconfig file. Default $KUBECONFIG, then ~/.kube/config.
    type: path
  context:
    description: Kubeconfig context.
    type: str
"""

RETURN = r"""
decisions:
  description: New decisions, oldest first. Each has the extra keys C(_ts), C(_pod) and C(_key).
  type: list
  returned: always
decision:
  description: The newest new decision, or an empty dict.
  type: dict
  returned: always
"""

from ansible.module_utils.basic import AnsibleModule

try:
    from ansible.module_utils.policy_router import decision_key, load_core_v1, wait_new_decisions
    HAS_DEPS = True
except ImportError:  # the kubernetes client is imported lazily; this guards only the module_utils file
    HAS_DEPS = False


def main():
    module = AnsibleModule(
        argument_spec={
            "namespace": {"type": "str", "required": True},
            "label_selector": {"type": "str", "default": "app=litellm"},
            "container": {"type": "str"},
            "since_seconds": {"type": "int", "required": True},
            "exclude": {"type": "list", "elements": "str", "default": []},
            "timeout": {"type": "float", "default": 10},
            "kubeconfig": {"type": "path"},
            "context": {"type": "str"},
        },
        supports_check_mode=True,
    )
    if not HAS_DEPS:
        module.fail_json(msg="module_utils policy_router not found")
    p = module.params
    try:
        core_v1 = load_core_v1(p["kubeconfig"], p["context"])
        new = wait_new_decisions(core_v1, p["namespace"], p["label_selector"], p["since_seconds"],
                                 exclude=p["exclude"], timeout=p["timeout"], container=p["container"])
    except Exception as exc:  # report any API or config error as a module failure
        module.fail_json(msg=f"cannot read the LiteLLM logs: {type(exc).__name__}: {exc}")
    for decision in new:
        decision["_key"] = decision_key(decision)
    module.exit_json(changed=False, decisions=new, decision=new[-1] if new else {})


if __name__ == "__main__":
    main()
