# GNS3 server - design note (Configuration Engineer)

| | |
|---|---|
| Guest / zone | `hm1-stg-net01` (VMID 810, 192.168.76.10, role `net`, zone stg), built by infra #195 |
| Sizing | 10 vCPU (cpu_type host, nested virtualisation on), 12 GB, 80 GB on `lvmt-sata01`: equal to the guest spec, `catalog.size` mirrors it |
| Version | GNS3 server **3.0.6** (PyPI, released 2026-01-28), wheel `gns3_server-3.0.6-py3-none-any.whl`, sha256 `6293f064...bc9` from the PyPI JSON API; ubridge **1.3.1** (GitHub tag tarball, sha256 in role defaults) |
| Install | venv `/opt/gns3/venv`, wheel pip-installed from a sha256-checked download (dependencies come from PyPI at the versions the wheel pins; they are not hash-pinned). ubridge built from source (not in the Ubuntu archive) and installed to `/usr/local/bin` with `cap_net_admin,cap_net_raw` file capabilities. dynamips, vpcs, QEMU, Docker from the Ubuntu archive |
| Service | `gns3-server.service`, user `gns3` (groups kvm, docker), `HOME=/var/lib/gns3`, `--config /var/lib/gns3/conf/gns3_server.conf` |
| Ports | 3080/tcp web UI + REST API (`/v3`), 5000-5099/tcp node consoles (flow `tru-gns3-ui`, UDM policy needed: tru is gateway-routed) |
| Lab NICs | net1..net9 = vmbr10..vmbr18 (extra NICs, no IP, no uplink). **Not configured by the role**: GNS3/QEMU/ubridge attach to them when a project uses them. If a project needs a NIC up, that is a follow-up PR |
| Auth | GNS3 3.x requires login (JWT). Default admin/admin is replaced: password generated on the guest (`openssl rand -hex 16`) into root-only `/etc/hm1/gns3-admin.pass`, read by the config, escrowed once in OpenBao `stg/secret/apps/gns3/hm1-stg-net01` keys `username`, `password`. Plaintext also sits in the 0600 config because GNS3 reads `default_admin_password` from it. The default admin is only created when the server database is first initialised: changing the password later is done in the UI/API, then update OpenBao by hand |
| Compute auth | local compute uses user `gns3` with a password the server generates at each start (server log warning); fine for the built-in local compute, set `compute_password` only when an external controller is added |
| Data | projects, images, appliances, DB and JWT key under `/var/lib/gns3` (home of `gns3`). Not backed up (open item A-1): the lab projects are the only state, export important ones from the UI |
| TLS | none (plain HTTP on 3080). A reverse proxy with ACME (PKI role `stg.lox-internal.dev`) is a follow-up if wanted; only `tru` may reach the port |
| Upgrade | change `version.pin` (must be in `version.known`, add the wheel sha256); a 3.0.x to 3.1 move is a new minor and runs the server's own DB migration at start. ubridge: bump `app_gns3_ubridge_version/_sha256` in role defaults (Class B) |
| Healthcheck | `health.command` in app.yaml (`/v3/version`, public); L4 in `tests/l4.goss.yaml.j2` (listener, version, auth required, default login rejected, ubridge caps, kvm access, emulators present) |
| Why 3.0.6 and not 2.2.61 | both are non-prerelease. GitHub marks 2.2.61 (2026-07-30) as Latest, but it is the maintenance branch; 3.0.6 is the newest release of the current major, declares Python 3.14 support (Ubuntu 26.04 ships 3.14) and has built-in authentication. 2.2.61 is also in the GNS3 PPA for 26.04 |
