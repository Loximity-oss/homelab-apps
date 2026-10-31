# GNS3 server - runbook

Host: `hm1-stg-net01` (`deployments/stg/gns3.yaml`). Role, templates and defaults: `catalog/gns3/`.

## Check
- `systemctl is-active gns3-server docker`
- `curl -s http://127.0.0.1:3080/v3/version` (public; `{"controller_host":...,"version":"3.0.6",...}`)
- `journalctl -u gns3-server -n 50 --no-pager`
- `getcap /usr/local/bin/ubridge` (expect cap_net_admin, cap_net_raw), `id gns3` (groups kvm, docker), `ls -l /dev/kvm`
- Web UI from a `tru` client: `http://192.168.76.10:3080` (needs the UDM policy `Allow TRU to GNS3 UI and consoles`, Class C, Faris).

## Admin password
- Login `admin` with the password in OpenBao `stg/secret/apps/gns3/hm1-stg-net01` (key `password`; Faris reads it, agents cannot).
- **Secret exists in OpenBao but `/etc/hm1/gns3-escrow.done` is missing** (deploy died between write and marker, or the guest was rebuilt): the role fails at "Store the admin credentials once". The password on the guest (`/etc/hm1/gns3-admin.pass`) may differ from OpenBao. Decide: delete the OpenBao secret (Faris) and re-run the deploy, or write the marker by hand if the stored value is still the right one.
- Changing the password in the UI does not change the config file; the config value is only used when the server database is first created. Keep OpenBao in sync by hand.
- Lost password: stop the service, delete `/var/lib/gns3/conf/` database state is separate (`/var/lib/gns3/.config/GNS3/...`), so simplest is to reset the admin password through the UI as another admin; if none, remove the controller DB (loses users, not projects) and redeploy.

## Common problems
- **Service does not start, "hardware acceleration" error**: /dev/kvm missing or not accessible. Check `ls -l /dev/kvm`, `id gns3`, the guest spec `cpu_type: host` and nested virtualisation on the Proxmox host (infra, Faris).
- **Config rejected at start**: `journalctl -u gns3-server` shows the pydantic error with the key; the config is rendered from `files/gns3_server.conf.j2`. Revert the PR; the next deploy reinstalls the previous config.
- **ubridge: operation not permitted** on a node link: `getcap /usr/local/bin/ubridge` is empty (the file was replaced without capabilities). Re-run the deploy after deleting `/etc/hm1/gns3-ubridge-<version>` (forces `make install` again).
- **Node console unreachable from tru**: only 5000-5099/tcp are allowed (flows.yaml). More than ~100 consoles need a flow and `app_gns3_console_end` change by PR.
- **Lab links**: net1..net9 appear as ens19-ens23, ens1-ens4 (names vary). They are up without IP; GNS3 binds to them via ubridge. Do not put addresses on them.
- **Docker nodes fail to start**: `systemctl is-active docker`, `id gns3` must list docker.
- **Wheel install fails (dependency build)**: needs egress to PyPI (443/tcp) and `build-essential`, `python3-dev` (installed by the role).

## Restore / rebuild
No backups (A-1). A rebuilt guest starts empty: delete the stale OpenBao secret first (see Admin password), then redeploy.
