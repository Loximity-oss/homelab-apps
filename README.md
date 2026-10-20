# homelab-apps

Application layer: catalogue + deployments (homelab-infra docs/proposals/homelab-apps, #127)

## Pipeline

| Workflow | When | What |
|---|---|---|
| `pr` | every PR (`pull_request_target`, main's code) | lint, validate, classify, offline render; writes the class for the merge broker |
| `deploy` | push to `main`, dispatch | deploys in waves; **check mode until cutover** (migration step 10) |
| `verify` | dispatch (nightly 03:15 from cutover) | L4 + app drift, read-only |

Merges: only the merge broker (infra #135) or Faris. Class A merges when `pr` is green; B needs `oc-rev` or Faris;
C needs Faris. No agent writes `.gitea/**` (infra #126).
