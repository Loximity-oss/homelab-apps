# <App> - design note (Configuration Engineer)

Template; copy to catalog/<app>/docs/design.md. The example app is traefik/whoami in Docker Compose.

| | |
|---|---|
| Guest / zone | `hm1-<zone>-<role><nn>` |
| Service name | `<service>.lox-internal.dev` (services.yaml) |
| Sizing | cores / memory_mb / disk_gb as in app.yaml `size` |
| Ports | 8080/tcp |
| Data | paths on the guest - state whether backed up |
| Secrets | names only: `<zone>/secret/apps/<app>` keys ... (never values) |
| TLS | ACME, OpenBao PKI role `<zone>.lox-internal.dev` |
| Upgrade | change `version.pin` (must be in `version.known`); bump class per `bump_class` |
| Healthcheck | `health.command` in app.yaml; L4 in tests/l4.goss.yaml.j2 |
