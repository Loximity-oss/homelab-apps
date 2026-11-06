# Samba AD DC - runbook

Hosts: those in `deployments/<zone>/samba-dc.yaml` (stg: hm1-stg-dc01). Role, templates and values: `catalog/samba-dc/`. Design and fallback: `design.md`.

## Where config lives and how to change it
- `catalog/samba-dc/files/smb.conf.j2` and `role/defaults/main.yml` (all zones) or `vars`/`host_vars` of the deployment (one zone/host), by PR. The deploy re-renders `/etc/samba/smb.conf` (validated with `testparm`) and restarts samba-ad-dc only when it changed. Manual edits of `smb.conf` are reverted.
- Domain data (users, groups, DNS records, GPOs) lives inside Samba and is not managed by the deploy.

## Check
- `systemctl is-active samba-ad-dc chrony` (smbd, nmbd, winbind must be inactive and masked)
- `samba --version`; `samba-tool domain info 127.0.0.1`
- `dig +short @127.0.0.1 _ldap._tcp.dc._msdcs.ad.lox-internal.dev SRV` and `dig +short @127.0.0.1 example.com`
- `smbclient -L 127.0.0.1 -N` (sysvol, netlogon), `samba-tool ntacl sysvolcheck`
- `ss -lntup | grep -E ':(53|88|389|445|636) '`
- `journalctl -u samba-ad-dc -n 50 --no-pager`
- Time: `systemctl status chrony`, `ls -l /var/lib/samba/ntp_signd/`
- Kerberos (interactive, needs the password from OpenBao): `kinit Administrator@AD.LOX-INTERNAL.DEV` from a client with `krb5-user`.

## Administrator password
Generated on the guest at the first deploy (`/etc/hm1/samba-dc-admin.pass`, root 0600), escrowed in OpenBao `<zone>/secret/apps/samba-dc/<host>` (create-only; read it as Faris). To rotate: `samba-tool user setpassword Administrator` on the guest, then update OpenBao by hand (Faris; the pipeline never overwrites).
**Secret exists in OpenBao but `/etc/hm1/samba-dc-escrow` is missing** (deploy died between write and marker, or guest rebuilt): the role fails with the escrow message. Faris reads or deletes the OpenBao secret; if the domain is not provisioned yet delete the secret and re-run the deploy; if it is, record the existing value and create the marker by hand.

## Common problems
- **Provision fails in the unprivileged LXC** (feasibility test): read the error (`samba-tool` output in the deploy log; typical: xattr, `chown`/`lchown` EINVAL on sysvol, idmap). It means the LXC route is not feasible: follow the fallback in `design.md` (privileged LXC or VM) via the Infrastructure Engineer and Faris. In stg a failed first provision is cleaned by `rm -rf /var/lib/samba/private/* /var/lib/samba/sysvol/* /etc/samba/smb.conf` and a redeploy (never on a domain with users).
- **samba-ad-dc fails on :53 "address in use"**: the resolved stub is back. Check `/etc/systemd/resolved.conf.d/samba-dc.conf`, `ss -lntup | grep :53`, re-run the deploy.
- **Guest cannot resolve (apt)**: `/etc/resolv.conf` should say `nameserver <own IP>`; if it is a stub link the role relinks it for apt and rewrites it at the end of a run. After a CT restart Proxmox may rewrite `resolv.conf` (spec `nameserver`): ask the Infrastructure Engineer to set the spec nameserver to the DC's own IP and search domain to `ad.lox-internal.dev`; L4 `app-dc-resolv-conf` fails until then.
- **Provision complains the hostname/FQDN does not resolve to the address**: `/etc/hosts` must carry `<ip> <host>.ad.lox-internal.dev <host>` and no `127.0.1.1` line; the role writes it (Proxmox may rewrite `/etc/hosts` on a hostname change).
- **External names do not resolve**: forwarders unreachable. stg `1.1.1.1 8.8.8.8` need egress 53 (UniFi/coresw rules); wifi uses hm1-wifi-dns01/02. Check `dig @<forwarder> example.com` from the guest.
- **chrony does not start**: in a container it needs the drop-in `/etc/systemd/system/chrony.service.d/container.conf` and `DAEMON_OPTS="-x"` in `/etc/default/chrony`; `journalctl -u chrony`. Members get time errors if the socket `/var/lib/samba/ntp_signd/socket` is missing (samba not running) or the dir is not group `_chrony`.
- **Series check fails** (`samba --version` not `4.23.`): the OS patching moved the package to a new series; bump the catalogue (`design.md`, Upgrade).
- **Healthcheck SRV empty after a restart**: samba needs up to a minute to register records; `samba_dnsupdate --verbose --all-names` repairs missing records.
- **L4 `app-dc-eadb` fails**: `posix:eadb` missing from smb.conf or the tdb empty; re-run the deploy; if the tdb was lost, `samba-tool ntacl sysvolreset`.

## Restore / rebuild
Data is not backed up (open item A-1). Before risky work: `samba-tool domain backup offline --targetdir=/root/` (stops the DC briefly) or an SRE snapshot. Rebuild of a lost single DC: a new provision creates a new domain (new SIDs); members must rejoin. Restoring from `samba-tool domain backup restore` needs the backup file, which is only on the guest unless copied off by hand.
