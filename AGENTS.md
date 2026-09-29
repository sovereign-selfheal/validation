# AGENTS.md — `validation` repository

Guidance for AI coding agents (Claude Code, Codex, Cursor, etc.) and humans working in this repo.
Read this file fully before making changes.

## 1. Purpose

This repository checks the demo
*"The Sovereign, Self-Healing Platform — Smart LLM Routing & Autonomous AI-Driven Triage on OpenShift AI"*
on a live cluster, after the bootstrap of the `ansible` repo. It does **not** install or configure
anything: it reads the cluster, sends requests through the public router route and reports the
results. How to run it: [`README.md`](README.md).

It replaces the loose e2e scripts used until 2026-09-26 (`e2e.py`, `infra-checks.sh`, `failover.py`).
Every check exists **once**: in Ansible, or in `harness/load.py`, never in both.

## 2. Where a check goes

- **Ansible (default).** Cluster state (`kubernetes.core.k8s_info`), one HTTP request per case
  (`ansible.builtin.uri`), commands in a pod (`kubernetes.core.k8s_exec`).
- **`harness/load.py`** only for what Ansible does badly: long or streaming answers, concurrent
  requests, loops until a condition on the answers, traffic while pods are deleted.
- **`cases/routing.yml`** holds the routing and demo prompts with the expected decision. A new
  routing case is a new entry there, not new tasks.

## 3. Rules

- **Read-only by default.** The default groups (`platform`, `isolation`, `access`, `routing`,
  `demo`) must not change any workload. `isolation` may create and delete its own temporary
  namespace. Anything that loads the cluster, uses up a token budget or changes a managed object
  is a *disruptive* group: it runs only with `validation_disruptive: true` or when its tag is
  named with `--tags` (`when: validation_disruptive | bool or '<tag>' in ansible_run_tags`).
- **A failed check does not stop the run.** Each check records one result
  `{group, id, name, status, detail}` in `validation_results` (`status`: `PASS`, `FAIL`, `WARN`,
  `SKIP`). Only `validate_report` fails the play, at the end. Use `WARN` for states that are
  expected by design (for example a pending InstallPlan left unapproved by the pinning policy).
- **Honest change reporting.** Read-only tasks use `changed_when: false`. Tasks that really change
  the cluster (probe namespace, pod deletion, self-heal patch) report their change.
- **Waits, not sleeps.** `pause` is forbidden; poll a condition (`until`, or the timeout of the
  module `policy_decisions`).
- **No `oc` or `kubectl` via shell.** Use the `kubernetes.core` modules, or the `kubernetes`
  Python client in the harness. The only tools needed are uv and a kubeconfig.
- **Nothing from the operator's laptop.** No vault, no local files outside the repo: the playbook
  reads the profile of the demo from the root Application and the API keys from the cluster.
  Keep the API keys out of the logs (`no_log: true` on tasks that carry them; environment
  variables for the harness, never command-line arguments).
- **Unique ids.** Each check has a unique id: `P` platform, `I` isolation, `A` access, `R` routing,
  `D` demo, `S` sota, `C` parallel (concurrency), `L` legal, `F` failover, `H` self-heal;
  `X` is reserved for an aborted run.
- **Numbers from `-e` are strings.** Compare an overridable number with `| int`
  (`validate_platform_min_replicas | int`), else `-e name=3` breaks the template.

## 4. Conventions

Same as the `ansible` repo:

- Fully qualified collection names; roles and variables in `snake_case`. Role-private variables
  start with the role name. The shared variables start with `validation_` (see `.ansible-lint`).
- Every role starts with a `debug` line that says what it checks.
- Tags are the group names above; `validate_context` and `validate_report` are `always`.
- Comments, docs and commit messages in **English**, level B2/C1: short, clear sentences.
- **Python tools with uv**, never pip: change `pyproject.toml`, run `uv lock`, commit both files.

Before opening a PR:

```bash
uv run yamllint .
uv run ansible-lint
uv run ansible-playbook playbooks/validate.yml --syntax-check
uv run ruff check harness/ tests/ roles/
uv run pytest
```

These commands check the code, not the checks: a change to a check is done only after a run on
a real cluster.

## 5. Contract with the other repos

The checks depend on names and behaviour defined elsewhere. When one of these changes, update
this repo in the same change set.

- **`gitops` repo.**
  - Routing policies (`components/litellm-router/files/chain.yaml`, `privacy-plus.yaml`) →
    expected decisions in `cases/routing.yml`.
  - Names and labels: namespaces `maas-routing`, `local-models`, `observability`; Deployments
    `litellm` (`app=litellm`, container `litellm`) and `presidio-analyzer`
    (`app=presidio-analyzer`); PDBs with the same names; ConfigMap `litellm-config`; API key
    Secrets `apikey-<tier>-1` (key `api_key`); InferenceService names per profile
    (`qwen38-local`, `qwen25-05b-local`) and their HardwareProfiles `<name>-<profile>` (P19); Tempo `tempo`, collector Deployment `otel-collector`.
  - Root Application values read by `validate_context`: `modelProfile`, `sota.enabled`,
    `classifier.mode`, `observability.enabled`, `secretStore.enabled`.
  - Tiers: the legal budget (20,000 tokens / 5 minutes) drives L1.
- **`router` repo.** The format of the `[policy-router] {...}` log line (a Python dict repr with
  `routed_to`, `decided_by`, `reason`, `team`, `trace_id`). The parser is
  `roles/validate_router/module_utils/policy_router.py`; its tests are in `tests/`.
  D1-D8 need router v0.4.0 or later (`(no id)`, `(<2 words)` markers).
- **`ansible` repo.** Operator packages (`validate_platform_operators`), Route `maas-router` and
  its 10m timeout, the load balancer idle timeout, Gateway `openshift-ai-inference` with its
  HPA fixed at 2, Authorino replicas, the ClusterSecretStore `sovereign-selfheal`, the root
  Application `openshift-gitops/sovereign-selfheal`.

## 6. Out of scope

- Installing, fixing or tuning the demo → `ansible` and `gitops` repos. A check reports; it never
  repairs.
- Unit tests and the offline evaluation of the router → `router` repo (`tests/`, `eval/`).
- Load or performance benchmarks beyond the few requests of the demo.
