# k3s HA cluster - design note (Configuration Engineer)

Moved from homelab-infra `docs/apps/k3s.md` (migration step 8, infra #127). Paths below are in this repo. Until the
cutover (step 10) infra still deploys the cluster; this entry has no deployment yet and nothing deploys from it.

| | |
|---|---|
| Guests / zone | zone shr, VMs cloned from template 9000 (Ubuntu 26.04). Control (role `k3sc`): `hm1-shr-k3sc01..03` (VMID 310-312, 192.168.70.30-.32, 2 vCPU / 4 GB / 20 GB, `lvmt-nvme01`). Workers (role `k3sw`): `hm1-shr-k3sw01..02` (VMID 313-314, .33-.34, 4 vCPU / 6 GB / 40 GB, `lvmt-sata01`) |
| VMs, not LXC | k3s needs kernel modules (overlay, br_netfilter), sysctls and /dev/kmsg, which the unprivileged LXC module cannot give |
| Role / files | `catalog/k3s/role/` (one role, node type from the host's role code: `k3sc` server, `k3sw` agent; `tasks/escrow.yml` for the token escrow), templates in `catalog/k3s/files/` (`config.yaml.j2`, `k3s.service.j2`, `keepalived.conf.j2`, `k3s-api-check.sh`) |
| Version | k3s `v1.36.5+k3s1`, binary from the official release, sha256 pinned in `app.yaml` `version.known`. `bump_class: C` (every k3s change is Class C) |
| API endpoint | VIP `192.168.70.200:6443` (shr vip band .200-.239), keepalived VRRP on the control nodes. No service DNS name yet |
| Ports | 22 (CT 300), 6443 API, 2379-2380 etcd, 10250 kubelet, 8472/udp flannel VXLAN, VRRP (IP protocol 112) between control nodes, 30000-32767 NodePort if used. All five nodes share VLAN 20: node-to-node traffic is not filtered. No host firewall (ufw inactive) |
| Data | `/var/lib/rancher/k3s` (etcd in `server/db`, snapshots in `server/db/snapshots`), `/etc/rancher/k3s/config.yaml` and `agent-token` (0600) - **not backed up** (open item A-1) |
| Secrets | names only: `shr/secret/apps/k3s/hm1-shr-k3sc01` keys `server_token`, `agent_token` (create-only via `bao_create_secret`, never read by agents). Nothing in Git |
| TLS | k3s internal CA. API serving cert SANs: VIP, node IP, hostname. No ACME/OpenBao PKI |
| Upgrade | add the new version and sha256 (release `sha256sum-amd64.txt`, line `k3s`) to `version.known`, change `version.pin` (Class C), one minor version at a time, `etcd-snapshot save` first. Rolling, one node at a time, control nodes first |
| Healthcheck | `health` in `app.yaml`, run by `playbooks/deploy.yml` after each node and before the next: control node = `/readyz` is `ok`, own node `Ready`, `k3s` active; worker = `k3s-agent` active and kubelet `healthz` ok. Retries for up to 250 s inside the command |
| L4 | `tests/l4.goss.yaml.j2`, scoped per role (proposal 4.5); new, infra had none. Deferred: VIP holder check (deployment level, needs the verify script) |

## Differences from the infra role (svc_k3s)
| Topic | Infra | Here |
|---|---|---|
| File paths | `{{ playbook_dir }}/../../apps/k3s/...` | `{{ role_path }}/../files/...` |
| Node type | set per play in `site.yml` | from the host's role code (`k3sc` server, anything else agent) |
| Init host | `groups['role_k3sc'] \| sort \| first` | `(groups['role_k3sc'] \| sort)[0]` (`first` is not on the Jinja allow-list); the group is limited to the deployment's hosts |
| Token escrow | `uri` POST with `lookup('env', ...)`, OpenBao URL and CA in the role, its own last play | `bao_create_secret` with the tokens as `data` (no `generate`, no `hash`), no lookup, no OpenBao address; runs at the end of the first control node |
| Node Ready, `/readyz` retries | tasks in the role | `health` in `app.yaml` |
| VIP `/livez` check from the runner (`delegate_to: localhost`) | task in the role | not in the role (localhost is banned); L4 checks the VIP from every node, probes check `:6443` |
| Worker "node Ready" | asked on the first control node | worker `health` uses the kubelet healthz; L4 on the control nodes checks that all 5 nodes are `Ready` |
| Read-only tasks | plain | `check_mode: false` + `changed_when: false` (swap, `systemctl is-active`, `--version`, token and marker reads) |
| Rollout | control plane play, then worker play, then escrow play | one deployment, `serial: 1`, `order: sorted`: k3sc01..03 sort before k3sw01..02. Drain/uncordon hooks and the separate escrow play are not available in `deploy.yml` yet (deferred) |

The templates and the role logic are otherwise unchanged. The rendered `config.yaml`, unit files and `keepalived.conf` must be byte-equal to the live ones (checked in step 8 by the check-mode run).

## Design
- **Embedded etcd HA, 3 servers + 2 agents.** The first control node in sorted order (`k3sc01`) runs `cluster-init: true`; `k3sc02/03` join it with `server:` and `token:`. Quorum tolerates the loss of one control node. Workers join through the VIP.
- **Servers are tainted** `CriticalAddonsOnly=true:NoExecute`: workloads land on the two workers; bundled addons tolerate that taint and may run on control nodes.
- **Native binary and own systemd unit** (no `get.k3s.io` script): `get_url` with sha256 check, `/usr/local/bin/k3s`, units `k3s.service` (server) and `k3s-agent.service` (agent). `KillMode=process` so a k3s restart does not kill pods.
- **Kernel prerequisites in the role:** modules `overlay`, `br_netfilter`, sysctls for forwarding and bridge netfilter, swap off (now and fstab).
- **secrets-encryption** on. etcd snapshots every 12 h, keep 10, on the control nodes' local disk only.
- **Restart rule.** k3s is restarted only if it was already running and its binary, config or unit changed; a first start is never followed by a restart. No handler restarts k3s (only keepalived).
- **Rollout safety.** `serial: 1`, `any_errors_fatal`, `health_between_hosts`: a node that does not become `Ready` stops the run before the next node is touched.
- **cred/passwd.** The control-node unit drops `server/cred/passwd` before every start (crash loop after the 2026-10-17 host reboot); see the runbook.
- **Anonymous API access stays off.** `/livez` and `/readyz` answer 401 without credentials; checks treat 200 and 401 as "the API is serving".
- **API VIP: keepalived** (distro package), unicast VRRP between the control nodes, router id 70, priorities 150/140/130, health script `k3s-api-check`. No kube-vip: no in-cluster component or image to pin.
- **Join tokens.** Server token: generated by k3s on `k3sc01`, read only by `k3sc02/03`. Agent token: distinct secret generated once on `k3sc01` (`openssl rand -hex 32`), set as `agent-token` on all control nodes and as `token:` on workers; it cannot join a control node. Both are read from `k3sc01` during the run (controller memory, `no_log`).
- **Escrow.** Both tokens are written once to `shr/secret/apps/k3s/hm1-shr-k3sc01`, create-only, through `bao_create_secret`. The marker `/etc/hm1/k3s-tokens-escrowed` is written after the successful write only. If the secret exists but the marker is gone the role fails and a human decides (runbook).
- **Patching ring:** all five nodes `ring1` (Sev1/2 packages auto-apply); every reboot waits for Faris. `reboot.policy: drain`.

## Check mode
Read-only tasks carry `check_mode: false` + `changed_when: false`. Known check-mode exceptions: the `creates:`-gated `openssl rand` (skipped when the token exists) and the `--version`-gated download block. A check-mode run from main must change nothing on 310-314.

## Open points
- No backup of etcd snapshots or `/var/lib/rancher` (A-1); snapshots are local only.
- Optional standards PR: service name `k8s` for the VIP.
- Ingress/LoadBalancer exposure and persistent storage (local-path only) are not designed.
- Kubeconfig `/etc/rancher/k3s/k3s.yaml` on control nodes is a cluster-admin credential: handle as a secret, never commit.
- Deferred in this entry: rollout `groups` with drain/uncordon, a VIP probe module that accepts 401, the VIP-holder L4 check, per-role `size` and probe `roles` read by `plan_from_catalog` and the file_sd generator (schema additions, step 11/12).
