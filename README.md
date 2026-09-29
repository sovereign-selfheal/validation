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
| `platform` | P1-P18: operators (CSV `Succeeded`, `Manual` approval, pending InstallPlans), GPU node and ClusterPolicy, load balancer and Route timeouts, inference Gateway and its HPA, Kuadrant wasm module on every gateway pod (P18), Argo CD Applications, HA replicas and PDBs, Authorino, local model, ESO, observability, team access (P17, only when the Group `selfheal-team` exists) | yes |
| `isolation` | I0-I5: a probe pod in a temporary namespace cannot reach LiteLLM, Presidio or the local model; Presidio has no egress (IP and DNS) | yes |
| `access` | A1-A3: no key and invalid key get 401; the tier comes from the key, not from the `x-team` header | yes |
| `routing` | R1-R8: short, complex, Italian, PII, implicit sensitivity (lexicon and C2 classifier), prompt injection, agent tool call | yes |
| `demo` | D1-D8: the prompts of the demo video (router v0.4.0 or later) | yes |
| `sota` | S1-S3: long SOTA answer, reasoning on per request, streaming with usage | no |
| `parallel` | C1: 5 concurrent calls to the local model | no |
| `legal` | L1-L2: the legal tier gets 429 after its token budget; research is not affected | no |
| `failover` | F1: one LiteLLM pod and one Presidio pod are deleted under load; every request must answer 200 | no |
| `selfheal` | H1: a managed ConfigMap is changed by hand; Argo CD must revert it | no |

The groups marked "no" load or change the cluster. They run with
`-e validation_disruptive=true`, or when you name their tag with `--tags` (naming a tag is
consent). The load groups (`sota`, `parallel`, `legal`, `failover`) run in
[`harness/load.py`](harness/load.py), started by the playbook.

Things to know:

- **Legal tier.** After L1, the legal tier answers 429 for up to 10 minutes. A3 uses the legal
  key, so `--tags access` fails A3 during that time.
- **Local-only mode.** With `sota.enabled: false`, the alias `sota-smart` is served by the local
  model. The routing decisions are the same, so `routing` and `demo` still apply; the `sota`
  group is skipped.
- **Classifier off.** With `classifier.mode: off`, R6b and R6c are skipped.
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
PASS  P1   demo operators subscribed | 13 expected
PASS  R4   Italian PII -> local (privacy) | HTTP 200 1.9s routed_to=local-fast by=privacy team=research ...
WARN  P4   no unapproved InstallPlans | left unapproved by the pinning policy: gpu-operator-certified.v26.7.1
```

## Layout

```
playbooks/validate.yml       entry point: the roles below, in order
cases/routing.yml            routing and demo prompts with the expected decisions
harness/load.py              sota, parallel, legal, failover (Python, requests + kubernetes client)
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
