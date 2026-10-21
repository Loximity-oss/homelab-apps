# AdGuard Home - design note (Configuration Engineer)

Moved from homelab-infra `docs/apps/adguard.md` (migration step 7, infra #127). Paths below are in this repo.

| | |
|---|---|
| Guest / zone | every guest with `role: dns` in a deployment. Planned: `hm1-wifi-dns01/02` (VMID 501/502, 192.168.69.10/.11, wave 2) and `hm1-iot-dns01/02` (VMID 601/602, 192.168.74.10/.11, wave 3); LXC, 1 core, 1 GiB, 8 GB |
| Service name | none (clients use the two IPs of their zone as DNS servers) |
| Ports | 53/tcp+udp DNS; 80/tcp admin UI on 0.0.0.0 (plain HTTP, reachable from the guest's own zone only; no TLS yet) |
| Data | `/opt/AdGuardHome/` (root-owned binary; `conf/AdGuardHome.yaml` live config; `data/` query log + stats) - **not backed up** (open item A-1). Config is code, so only query log/stats are lost |
| Ownership | `/opt/AdGuardHome` and the binary are root-owned (dir `root:adguard 0750`); only `conf/` and `data/` are writable by `adguard` |
| Secrets | names only: `<zone>/secret/apps/adguard/<hostname>` keys `username`, `password` (create-only via `bao_create_secret`, never read by agents). The guest keeps only the bcrypt hash in `/etc/hm1/adguard-admin.hash` (root, 0600). No secret is in the repo |
| TLS | none for the UI today |
| Upgrade | add the new version and its sha256 (release `checksums.txt`) to `version.known` and change `version.pin` in `app.yaml`, PR (bump class B). The service restarts once per host, one host at a time |
| Healthcheck | `health.command` in `app.yaml`: the `probe.hm1.lox-internal.dev` rewrite answers 192.0.2.53 (no upstream needed). The role also checks a real upstream query (`example.com`) and the web UI (200 or 302) |

## Single source of truth
| What | Where |
|---|---|
| Canonical config template (all zones) | `catalog/adguard/files/AdGuardHome.yaml.j2` |
| Shared values: ports, bind, upstreams, bootstrap, cache, retention, blocklists, rewrites, user rules, clients, allow/deny, paths, service user | `catalog/adguard/role/defaults/main.yml` |
| Per-zone / per-host overrides (only what truly differs) | `deployments/<zone>/adguard.yaml` `vars` / `host_vars` |
| Binary version and checksum | `version` in `app.yaml` (set into `app_adguard_version/url/sha256` at deploy) |
| Admin password | generated at first deploy by `bao_create_secret`, OpenBao `<zone>/secret/apps/adguard/<hostname>`; only the bcrypt hash is on the guest |

Precedence: deployment `vars`/`host_vars` beat role defaults; the catalogue sets only the three version variables. Lists are replaced, not merged, except `app_adguard_rewrites_extra`, `app_adguard_user_rules_extra`, `app_adguard_clients_extra`, which are appended: use those for zone-specific entries. Deployment values are plain data (no Jinja).

Today there are no overrides: iot and wifi get the identical config (only the admin hash differs per host).

## Design
- Native binary, no Docker. Release `v0.107.79` (linux_amd64), sha256 verified by `get_url`. Runs as user `adguard` with only `CAP_NET_BIND_SERVICE`; `--no-check-update` (upgrades are by PR).
- `systemd-resolved` stub listener is switched off and `/etc/resolv.conf` re-pointed if it was the stub symlink.
- Upstreams: DoH Cloudflare + Quad9, `parallel`; bootstrap 1.1.1.1 / 9.9.9.9; cache 4 MiB; query log and stats 7 days (`168h`).
- **probe rewrite.** `probe.hm1.lox-internal.dev -> 192.0.2.53` (TEST-NET, never routed) is rendered by the template itself, always first, so a deployment cannot drop it. It exists in the config before the service first starts, so the first healthcheck, `health` and the blackbox probes need no WAN or upstream. L4 checks the rewrite and the partner's rewrite (peer check) and keeps the real upstream query check.
- **Blocklists.** Four lists from AdGuard-hosted copies: id 1 AdGuard DNS filter, 3 Phishing URL Blocklist, 4 Malicious URL Blocklist (URLhaus), 5 Stalkerware Indicators List. Id 2 (HaGeZi Multi PRO) is retired and must not be reused (a same-id URL swap keeps the old file). Do not add Threat Intelligence Feeds (`tif.txt`, about 1.95M rules): too large for 1 GiB guests.
- **Removing a filter.** The template renders only the ids in `app_adguard_filters`; a removed list drops out of the managed config at the next deploy (drift, config replaced, one restart). The role never deletes `data/filters/<id>.txt`; it costs disk only. Remove it by hand if the space matters.
- **Convergence.** AdGuard rewrites its own YAML, so bytes are never compared. The role renders `conf/AdGuardHome.yaml.managed`, then `hm1-adguard-drift` (`files/adguard-drift.py`, needs `python3-yaml`) checks that every key set by the template has the same value in the live file. On any difference, or no live file, the **whole live file is replaced** by the rendered template and AdGuard is restarted; otherwise nothing changes. The deploy log lists the drifted key paths (never values).
- **Validation before install.** The render goes to `AdGuardHome.yaml.managed` (never the live path) and is checked with `AdGuardHome --check-config`, run as the `adguard` user. If it fails, the live file is not touched, nothing restarts and the rollout stops.
- **Serial rollout.** `rollout: serial 1, sorted, stop_on_failure, health_between_hosts`: dns01 before dns02, a failure stops the remaining hosts; the other resolver of a zone keeps answering.
- **Check mode.** Read-only tasks (`--version`, drift script, `--check-config`, health and status commands) carry `check_mode: false` + `changed_when: false`. Known check-mode exceptions: the `.managed` rewrite and the `--version`-gated download block. A check-mode run from main must change neither the live file nor the service.
- **Admin password.** `bao_create_secret` (run on the controller) writes it create-only to OpenBao and returns only the bcrypt hash; the plaintext never reaches the role or the guest. No `lookup`, no OpenBao address or CA path in this role.
- **What is managed (reverted on drift):** `http`, `users`, `dns` (bind, port, upstreams, bootstrap, cache, allowed/disallowed clients), `tls.enabled`, `dhcp.enabled`, `querylog`, `statistics`, `filtering` (enabled, blocking mode, rewrites), `filters`, `user_rules`, `clients.persistent`. UI edits to these are overwritten, including a changed admin password (the one in OpenBao stays valid). Settings the template does not mention (safe-browsing, parental control, safe search) reset to AdGuard defaults whenever the live file is replaced; add them to the template to keep them.

## Risks
- If AdGuard normalises a value differently from the template, the drift check reports a permanent drift and restarts AdGuard at every deploy. The deploy log shows the key path; align the value in the defaults.
- Existing secret but missing hash marker: `bao_create_secret` refuses (OpenBao answers 403 for an existing path); see the runbook.

## Open points
- No backup of query log/stats (A-1). The UniFi DHCP/DNS settings and the zone's DNS flows (wifi, iot are gateway-routed: UDM policy from Faris) are network tasks.
