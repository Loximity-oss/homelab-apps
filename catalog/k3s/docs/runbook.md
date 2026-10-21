# k3s cluster - runbook

Moved from homelab-infra `docs/apps/k3s-runbook.md` (migration step 8, infra #127).

Hosts: `hm1-shr-k3sc01..03` (VMID 310-312), `hm1-shr-k3sw01..02` (VMID 313-314). Role, templates: `catalog/k3s/`. API VIP 192.168.70.200:6443. Design: `docs/design.md` next to this file.

## Check
On a control node (`ssh_exec` on 310/311/312):
- `systemctl is-active k3s keepalived` (workers: `k3s-agent`)
- `k3s kubectl get nodes -o wide` (5 nodes `Ready`; servers show `control-plane,etcd`)
- `k3s kubectl get --raw '/readyz?verbose' | tail -5`
- `k3s etcd-snapshot ls` (snapshots), `ip -4 addr show | grep 192.168.70.200` (which node holds the VIP)
- `curl -ks -o /dev/null -w '%{http_code}\n' https://192.168.70.200:6443/livez` from any shr guest: expect `401` (anonymous auth is off; 401 means the API answers)
- `/usr/local/bin/k3s-api-check; echo $?` (0 = healthy, the keepalived check)
- `journalctl -u k3s -n 80 --no-pager` (workers: `-u k3s-agent`)
- Escrow done: `test -f /etc/hm1/k3s-tokens-escrowed && cat /etc/hm1/k3s-tokens-escrowed` on k3sc01
- Worker kubelet: `curl -s http://127.0.0.1:10248/healthz` (expect `ok`)

## Common problems
- **VIP not answering**: `systemctl status keepalived` and `journalctl -u keepalived` on the control nodes; a node in VRRP FAULT means `/usr/local/bin/k3s-api-check` fails (API not serving). VRRP is IP protocol 112 between the control nodes. Nodes still reach each other by IP; only workers and kubectl use the VIP. Do not enable `anonymous-auth` to "fix" a 401: 401 is the healthy answer.
- **A control node crash-loops with `server/cred/passwd newer than datastore and could cause a cluster outage`**: see the next section.
- **A control node does not join**: `journalctl -u k3s` on it; check it reaches `192.168.70.30:6443` and 2379-2380 between servers, clocks in sync, and `/etc/rancher/k3s/config.yaml` has `server:` and `token:` (the server token). Re-run the deploy (idempotent).
- **Worker stays NotReady / does not join**: reach `192.168.70.200:6443`, check `br_netfilter`/`overlay` (`lsmod`), `sysctl net.ipv4.ip_forward` = 1, swap off (`swapon --show` empty), `journalctl -u k3s-agent`. A worker's `token:` must be the agent token (`/etc/rancher/k3s/agent-token` on k3sc01); the server token does not belong on workers.
- **Pods stuck Pending on all workers**: the taint is intended on servers only; check `kubectl describe node`.
- **Lost quorum (2 of 3 control nodes down)**: API unavailable. Bring the nodes back first. Restoring from a snapshot is a last resort: `k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>` on one node, then wipe `/var/lib/rancher/k3s/server/db` on the others and rejoin (Faris decides).
- **Deploy failed at "Write once to OpenBao" (token escrow)**: it runs on k3sc01 first, so no other node was touched and the cluster is unchanged. The error names the cause: a refusal by `bao_create_secret` (OpenBao 403: the per-app policy lacks create on `shr/secret/apps/k3s/*`, the namespace is missing, or the secret already exists) is a platform matter for Faris. If the secret already exists but `/etc/hm1/k3s-tokens-escrowed` is missing (the run died between write and marker): Faris deletes `shr/secret/apps/k3s/hm1-shr-k3sc01` in OpenBao, then re-run the deploy.
- **Health failed after a node (deploy stops, later nodes untouched)**: read the node's `journalctl -u k3s` (workers `-u k3s-agent`), fix, re-run the deploy. The health command waits up to about 250 s per node.

## Crash loop: `cred/passwd newer than datastore`
Cause: k3s rewrites `/var/lib/rancher/k3s/server/cred/passwd` itself (join, restart, credential change). If its mtime is newer than the datastore's bootstrap record, k3s refuses the next START (checked at start only). A server that can reach the init server (k3sc01, `server:` in config of c02/c03) compares against it and passes; the init server (nobody to reconcile with) and any server that cannot reach k3sc01 fail. Seen 2026-10-17 after a host reboot: all three servers, including k3sc01, fataled; k3sc02/03 only failed while k3sc01:6443 was down.

Prevention: the unit of ALL control nodes (not workers) runs `ExecStartPre=-/bin/rm -f /var/lib/rancher/k3s/server/cred/passwd`, so every start (boot, crash restart, role restart) drops the file and k3s recreates it from the datastore (the documented k3s remedy).
- Why it is safe: the file only caches credentials, the datastore (etcd bootstrap data) is the source of truth.
- Downside: a real divergence (restore, a credential change not yet in the datastore) is masked; the datastore wins without a warning. Take etcd snapshots before credential changes or restores.

Manual recovery if it still happens (SRE), one control node at a time, never two together (etcd quorum):
1. Snapshot the VM first (`pre-change` snapshot) and, if the API still answers on another node, `k3s etcd-snapshot save` there.
2. Start with k3sc01: `systemctl stop k3s`, `mv /var/lib/rancher/k3s/server/cred/passwd /root/passwd.bak`, `systemctl start k3s`; k3s logs 'Updating bootstrap data on disk from datastore' and regenerates it. Wait for `/readyz` ok.
3. k3sc02/03 usually recover by themselves once k3sc01:6443 answers (restart loop ends on its own). If one does not, repeat step 2 on it, after the previous node is `Ready`.
4. Do not delete the file on a healthy, running node by hand; the unit does it at the next start anyway.

VM start order (k3sc01 first, delay before the others) is handled in Terraform (infrastructure-engineer), not here.

## Restart rule (role)
No handler restarts k3s (only keepalived). The role restarts k3s only if it was already running before the run and its binary, config or unit changed, so a no-op run never restarts it. A first start is never followed by a restart. The deploy is `serial: 1`, sorted, `any_errors_fatal`, and each node must pass `health` (`/readyz`, node `Ready`) before the next one starts, so a restart never takes quorum down. A unit change therefore restarts k3sc01, c02, c03 one after the other, then the workers.

## Replacing a node
- **Worker**: `kubectl drain <w> --ignore-daemonsets --delete-emptydir-data`, `kubectl delete node <w>`, then rebuild the VM (infra PR); the deploy joins it with the agent token from `k3sc01`.
- **Control node other than k3sc01**: `kubectl delete node <c>` and remove its etcd member, rebuild, deploy.
- **k3sc01 (first sorted host)**: do not rebuild blindly. A fresh k3sc01 would run `cluster-init` and start a new cluster, and generate a new agent token. Instead make it join: first temporarily set `svc_k3s_init_host` to another healthy control node (deployment `vars`, Class B; Faris approves the plan), copy `/etc/rancher/k3s/agent-token` to that node, rebuild, deploy, revert.

## Patching and reboots
All nodes are `ring1`. Reboots wait for Faris. One control node at a time: `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`, reboot, wait `Ready`, `kubectl uncordon`, check `get nodes` and `/readyz`, then the next. Never two control nodes together. Workers one at a time, drained first. A host reboot restarts all guests at once: see the crash loop section.

## Upgrade / rollback
Add the new version and its sha256 (release `sha256sum-amd64.txt`, line `k3s`) to `version.known` in `app.yaml` and change `version.pin` by PR (Class C; one minor version at a time). Take an etcd snapshot first (`k3s etcd-snapshot save`). The deploy upgrades control nodes then workers, one at a time, stopping on the first node that fails `health`. Rollback of a minor upgrade needs a snapshot restore.

## Tokens
Two different secrets, both root-only on the guests, never in Git or logs:
- **Server token** `/var/lib/rancher/k3s/server/token` on the control nodes (also in `config.yaml` of `k3sc02/03`). Lets a node join as a control node.
- **Agent token** `/etc/rancher/k3s/agent-token` on `k3sc01` (generated once), set as `agent-token` in `config.yaml` on all control nodes and as `token:` on the workers. Lets a node join as a worker only.

**Escrow in OpenBao.** Both tokens are copied once to `shr/secret/apps/k3s/hm1-shr-k3sc01` (keys `server_token`, `agent_token`) by the role on k3sc01 (`tasks/escrow.yml`, `bao_create_secret`), guarded by `/etc/hm1/k3s-tokens-escrowed` (root, 0600). The write is create-only: the pipeline can never read, overwrite or update it, and agents see key names only. Faris reads it in OpenBao (namespace `shr`, `secret/apps/k3s/hm1-shr-k3sc01`). Use it to rebuild or re-seed a control node after a total loss of the cluster nodes (the etcd data itself is not in it).

**Rotation.** The escrow is a one-time copy and is not updated by the pipeline. After rotating a token: Faris deletes the secret (all versions) in OpenBao, deletes `/etc/hm1/k3s-tokens-escrowed` on k3sc01, then re-run the deploy, which writes the new values.
- Agent token: delete `/etc/rancher/k3s/agent-token` on k3sc01 and re-run the deploy (control nodes restart one at a time, workers are re-configured and restarted), then redo the escrow as above.
- Server token: `k3s token rotate` on a server, then re-run the deploy, then redo the escrow as above.

## Data / backup
etcd and snapshots live in `/var/lib/rancher/k3s/server/db`; **not backed up** (open item A-1). Config, binary and units are rebuilt by the deploy; the two tokens are escrowed in OpenBao (the agent token is regenerated if lost and no escrow exists).
