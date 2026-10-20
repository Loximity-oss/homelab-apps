# Verification

Design: homelab-infra `docs/proposals/homelab-apps/4-verification.md` (4.2 parts, 4.3 probes, 4.4 peer checks, 4.7 loop).

## Layers

| Layer | What | Owner |
|---|---|---|
| L1-L3 | platform, reachability, OS baseline (Goss `tests/os/*`) | homelab-infra |
| L4 | app checks, `catalog/<app>/tests/l4.goss.yaml.j2`, read-only | this repo |
| drift | app config vs desired (`drift: {in_l4: true}`) | this repo |
| probes | blackbox_exporter every 60 s, targets from specs + each entry's `probes` | openclaw (config), this repo (data) |
| peer checks | each host of a `pair`/`cluster` checks its partner(s) (`peer_check`) | this repo |

## When L4 runs

- After every deploy, per wave (change gate).
- Nightly 03:15 (`verify` workflow, from cutover): L4 plus app drift, one `app-verify: <app>/<host>` issue per failing host.
- L1-L3 run at 03:00 in homelab-infra.

## Writing L4

- Read-only; no secrets in the test file.
- Scope checks per role when an entry has several role codes.
- Keep checks that prove the real job (for example a real DNS query), not only that a port is open.
- Declare `health` in `app.yaml` as well: patching and reboots use it as their gate.

## The loop (4.7)

probe alert or failed test, then SRE triage, then a fix (PR, or hotfix then PR), then PM `verify_guest` / `verify_app`, then close the issue with evidence.
