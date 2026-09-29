#!/usr/bin/env python3
# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Load and resilience checks of the router that do not fit Ansible well.

Groups (run in this order, the order matters for the side effects):
  sota      S1 long SOTA answer, S2 SOTA reasoning on per request, S3 SOTA streaming + usage
  parallel  C1 5 concurrent local calls
  legal     L1 legal tier gets 429 (blocks the legal tier for up to 5 minutes), L2 research not affected
  failover  F1 one LiteLLM pod and one Presidio pod deleted under load: every request must answer 200

Normally started by the role validate_load. Standalone:
  VALIDATION_KEY_RESEARCH=... VALIDATION_KEY_LEGAL=... uv run python harness/load.py \
    --url https://router.<apps domain>/v1/chat/completions --groups sota,parallel --report /tmp/load.json
The API keys come from the environment, never from the command line.
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/validate_router/module_utils"))
from policy_router import decision_key, load_core_v1, wait_new_decisions  # noqa: E402

LONG = ("Analyze in depth how a relational database executes a query: parsing, planning, cost-based "
        "optimization, join algorithms and indexes. Explain why each step matters, compare the join "
        "algorithms step by step, and give pseudo-code for a hash join and a sort-merge join.")
SOTA_ALIAS = "sota-smart"


class Suite:
    def __init__(self, args):
        self.args = args
        self.keys = {"research": os.environ.get("VALIDATION_KEY_RESEARCH", ""),
                     "legal": os.environ.get("VALIDATION_KEY_LEGAL", "")}
        self.verify = not args.insecure
        self.core = load_core_v1()
        self.seen = set()
        self.results = []

    # ------------------------------------------------------------------ helpers
    def record(self, group, rid, name, ok, detail, status=None):
        status = status or ("PASS" if ok else "FAIL")
        self.results.append({"group": group, "id": rid, "name": name, "status": status, "detail": detail})
        print(f"{status:<4}  {rid}  {name} | {detail}", flush=True)

    def chat(self, prompt, tier="research", max_tokens=200, timeout=660, **extra):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.keys[tier]}"}
        body = {"model": "auto", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        body.update(extra)
        start = time.time()
        r = requests.post(self.args.url, headers=headers, json=body, timeout=timeout, verify=self.verify)
        return r, time.time() - start

    def routed(self, prompt, **kw):
        """Send one request; return (response, seconds, decision or {})."""
        start = time.time()
        r, secs = self.chat(prompt, **kw)
        new = wait_new_decisions(self.core, self.args.namespace, self.args.litellm_selector,
                                 time.time() - start + 2, exclude=self.seen, container=self.args.container)
        self.seen.update(decision_key(d) for d in new)
        return r, secs, (new[-1] if new else {})

    @staticmethod
    def usage(r):
        try:
            return r.json().get("usage", {}).get("total_tokens")
        except ValueError:
            return None

    @staticmethod
    def answer(r):
        try:
            return (r.json()["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")[:80]
        except (ValueError, KeyError, IndexError):
            return ""

    # ------------------------------------------------------------------ groups
    def sota(self):
        r, s, d = self.routed(LONG, max_tokens=6000)
        ok = r.status_code == 200 and d.get("routed_to") == SOTA_ALIAS and self.answer(r)
        self.record("sota", "S1", "long SOTA answer (no streaming)", ok,
                    f"HTTP {r.status_code} {s:.0f}s tokens={self.usage(r)} routed_to={d.get('routed_to')} "
                    f"| {self.answer(r)[:60]}")
        # Reasoning on, per request (the default is off). An empty answer is a known behaviour
        # of the SOTA provider with a large reasoning budget: reported, not failed.
        r, s, d = self.routed(LONG, max_tokens=8000, chat_template_kwargs={"enable_thinking": True})
        ok = r.status_code == 200 and d.get("routed_to") == SOTA_ALIAS
        self.record("sota", "S2", "SOTA reasoning on per request", ok,
                    f"HTTP {r.status_code} {s:.0f}s tokens={self.usage(r)} "
                    f"content={'yes' if self.answer(r) else 'EMPTY (known)'}")
        headers = {"Authorization": f"Bearer {self.keys['research']}", "Content-Type": "application/json"}
        body = {"model": "auto", "stream": True, "max_tokens": 3000, "messages": [{"role": "user", "content": LONG}]}
        start, first, chunks, use = time.time(), None, 0, None
        with requests.post(self.args.url, headers=headers, json=body, stream=True, timeout=660,
                           verify=self.verify) as r:
            for line in r.iter_lines():
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if event.get("choices") and event["choices"][0].get("delta", {}).get("content"):
                    chunks += 1
                    first = first or time.time() - start
                use = event.get("usage") or use
        ok = r.status_code == 200 and chunks > 0 and use
        self.record("sota", "S3", "SOTA streaming with usage at the end", ok,
                    f"HTTP {r.status_code} ttft={first and round(first, 1)}s total={time.time() - start:.0f}s "
                    f"chunks={chunks} usage={use and use.get('total_tokens')}")

    def parallel(self):
        prompts = [f"Write a short paragraph about topic number {i}: cloud-native storage." for i in range(5)]
        start = time.time()
        with cf.ThreadPoolExecutor(5) as ex:
            res = list(ex.map(lambda p: self.chat(p, max_tokens=200), prompts))
        codes = [r.status_code for r, _ in res]
        tokens = sum((r.json().get("usage", {}).get("completion_tokens") or 0) for r, _ in res if r.status_code == 200)
        wall = time.time() - start
        self.record("parallel", "C1", "5 concurrent local calls", all(c == 200 for c in codes),
                    f"codes={codes} wall={wall:.1f}s completion_tokens={tokens} (~{tokens / wall:.0f} tok/s)")

    def legal(self):
        total, calls, code, start = 0, 0, 200, time.time()
        prompt = "Write a detailed essay of about 800 words on the history of the Roman law."
        # Budget 20k tokens / 5 min (gitops bootstrap/values.yaml): about 5 answers of 4k.
        while code == 200 and calls < 20:
            r, _ = self.chat(prompt, tier="legal", max_tokens=4000)
            code = r.status_code
            calls += 1
            total += self.usage(r) or 0
        self.record("legal", "L1", "legal tier gets 429 when its token budget is used", code == 429,
                    f"HTTP {code} after {calls} calls, {total} tokens counted, {time.time() - start:.0f}s")
        r, _ = self.chat("hi", tier="research")
        self.record("legal", "L2", "research tier not affected", r.status_code == 200, f"HTTP {r.status_code}")

    def failover(self):
        codes, deleted = Counter(), []
        stop = time.time() + self.args.failover_seconds

        def kill(after, selector):
            time.sleep(after)
            pods = self.core.list_namespaced_pod(self.args.namespace, label_selector=selector).items
            running = [p for p in pods if p.status.phase == "Running" and not p.metadata.deletion_timestamp]
            if running:
                name = running[0].metadata.name
                self.core.delete_namespaced_pod(name, self.args.namespace)
                deleted.append(f"t+{after}s {name}")

        killers = [threading.Thread(target=kill, args=(10, self.args.litellm_selector)),
                   threading.Thread(target=kill, args=(35, self.args.presidio_selector))]
        for t in killers:
            t.start()
        while time.time() < stop:
            try:
                r, _ = self.chat("Analyze this record and explain why it was flagged: Mario Rossi, "
                                 "email mario.rossi@example.com.", max_tokens=16, timeout=60)
                codes[r.status_code] += 1
            except requests.RequestException as exc:
                codes[type(exc).__name__] += 1
        for t in killers:
            t.join()
        back = self.wait_ready([self.args.litellm_selector, self.args.presidio_selector], replicas=2, timeout=300)
        total = sum(codes.values())
        ok = total > 0 and codes.get(200, 0) == total and len(deleted) == 2 and back
        self.record("failover", "F1", "LiteLLM and Presidio pod deleted under load: no errors", ok,
                    f"{codes.get(200, 0)}/{total} OK {dict(codes)}, deleted: {', '.join(deleted) or 'none'}, "
                    f"replicas back: {'yes' if back else 'NO'}")

    def wait_ready(self, selectors, replicas, timeout):
        """Wait until each selector has `replicas` Ready pods again."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = []
            for selector in selectors:
                pods = self.core.list_namespaced_pod(self.args.namespace, label_selector=selector).items
                ready.append(sum(1 for p in pods if not p.metadata.deletion_timestamp and any(
                    c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))))
            if all(n >= replicas for n in ready):
                return True
            time.sleep(5)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="router URL, .../v1/chat/completions")
    parser.add_argument("--namespace", default="maas-routing")
    parser.add_argument("--groups", default="sota,parallel,legal,failover")
    parser.add_argument("--report", required=True, help="JSON file for the results")
    parser.add_argument("--litellm-selector", default="app=litellm")
    parser.add_argument("--presidio-selector", default="app=presidio-analyzer")
    parser.add_argument("--container", default="litellm", help="LiteLLM container name")
    parser.add_argument("--failover-seconds", type=int, default=75)
    parser.add_argument("--insecure", action="store_true", help="do not verify the router certificate")
    args = parser.parse_args()
    if args.insecure:
        urllib3.disable_warnings()

    suite = Suite(args)
    order = ["sota", "parallel", "legal", "failover"]
    wanted = [g for g in args.groups.split(",") if g]
    unknown = sorted(set(wanted) - set(order))
    if unknown:
        parser.error(f"unknown groups: {', '.join(unknown)}")
    for group in (g for g in order if g in wanted):
        try:
            getattr(suite, group)()
        except requests.RequestException as exc:  # a dropped connection fails the group, not the run
            suite.record(group, group, f"{group} (group aborted)", False, f"{type(exc).__name__}: {exc}")
    Path(args.report).write_text(json.dumps(suite.results, indent=2) + "\n")
    failed = [r for r in suite.results if r["status"] == "FAIL"]
    print(f"\n{len(suite.results) - len(failed)}/{len(suite.results)} passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
