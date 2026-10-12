# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Lookup of the validate_router role: a new W3C traceparent header for one request.

The header is built by ``new_traceparent()`` of the role's module_utils, the same function as
in harness/load.py, so both send the same header.
"""

import sys
from pathlib import Path

from ansible.plugins.lookup import LookupBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "module_utils"))

from policy_router import new_traceparent  # noqa: E402

DOCUMENTATION = r"""
name: validate_router_traceparent
short_description: A new W3C traceparent header and its trace id
description:
  - Returns a dict with C(header) (the value of the C(traceparent) header) and C(trace_id)
    (32 hex characters). Each call gives a new trace id.
"""


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        header, trace_id = new_traceparent()
        return [{"header": header, "trace_id": trace_id}]
