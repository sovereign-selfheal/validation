# validation: checks of the demo on a live cluster

This repo checks the demo *The Sovereign, Self-Healing Platform* after the bootstrap
(`playbooks/site.yml` of the `ansible` repo). It runs from a laptop or a bastion against the
cluster API and the public router route. Read [`AGENTS.md`](AGENTS.md) before changing anything.

## Quick start

```bash
# once
uv sync                                                      # ansible-core, kubernetes, requests, linters
uv run ansible-galaxy collection install -r requirements.yml

# every run
export KUBECONFIG=~/.kube/demo.kubeconfig                    # or: oc login ...
uv run ansible-playbook playbooks/validate.yml                                  # default groups
uv run ansible-playbook playbooks/validate.yml -e validation_disruptive=true    # all groups
uv run ansible-playbook playbooks/validate.yml --tags routing,demo              # some groups
```

No vault and no extra variables are needed. The playbook reads everything from the cluster:
the apps domain, the profile of the demo (from the root Argo CD Application) and the API keys
of the tiers (from the Secrets that ESO creates).

## Groups

| Tag | Checks | Default |
|---|---|---|
| `platform` | P1-P25: operators (CSV `Succeeded`, `Manual` approval, pending InstallPlans), GPU node and ClusterPolicy, load balancer and Route timeouts, inference Gateway and its HPA, Kuadrant wasm module on every gateway pod (P18), Kuadrant metrics with the `tier` label (P20), Prometheus retention and volumes (P21), Argo CD Applications, HA replicas and PDBs, Authorino, local model and its hardware profile (P19), a name for each serving runtime in the RHOAI dashboard (P23), ESO, observability, team access (P17, only when the Group `selfheal-team` exists), decision model: Ready, on its GPU pool, `/v1/systemone` answers (P22, only with `decisionModel.enabled`), namespace policy: data-class labels, namespace reader RBAC, labels read by every LiteLLM pod when the policy is on (P24), live page `routing-live-view` behind the OpenShift sign-in (P25, SKIP when not deployed) | yes |
| `isolation` | I0-I5: a probe pod in a temporary namespace cannot reach LiteLLM, Presidio or the local model; Presidio has no egress (IP and DNS) | yes |
| `access` | A1-A3: no key and invalid key get 401; the tier comes from the key, not from the `x-team` header | yes |
| `routing` | R1-R8: short, complex, Italian, PII, implicit sensitivity (lexicon and C2 classifier), prompt injection, agent tool call | yes |
| `namespace` | N1-N8: namespace policy of router v0.11.0: a restricted namespace from the agent hint or from the text stays local before the gates; public, unknown or no namespace keep the normal routing. N7 and N8 need router v0.11.1: PromQL in JSON tool call arguments (escaped quotes) and a regex matcher with two namespaces. A case is SKIP when its switch (`namespacePolicy.scan`/`hint`) is off | yes |
| `demo` | D1-D8: the prompts of the demo video (router v0.4.0 or later) | yes |
| `sota` | S1-S3: long SOTA answer, reasoning on per request, streaming with usage | no |
| `parallel` | C1: 5 concurrent calls to the local model | no |
| `legal` | L1-L2: the legal tier gets 429 after its token budget; research is not affected | no |
| `context` | K1-K10. K1-K7: agents' large contexts (about 8k to 100k tokens: system prompt, tool calls, tool output, streamed). A context with a sensitive sentence must stay LOCAL; a benign context that stays LOCAL only because Presidio or C2 timed out is a WARN (fail-closed, no leak). K8-K10 (router v0.8.0): the SOTA size cap `efficiency.sota_max_prompt_chars`, read from `sota_cap` in the log line. Benign and sensitive contexts just under the cap (the cap must not apply; the sensitive one stays LOCAL) and a benign one just over it (LOCAL by the efficiency gate, no detector). SKIP when the cap is off. Uses about 300k tokens of the research tier (about 450k with the cap on), paced over about 5-8 minutes | no |
| `failover` | F1: one LiteLLM pod and one Presidio pod are deleted under load; every request must answer 200 | no |
| `decision` | F2-F3: the decision model is stopped (KServe stop annotation); R6b must still route LOCAL through the chat fallback of C2 (`fallback/llm@` in the reason); then the model must be Ready again. Only with `decisionModel.enabled` and router v0.7.0 | no |
| `selfheal` | H1: a managed ConfigMap is changed by hand; Argo CD must revert it | no |

The groups marked "no" load or change the cluster. They run with
`-e validation_disruptive=true`, or when you name their tag with `--tags` (naming a tag is
consent). The load groups (`sota`, `parallel`, `legal`, `failover`) run in
[`harness/load.py`](harness/load.py), started by the playbook.

Things to know:

- **Legal tier.** After L1, the legal tier answers 429 for up to 5 minutes. A3 uses the legal
  key, so `--tags access` fails A3 during that time.
- **Local-only mode.** With `sota.enabled: false`, the alias `sota-smart` is served by the local
  model. The routing decisions are the same, so `routing` and `demo` still apply; the `sota`
  group is skipped.
- **Classifier off.** With `classifier.mode: off`, R6b and R6c are skipped.
- **Presidio NER off.** With the decision model, the gitops repo turns C1 off
  (`NER_ENABLED=0` in the LiteLLM Deployment, router v0.10.0). The validation reads that
  variable: when C1 is off, the Presidio labels of D1, D2, D4 and D6 (`ner_reason_contains`) are
  not checked; their routing decisions are.
- **One run at a time.** The routing decision is read from the `[policy-router]` log lines of
  LiteLLM. Other traffic during a run can make a routing case ambiguous (the detail says so).
- **Changes.** `platform`, `access`, `routing` and `demo` are read-only (changed=0).
  `isolation` creates and deletes the namespace `validation-probe`. `failover` and `selfheal`
  report their changes.

## Reading the results

Every check gives one line: `PASS`, `FAIL`, `WARN` or `SKIP`, an id, a name and a detail. The
play fails at the end when at least one check is `FAIL`. A `WARN` does not fail the play, for
example an InstallPlan for a newer operator version that the pinning policy leaves unapproved.
When a task fails hard (for example the cluster API is not reachable), the run stops, records
one `X` result (`run aborted at task: ...`) and still prints and saves the results collected so far.

The same results are written as JSON in `reports/validation-<cluster>-<time>.json` (ignored by
git). The JSON also holds the context: cluster, profile, tags.

```text
PASS  P1   demo operators subscribed | 15 expected
PASS  R4   Italian PII -> local (privacy) | HTTP 200 1.9s routed_to=local-fast by=privacy team=research ...
WARN  P4   no unapproved InstallPlans | left unapproved by the pinning policy: gpu-operator-certified.v26.7.1
```

## Layout

```
playbooks/validate.yml       entry point: the roles below, in order
cases/routing.yml            routing and demo prompts with the expected decisions
harness/load.py              sota, parallel, legal, context, failover (Python, requests + kubernetes client)
roles/
  validate_context/          cluster, URL, profile, API keys; starts the result list
  validate_platform/         platform group
  validate_isolation/        isolation group
  validate_router/           access, routing and demo groups; module policy_decisions
  validate_load/             runs harness/load.py and merges its results
  validate_selfheal/         selfheal group
  validate_report/           result table, JSON report, final assertion
tests/                       unit tests of the log parser and the filters (pytest)
```

## Development

```bash
uv run yamllint .
uv run ansible-lint
uv run ansible-playbook playbooks/validate.yml --syntax-check
uv run ruff check harness/ tests/ roles/
uv run pytest
```

CI (GitHub Actions) runs the same commands on every pull request.
