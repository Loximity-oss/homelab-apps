# Samba AD DC - design

Status: beta (stg only). Entry `catalog/samba-dc/app.yaml`, role `catalog/samba-dc/role/`, runbook `docs/runbook.md`.

## What
One Samba Active Directory domain controller: realm `AD.LOX-INTERNAL.DEV` (DNS domain `ad.lox-internal.dev`), NetBIOS domain `LOX`, Samba internal DNS backend, RFC2307 attributes on. Role code `dc`. Ubuntu 26.04 package `samba-ad-dc` (4.23.x); no upstream tarball, so the catalogue pin is the upstream version with no checksum and the role asserts the series (`4.23.`). OS patching updates the package; a new series needs a bump PR (class C).

## Route (Faris approved)
1. stg: `deployments/stg/samba-dc.yaml`, one host `hm1-stg-dc01`, as an unprivileged LXC. This is the feasibility test.
2. `beta` to `stable` (class C) after 7 days green in stg.
3. `deployments/wifi/samba-dc.yaml` (wave 2, needs the wifi guest PR infra #217 and a UDM policy from Faris).

## Sizing and size type
`lxc`, 2 cores, 2048 MB, 16 GB (spec: `datastore` default sata). A DC for a handful of objects is small. Features needed on the CT: `nesting=1` is already the baseline; no extra features expected for the unprivileged attempt.

## Ports
DNS 53 tcp/udp, Kerberos 88 and kpasswd 464 tcp/udp, NTP 123/udp, RPC 135/tcp and dynamic 49152-65535/tcp, NetBIOS 137/138 udp and 139/tcp, LDAP 389 tcp/udp, SMB 445, LDAPS 636, Global Catalog 3268/3269. All listed in `catalog.flows` for the DC's own zone (L2, no rule); cross-zone clients need flows when they exist (none planned yet).

## DNS
Samba internal backend, `dns forwarder` for everything outside the realm. Default (wifi): `192.168.69.10 192.168.69.11` (hm1-wifi-dns01/02, same L2). stg override: `1.1.1.1 8.8.8.8` (the resolvers the other stg guests use; the wifi AdGuard pair is in another zone behind the UDM). systemd-resolved stub listener is switched off (port 53). `/etc/resolv.conf` points at the DC itself, search `ad.lox-internal.dev`.
Open follow-up (not in this PR): AdGuard needs an upstream rule `[/ad.lox-internal.dev/]<dc ip>` so wifi clients can find the DC, and a flow wifi/iot clients to DC.

## Time
chrony serves signed NTP to members through the samba `ntp_signd` socket (`ntpsigndsocket`, dir group `_chrony` 0750). In a container the clock is not ours (no CAP_SYS_TIME) and chrony.service has `ConditionVirtualization=|!container`; the role removes the condition and runs `chronyd -x` with `local stratum 10`, i.e. it serves the PVE host's time. On a VM set `app_samba_dc_chrony_container: false` for normal chrony.

## Unprivileged LXC: the xattr measure and its limits
Samba stores NT ACLs in `security.NTACL` xattrs; an unprivileged container cannot write `security.*`. Measure: `posix:eadb = /var/lib/samba/private/eadb.tdb` (passed to provision with `--option` and kept in the managed smb.conf), so NT ACLs live in a tdb. L4 checks the setting, the tdb and `samba-tool ntacl sysvolcheck`.
Risks that may still make the LXC infeasible (that is what stg tests):
- The AD id mapping uses uids/gids from 3000000 (`idmap_ldb`); an unprivileged CT maps only 65536 ids, so `chown`/`sysvolreset` on sysvol can fail with EINVAL.
- Kernel and AppArmor limits (`samba` needs `mknod`-free operation; none expected), `/var/lib/samba/sysvol` ACL operations.
- No CAP_SYS_TIME (handled above).

## Fallback (documented, not implemented)
If provisioning or `sysvolcheck` fails in the unprivileged CT, in this order:
1. Privileged LXC (guest spec: `unprivileged: false`, a pipeline/Class C decision by Faris and the Infrastructure Engineer; protected-host rules unchanged). Real `security.*` xattrs work, `app_samba_dc_use_eadb: false`. Keeps the CT cheap but weakens isolation.
2. VM (`size.type: vm`, e.g. 2 cores / 2048 MB / 16 GB): full kernel, normal chrony (`app_samba_dc_chrony_container: false`), `app_samba_dc_use_eadb: false`. The catalogue size changes by PR and the CT is replaced (Class C).
The role itself does not change for either: the three variables above are the switch.

## Data and backup
Domain data: `/var/lib/samba/private` (sam.ldb, secrets.ldb, eadb.tdb), `/var/lib/samba/sysvol`, `/etc/samba/smb.conf`. It lives on the guest disk and is NOT backed up (open item A-1). Before any risky change use `samba-tool domain backup offline --targetdir=/root/` on the guest (Samba tool, stops writes) or a Proxmox snapshot via the SRE. Never restore a DC from a snapshot of a multi-DC domain; with a single DC it is fine.

## Secrets
`<zone>/secret/apps/samba-dc/<host>` keys `username`, `password` (by: role). Deviation from the AdGuard pattern: `samba-tool domain provision` needs the Administrator password in clear, and `bao_create_secret` with `generate` never returns the plaintext. So the role generates it on the guest (root-only file `/etc/hm1/samba-dc-admin.pass`, 0600), then escrows it once with `bao_create_secret` (create-only, data given), like GNS3. The password is passed to samba-tool as `--adminpass="$(cat file)"` and is visible in the process list of the guest for the duration of the provision (root only).

## Upgrade
Patch updates come with OS patching (ring of the guest; a reboot waits for Faris). A new Samba series (4.24) is a catalogue bump (class C): change `version.pin`, `known` and `app_samba_dc_series`, check release notes for schema changes, snapshot first.

## Not in scope
A second DC, replication, GPOs, member joins, DHCP, reverse zones, AdGuard rules and client flows.
