# AdGuard Home - runbook

Moved from homelab-infra `docs/apps/adguard-runbook.md` (migration step 7, infra #127).

Hosts: the hosts of the `deployments/<zone>/adguard.yaml` files (wifi 501/502, iot 601/602). Role, templates and shared values: `catalog/adguard/`.

## Where config lives and how to change it
- Config is code. Change `catalog/adguard/files/AdGuardHome.yaml.j2` or the values (`role/defaults/main.yml` for all zones; `vars`/`host_vars` in the deployment for one zone or host) **by PR only**. The deploy renders and installs it on the deployment hosts and restarts AdGuard only when something differs.
- **Manual edits are reverted.** Editing `AdGuardHome.yaml` on a guest or in the UI (users, DHCP, clients, rewrites, rules, filters, upstreams, retention) is detected at the next deploy, overwritten from the template, and AdGuard restarts. The log shows the reverted key paths. If a UI change is wanted, put it in the PR. Query log and stats data are not affected.
- For a hotfix during an incident use the UI/ssh, then codify via PR before the next deploy.

## Status report in the deploy log
Every deploy ends with the read-only task `AdGuard status | report` (tag `adguard_status`), one line per host:

`ADGUARD-STATUS hm1-wifi-dns01: service=active rules=[ 1=<n> 3=<n> 4=<n> 5=<n>] journal_err_10m=0 drift_rc=0 dig=ok <ip> OK`

- `service`: `systemctl is-active AdGuardHome`. Anything but `active` fails the play.
- `rules`: `<filter id>=<rule count>` per enabled filter, counted from `/opt/AdGuardHome/data/filters/<id>.txt` (lines that are not blank, not comments `!`/`#` and not the `[Adblock Plus ...]` header). Rough expectation: id 1 in the 100k range, 3 ~10k+, 4 ~3.5k+, 5 ~1.3k. An enabled filter with 0 rules (or a missing file) after ~60s of retries fails the play; the report line is printed before it.
- Lists are downloaded only on a new guest or when a filter has a new id. An existing guest loads its lists from disk and keeps the old file if a refresh fails, so a count there can be stale. When swapping a list, use a new filter id (retired ids, 2, are not reused) and remove the old one.
- `journal_err_10m`: lines matching `error|failed` in the last 10 minutes of the journal. Not fatal; non-zero usually means a failed list download or an upstream problem.
- `drift_rc`: managed vs live config. 0 in sync, 1 drifted keys, 2 cannot compare. Expected 0 after a deploy; not fatal.
- `dig`: answer for example.com from 127.0.0.1, `FAIL` when none. Not fatal here (the healthcheck already retries).
- Last word `OK` = nothing to look at; `WARN` = journal errors, drift rc != 0 or dig FAIL. No credentials are involved, so nothing sensitive is in the log.

## Check
- `systemctl is-active AdGuardHome`
- `dig +short @127.0.0.1 probe.hm1.lox-internal.dev` (expect 192.0.2.53) and `dig +short @127.0.0.1 example.com` (upstream path); from a client in the same zone `dig @<guest ip> example.com`
- `curl -sI http://127.0.0.1/` (200 or 302)
- `journalctl -u AdGuardHome -n 50 --no-pager`
- Drift now, read-only: `hm1-adguard-drift /opt/AdGuardHome/conf/AdGuardHome.yaml.managed /opt/AdGuardHome/conf/AdGuardHome.yaml; echo rc=$?`
- Memory: `free -m` and `systemctl status AdGuardHome` on the guest; guests have 1 GiB.

## Common problems
- **Service fails with "address already in use" on :53**: the resolved stub is back. Check `/etc/systemd/resolved.conf.d/adguardhome.conf`, `ss -lntup | grep :53`, re-run the deploy.
- **Guest cannot resolve (apt/download)**: `/etc/resolv.conf` must not point at 127.0.0.53; it should link to `/run/systemd/resolve/resolv.conf`, or use AdGuard on 127.0.0.1.
- **Upstream (DoH) failures**: check the guest reaches 1.1.1.1 / 9.9.9.9 and TCP 443 outbound (UniFi rules for the guest's zone).
- **probe.hm1 does not answer 192.0.2.53 but the service is up**: the live config lacks the rewrite; run the drift check, the next deploy reinstalls the template.
- **Status fails with an enabled filter at 0 rules**: the list download failed (DNS/egress to `adguardteam.github.io`, or too little memory). Check the journal and egress rules, then re-run the deploy.
- **Memory tight / OOM**: check `free -m` first. Raise the guest memory (Terraform spec, Infrastructure Engineer) or drop a list by PR. Do not add the Threat Intelligence Feeds list.
- **AdGuard restarts at every deploy**: a permanent drift. The log lists the key path; AdGuard normalises that value differently from the template. Align the value in the defaults by PR.
- **AdGuard rejects the config after a template change** (service fails to start, healthcheck fails): revert the PR; the next deploy reinstalls the previous config.
- **Deploy failed at "Admin password, once"**: platform problem (OpenBao policy or token for the per-app child token, missing zone namespace). No hash file was written, so the next run retries.
- **Secret exists in OpenBao but `/etc/hm1/adguard-admin.hash` is missing** (a deploy died between the two steps, or the guest was rebuilt): `bao_create_secret` fails (OpenBao answers 403 for an existing path). Faris deletes `<zone>/secret/apps/adguard/<host>` in OpenBao, then re-run the deploy.

## Admin password
Stored by `bao_create_secret` at `<zone>/secret/apps/adguard/<hostname>` (keys `username`, `password`). Agents cannot read it; Faris reads it from OpenBao. Rotation: Faris deletes the secret and `/etc/hm1/adguard-admin.hash` on the guest, then re-run the deploy. A password changed in the UI is reverted to this one.

## Upgrade / rollback
Add the new version with its sha256 (release `checksums.txt`) to `version.known` and change `version.pin` in `catalog/adguard/app.yaml` by PR. Rollback is the reverse PR.

## Data / backup
`/opt/AdGuardHome/data` holds the query log and stats; **not backed up** (open item A-1). Config and binary are rebuilt by the deploy.
