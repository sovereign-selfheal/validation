# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Filters of the validate_report role."""


def validation_lines(results):
    """One aligned text line per result: STATUS  ID  name | detail."""
    width = max((len(str(r.get("id", ""))) for r in results), default=0)
    return [
        f"{r.get('status', '?'):<4}  {str(r.get('id', '')):<{width}}  {r.get('name', '')} | {r.get('detail', '')}"
        for r in results
    ]


class FilterModule:
    def filters(self):
        return {"validation_lines": validation_lines}
