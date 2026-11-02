# homelab-apps

This repo holds the application catalogue and the deployments for site `hm1`. A catalogue entry says what an application is. A deployment says where it runs. The `homelab-infra` repo creates the guests.

## Status

- State: live.
- Checked: 2026-11-02.
- Used by: the runner `host-apps` on `hm1-shr-cicd01` and the OpenClaw agents.
- Applications today: AdGuard Home, k3s, and GNS3.
- A merge to `main` starts a deploy for the changed deployments.

## Place in the system

| Repo | Owns |
|---|---|
| `homelab-infra` | Guests, base OS, pipeline, patching |
| **`homelab-apps`** (this repo) | Application catalogue and deployments |
| `homelab-standards` | Naming rules, hard limits, OpenBao policies |
| `openclaw` | The AIOps agent team |

- Guest configuration is in `homelab-infra`: the guest exists, it is reachable, it is patched, and it passes the OS tests.
- What makes a guest do its job is in this repo.
- Triggers go from `homelab-infra` to this repo. A deploy starts after an infra apply that configured a guest.

## Layout

```
catalog/<app>/          app.yaml, role/, files/, tests/l4.goss.yaml.j2, docs/
catalog/_example/       a skeleton entry
deployments/<zone>/     <app>.yaml: hosts, version, wave, vars
environments.yaml       the meaning of each zone for applications: wave and allowed status
playbooks/              deploy.yml, verify.yml, roles/bao_create_secret
policy/                 classify.py, jinja-allow.yaml, no-apps.yaml
scripts/                pipeline scripts, the lint gate, and tests
requirements.yml        Ansible collections
docs/                   guides
.gitea/workflows/       pr.yml, deploy.yml, verify.yml
```

## Change process

- Every change is a PR. Nobody pushes to `main`.
- Branch: `<type>/<issue#>-<slug>`. Title: `type(scope): text`. The scope is the name of the catalogue entry. Start the title with `WIP: ` for a plan without a merge.
- The `pr` workflow lints the PR, validates the catalogue, renders it offline, and computes the class. The code of the PR never runs.
- `policy/classify.py` computes the class. It fails closed: a change that is not listed for A or B is Class C.

| Class | Change | Approval |
|---|---|---|
| A | `catalog/*/docs/**`, `docs/**`, `README.md` | None. The merge broker merges |
| B | The role tasks, defaults, handlers, and templates. `catalog/*/files/**`. Some `app.yaml` keys (summary, owner, size, os, ports, rollout). A deployment `vars`, `host_vars`, or `pin_until`. An allowed version bump. A wave change to wave 2 or later. A `timeout_min` of 120 or less | `oc-rev` or Faris approves |
| C | Every other change. Examples: a new or deleted entry or deployment, `playbooks/`, `policy/`, `scripts/`, `environments.yaml`, `.gitea/`, a version bump that is not allowed | Faris approves |

- Only the merge broker or Faris merges a PR.
- A new entry starts with the status `beta`. It runs in `stg` only. The change from `beta` to `stable` is Class C, after 7 days of green results in `stg`.

## Write access

- Branch `main`: nobody can push. Only `ci-merger` and `faris` can merge. `faris` and `oc-rev` can approve. Checked live on 2026-11-02.
- Other branches: `ci-merger`, `faris`, `oc-cfg`, and `oc-infra` can push.
- The agent tools limit the files that each agent can write. The limits are in `mcp/devops_server.py` in the `openclaw` repo.

| Agent | Can write |
|---|---|
| `oc-cfg` | `catalog/**`, `deployments/**`, `playbooks/**`. Not `playbooks/roles/bao_create_secret` |
| `oc-infra` | `scripts/**`, `policy/**`, `docs/**`, `README.md`, `ansible.cfg`, `requirements.yml` |
| `oc-net` | None |

No agent can write `.gitea/**`.

## Key concepts

