# homelab-apps

Application layer: catalogue + deployments (homelab-infra docs/proposals/homelab-apps, #127)

## Layout

```
catalog/<app>/        app.yaml, role/, files/, tests/l4.goss.yaml.j2, docs/{design,runbook}.md
catalog/_example/     skeleton entry (status example, compose-based)
deployments/<zone>/   <app>.yaml: hosts, version, wave, vars
environments.yaml     what each zone means for apps (waves, status allowed)
playbooks/            deploy.yml
scripts/  policy/     pipeline helpers, lint, classifier, tests
.gitea/workflows/     pr, deploy, verify (Faris/Claude only)
docs/                 guides below
```

| Guide | Topic |
|---|---|
| `docs/adding-an-app.md` | add a catalogue entry and a deployment |
| `docs/orchestration.md` | triggers, locks, waves, failure ownership |
| `docs/verification.md` | L4, drift, probes, peer checks |
| `docs/patching.md` | app version bumps, reboots, snapshots |

The boundary rule: guest config (exists, reachable, patched, testable) lives in homelab-infra; what makes a guest do its job lives here.

## Pipeline

| Workflow | When | What |
|---|---|---|
| `pr` | every PR (`pull_request_target`, main's code) | lint, validate, classify, offline render; writes the class for the merge broker |
| `deploy` | push to `main`, dispatch | deploys in waves; **check mode until cutover** (migration step 10) |
| `verify` | dispatch (nightly 03:15 from cutover) | L4 + app drift, read-only |

Merges: only the merge broker (infra #135) or Faris. Class A merges when `pr` is green; B needs `oc-rev` or Faris;
C needs Faris. No agent writes `.gitea/**` (infra #126).
