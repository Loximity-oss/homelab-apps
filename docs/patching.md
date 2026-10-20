# Patching

Design: homelab-infra `docs/proposals/homelab-apps/3-patching.md` (3.1 ownership, 3.3 app versions, 3.4 reboots, 3.5 snapshots). OS patching: homelab-infra `docs/patching-os.md`.

## Who patches what

| Bits | Owner |
|---|---|
| OS packages, including distro packages an app needs | homelab-infra (rings) |
| App binaries pinned by the catalogue | this repo (version pin and bump PR) |
| Templates, PVE host, protected CTs | infra / Faris / unattended updates |

After patching a host, infra runs that host's catalogue `health` from a read-only clone of this repo. Unhealthy stops the rollout.

## App version bumps

1. Monday 04:30 the version check opens `app update: <app> <version>` with the new sha256 and release notes (security releases flagged Sev2).
2. oc-cfg changes `version.pin` and adds the checksum to `version.known`.
3. Class: `bump_class` for patch/minor (default B); major is C; k3s is always C; unparsable versions are C.
4. Merge deploys in waves: stg, L4 green, `wave_delay`, then production; one host at a time with `health` between hosts.
5. Hold back: a deployment pins an older `known` version with `pin_until` (B); the weekly check reminds after the date.

k3s uses `rollout.groups` hooks: drain, upgrade, uncordon, quorum check; control nodes first, one at a time.

## Reboots

Every guest reboot waits for Faris's `reboot` comment on `patch: reboot <host>`. Reboots run through the infra `reboot` workflow and follow the entry's `reboot.policy`:

- `one_at_a_time`: snapshot, reboot, wait for `health`.
- `pair`: refuse unless the partner is healthy; wait for `health` on both hosts after.
- `drain`: `before` (kubectl drain), reboot, `health`, `after` (uncordon); never two control nodes at once.

## Snapshots

`pre-deploy_*` is taken by deploy and pruned after 7 days (03:00 verify). Rollback outside stg is manual by the SRE.
