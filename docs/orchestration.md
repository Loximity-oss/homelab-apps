# Orchestration

Design: homelab-infra `docs/proposals/homelab-apps/2-orchestration.md` (2.1 triggers, 2.3 locks, 2.5 waves, 2.7 failure ownership).

## Who triggers whom

- Triggers are one-way: homelab-infra to homelab-apps. Reads go both ways, read-only.
- Infra apply: locks, snapshot, Terraform, Ansible base, L1-L3, then dispatches `deploy` for hosts that passed and are in a deployment. The switch is `APPS_DISPATCH` in infra's apply workflow (off until cutover, migration step 10).
- Apps PR merged: `deploy` runs for the changed entries and deployments, in waves.
- `deploy` and `verify` refuse any ref except `main`. Until cutover, deploy runs in check mode.

## Per run

1. Lock the deployment hosts (waits for infra to release; `/var/lib/devops/locks/host-<hostname>.lock`).
2. Snapshot `pre-deploy_<ts>` per host (kept 7 days).
3. Run the role per deployment; deployment `vars` become inventory group vars, never extra-vars (1.7).
4. Deploy guard first: host in specs, not protected, IP matches spec, zone matches.
5. L4 tests, then a report and a comment on the originating PR.

## Waves (2.5)

- Waves come from `environments.yaml`: stg 1, shr/int/wifi 2, iot 3, dmz 3.
- Wave N+1 starts only when every host of wave N passed L4 and `wave_delay` (default 1 h) elapsed.
- Time budget: a deployment may set `timeout_min` (5..240, default 60) for its play. Up to 120 is Class B, more is Class C. Before an apply run changes anything, `ci-deploy.sh` adds lock wait (60 min) + per wave 15 min slack + its plays' budgets + the wave delays; if that exceeds `APPS_RUN_MAX` (21600 s = the runner `timeout`, 6 h) the run is refused. The runner `timeout` is root-owned: a PR can ask for time under it, never raise it.
- Inside a deployment, hosts go one at a time; the next starts after the previous passes `health`.
- A failed wave stops the run, raises `pipeline.failed` and an `app-verify: <app>/<host>` issue. stg rolls back automatically; elsewhere the SRE restores from `pre-deploy_*` and a revert PR follows. `redeploy_app(app, from_wave=N)` resumes.

## Pipeline in this repo

See the Pipeline table in `README.md`. No agent writes `.gitea/**`; workflows are Class C, written by Faris or Claude.

## Failure ownership (2.7)

| Fails | Signal | First responder |
|---|---|---|
| deploy, L4, drift, wave stop | `app-verify: <app>/<host>` issue, PR comment | SRE, then oc-cfg |
| PR lint, render or check | red `pr` check on the PR | PR author |
| lock wait timeout | `pipeline: lock wait timeout <host>` | SRE, then oc-infra |