- **Catalogue entry:** `catalog/<app>/app.yaml`. It sets the owner, status, role, size, ports, flows, secrets, version, and health check. The pattern is `single`, `pair`, or `cluster`.
- **Deployment:** `deployments/<zone>/<app>.yaml`. It sets the hosts, the version, the wave, and the variables. The hosts must exist in the guest specs of `homelab-infra`.
- **Wave:** `environments.yaml` gives each zone a wave. `stg` is wave 1. `shr`, `int`, and `wifi` are wave 2. `iot` and `dmz` are wave 3. Wave N+1 starts only after every guest of wave N passes the L4 checks and the wave delay ends.
- **L4 check:** a Goss test in `catalog/<app>/tests/` that checks the application on the guest. It is read-only.
- **Secret:** a role never sees a token. The role `bao_create_secret` creates a secret in OpenBao with a short-lived token for one application.
- **No-apps list:** `policy/no-apps.yaml` lists the VMIDs that never receive an application. VM 210 is on the list.

## Local checks

Run these checks on your branch before the PR:

```
python3 scripts/catalog.py --root . validate
python3 scripts/lint_gate.py --root .
yamllint -d relaxed .
gitleaks dir . --no-banner --redact
python3 <standards checkout>/lint/naming.py --repo apps --apps . --standards <standards checkout> --branch <branch> --title "<title>"
python3 -m unittest discover scripts/tests
```

Compute the class of a change:

```
git diff --raw -M <base> <head> > changes.txt
python3 policy/classify.py --base <base checkout> --head <head checkout> --changes changes.txt
```

The output is JSON with the class and the reason for each path.

## Operations

| Workflow | Runner | When (MYT) | What |
|---|---|---|---|
| `pr` | `host-plan` | Each PR event | Lint, validate, render, classify, gate |
| `deploy` | `host-apps` | A push to `main`. A dispatch from `homelab-infra` or an OpenClaw tool | Deploy in waves, then run the L4 checks |
| `verify` | `host-apps` | 03:15 every day. A dispatch | L4 checks and drift check. Read-only |

A deploy run does these steps:

1. Lock the hosts of the deployment. The lock waits for `homelab-infra` to release them. The locks are in `/var/lib/devops/locks`.
2. Take a `pre-deploy_<timestamp>` snapshot of each guest. The snapshot is kept for 7 days.
3. Run the role on each guest, one guest at a time.
4. Run the L4 checks. Then start the next wave after the wave delay of 1 hour.

- A failed wave stops the run. The pipeline opens an `app-verify: <app>/<host>` issue.
- The `deploy` and `verify` workflows refuse every ref except `main`.
- The runner `host-apps` runs as the user `runner-apps` with the AppRole `ci-apps`.
- A run is refused before it changes anything when its worst-case time is more than 6 hours.

## Secrets and safety

This repo holds no secret value. The deploy logs in to OpenBao with the AppRole `ci-apps`. It mints one create-only child token for each application, and it logs in again before each token.

- Caution: the deploy guard refuses a guest in one of these cases. The guest is not in the guest specs. The guest is protected. The IP or zone does not match the spec.
- Caution: never add a VMID from `policy/no-apps.yaml` to a deployment.
- Caution: the role `bao_create_secret` is Class C. It is the only controller-side action that a catalogue role can use.
- Caution: `.gitea/**` is Class C. Faris or Claude writes it, and no agent writes it.

## Documents

| Document | Topic |
|---|---|
| `docs/adding-an-app.md` | Add a catalogue entry and a deployment |
| `docs/orchestration.md` | Triggers, locks, waves, and who owns a failure |
| `docs/verification.md` | The L4 checks, the drift check, probes, and peer checks |
| `docs/patching.md` | Version bumps, reboots, and snapshots |
| `playbooks/roles/bao_create_secret/README.md` | The secret role |
| `catalog/<app>/docs/` | The design and the runbook of each application |

The design of this repo is in `docs/proposals/homelab-apps/` in the `homelab-infra` repo.

## Known gaps

- `docs/orchestration.md` still says that the deploy runs in check mode until the cutover. The deploy runs in apply mode.
- A run that changes deployments in all three waves can pass the 6-hour limit. The run is then refused. Dispatch `deploy` for a smaller set of applications to split the run.
