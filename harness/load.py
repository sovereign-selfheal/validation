#!/usr/bin/env python3
# Copyright: sovereign-selfheal contributors
# Apache License 2.0 (see LICENSE)
"""Load and resilience checks of the router that do not fit Ansible well.

Groups (run in this order, the order matters for the side effects):
  sota      S1 long SOTA answer, S2 SOTA reasoning on per request, S3 SOTA streaming + usage
  parallel  C1 5 concurrent local calls
  legal     L1 legal tier gets 429 (blocks the legal tier for up to 5 minutes), L2 research not affected
  failover  F1 one LiteLLM pod and one Presidio pod deleted under load: every request must answer 200
  context   K1-K7 agents' large contexts (about 8k to 100k tokens): system prompt, tool calls and
            tool output, as the self-heal agents send them. A context with a sensitive sentence must
            stay LOCAL (FAIL otherwise); a benign context that stays LOCAL only because a detector
            timed out is a WARN (fail-closed, no leak). Paced to the research token budget.

Normally started by the role validate_load. Standalone:
  VALIDATION_KEY_RESEARCH=... VALIDATION_KEY_LEGAL=... uv run python harness/load.py \
    --url https://router.<apps domain>/v1/chat/completions --groups sota,parallel --report /tmp/load.json
The API keys come from the environment, never from the command line.
"""

import argparse
import concurrent.futures as cf
import json
import os
import random
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

# Agents' context (agentic-triage: OGX loop with MCP tools). Deterministic: the same seed gives
# the same payload, so two runs send the same text.
AGENT_SYSTEM = ("You are an SRE triage agent for an OpenShift platform. Use the tools to read pod logs, "
                "events and tickets, find the root cause of the incident, and propose a safe fix. ") * 8
AGENT_LINES = (
    "{ts} pod/checkout-api-{n} in namespace shop-{n2} restarted: OOMKilled, exit code 137",
    "{ts} Warning BackOff kubelet Back-off restarting failed container payment in pod payment-{n}",
    "{ts} ERROR c.e.checkout.PaymentClient - HTTP 503 from https://payments.internal:8443/v2/charge"
    " (attempt {n2}/5, trace {hexid})",
    "{ts} INFO  Deployment checkout-api rolled out revision {n2} image quay.io/shop/checkout:{n}.{n2}",
    "{ts} node ip-10-0-{n2}-{n}.us-east-2.compute.internal condition MemoryPressure=True",
)
NEEDLE = ("Note from the ticket: the on-call engineer Laura Bianchi told us in confidence that she is "
          "going through a divorce and may be signed off sick next week, so do not page her.")
AGENT_QUESTION = ("Analyze step by step all the tool output above: explain the root cause of the checkout "
                  "failures and propose a safe remediation plan with rollback steps.")


def agent_messages(tokens, seed, needle=False):
    """OpenAI chat messages like an agent's turn: system prompt, the incident, rounds of tool calls
    with their output (about 50 tokens per log line), and the final question. `needle` puts a
    sensitive sentence in the middle of the tool output."""
    rnd = random.Random(seed)
    lines = []
    for _ in range(max(1, tokens // 50)):
        lines.append(rnd.choice(AGENT_LINES).format(
            ts=f"2026-10-01T{rnd.randrange(24):02d}:{rnd.randrange(60):02d}:{rnd.randrange(60):02d}Z",
            n=rnd.randrange(10, 9999), n2=rnd.randrange(1, 99), hexid=f"{rnd.getrandbits(64):016x}"))
    if needle:
        lines.insert(len(lines) // 2, NEEDLE)
    messages = [{"role": "system", "content": AGENT_SYSTEM},
                {"role": "user", "content": "Incident INC-4711: checkout failures in production."}]
    chunk = max(1, len(lines) // 4)
    for i in range(0, len(lines), chunk):
        call_id = f"call_{seed}_{i}"
        messages.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "get_pod_logs", "arguments": json.dumps({"namespace": "shop", "tail": chunk})}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": "\n".join(lines[i:i + chunk])})
    messages.append({"role": "user", "content": AGENT_QUESTION})
    return messages


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

    def agent_call(self, messages, max_tokens=300):
        """One streamed agent call: (status, seconds to the first chunk, total seconds, prompt
        tokens, decision)."""
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.keys['research']}"}
        body = {"model": "auto", "messages": messages, "max_tokens": max_tokens, "stream": True,
                "stream_options": {"include_usage": True}}
        start, first, prompt_tokens = time.time(), None, None
        with requests.post(self.args.url, headers=headers, json=body, timeout=660, verify=self.verify,
                           stream=True) as r:
            for line in r.iter_lines():
                if first is None and line:
                    first = time.time() - start
                if line.startswith(b"data: {"):
                    usage = json.loads(line[6:]).get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens")
            status = r.status_code
        total = time.time() - start
        new = wait_new_decisions(self.core, self.args.namespace, self.args.litellm_selector, total + 2,
                                 exclude=self.seen, container=self.args.container)
        self.seen.update(decision_key(d) for d in new)
        return status, first, total, prompt_tokens, (new[-1] if new else {})

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

    def context(self):
        """K1-K7. The research tier has 100k tokens a minute (gitops bootstrap/values.yaml): after a
        large call the next one waits, so the budget of one minute is never exceeded."""
        cases = [("K1", 8000, False), ("K2", 8000, True), ("K3", 30000, False), ("K4", 30000, True),
                 ("K5", 60000, False), ("K6", 60000, True), ("K7", 100000, False)]
        for rid, tokens, needle in cases:
            status, first, total, ptok, d = self.agent_call(agent_messages(tokens, seed=tokens, needle=needle))
            reason = d.get("reason", "")
            timed_out = "error" in reason  # ner error:<type> or the classifier error signal
            routed = d.get("routed_to", "?")
            detail = (f"HTTP {status} prompt_tokens={ptok} first chunk {first or 0:.1f}s total {total:.1f}s "
                      f"routed_to={routed} by={d.get('decided_by', '?')} | {reason[:220]}")
            name = f"agent context ~{tokens // 1000}k tokens, {'sensitive sentence' if needle else 'benign'}"
            if status != 200 or not d:
                self.record("context", rid, name, False, detail)
            elif needle:
                self.record("context", rid, name, routed == "local-fast", detail)
            else:
                self.record("context", rid, name, True, detail, status="WARN" if timed_out else "PASS")
            time.sleep(max(5, (ptok or tokens) / 100000 * 60))  # stay below 100k tokens a minute

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
    parser.add_argument("--groups", default="sota,parallel,legal,failover")  # context: opt-in
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
    order = ["sota", "parallel", "legal", "context", "failover"]
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
